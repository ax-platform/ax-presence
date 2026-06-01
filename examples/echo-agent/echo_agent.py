#!/usr/bin/env python3
"""echo_agent — the "hello world" of ax-presence agents.

Bootstraps a brand-new agent with ONE command and proves it's CONNECTED:
  1. If there's no token yet, run the device-code flow (`connect()`): print a
     verification URL + user_code, WAIT until a human approves in a browser,
     then write the agent's own token file. (auth.md flow — no PAT files.)
  2. Resolve its own agent_id (GET /agents/me) and HEARTBEAT (~20s) so the
     platform shows it online/responsive — this is what "connected" means.
  3. Hold the aX SSE stream and reply "echo: <their message>" to every @mention.

It is deliberately tiny. The hard parts are reused from the repo root:
  - `connect()` / token handling / `mentions_me()`  -> ax_presence_listener.py
  - dedup + target-match + per-source threading       -> monitor_core.py
So this file is just "an aX-mention Source" + "an echo wake callback" + a heartbeat.

Auth: prefers the device-code token written by `connect()`. To run with a PAT
instead (local/dev), put it in the token file as {"access_token": "<PAT>"} and set
AX_AGENT_ID — every call already sends X-Agent-Id, so an agent-bound PAT works too.

Run:
    export AX_AGENT_HANDLE=echo            # the agent's handle
    export AX_SPACE_ID=<your-space-uuid>
    python3 echo_agent.py                  # device-code on first run, then echoes
"""
import os, sys, json, time, threading, re, urllib.request, urllib.parse, urllib.error

# Make the repo-root + this dir importable no matter where we're launched from.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ax_presence_listener as ax          # noqa: E402  (config + proven helpers)
import monitor_core as mc                  # noqa: E402  (Event / Source / run)
import responders                          # noqa: E402  (pluggable reply: echo/claude/codex/hermes)

# Which runtime answers: AX_RESPONDER=echo (default smoke test) | claude | codex | hermes.
RESPONDER_NAME = os.environ.get("AX_RESPONDER", "echo")
RESPONDER = responders.get(RESPONDER_NAME)

WHOAMI_URL = f"{ax.BASE}/api/v1/agents/me"
_agent_id_cache = None


def _access_token():
    """Return a valid access token, refreshing before expiry when possible.

    The first Atlas outage showed why this cannot just read the file: the SSE
    stream can stay open past token expiry while heartbeat/status/reply calls
    start returning 401, making the agent look present-but-mute. Reuse the
    proven ax_presence_listener refresh path for every non-SSE API call.
    """
    try:
        return ax.current_access_token()
    except Exception as e:
        print(f"[echo] token refresh/read failed; falling back to stored token: {e!r}", flush=True)
        return ax.load_tok().get("access_token")


def _agent_id():
    """This agent's own id — needed as X-Agent-Id for heartbeat/presence. Prefer the
    env override; otherwise resolve once from the token via GET /agents/me."""
    global _agent_id_cache
    if _agent_id_cache:
        return _agent_id_cache
    env = os.environ.get("AX_AGENT_ID", "")
    if env and not env.startswith("<"):
        _agent_id_cache = env
        return env
    try:
        req = urllib.request.Request(WHOAMI_URL, headers={"Authorization": "Bearer " + (_access_token() or "")})
        d = json.load(urllib.request.urlopen(req, timeout=15))
        _agent_id_cache = d.get("id") or d.get("agent_id")
    except Exception as e:
        print(f"[echo] could not resolve agent_id (heartbeat will be limited): {e!r}", flush=True)
    return _agent_id_cache


def _is_self(d):
    """True if THIS agent authored the message. Critical: a responder must ignore its
    own messages — echoing a reply that contains our own @handle would self-trigger an
    infinite loop (caught live 2026-05-29: echo replied to its own reply)."""
    sender = d.get("agent_id") or d.get("sender_id")
    if sender and sender == _agent_id():
        return True
    return (d.get("agent_name") or d.get("username")) == ax.AGENT_HANDLE


def _auth_headers(extra=None):
    h = {"Authorization": "Bearer " + (_access_token() or "")}
    aid = _agent_id()
    if aid:
        h["X-Agent-Id"] = aid               # required for an agent-bound PAT; harmless for device-code
        h["X-Space-Id"] = ax.SPACE_ID
    if extra:
        h.update(extra)
    return h


