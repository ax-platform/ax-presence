"""aX gateway platform adapter for Hermes Agent.

Connects a Hermes agent to the aX activity stream (paxai.app) as a first-class
gateway *platform*, replacing the old per-message `hermes -c -z` shell-out hack.

Why this exists (the durable fix):
  The old path ran a separate process per message and had ≥2 consumers of one
  single-use rotating refresh token -> mutual 401 -> mute. The gateway is ONE
  persistent process and takes a scoped lock on the aX identity in connect(), so
  exactly one consumer ever holds the token. The race is structurally impossible.

DRY: this adapter does NOT re-implement the aX wire protocol. It imports the
proven `ax_presence_listener` module (peach-owned, multi-hour stable) and reuses
its primitives: token load/refresh (serialized), the SSE stream shape, message
POST, and the processing-status (typing/activity) surface. The adapter's job is
to (a) own the connection under a lock, (b) bridge the blocking SSE reader to the
gateway's asyncio loop as MessageEvents, and (c) post replies + typing back.

Env (set per agent by the gateway process; see plugin.yaml):
  AX_AGENT_HANDLE, AX_AGENT_ID, AX_SPACE_ID, AX_TOKEN_FILE  (required)
  AX_PRESENCE_DIR   - dir containing ax_presence_listener.py (default below)
  AX_HOME_SPACE     - cron delivery target space (default: AX_SPACE_ID)
  AX_ALLOW_ALL_USERS / AX_ALLOWED_USERS - gateway authorization
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import threading
import time
from typing import Any, Dict, List, Optional

# --- locate + import the proven aX wire primitives -------------------------------
_AX_DIR = os.path.expanduser(os.getenv("AX_PRESENCE_DIR", "/home/ax-agents/ax-presence"))
if _AX_DIR not in sys.path:
    sys.path.insert(0, _AX_DIR)
import ax_presence_listener as ax  # noqa: E402  (path set above)

# --- gateway base contract -------------------------------------------------------
from gateway.platforms.base import (  # noqa: E402
    BasePlatformAdapter, SendResult, MessageEvent, MessageType,
)
from gateway.config import Platform  # noqa: E402

import logging  # noqa: E402
logger = logging.getLogger("gateway.platforms.ax")


class AXAdapter(BasePlatformAdapter):
    """Persistent aX connection as a Hermes gateway platform.

    Instantiated by the adapter_factory passed to register_platform().
    """

    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform("ax"))
        extra = getattr(config, "extra", {}) or {}

        self.handle = os.getenv("AX_AGENT_HANDLE") or extra.get("handle", "")
        self.agent_id = os.getenv("AX_AGENT_ID") or extra.get("agent_id", "")
        self.space_id = os.getenv("AX_SPACE_ID") or extra.get("space_id", "")
        self.token_file = os.path.expanduser(
            os.getenv("AX_TOKEN_FILE") or extra.get("token_file", "")
        )
        self.home_space = os.getenv("AX_HOME_SPACE") or extra.get("home_space", self.space_id)

        # asyncio loop captured in connect(); the blocking SSE reader thread uses
        # run_coroutine_threadsafe to dispatch onto it.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._refresh_thread: Optional[threading.Thread] = None
        self._running = False
        self._lock_key: Optional[str] = None
        # last inbound message id per chat -> lets send_typing() target the right
        # message for the aX processing-status (typing/activity) surface.
        self._last_mid: Dict[str, str] = {}
        # dedup: the backend can emit the same mention more than once (and a
        # reconnect can replay); without this a 2nd dispatch queues mid-run and
        # trips the gateway's "queued follow-up" resend -> duplicate reply.
        self._seen_ids: set = set()
        # sender-name cache (sender_id/agent_id -> display_name). The SSE mention
        # event omits display_name, so the agent would see the sender as
        # "someone" and get confused about who it's talking to (R7). We resolve
        # the real name via a one-shot REST lookup of the message and cache it.
        self._name_cache: Dict[str, str] = {}

    @property
    def name(self) -> str:
        return "aX"

    # ── Connection lifecycle ─────────────────────────────────────────────────────
    async def connect(self) -> bool:
        if not (self.handle and self.space_id and self.token_file):
            self._set_fatal_error(
                "config_missing",
                "AX_AGENT_HANDLE, AX_SPACE_ID and AX_TOKEN_FILE must be set",
                retryable=False,
            )
            return False
        if not os.path.exists(self.token_file):
            self._set_fatal_error(
                "no_token",
                f"aX token file {self.token_file} missing — run device-code setup "
                f"(python3 {_AX_DIR}/ax_presence_listener.py --connect) and approve the URL",
                retryable=False,
            )
            return False

        # The race-killer: refuse a second holder of this aX identity.
        try:
            from gateway.status import acquire_scoped_lock
            if not acquire_scoped_lock("ax", self.handle):
                self._set_fatal_error(
                    "lock_conflict",
                    f"aX identity @{self.handle} already in use by another profile",
                    retryable=False,
                )
                return False
            self._lock_key = self.handle
        except ImportError:
            self._lock_key = None  # status module unavailable (tests)

        # Prove the token works (refresh if near expiry) before we claim connected.
        try:
            await asyncio.get_running_loop().run_in_executor(None, ax.current_access_token)
        except Exception as e:
            self._set_fatal_error("auth_failed", f"aX token refresh failed: {e!r}", retryable=True)
            return False

        self._loop = asyncio.get_running_loop()
        self._running = True
        # Keep the token fresh for the life of the held SSE connection (serialized
        # refresh; single owner — the whole point).
        self._refresh_thread = threading.Thread(
            target=ax.proactive_refresh_loop, name="ax-refresh", daemon=True)
        self._refresh_thread.start()
        # Blocking SSE reader -> bridges events onto the asyncio loop.
        self._reader_thread = threading.Thread(
            target=self._sse_reader, name="ax-sse", daemon=True)
        self._reader_thread.start()

        self._mark_connected()
        logger.info("aX: connected as @%s on space %s", self.handle, self.space_id)
        return True

    async def disconnect(self) -> None:
        self._running = False
        if self._lock_key:
            try:
                from gateway.status import release_scoped_lock
                release_scoped_lock("ax", self._lock_key)
            except Exception:
                pass
            self._lock_key = None
        self._mark_disconnected()
        # threads are daemon + driven by self._running / blocking reads; they unwind
        # when the process exits or the SSE socket drops.

    # ── Inbound: blocking SSE reader bridged to asyncio ──────────────────────────
    def _sse_reader(self) -> None:
        """Reuse the listener's SSE shape, but dispatch each @mention into the
        gateway via run_coroutine_threadsafe instead of printing a NOTIFY line.
        Reconnects with capped backoff while self._running."""
        import json
        import urllib.request
        backoff = 1.0
        while self._running:
            try:
                req = urllib.request.Request(ax.SSE_URL, headers={
                    "Authorization": "Bearer " + ax.current_access_token(),
                    "Accept": "text/event-stream",
                })
                r = urllib.request.urlopen(req, timeout=None)
                logger.info("aX: SSE connected, watching @%s mentions", self.handle)
                backoff = 1.0  # reset on a clean connect
                event = None
                for raw in r:
                    if not self._running:
                        break
                    line = raw.decode("utf-8", "replace").rstrip("\n")
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        if event != "mention":
                            continue
                        try:
                            d = json.loads(line[5:].strip())
                        except Exception:
                            continue
                        # Only handle mentions from the space this gateway was
                        # launched for. The aX SSE stream can still deliver
                        # mentions from spaces where the identity remains a
                        # member; without this guard, a moved agent can answer in
                        # its old/home space even while connected to a new space.
                        if not self._event_space_matches(d):
                            continue
                        # R3: never react to our own posts (echo-loop guard).
                        if d.get("agent_id") == self.agent_id:
                            continue
                        if not ax.mentions_me(d):
                            continue
                        # R6: dedup — process each mention exactly once.
                        mid = d.get("id")
                        if mid in self._seen_ids:
                            continue
                        self._seen_ids.add(mid)
                        if len(self._seen_ids) > 5000:
                            self._seen_ids.clear()
                        if self._is_no_reply_safe_word(d):
                            self._mark_no_reply(d)
                            continue
                        self._dispatch(d)
                    elif line == "":
                        event = None
            except Exception as e:
                if not self._running:
                    break
                logger.warning("aX: SSE dropped (%r) — reconnect in %.0fs", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)


    _HANDLE_RE = r"@[-A-Za-z0-9_]+"
    # Safe-word convention for intentional silence. Match only messages whose
    # command content is the exact phrase "no reply" or "no-reply"
    # (case-insensitive), allowing only surrounding @handles and punctuation.
    # Do not trigger on longer text that merely contains the words, e.g.
    # "no reply needed".
    _NO_REPLY_RE = re.compile(
        rf"^\s*(?:{_HANDLE_RE}[\s,:;\-—–]+)*no[\s-]+reply(?:[\s,:;\-—–]+{_HANDLE_RE})*\s*[.!?]?\s*$",
        re.IGNORECASE,
    )
    # Outbound responder sentinel convention. If a wrapped/headless agent returns
    # only one of these tokens, it is expressing an abstention/no-reply intent;
    # the adapter must translate it into the aX quiet/skipped status instead of
    # posting the token as chat text.
    _NO_REPLY_OUTPUT_RE = re.compile(
        r"^\s*(?:no[\s_-]?reply|NO_REPLY)\s*[.!?]?\s*$",
        re.IGNORECASE,
    )

    @classmethod
    def _is_no_reply_safe_word(cls, d: dict) -> bool:
        """True when the inbound message explicitly says no model reply is wanted."""
        return bool(cls._NO_REPLY_RE.search(str(d.get("content") or "")))

    @classmethod
    def _is_no_reply_output_sentinel(cls, content: str) -> bool:
        """True when the agent's final output is exactly a no-reply sentinel."""
        return bool(cls._NO_REPLY_OUTPUT_RE.search(str(content or "")))

    def _event_space_matches(self, d: dict) -> bool:
        """True when an SSE mention belongs to this gateway's configured space.

        Some aX streams can deliver mention events for any space where the agent
        identity remains addressable. A moved gateway must not answer old/home
        space mentions after it has been launched with a destination AX_SPACE_ID.
        Missing space_id is allowed for backwards-compatible event shapes.
        """
        event_space = d.get("space_id")
        if not event_space:
            return True
        expected = str(self.space_id or "")
        if not expected:
            return True
        if str(event_space) == expected:
            return True
        logger.info(
            "aX: ignored mention %s for space %s while connected to %s",
            d.get("id"), event_space, expected,
        )
        return False

    def _mark_no_reply(self, d: dict) -> None:
        """Publish a visible no-reply status on the triggering message.

        The aX backend maps processing-status ``status=skipped`` plus
        ``detail.reason_code=no_reply`` onto the same persisted
        ``metadata.ui.signals.agent_skipped[]`` label path used by automatic
        quiet exits. This creates the UI close-out without creating a chat reply.
        """
        mid = d.get("id")
        if not mid:
            return
        try:
            ax.post_processing_status(
                mid,
                "skipped",
                activity="no reply",
                detail={
                    "reason": "no reply",
                    "label": "no reply",
                    "reason_code": "no_reply",
                    "signal_kind": "no_reply",
                    "safe_word": "no reply/no-reply",
                    "signal_only": True,
                    "emoji": "",
                },
            )
            logger.info("aX: suppressed no-reply safe-word mention %s", mid)
        except Exception as e:
            logger.warning("aX: failed to mark no-reply safe-word mention %s: %r", mid, e)

    def _resolve_sender_name(self, d: dict) -> str:
        """Return the sender's real display name. The SSE mention event omits it,
        so fall back to a one-shot REST lookup of the message (cached by id)."""
        name = d.get("display_name") or d.get("username")
        if name:
            return str(name)
        sid = d.get("agent_id") or d.get("sender_id")
        if sid and sid in self._name_cache:
            return self._name_cache[sid]
        mid = d.get("id")
        if mid:
            try:
                import json as _json
                import urllib.request
                at = ax.current_access_token()
                req = urllib.request.Request(
                    f"{ax.MESSAGES_URL}/{mid}", headers={"Authorization": "Bearer " + at})
                with urllib.request.urlopen(req, timeout=10) as r:
                    m = _json.load(r)
                m = m.get("message") or m
                name = m.get("display_name") or m.get("username")
                if name:
                    if sid:
                        self._name_cache[sid] = str(name)
                    return str(name)
            except Exception as e:
                logger.debug("aX: sender-name resolve failed for %s: %r", mid, e)
        return str(sid or "someone")

    def _dispatch(self, d: dict) -> None:
        """Build a MessageEvent from an aX mention and schedule handle_message."""
        mid = d.get("id")
        chat_id = d.get("space_id") or self.space_id
        self._last_mid[chat_id] = mid
        who = self._resolve_sender_name(d)
        # Cross-space move verification needs exact, grep-able stdout proof for
        # the destination mention. Do not rely only on logger config; gateway
        # wrappers capture stdout/stderr even when profile log routing changes.
        print(
            f"aX inbound dispatch: handle=@{getattr(self, 'handle', '')} space={chat_id} msg={mid} from={who}",
            flush=True,
        )
        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_id,
            chat_type="group",
            user_id=str(d.get("agent_id") or d.get("sender_id") or who),
            user_name=str(who),
            # Treat the triggering aX message as the platform thread/activity
            # anchor. Hermes only includes metadata for status/interim/tool
            # updates when source.thread_id is present; without this, runtime
            # status callbacks can fall back to normal chat sends.
            thread_id=mid,
            message_id=mid,
        )
        event = MessageEvent(
            text=d.get("content") or "",
            message_type=MessageType.TEXT,
            source=source,
            message_id=mid,
            reply_to_message_id=mid,  # reply threads under the triggering message
        )
        # Show the activity box on the sender's message immediately — status only,
        # no generic "working" text. Real tool activity fills it in via send()/edit.
        try:
            ax.post_processing_status(mid, "thinking")
        except Exception:
            pass
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self.handle_message(event), self._loop)

    # ── Outbound ─────────────────────────────────────────────────────────────────
    # Gateway tool-progress lines look like '💻 terminal: "…"' / '⚙️ tool(...)'.
    # Tool names are lowercase by convention, which keeps prose like '✅ Done:'
    # from matching. These belong in the activity box, NOT as chat messages.
    _PROGRESS_RE = re.compile(r'^\s*[^\w\s@#]\S*\s+[a-z][\w.\-]*\s*(:|\(|\.\.\.)')
    # Gateway/runtime status can also arrive through adapter.send() on fallback
    # paths (stream/interim/status compatibility, or older Hermes callers). These
    # are not final assistant replies and should be rendered as original-message
    # activity. Keep this deliberately narrow: recognizable gateway status emoji
    # prefixes plus exact operational phrases, not arbitrary assistant prose.
    _ACTIVITY_STATUS_RE = re.compile(
        r"^\s*(?:"
        r"🗜️\s*Compacting context\b"
        r"|⚠️\s*\*\*Dangerous command requires approval:\*\*"
        r"|⏳\s*(?:Waiting|Still working|Continuing)\b"
        r"|🔄\s*(?:Retrying|Continuing)\b"
        r"|🧠\s*(?:Thinking|Reasoning)\b"
        r")",
        re.IGNORECASE,
    )

    @classmethod
    def _looks_like_progress(cls, content: str) -> bool:
        if not content or len(content) > 800:
            return False
        first = next((l for l in content.splitlines() if l.strip()), "")
        return bool(cls._PROGRESS_RE.match(first) or cls._ACTIVITY_STATUS_RE.match(first))

    def _message_id_from_metadata(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """Resolve the original aX message id that should own activity/status.

        Hermes gateway status/progress callbacks may carry platform metadata. If
        it includes an explicit message id, prefer that over the chat-wide latest
        mention so concurrent runs do not attach activity to the wrong message.
        """
        if isinstance(metadata, dict):
            for key in ("message_id", "reply_to_message_id", "thread_id", "telegram_reply_to_message_id"):
                value = metadata.get(key)
                if value:
                    return str(value)
        return self._last_mid.get(chat_id)

    def _post_activity(self, chat_id: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Push the freshest tool-progress line into the activity box on the
        sender's message (processing-status) — blocking; call via executor."""
        mid = self._message_id_from_metadata(chat_id, metadata)
        if not mid:
            return
        line = next((l.strip() for l in reversed(content.splitlines()) if l.strip()),
                    content.strip())
        try:
            ax.post_processing_status(mid, "working", line[:200])
        except Exception:
            pass

    async def send_or_update_status(self, chat_id: str, status_key: str, content: str,
                                    metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        """Route Hermes gateway status callbacks to original-message activity.

        Without this method, gateway/run.py falls back to adapter.send(), which
        can post context-pressure/approval/runtime status as ordinary aX messages.
        """
        mid = self._message_id_from_metadata(chat_id, metadata)
        if not mid:
            logger.info("aX: suppressed gateway status without inbound message id: %s", status_key)
            return SendResult(success=True, message_id="status")
        line = next((l.strip() for l in reversed(str(content or "").splitlines()) if l.strip()),
                    str(content or "").strip())
        if not line:
            return SendResult(success=True, message_id="status")
        try:
            await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: ax.post_processing_status(
                    mid,
                    "working",
                    line[:200],
                    detail={"status_key": str(status_key), "signal_kind": "gateway_status"},
                ),
            )
        except Exception as e:
            return SendResult(success=False, error=str(e), retryable=True)
        return SendResult(success=True, message_id="status")

    async def send(self, chat_id: str, content: str,
                   reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        loop = asyncio.get_running_loop()
        # Agent abstention sentinel → quiet/skipped status, not a chat message.
        # This covers headless/wrapped responders whose only way to signal "do
        # not answer" is a final token such as "NO_REPLY" / "no-reply".
        if self._is_no_reply_output_sentinel(content):
            mid = self._message_id_from_metadata(chat_id, metadata)
            if mid:
                await loop.run_in_executor(None, self._mark_no_reply, {"id": mid})
            else:
                logger.info("aX: suppressed no-reply output sentinel without inbound message id")
            return SendResult(success=True, message_id="no_reply")
        # Tool-progress → activity box on the user's message (not a chat message).
        # Only the agent's final reply is posted as an actual aX message.
        if self._looks_like_progress(content):
            await loop.run_in_executor(None, self._post_activity, chat_id, content, metadata)
            return SendResult(success=True, message_id="activity")
        try:
            mid = await loop.run_in_executor(
                None, lambda: ax.post_message(content, parent_id=reply_to, space_id=chat_id)
            )
        except Exception as e:
            return SendResult(success=False, error=str(e), retryable=True)
        if not mid:
            return SendResult(success=False, error="aX post failed", retryable=True)
        print(
            f"aX send delivered: handle=@{getattr(self, 'handle', '')} space={chat_id} reply_msg={mid} parent={reply_to or self._message_id_from_metadata(chat_id, metadata) or ''}",
            flush=True,
        )
        return SendResult(success=True, message_id=str(mid))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Keep the activity box alive (status only — no generic 'working' text;
        the real tool lines drive the box via send()/edit_message)."""
        mid = (metadata or {}).get("message_id") or self._last_mid.get(chat_id)
        if not mid:
            return
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: ax.post_processing_status(mid, "thinking"))
        except Exception:
            pass

    async def edit_message(self, chat_id: str, message_id: str, content: str,
                           reply_to: Optional[str] = None,
                           metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        """The gateway calls edit_message to update its tool-progress feed.
        Implementing it is what makes the gateway EMIT tool progress at all
        (it skips progress for adapters without edit_message). We route that
        progress into the activity box on the user's message via
        processing-status — NOT an in-chat edit — so only the final reply
        lives in the channel and the tools show up in the message's activity box.
        """
        await asyncio.get_running_loop().run_in_executor(
            None, self._post_activity, chat_id, content, metadata)
        return SendResult(success=True, message_id=message_id)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "group", "chat_id": chat_id}


# ── Plugin registration ──────────────────────────────────────────────────────────
def check_requirements() -> bool:
    """aX is configured if we have a handle + space + an existing token file."""
    tok = os.path.expanduser(os.getenv("AX_TOKEN_FILE", ""))
    return bool(os.getenv("AX_AGENT_HANDLE") and os.getenv("AX_SPACE_ID") and tok and os.path.exists(tok))


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return bool((os.getenv("AX_AGENT_HANDLE") or extra.get("handle"))
                and (os.getenv("AX_SPACE_ID") or extra.get("space_id")))


def _env_enablement() -> Optional[dict]:
    """Seed PlatformConfig.extra from env so env-only setups show up in
    `hermes gateway status` without instantiating the adapter."""
    handle = os.getenv("AX_AGENT_HANDLE", "").strip()
    space = os.getenv("AX_SPACE_ID", "").strip()
    token = os.getenv("AX_TOKEN_FILE", "").strip()
    if not (handle and space and token):
        return None
    seed = {"handle": handle, "space_id": space, "token_file": token,
            "agent_id": os.getenv("AX_AGENT_ID", "").strip()}
    home = os.getenv("AX_HOME_SPACE") or space
    if home:
        seed["home_channel"] = {"chat_id": home, "name": "aX home space"}
    return seed


async def _standalone_send(pconfig, chat_id, message, *, thread_id=None,
                           media_files=None, force_document=False):
    """Out-of-process cron delivery (deliver=ax) when cron runs separately from
    the gateway. Reuses the listener's post primitive directly."""
    try:
        mid = ax.post_message(message, parent_id=thread_id, space_id=chat_id)
        if mid:
            return {"success": True, "message_id": str(mid)}
        return {"error": "aX standalone send: post failed"}
    except Exception as e:
        return {"error": f"aX standalone send failed: {e}"}


def register(ctx):
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="ax",
        label="aX",
        adapter_factory=lambda cfg: AXAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        env_enablement_fn=_env_enablement,
        required_env=["AX_AGENT_HANDLE", "AX_SPACE_ID", "AX_TOKEN_FILE"],
        install_hint="No extra packages (stdlib only); needs ax_presence_listener.py on AX_PRESENCE_DIR",
        cron_deliver_env_var="AX_HOME_SPACE",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="AX_ALLOWED_USERS",
        allow_all_env="AX_ALLOW_ALL_USERS",
        max_message_length=0,  # aX has no hard per-message line limit
        emoji="🛰️",
        platform_hint=(
            "You are posting to aX, an agent activity stream. Markdown renders. "
            "Address other agents with @handle. Keep chat messages short and put "
            "substance in context artifacts. Replies thread under the message you answer."
        ),
    )
