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
  AX_AGENT_HANDLE, AX_AGENT_ID, AX_TOKEN_FILE  (required)
  AX_PRESENCE_DIR   - dir containing ax_presence_listener.py (default below)
  AX_ALLOW_ALL_USERS / AX_ALLOWED_USERS - gateway authorization
"""
from __future__ import annotations

import asyncio
import hashlib
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
from gateway.config import HomeChannel, Platform  # noqa: E402

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
        # Home-channel routing must come from the backend agent record, not a
        # launch/config "home space" override.  Moved agents otherwise keep
        # leaking cron/default sends to a stale space.
        self.home_space = ""

        # asyncio loop captured in connect(); the blocking SSE reader thread uses
        # run_coroutine_threadsafe to dispatch onto it.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._reader_threads: List[threading.Thread] = []
        self._refresh_thread: Optional[threading.Thread] = None
        self._running = False
        self._lock_key: Optional[str] = None
        # last inbound message id per chat -> lets send_typing() target the right
        # message for the aX processing-status (typing/activity) surface.
        self._last_mid: Dict[str, str] = {}
        # inbound message id -> aX channel. Final replies must use the same
        # channel as their parent; the backend rejects replies from main ->
        # activity parents with HTTP 400.
        self._message_channels: Dict[str, str] = {}
        # dedup: the backend can emit the same mention more than once (and a
        # reconnect can replay); without this a 2nd dispatch queues mid-run and
        # trips the gateway's "queued follow-up" resend -> duplicate reply.
        self._seen_ids: set = set()
        # sender-name cache (sender_id/agent_id -> display_name). The SSE mention
        # event omits display_name, so the agent would see the sender as
        # "someone" and get confused about who it's talking to (R7). We resolve
        # the real name via a one-shot REST lookup of the message and cache it.
        self._name_cache: Dict[str, str] = {}
        # Agent-to-agent loop guard: keep a tiny per-sender window so short
        # acknowledgement ping-pongs ("@peach roger." / "@canary roger.") do
        # not burn model calls indefinitely. Human messages are never gated by
        # this path because aX user messages do not carry agent_id.
        self._agent_ack_window: Dict[str, List[float]] = {}
        # aX REST/SSE routes are session-space scoped: using a token that is still
        # scoped to the home space can make cross-space message readback/listing
        # return the SPA HTML shell and can make SSE keep delivering old-space
        # mentions. Cache a space-switched access token for inbound/readback only;
        # final agent reply POSTs use the base agent token plus X-Agent-Id/X-Space-Id.
        self._space_token: Dict[str, tuple[str, float]] = {}
        self._space_token_lock = threading.Lock()
        # Spaces with active SSE subscriptions. This must stay aligned with the
        # authoritative backend agent-record space; membership in other spaces is
        # not routing authority and must not wake the agent there.
        self._subscribed_spaces: set[str] = set()
        # Live-move support (agent_placement_changed SSE control event):
        # _reader_epoch is the per-generation stop signal for SSE reader threads.
        # When placement changes we bump it, tear down the old readers (they
        # self-terminate on their next line), re-resolve the DB space, and spawn
        # a fresh generation. _running stays the global lifecycle flag (it must
        # NOT be used for teardown or it would also stop heartbeat/refresh and
        # block respawn). The single-flight lock + flag guarantee exactly one
        # reader performs the teardown/respawn even though every subscribed
        # reader receives the broadcast.
        self._reader_epoch: int = 0
        self._resub_lock = threading.Lock()
        self._resubscribing = False

    @property
    def name(self) -> str:
        return "aX"

    # ── Connection lifecycle ─────────────────────────────────────────────────────
    async def connect(self) -> bool:
        if not (self.handle and self.token_file):
            self._set_fatal_error(
                "config_missing",
                "AX_AGENT_HANDLE and AX_TOKEN_FILE must be set",
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

        try:
            derived_space = await asyncio.get_running_loop().run_in_executor(
                None, self._derive_agent_space_id
            )
        except Exception as e:
            self._set_fatal_error(
                "space_lookup_failed",
                f"aX agent space lookup failed: {e!r}",
                retryable=True,
            )
            return False
        if derived_space:
            configured_space = self.space_id
            self._apply_space_id(derived_space)
            if configured_space and str(configured_space) != str(derived_space):
                logger.warning(
                    "aX: ignoring configured space %s for @%s; backend agent record says %s",
                    configured_space,
                    self.handle,
                    derived_space,
                )
        elif not self.space_id:
            self._set_fatal_error(
                "space_missing",
                "aX agent record did not include space_id",
                retryable=True,
            )
            return False

        self._loop = asyncio.get_running_loop()
        self._running = True
        # Keep the token fresh for the life of the held SSE connection (serialized
        # refresh; single owner — the whole point).
        self._refresh_thread = threading.Thread(
            target=ax.proactive_refresh_loop, name="ax-refresh", daemon=True)
        self._refresh_thread.start()
        # Platform heartbeat (POST /api/v1/agents/heartbeat ~every 20s) so the
        # agent shows Online in the availability views. Without this a live
        # gateway agent reads Dormant/Offline (it never POSTs liveness). Uses
        # the listener module globals hydrated from the backend agent record.
        self._heartbeat_thread = threading.Thread(
            target=ax.presence_loop, name="ax-heartbeat", daemon=True)
        self._heartbeat_thread.start()
        # Blocking SSE reader(s) -> bridge events from each subscribed member
        # space onto the asyncio loop. The backend agent-record space remains the
        # Hermes home/default target, while mentions in other member spaces are
        # real wake targets too.
        subscribed_spaces = await asyncio.get_running_loop().run_in_executor(
            None, self._discover_subscribed_spaces
        )
        self._subscribed_spaces = set(subscribed_spaces)
        self._spawn_readers(subscribed_spaces)

        self._mark_connected()
        logger.info("aX: connected as @%s on home space %s; subscribed spaces=%s",
                    self.handle, self.space_id, sorted(self._subscribed_spaces))
        return True

    def _spawn_readers(self, spaces: List[str]) -> None:
        """Spawn one SSE reader thread per space for the CURRENT epoch.

        Each reader captures the epoch live at start; a later epoch bump makes
        the old generation self-terminate on its next SSE line. Used by both the
        initial connect() and the live-move re-subscribe path.
        """
        epoch = self._reader_epoch
        self._reader_threads = []
        for sid in spaces:
            t = threading.Thread(
                target=self._sse_reader,
                args=(sid, epoch),
                name=f"ax-sse-{sid[:8]}",
                daemon=True,
            )
            t.start()
            self._reader_threads.append(t)
        self._reader_thread = self._reader_threads[0] if self._reader_threads else None

    def _derive_agent_space_id(self) -> str:
        """Read the authoritative aX placement from the agent record.

        Launch scripts must not pin a space. The backend agent row owns current
        placement, so moved agents do not drift into "connected but ignoring the
        intended space" state.
        """
        import json as _json
        import urllib.request

        token = ax.current_access_token()
        paths = []
        if self.agent_id:
            paths.append(f"/api/v1/agents/{self.agent_id}")
        paths.append("/api/v1/agents/me")
        for path in paths:
            req = urllib.request.Request(
                f"{ax.BASE}{path}",
                headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
            data = _json.loads(raw.decode() if raw else "{}")
            agent = data.get("agent") if isinstance(data, dict) else None
            if isinstance(agent, dict):
                data = agent
            space = data.get("space_id") if isinstance(data, dict) else None
            if space:
                return str(space)
        return ""

    def _apply_space_id(self, space_id: str) -> None:
        self.space_id = str(space_id)
        # The caller passes only the authoritative backend agent-record space.
        # Keep Hermes' home channel aligned with that record and ignore any
        # stale launch/config home-space value that may still exist in env.
        self.home_space = self.space_id
        try:
            hc = HomeChannel(
                platform=Platform("ax"),
                chat_id=self.home_space,
                name="aX home space",
            )
            # Legacy top-level attr (kept for any direct readers).
            self.config.home_channel = hc
            # Authoritative slot: Hermes' get_home_channel() reads
            # config.platforms[<platform>].home_channel. Populate it from the
            # DB-derived space so the gateway has a home channel WITHOUT any
            # AX_HOME_SPACE/AX_HOME_CHANNEL env var and WITHOUT a manual
            # /sethome. Without this, run.py emits a "No home channel is set"
            # prompt on every fresh inbound and cron/cross-platform delivery
            # has no destination.
            pc = self.config.platforms.get(Platform("ax"))
            if pc is None:
                from gateway.config import PlatformConfig
                pc = PlatformConfig(enabled=True)
                self.config.platforms[Platform("ax")] = pc
            pc.home_channel = hc
        except Exception:
            pass
        ax.SPACE_ID = self.space_id
        if self.agent_id:
            ax.AGENT_ID = self.agent_id
        if self.handle:
            ax.AGENT_HANDLE = self.handle
        if self.token_file:
            ax.TOKEN_FILE = self.token_file

    @staticmethod
    def _ordered_unique(values: List[str]) -> List[str]:
        seen = set()
        ordered = []
        for value in values:
            if not value:
                continue
            text = str(value)
            if text in seen:
                continue
            seen.add(text)
            ordered.append(text)
        return ordered

    def _discover_subscribed_spaces(self) -> List[str]:
        """Return spaces that should have live SSE subscriptions.

        SECURITY: an agent must operate ONLY in the space its backend DB record
        authorizes — its ``space_id`` (and any explicit ``space_access``), and
        NOTHING ELSE. Subscribing to every space the token can *see*
        (``GET /api/v1/spaces`` ``is_member``) made agents wake & respond in
        spaces they were never placed in (e.g. a predictions-lab agent replying
        in the owner's personal workspace) — a cross-space data-leak. We honor
        the DB placement only.
        """
        home = str(self.space_id or "")
        spaces = [home] if home else []
        # If the agent record exposed additional explicitly-authorized spaces,
        # include only those (DB-authoritative); never fall back to is_member.
        for sid in (getattr(self, "_authorized_space_ids", None) or []):
            if sid:
                spaces.append(str(sid))
        return self._ordered_unique(spaces)

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

    def _access_token_for_space(self, space_id: Optional[str] = None) -> str:
        """Return an aX access token scoped to the requested space.

        aX accepts ``space_id``/``X-Space-Id`` on some APIs, but message GET/POST
        and the SSE stream are still session-space sensitive. If a moved gateway
        keeps using a token scoped to its old/home space, cross-space POST can
        return the SPA HTML shell (causing JSONDecodeError) and SSE can keep
        emitting old-space mentions. Switch the access token at the platform
        boundary so moved agents consume and send in their configured space.
        """
        target = str(space_id or self.space_id or "")
        base_token = ax.current_access_token()
        if not target:
            return base_token
        now = time.time()
        with self._space_token_lock:
            cached = self._space_token.get(target)
            if cached and cached[1] > now:
                return cached[0]
        try:
            import json as _json
            import urllib.request
            body = _json.dumps({"space_id": target}).encode()
            req = urllib.request.Request(
                f"{ax.BASE}/api/spaces/switch",
                data=body,
                method="POST",
                headers={
                    "Authorization": "Bearer " + base_token,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
            data = _json.loads(raw.decode() if raw else "{}")
            token = data.get("new_token") or data.get("access_token")
            if token:
                with self._space_token_lock:
                    self._space_token[target] = (str(token), now + 600)
                return str(token)
        except Exception as e:
            logger.warning("aX: space token switch failed for %s: %r", target, e)
        return base_token

    # ── Inbound: blocking SSE reader bridged to asyncio ──────────────────────────
    def _sse_reader(self, space_id: Optional[str] = None, my_epoch: Optional[int] = None) -> None:
        """Reuse the listener's SSE shape, but dispatch each @mention into the
        gateway via run_coroutine_threadsafe instead of printing a NOTIFY line.
        Reconnects with capped backoff while self._running.

        my_epoch identifies this reader's generation. When placement changes the
        adapter bumps self._reader_epoch and respawns readers; an old-generation
        reader self-terminates on its next SSE line (the epoch check below). This
        is independent of self._running, which stays the global lifecycle flag."""
        import json
        import urllib.request
        reader_space = str(space_id or self.space_id or "")
        if my_epoch is None:
            my_epoch = self._reader_epoch
        backoff = 1.0
        while self._running:
            if getattr(self, "_reader_epoch", 0) != my_epoch:
                return  # superseded by a newer generation (live move)
            try:
                req = urllib.request.Request(ax.SSE_URL, headers={
                    "Authorization": "Bearer " + self._access_token_for_space(reader_space),
                    "Accept": "text/event-stream",
                    "X-Space-Id": reader_space,
                })
                r = urllib.request.urlopen(req, timeout=None)
                logger.info("aX: SSE connected, watching @%s mentions in space %s", self.handle, reader_space)
                backoff = 1.0  # reset on a clean connect
                event = None
                for raw in r:
                    if not self._running:
                        break
                    # Live-move teardown: an old reader generation must stop
                    # dispatching the moment a newer generation exists. Checked
                    # before any branch so a stale reader never re-handles an
                    # event or wakes the agent in the old space.
                    if getattr(self, "_reader_epoch", 0) != my_epoch:
                        return
                    line = raw.decode("utf-8", "replace").rstrip("\n")
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        # Control branch FIRST: the backend emits
                        # agent_placement_changed on the agent's OLD (still
                        # subscribed) space stream when its placement moves. We
                        # handle it before the mention gate so it bypasses
                        # _event_space_matches (the broker injects the OLD space
                        # as space_id, which would otherwise be rejected),
                        # mentions_me, the own-post guard, and dedup — all of
                        # which are mention-only semantics.
                        if event == "agent_placement_changed":
                            try:
                                d = json.loads(line[5:].strip())
                            except Exception:
                                continue
                            self._handle_placement_changed(d)
                            # This reader belongs to a now-stale space set; the
                            # handler bumps the epoch and respawns. Exit here so
                            # we do not keep reading the old stream.
                            return
                        if event != "mention":
                            continue
                        try:
                            d = json.loads(line[5:].strip())
                        except Exception:
                            continue
                        # Only handle mentions from a space this gateway actually
                        # subscribed to. The aX SSE stream can be token-scoped and
                        # may deliver cross-space events; the subscribed-space set
                        # is the explicit wake contract for this gateway process.
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
                        if self._is_agent_ack_loop_candidate(d):
                            continue
                        self._dispatch(d)
                    elif line == "":
                        event = None
            except Exception as e:
                if not self._running:
                    break
                logger.warning("aX: SSE for space %s dropped (%r) — reconnect in %.0fs", reader_space, e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    _ACK_WORD_RE = r"(?:roger|ack(?:nowledged)?|aligned|standing by|sounds good|noted|thanks|thank you|copy|copied|received|seen|confirmed|affirmative|ok(?:ay)?)"
    _HANDLE_RE = r"@[-A-Za-z0-9_]+"
    _ACK_LOOP_RE = re.compile(
        rf"^\s*(?:{_HANDLE_RE}[\s,:;\-—–]*)*{_ACK_WORD_RE}\b[\s.!?,:;\-—–]*(?:{_HANDLE_RE}\b[\s.!?,:;\-—–]*)*$",
        re.IGNORECASE,
    )
    _ACK_WITH_HANDLE_RE = re.compile(
        rf"(?:^|\s){_ACK_WORD_RE}\b|{_HANDLE_RE}",
        re.IGNORECASE,
    )
    _SHORT_AGENT_MENTION_WINDOW_SECONDS = 45
    _SHORT_AGENT_MENTION_THRESHOLD = 2
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
        """True when an SSE mention belongs to one of this gateway's subscriptions.

        Reject events that carry an explicit space_id outside the discovered
        subscription set. Missing space_id remains allowed for backwards-
        compatible event shapes.
        """
        event_space = d.get("space_id")
        if not event_space:
            return True
        expected = {str(s) for s in getattr(self, "_subscribed_spaces", set()) if s}
        if not expected and self.space_id:
            expected = {str(self.space_id)}
        if not expected or str(event_space) in expected:
            return True
        logger.info(
            "aX: ignored mention %s for unsubscribed space %s; subscribed=%s",
            d.get("id"), event_space, sorted(expected),
        )
        return False

    def _handle_placement_changed(self, d: dict) -> None:
        """React to the backend ``agent_placement_changed`` SSE control event.

        Contract (DB-authoritative): this event is ONLY a wake/re-resolve
        trigger. We do NOT trust ``new_space_id`` from the payload (a stale or
        forged old-space stream could carry a wrong value); we always re-read the
        committed placement via ``_derive_agent_space_id`` (GET
        /api/v1/agents/me). The payload is used only to (a) ignore events naming
        a different agent that shares the old-space stream, and (b) log staleness.

        Loop-safety: every subscribed reader receives the broadcast (plus an
        optional dual-publish to old+new spaces), so the SAME agent can see this
        event 2..N times. A single-flight lock + flag ensure exactly one reader
        performs the teardown/respawn; the rest no-op. A derive-and-compare
        short-circuit absorbs duplicates/no-op moves (target == current).
        """
        # Wrong-agent guard: an old-space stream carries many agents' events.
        # React ONLY to our own agent_id (the inverse of the mention echo-guard).
        evt_agent = d.get("agent_id")
        if evt_agent and self.agent_id and str(evt_agent) != str(self.agent_id):
            return

        with self._resub_lock:
            if self._resubscribing:
                return  # another reader is already handling this move
            self._resubscribing = True
        try:
            # Re-resolve DB-authoritative placement. Never trust payload values.
            try:
                new_space = self._derive_agent_space_id()
            except Exception as e:
                # Hard failure resolving placement: do NOT tear down the live
                # subscription (that would make the agent deaf everywhere).
                logger.warning(
                    "aX: placement re-resolve failed (%r); keeping current subscription", e)
                return

            if not new_space:
                # Suspended/detached/unauthorized after move -> empty derive.
                # Keep existing readers rather than blanking the subscription.
                logger.warning(
                    "aX: placement event for @%s but derive returned no space; "
                    "keeping current subscription (ts=%s)", self.handle, d.get("ts"))
                return

            # No-op short-circuit (idempotent against duplicate/repeated events
            # and moves whose target equals the current space): compare BOTH the
            # home space AND the full recomputed subscription set.
            recomputed = set(self._compute_subscribed_for(new_space))
            if (str(new_space) == str(self.space_id)
                    and recomputed == set(self._subscribed_spaces)):
                logger.info(
                    "aX: placement event for @%s is a no-op (space unchanged: %s)",
                    self.handle, new_space)
                return

            logger.info(
                "aX: placement change for @%s: %s -> %s (ts=%s); re-subscribing",
                self.handle, self.space_id, new_space, d.get("ts"))

            # Bump epoch: old readers self-terminate on their next SSE line.
            self._reader_epoch += 1

            # Re-resolve home + apply (updates self.space_id, home_space,
            # HomeChannel in config.platforms['ax'], ax.SPACE_ID).
            self._apply_space_id(new_space)

            # Set the subscription set BEFORE spawning so _event_space_matches
            # admits the new space immediately.
            subscribed = self._discover_subscribed_spaces()
            self._subscribed_spaces = set(subscribed)

            # Invalidate stale per-space tokens (drops the 600s cached old-space
            # token) so the moved agent does not consume/send on the old space.
            with self._space_token_lock:
                self._space_token.clear()

            # Spawn the new generation (captures the bumped epoch).
            self._spawn_readers(subscribed)
        finally:
            self._resubscribing = False

    def _compute_subscribed_for(self, home_space: str) -> List[str]:
        """Recompute the subscription set as if ``home_space`` were current.

        Mirrors _discover_subscribed_spaces but parameterized on the candidate
        home space so the no-op short-circuit can compare without mutating state.
        """
        home = str(home_space or "")
        spaces = [home] if home else []
        for sid in (getattr(self, "_authorized_space_ids", None) or []):
            if sid:
                spaces.append(str(sid))
        return self._ordered_unique(spaces)

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

    def _is_agent_ack_loop_candidate(self, d: dict) -> bool:
        """Return True when an inbound agent mention looks like ping-pong noise.

        The adapter must still allow meaningful agent-to-agent handoffs, but it
        should never spend unbounded model calls on repeating acknowledgements.
        We therefore drop:
        - short acknowledgement-only mentions from another aX agent, even when
          the @handle appears before or after the ack word; and
        - the 2nd short agent mention from the same sender within 45s, before a
          three-message ping-pong can form.
        Human/user messages are not affected because they do not carry agent_id.
        """
        sender_agent = str(d.get("agent_id") or "")
        if not sender_agent:
            return False
        text = str(d.get("content") or "").strip()
        compact = " ".join(text.split())
        if len(compact) <= 160 and self._is_short_agent_ack_mention(compact):
            logger.info("aX: suppressed agent short-ack mention from %s: %.120r", sender_agent, compact)
            return True

        # Fallback burst guard for short agent-to-agent mentions even if wording
        # differs from the explicit ack regex. Threshold 2 makes the guard
        # preventive: the second short peer mention is suppressed before a third
        # message can establish a cascade.
        if len(compact) <= 220:
            now = time.time()
            window = [
                t for t in self._agent_ack_window.get(sender_agent, [])
                if now - t < self._SHORT_AGENT_MENTION_WINDOW_SECONDS
            ]
            window.append(now)
            self._agent_ack_window[sender_agent] = window[-10:]
            if len(window) >= self._SHORT_AGENT_MENTION_THRESHOLD:
                logger.warning(
                    "aX: suppressed possible agent-to-agent loop from %s (%d short mentions in %ds)",
                    sender_agent, len(window), self._SHORT_AGENT_MENTION_WINDOW_SECONDS,
                )
                return True
        return False

    @classmethod
    def _is_short_agent_ack_mention(cls, compact: str) -> bool:
        """True for short peer ack mentions that contain no substantive text."""
        if not cls._ACK_LOOP_RE.match(compact):
            return False
        remainder = cls._ACK_WITH_HANDLE_RE.sub(" ", compact)
        remainder = re.sub(r"[\s.!?,:;\-—–]+", "", remainder)
        return remainder == ""

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
                at = self._access_token_for_space(d.get("space_id") or self.space_id)
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

    _LEADING_MENTION_COMMAND_RE = re.compile(
        r"^\s*(?:@[A-Za-z0-9_.-]+\s*(?:[:,;\u2014\u2013-]\s*)?)+(?=/[A-Za-z0-9])"
    )

    @classmethod
    def _normalize_inbound_text(cls, content: str) -> str:
        """Normalize aX mention text before handing it to Hermes.

        aX routes agent input by @mention, so users naturally write
        ``@peach /commands``. Hermes gateway slash dispatch only recognizes
        commands when the normalized MessageEvent text starts with ``/``;
        without this strip the command falls through to the model as plain chat.
        Strip only leading @handle prefixes immediately followed by a slash so
        ordinary mentions and prose remain untouched.
        """
        text = str(content or "")
        return cls._LEADING_MENTION_COMMAND_RE.sub("", text, count=1)

    def _dispatch(self, d: dict) -> None:
        """Build a MessageEvent from an aX mention and schedule handle_message."""
        mid = d.get("id")
        chat_id = d.get("space_id") or self.space_id
        self._last_mid[chat_id] = mid
        if mid and d.get("channel"):
            self._message_channels[str(mid)] = str(d.get("channel"))
        who = self._resolve_sender_name(d)
        # Move/create proof needs a grep-able stdout line for the exact
        # destination-space mention. Do not rely only on logger config; the tmux
        # gateway wrappers always capture stdout/stderr.
        print(
            f"aX inbound dispatch: handle=@{getattr(self, 'handle', '')} space={chat_id} msg={mid} from={who}",
            flush=True,
        )
        logger.info(
            "aX inbound dispatch: handle=@%s space=%s msg=%s from=%s",
            getattr(self, "handle", ""), chat_id, mid, who,
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
            # status callbacks either fall back to chat-wide latest-message state
            # or, on older adapters, leak as normal stream messages.
            thread_id=mid,
            message_id=mid,
        )
        event = MessageEvent(
            text=self._normalize_inbound_text(d.get("content") or ""),
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
        r"|⏳\s*(?:Working|Waiting|Still working|Continuing)\b"
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

    def _recover_posted_message_id(self, token: str, content: str, space_id: str,
                                   parent_id: Optional[str] = None) -> Optional[str]:
        try:
            import json as _json
            import urllib.parse
            import urllib.request
            qs = urllib.parse.urlencode({"space_id": space_id, "limit": 50})
            req = urllib.request.Request(
                f"{ax.MESSAGES_URL}?{qs}",
                headers={
                    "Authorization": "Bearer " + token,
                    "Accept": "application/json",
                    "X-Agent-Id": self.agent_id,
                    "X-Space-Id": space_id,
                },
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
            data = _json.loads(raw.decode() if raw else "{}")
            arr = data.get("messages") or data.get("items") or (data if isinstance(data, list) else [])
            for item in arr:
                msg = item.get("message") or item
                if (msg.get("content") or "") != content:
                    continue
                if parent_id and msg.get("parent_id") != parent_id:
                    continue
                return str(msg.get("id")) if msg.get("id") else None
        except Exception as e:
            logger.warning("aX: recovery list failed for sent message in %s: %r", space_id, e)
        return None

    def _fetch_parent_channel(self, parent_id: str, space_id: str) -> Optional[str]:
        """Read the parent message channel when the SSE wake payload omitted it."""
        try:
            import json as _json
            import urllib.request

            token = self._access_token_for_space(space_id)
            req = urllib.request.Request(
                f"{ax.MESSAGES_URL}/{parent_id}",
                headers={
                    "Authorization": "Bearer " + token,
                    "Accept": "application/json",
                    "X-Agent-Id": self.agent_id,
                    "X-Space-Id": space_id,
                },
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                ctype = r.headers.get("content-type", "")
            if "json" not in ctype.lower():
                logger.warning(
                    "aX: parent channel lookup returned non-JSON content-type %s for parent %s in space %s",
                    ctype,
                    parent_id,
                    space_id,
                )
                return None
            data = _json.loads(raw.decode() if raw else "{}")
            msg = data.get("message") or data
            channel = msg.get("channel")
            if channel:
                return str(channel)
        except Exception as e:
            logger.warning("aX: parent channel lookup failed for %s in %s: %r", parent_id, space_id, e)
        return None

    def _channel_for_parent(self, parent_id: Optional[str], space_id: str) -> str:
        """Return the aX channel a threaded reply must use for its parent."""
        if not parent_id:
            return "main"
        if not hasattr(self, "_message_channels"):
            self._message_channels = {}
        parent_key = str(parent_id)
        cached = self._message_channels.get(parent_key)
        if cached:
            return cached
        fetched = self._fetch_parent_channel(parent_key, space_id)
        if fetched:
            self._message_channels[parent_key] = fetched
            return fetched
        return "main"

    @staticmethod
    def _trim_for_log(value: Any, limit: int = 500) -> str:
        text = str(value or "")
        text = re.sub(r"[\r\n\t]+", " ", text).strip()
        return text[:limit]

    @classmethod
    def _classify_post_failure(cls, status: Optional[int], body: str, parent_id: Optional[str]) -> str:
        body_l = body.lower()
        if status == 400 and parent_id and any(s in body_l for s in ("reminder", "activity", "card_only")):
            return "reply_to_activity_or_reminder_parent"
        if status in (401, 403) or any(s in body_l for s in ("unauthorized", "forbidden", "token", "scope")):
            return "auth_or_scope"
        if status == 400 and any(s in body_l for s in ("parent", "thread")):
            return "parent_thread_contract"
        if status == 400 and any(s in body_l for s in ("agent", "sender", "identity")):
            return "agent_identity_contract"
        if status == 400:
            return "bad_request_unknown_contract"
        return "unknown"

    def _log_post_failure(self, exc: Exception, payload: Dict[str, Any], space_id: str,
                          parent_id: Optional[str]) -> None:
        """Log enough context to diagnose aX post-contract failures without secrets."""
        status = getattr(exc, "code", None) or getattr(exc, "status", None)
        reason = getattr(exc, "reason", None) or getattr(exc, "msg", None)
        headers = getattr(exc, "headers", None)
        ctype = ""
        if headers is not None:
            try:
                ctype = headers.get("content-type", "") or headers.get("Content-Type", "")
            except Exception:
                ctype = ""
        raw_body = b""
        try:
            if hasattr(exc, "read"):
                raw_body = exc.read() or b""
        except Exception:
            raw_body = b""
        if isinstance(raw_body, bytes):
            body = raw_body.decode("utf-8", "replace")
        else:
            body = str(raw_body or "")
        body_preview = self._trim_for_log(body)
        content = str(payload.get("content") or "")
        content_sha = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()[:12]
        classification = self._classify_post_failure(
            int(status) if isinstance(status, int) else None,
            body_preview,
            parent_id,
        )
        logger.warning(
            "aX: post failed handle=@%s agent_id=%s space=%s parent=%s status=%s reason=%r "
            "content_type=%r payload_channel=%s payload_type=%s content_len=%d content_sha=%s "
            "classification=%s response_body=%r",
            getattr(self, "handle", ""),
            getattr(self, "agent_id", ""),
            space_id,
            parent_id or "",
            status,
            reason,
            ctype,
            payload.get("channel"),
            payload.get("message_type"),
            len(content),
            content_sha,
            classification,
            body_preview,
        )

    def _post_message(self, content: str, parent_id: Optional[str], space_id: str) -> Optional[str]:
        """Blocking final-reply POST as the configured agent into the destination space."""
        import json as _json
        import urllib.request
        token = ax.current_access_token()
        payload = {
            "content": content,
            "space_id": space_id,
            "channel": self._channel_for_parent(parent_id, space_id),
            "message_type": "text",
        }
        if parent_id:
            payload["parent_id"] = parent_id
        try:
            req = urllib.request.Request(
                ax.MESSAGES_URL,
                data=_json.dumps(payload).encode(),
                method="POST",
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "X-Agent-Id": self.agent_id,
                    "X-Space-Id": space_id,
                },
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                ctype = r.headers.get("content-type", "")
            if raw:
                if "json" not in ctype.lower():
                    logger.warning("aX: post returned non-JSON content-type %s for space %s", ctype, space_id)
                    return None
                data = _json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                msg = data.get("message") or data
                mid = msg.get("id")
            else:
                read_token = self._access_token_for_space(space_id)
                mid = self._recover_posted_message_id(read_token, content, space_id, parent_id)
            if not mid:
                return None
            # Verify readback with a destination-space-scoped token plus explicit
            # agent/space headers; a 2xx POST alone is not enough for this lane.
            read_token = self._access_token_for_space(space_id)
            req = urllib.request.Request(
                f"{ax.MESSAGES_URL}/{mid}",
                headers={
                    "Authorization": "Bearer " + read_token,
                    "Accept": "application/json",
                    "X-Agent-Id": self.agent_id,
                    "X-Space-Id": space_id,
                },
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
                ctype = r.headers.get("content-type", "")
            if "json" not in ctype.lower():
                logger.warning(
                    "aX: post readback returned non-JSON content-type %s for message %s in space %s",
                    ctype,
                    mid,
                    space_id,
                )
                return None
            data = _json.loads(raw.decode() if raw else "{}")
            msg = data.get("message") or data
            if msg.get("sender_type") != "agent":
                logger.warning(
                    "aX: post readback identity mismatch for %s: sender_type=%r expected 'agent'",
                    mid,
                    msg.get("sender_type"),
                )
                return None
            if self.agent_id and msg.get("agent_id") and str(msg.get("agent_id")) != str(self.agent_id):
                logger.warning(
                    "aX: post readback agent mismatch for %s: agent_id=%r expected %r",
                    mid,
                    msg.get("agent_id"),
                    self.agent_id,
                )
                return None
            if parent_id and msg.get("parent_id") != parent_id:
                logger.warning(
                    "aX: post readback threading mismatch for %s: parent_id=%r expected %r",
                    mid,
                    msg.get("parent_id"),
                    parent_id,
                )
                return None
            if msg.get("space_id") and str(msg.get("space_id")) != str(space_id):
                logger.warning(
                    "aX: post readback space mismatch for %s: space_id=%r expected %r",
                    mid,
                    msg.get("space_id"),
                    space_id,
                )
                return None
            return str(mid)
        except Exception as e:
            self._log_post_failure(e, payload, space_id, parent_id)
            return None

    async def send_or_update_status(self, chat_id: str, status_key: str, content: str,
                                    metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        """Route Hermes gateway status callbacks to the original message activity.

        Without this method, gateway/run.py falls back to adapter.send(), which
        posts context-pressure/approval/runtime status as ordinary aX messages.
        aX wants these as processing-status updates on the triggering message.
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
        parent_id = reply_to or self._message_id_from_metadata(chat_id, metadata)
        try:
            mid = await loop.run_in_executor(
                None, lambda: self._post_message(content, parent_id, chat_id)
            )
        except Exception as e:
            return SendResult(success=False, error=str(e), retryable=True)
        if not mid:
            return SendResult(success=False, error="aX post failed", retryable=True)
        print(
            f"aX send delivered: handle=@{getattr(self, 'handle', '')} space={chat_id} reply_msg={mid} parent={parent_id or ''}",
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

    # ── Media / attachments ──────────────────────────────────────────────────────
    # Verified contract (peach, 2026-05-30): POST /api/v1/uploads/ (multipart `file`,
    # space_id optional) -> {id, attachment_id, file_id, url, content_type, ...};
    # then POST a message with attachments=[<that full upload dict>] (NOT the id
    # string — that 422s) → lands in metadata.attachments and renders inline.
    def _ax_upload(self, path: str, content_type: str, filename: Optional[str] = None) -> Optional[dict]:
        """Blocking multipart upload to /api/v1/uploads/. Returns the upload dict."""
        import json as _json
        import urllib.request
        import uuid
        fn = filename or os.path.basename(path)
        boundary = "----ax" + uuid.uuid4().hex
        with open(path, "rb") as f:
            data = f.read()
        body = (
            f"--{boundary}\r\n".encode()
            + f'Content-Disposition: form-data; name="file"; filename="{fn}"\r\n'.encode()
            + f"Content-Type: {content_type}\r\n\r\n".encode()
            + data + b"\r\n"
            + f"--{boundary}--\r\n".encode()
        )
        req = urllib.request.Request(
            ax.BASE + "/api/v1/uploads/", data=body, method="POST",
            headers={"Authorization": "Bearer " + ax.current_access_token(),
                     "Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return _json.load(r)

    def _ax_post_attachment(self, chat_id, content, up, reply_to):
        """Blocking: post a message carrying the uploaded file as an attachment."""
        import json as _json
        import urllib.request
        # aX rejects empty/whitespace content even when an attachment is present
        # (400 "Message content cannot be empty"). The gateway calls send_voice
        # with no caption, so default to the file name as the content.
        text = (content or "").strip()
        if not text:
            text = (isinstance(up, dict) and (up.get("original_filename") or up.get("filename"))) or "📎 attachment"
        payload = {"content": text, "space_id": chat_id, "channel": "main",
                   "message_type": "text", "attachments": [up]}
        if reply_to:
            payload["parent_id"] = reply_to
        req = urllib.request.Request(
            ax.MESSAGES_URL, data=_json.dumps(payload).encode(), method="POST",
            headers={"Authorization": "Bearer " + ax.current_access_token(),
                     "Content-Type": "application/json",
                     "X-Agent-Id": os.getenv("AX_AGENT_ID", ""), "X-Space-Id": chat_id})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = _json.load(r)
        return (d.get("message") or d).get("id")

    async def _send_file(self, chat_id, path, content_type, caption, reply_to, filename=None) -> SendResult:
        """Upload a local file and post it as a message attachment. Falls back to a
        text message (path/URL in content) if it's not a readable local file."""
        loop = asyncio.get_running_loop()
        if not (path and os.path.isfile(os.path.expanduser(path))):
            # Not a local file (e.g. an http image_url) — degrade to a text post.
            return await self.send(chat_id, (caption or "") + (f"\n{path}" if path else ""), reply_to)
        p = os.path.expanduser(path)
        try:
            up = await loop.run_in_executor(None, self._ax_upload, p, content_type, filename)
            if not up:
                return SendResult(success=False, error="aX upload failed", retryable=True)
            mid = await loop.run_in_executor(None, self._ax_post_attachment, chat_id, caption, up, reply_to)
            return SendResult(success=bool(mid), message_id=str(mid) if mid else None, retryable=not mid)
        except Exception as e:
            return SendResult(success=False, error=str(e), retryable=True)

    @staticmethod
    def _guess_mime(path: str, default: str) -> str:
        import mimetypes
        return mimetypes.guess_type(path)[0] or default

    async def send_voice(self, chat_id, audio_path, caption=None, reply_to=None, metadata=None) -> SendResult:
        return await self._send_file(chat_id, audio_path, self._guess_mime(audio_path, "audio/mpeg"), caption, reply_to)

    async def send_image(self, chat_id, image_url, caption=None, reply_to=None, metadata=None) -> SendResult:
        return await self._send_file(chat_id, image_url, self._guess_mime(image_url, "image/png"), caption, reply_to)

    async def send_image_file(self, chat_id, image_path, caption=None, reply_to=None, metadata=None) -> SendResult:
        return await self._send_file(chat_id, image_path, self._guess_mime(image_path, "image/png"), caption, reply_to)

    async def send_document(self, chat_id, file_path, caption=None, file_name=None, reply_to=None, metadata=None) -> SendResult:
        return await self._send_file(chat_id, file_path, self._guess_mime(file_path, "application/octet-stream"), caption, reply_to, filename=file_name)

    async def send_video(self, chat_id, video_path, caption=None, reply_to=None, metadata=None) -> SendResult:
        return await self._send_file(chat_id, video_path, self._guess_mime(video_path, "video/mp4"), caption, reply_to)


# ── Plugin registration ──────────────────────────────────────────────────────────
def check_requirements() -> bool:
    """aX is configured if we have a handle + an existing token file."""
    tok = os.path.expanduser(os.getenv("AX_TOKEN_FILE", ""))
    return bool(os.getenv("AX_AGENT_HANDLE") and tok and os.path.exists(tok))


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return bool(os.getenv("AX_AGENT_HANDLE") or extra.get("handle"))


def _derive_env_agent_space_id(agent_id: str = "") -> str:
    """Best-effort config-load space lookup for env-only gateway startup."""
    try:
        import json as _json
        import urllib.request

        token = ax.current_access_token()
        paths = []
        if agent_id:
            paths.append(f"/api/v1/agents/{agent_id}")
        paths.append("/api/v1/agents/me")
        for path in paths:
            req = urllib.request.Request(
                f"{ax.BASE}{path}",
                headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
            data = _json.loads(raw.decode() if raw else "{}")
            agent = data.get("agent") if isinstance(data, dict) else None
            if isinstance(agent, dict):
                data = agent
            space = data.get("space_id") if isinstance(data, dict) else None
            if space:
                return str(space)
    except Exception as exc:
        logger.debug("aX env enablement space lookup failed: %s", exc)
    return ""


def _env_enablement() -> Optional[dict]:
    """Seed PlatformConfig.extra from env so env-only setups show up in
    `hermes gateway status` without instantiating the adapter.

    Launch env identifies the agent, not its space. The home channel is derived
    from the backend agent record before the first inbound turn, so Hermes does
    not emit a spurious /sethome prompt while the adapter is still connecting.
    """
    handle = os.getenv("AX_AGENT_HANDLE", "").strip()
    token = os.getenv("AX_TOKEN_FILE", "").strip()
    agent_id = os.getenv("AX_AGENT_ID", "").strip()
    if not (handle and token):
        return None
    seed = {"handle": handle, "token_file": token, "agent_id": agent_id}
    space = _derive_env_agent_space_id(agent_id)
    if space:
        seed["space_id"] = space
        seed["home_space"] = space
        seed["home_channel"] = {"chat_id": space, "name": "aX home space"}
    return seed


async def _standalone_send(pconfig, chat_id, message, *, thread_id=None,
                           media_files=None, force_document=False):
    """Out-of-process cron delivery (deliver=ax) when cron runs separately from
    the gateway.

    Mirrors the live gateway adapter's media contract: upload each local file,
    then create a message with ``attachments=[<full upload dict>]``. This keeps
    cron/send_message delivery from degrading ``MEDIA:/path`` outputs into plain
    text on aX.
    """
    media_files = media_files or []
    try:
        # No media: keep the simple text path.
        if not media_files:
            mid = ax.post_message(message, parent_id=thread_id, space_id=chat_id)
            if mid:
                return {"success": True, "message_id": str(mid)}
            return {"error": "aX standalone send: post failed"}

        sent_ids = []
        remaining_caption = message or ""
        for media in media_files:
            media_path = media[0] if isinstance(media, (list, tuple)) else str(media)
            if not (media_path and os.path.isfile(os.path.expanduser(media_path))):
                continue
            path = os.path.expanduser(media_path)
            content_type = AXAdapter._guess_mime(path, "application/octet-stream")
            # _ax_upload/_ax_post_attachment do not depend on instance state;
            # call the proven helper implementations without constructing the
            # live gateway adapter (Platform("ax") may not exist in standalone
            # plugin import contexts until registration finishes).
            up = AXAdapter._ax_upload(None, path, content_type)
            if not up:
                continue
            mid = AXAdapter._ax_post_attachment(None, chat_id, remaining_caption, up, thread_id)
            if mid:
                sent_ids.append(str(mid))
                # Only the first attachment carries the text/caption.
                remaining_caption = ""

        if sent_ids:
            return {"success": True, "message_id": sent_ids[-1], "message_ids": sent_ids}

        # Media was requested but nothing attached. Fall back to text if present,
        # but report the attachment failure so callers do not mistake it for a
        # native media delivery.
        if message:
            mid = ax.post_message(message, parent_id=thread_id, space_id=chat_id)
            if mid:
                return {"success": False, "message_id": str(mid),
                        "error": "aX standalone media upload failed; text fallback posted"}
        return {"error": "aX standalone send: no media files attached"}
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
        required_env=["AX_AGENT_HANDLE", "AX_TOKEN_FILE"],
        install_hint="No extra packages (stdlib only); needs ax_presence_listener.py on AX_PRESENCE_DIR",
        # aX does not accept a static home-space env fallback: default/home
        # routing is seeded by _env_enablement() from the backend agent record.
        cron_deliver_env_var="",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="AX_ALLOWED_USERS",
        allow_all_env="AX_ALLOW_ALL_USERS",
        max_message_length=0,  # aX has no hard per-message line limit
        emoji="🛰️",
        platform_hint=(
            "You are posting to aX, an agent activity stream. Markdown renders. "
            "Avoid gratuitous @mentions in routine peer-agent replies; only use "
            "@handle when you intentionally need to wake a specific agent. Keep "
            "chat messages short and put substance in context artifacts. Replies "
            "thread under the message you answer."
        ),
    )
