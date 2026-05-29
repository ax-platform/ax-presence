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
import os, sys, json, time, threading, urllib.request, urllib.parse, urllib.error

# Make the repo-root modules importable no matter where we're launched from.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import ax_presence_listener as ax          # noqa: E402  (config + proven helpers)
import monitor_core as mc                  # noqa: E402  (Event / Source / run)

WHOAMI_URL = f"{ax.BASE}/api/v1/agents/me"
_agent_id_cache = None


def _access_token():
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


def _auth_headers(extra=None):
    h = {"Authorization": "Bearer " + (_access_token() or "")}
    aid = _agent_id()
    if aid:
        h["X-Agent-Id"] = aid               # required for an agent-bound PAT; harmless for device-code
        h["X-Space-Id"] = ax.SPACE_ID
    if extra:
        h.update(extra)
    return h


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
                        if ax.mentions_me(d):
                            content = (d.get("content") or "").strip()
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


def echo_reply(event):
    """The wake callback: reply to the mention, echoing the message back."""
    mid = event.payload.get("mid")
    content = event.payload.get("content") or ""
    space_id = event.payload.get("space_id") or ax.SPACE_ID
    reply = f"echo: {content}" if content else "echo: 👋"
    body = json.dumps({"content": reply, "space_id": space_id, "channel": "main",
                       "message_type": "text", "parent_id": mid}).encode()
    req = urllib.request.Request(ax.MESSAGES_URL, data=body, method="POST",
                                 headers=_auth_headers({"Content-Type": "application/json"}))
    try:
        urllib.request.urlopen(req, timeout=15)
        print(f"[echo] replied to {event.payload.get('who')}: {reply}", flush=True)
    except Exception as e:
        print(f"[echo] reply failed: {e!r}", flush=True)


def main():
    # Step 1: ensure we have a token — device-code flow waits for human approval.
    if not (os.path.exists(ax.TOKEN_FILE) and _access_token()):
        print(f"[echo] no token for @{ax.AGENT_HANDLE} yet — starting device-code onboarding…", flush=True)
        ax.connect()                          # prints verification URL + user_code, WAITS, writes token
    if not _access_token():
        print("[echo] no token after connect — aborting.", flush=True)
        sys.exit(1)
    # Step 2: heartbeat so the platform shows us connected/online.
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    # Step 3: echo loop on the shared monitor core (dedup + target-match for free).
    print(f"[echo] @{ax.AGENT_HANDLE} is live. Mention it and it echoes back. Ctrl-C to stop.", flush=True)
    mc.run([AxMentionSource()], me=ax.AGENT_HANDLE, wake=echo_reply)


if __name__ == "__main__":
    main()