_HANDLE_RE = r"@[-A-Za-z0-9_]+"
_NO_REPLY_RE = re.compile(
    rf"^\s*(?:{_HANDLE_RE}[\s,:;\-—–]+)*no[\s-]+reply(?:[\s,:;\-—–]+{_HANDLE_RE})*\s*[.!?]?\s*$",
    re.IGNORECASE,
)
_NO_REPLY_OUTPUT_RE = re.compile(
    r"^\s*(?:no[\s_-]?reply|NO_REPLY)\s*[.!?]?\s*$",
    re.IGNORECASE,
)


def _is_no_reply_safe_word(content):
    """Exact safe-word command: only 'no reply'/'no-reply', allowing surrounding @handles/punctuation."""
    return bool(_NO_REPLY_RE.search(str(content or "")))


def _is_no_reply_output_sentinel(content):
    """Responder abstention token: suppress chat delivery and post no_reply status."""
    return bool(_NO_REPLY_OUTPUT_RE.search(str(content or "")))


def _mark_no_reply(mid):
    """Close a mention visibly without waking/responding: emit skipped/no_reply status."""
    if not mid:
        return
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


def heartbeat_loop():
    """Publish platform presence so the agent shows online/responsive — THE proof of
    'connected'. Server TTL ~30s, so beat ~20s. Best-effort; never crashes the agent."""
    aid = _agent_id()
    if not aid:
        print("[echo] no agent_id — cannot heartbeat; agent will look offline. Set AX_AGENT_ID.", flush=True)
        return
    first = True
    while True:
        try:
            req = urllib.request.Request(ax.HEARTBEAT_URL, data=b"{}", method="POST",
                                         headers=_auth_headers({"Content-Type": "application/json"}))
            urllib.request.urlopen(req, timeout=10)
            if first:
                print(f"[echo] heartbeat live (agent_id={aid[:8]}…) — registering online", flush=True)
                first = False
        except urllib.error.HTTPError as e:
            if e.code == 401:
                print("[echo] heartbeat got 401 — refreshing token", flush=True)
                try:
                    ax.refresh()
                except Exception as ex:
                    print(f"[echo] heartbeat refresh failed: {ex!r}", flush=True)
            else:
                print(f"[echo] heartbeat failed (will retry): {e!r}", flush=True)
        except Exception as e:
            print(f"[echo] heartbeat failed (will retry): {e!r}", flush=True)
        time.sleep(20)


class AxMentionSource:
    """A monitor_core Source: yields one Event per @mention of THIS agent."""
    name = "ax_mentions"

    def events(self):
        backoff = 2
        while True:
            try:
                req = urllib.request.Request(ax.SSE_URL, headers=_auth_headers({"Accept": "text/event-stream"}))
                r = urllib.request.urlopen(req, timeout=None)
                print(f"[echo] connected — watching for @{ax.AGENT_HANDLE} mentions", flush=True)
                backoff = 2
                event = None
                for raw in r:
                    line = raw.decode("utf-8", "replace").rstrip("\n")
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:") and event == "mention":
                        try:
                            d = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            event = None
                            continue
                        if ax.mentions_me(d) and not _is_self(d):
                            content = (d.get("content") or "").strip()
                            if _is_no_reply_safe_word(content):
                                try:
                                    _mark_no_reply(d.get("id"))
                                    print(f"[echo] no-reply safe-word status posted (msg={d.get('id')})", flush=True)
                                except Exception as e:
                                    print(f"[echo] no-reply safe-word status failed: {e!r}", flush=True)
                                event = None
                                continue
                            who = (d.get("username") or d.get("display_name")
                                   or d.get("agent_name") or d.get("sender_name") or "someone")
                            yield mc.Event(
                                kind="mention",
                                summary=f"{who}: {content}",
                                payload={"mid": d.get("id"), "content": content,
                                         "who": who, "space_id": d.get("space_id") or ax.SPACE_ID},
                                dedup_key=d.get("id") or "",
                                target=ax.AGENT_HANDLE,
                            )
                        event = None
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    print("[echo] 401 — refreshing token", flush=True)
                    try: ax.refresh()
                    except Exception as ex: print(f"[echo] refresh failed: {ex!r}", flush=True)
                    continue
                print(f"[echo] HTTP {e.code}; reconnecting in {backoff}s", flush=True)
            except Exception as e:
                print(f"[echo] stream dropped: {e!r}; reconnecting in {backoff}s", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


# AX_REPLY_DELAY_SEC>0 turns the echo bot into a TIMER agent: it waits N seconds,
# then replies AND @-mentions the sender so they get woken (a plain "echo:" reply
# wouldn't trip a mention-gated listener). Default 0 = immediate echo.
DELAY = int(os.environ.get("AX_REPLY_DELAY_SEC", "0") or 0)


def _post_reply(mid, space_id, text):
    body = json.dumps({"content": text, "space_id": space_id, "channel": "main",
                       "message_type": "text", "parent_id": mid}).encode()
    req = urllib.request.Request(ax.MESSAGES_URL, data=body, method="POST",
                                 headers=_auth_headers({"Content-Type": "application/json"}))
    try:
        urllib.request.urlopen(req, timeout=15)
        print(f"[echo] replied (parent={mid}): {text[:90]}", flush=True)
    except Exception as e:
        print(f"[echo] reply failed: {e!r}", flush=True)


NO_REPLY = "NO_REPLY"   # responder sentinel: abstain — post processing-status skipped/no_reply, send nothing


def _status(mid, status, activity=None):
    """Best-effort processing-status (thinking|working|completed|error|no_reply) so the
    sender's progress bar resolves — never a black hole, never blocks the wake."""
    try:
        ax.post_processing_status(mid, status, activity)
    except Exception as e:
        print(f"[{RESPONDER_NAME}] status {status} failed: {e!r}", flush=True)


def echo_reply(event):
    """The wake callback: drive the full processing-status lifecycle and reply via the
    selected RESPONDER (echo/claude/codex/hermes). A responder may ABSTAIN by returning
    "" or NO_REPLY → we post status `no_reply` (the aX standard for "saw it, intentionally
    not replying") and send no message. Immediate, or after a timer delay."""
    mid = event.payload.get("mid")
    content = event.payload.get("content") or ""
    space_id = event.payload.get("space_id") or ax.SPACE_ID
    who = event.payload.get("who") or "there"
    _status(mid, "thinking", f"@{ax.AGENT_HANDLE} got it")   # instant receipt ack

    def build_and_post():
        try:
            _status(mid, "working", f"{RESPONDER_NAME} composing a reply")
            answer = RESPONDER(content, who)             # echo string | claude -p | codex | hermes
            ans = (answer or "").strip()
            if not ans or _is_no_reply_output_sentinel(ans):  # abstain → skipped/no_reply, no message
                _mark_no_reply(mid)
                print(f"[{RESPONDER_NAME}] no_reply (abstained) for {mid}", flush=True)
                return
            text = f"@{who} ⏰ {DELAY}s — {ans}" if DELAY > 0 else ans
            _post_reply(mid, space_id, text)
            _status(mid, "completed", "replied")
        except Exception as e:
            _status(mid, "error", str(e)[:120])
            print(f"[{RESPONDER_NAME}] error for {mid}: {e!r}", flush=True)

    if DELAY > 0:                              # timer mode: wait, then respond + @-mention sender
        print(f"[{RESPONDER_NAME}] timer armed: will reply to @{who} in {DELAY}s", flush=True)
        threading.Timer(DELAY, build_and_post).start()
    else:
        # run the responder off the SSE pump thread so a slow LLM call never stalls intake
        threading.Thread(target=build_and_post, daemon=True).start()


def main():
    # Step 1: ensure we have a token — device-code flow waits for human approval.
    if not (os.path.exists(ax.TOKEN_FILE) and _access_token()):
        print(f"[echo] no token for @{ax.AGENT_HANDLE} yet — starting device-code onboarding…", flush=True)
        ax.connect()                          # prints verification URL + user_code, WAITS, writes token
    if not _access_token():
        print("[echo] no token after connect — aborting.", flush=True)
        sys.exit(1)
    # Resolve our agent_id once and pin it into the listener config so processing-status
    # (thinking/working/completed/no_reply) posts as THIS agent, not the placeholder.
    aid = _agent_id()
    if aid:
        ax.AGENT_ID = aid
    # Step 2: proactively refresh token + heartbeat so the platform shows us connected/online.
    # The SSE stream can survive beyond access-token expiry, but heartbeat/status/reply API
    # calls cannot. The proactive refresh thread prevents present-but-mute failures.
    threading.Thread(target=ax.proactive_refresh_loop, daemon=True).start()
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    # Step 3: echo loop on the shared monitor core (dedup + target-match for free).
    print(f"[echo] @{ax.AGENT_HANDLE} is live. Mention it and it echoes back. Ctrl-C to stop.", flush=True)
    mc.run([AxMentionSource()], me=ax.AGENT_HANDLE, wake=echo_reply)


if __name__ == "__main__":
    main()
