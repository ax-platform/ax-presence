#!/usr/bin/env python3
"""echo_agent — the "hello world" of ax-presence agents.

Bootstraps a brand-new agent with ONE command and nothing else:
  1. If there's no token yet, run the device-code flow (`connect()`): print a
     verification URL + user_code, WAIT until a human approves in a browser,
     then write the agent's own token file.
  2. Hold the aX SSE stream and, every time someone @-mentions this agent,
     reply "echo: <their message>".

It is deliberately tiny. The two hard parts are reused from the repo root:
  - `connect()` / token handling / `mentions_me()`  -> ax_presence_listener.py
  - dedup + target-match + per-source threading       -> monitor_core.py
So this file is just "an aX-mention Source" + "an echo wake callback".

Run:
    export AX_AGENT_HANDLE=echo            # your NEW agent's handle
    export AX_SPACE_ID=<your-space-uuid>
    python3 echo_agent.py                  # device-code on first run, then echoes

(or use ./bootstrap.sh — same thing with the run-dir/Docker ergonomics)
"""
import os, sys, json, time, urllib.request, urllib.parse, urllib.error

# Make the repo-root modules importable no matter where we're launched from.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import ax_presence_listener as ax          # noqa: E402  (config + proven helpers)
import monitor_core as mc                  # noqa: E402  (Event / Source / run)


def _access_token():
    """Current access token, refreshing once if the file is stale/expired."""
    tok = ax.load_tok()
    return tok.get("access_token")


class AxMentionSource:
    """A monitor_core Source: yields one Event per @mention of THIS agent.

    Holds the SSE stream (token-scoped: events from every space the agent is in),
    keeps only `event: mention` lines that name us (`mentions_me`), and yields them.
    Never raises out of the loop — reconnects with backoff so it can't go deaf.
    """
    name = "ax_mentions"

    def events(self):
        backoff = 2
        while True:
            try:
                req = urllib.request.Request(ax.SSE_URL, headers={
                    "Authorization": "Bearer " + (_access_token() or ""),
                    "Accept": "text/event-stream",
                })
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
                                dedup_key=d.get("id") or "",   # core skips the message/mention double-deliver
                                target=ax.AGENT_HANDLE,
                            )
                        event = None
            except urllib.error.HTTPError as e:
                if e.code == 401:                 # token expired mid-stream -> refresh + reconnect
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
    req = urllib.request.Request(ax.MESSAGES_URL, data=body, method="POST", headers={
        "Authorization": "Bearer " + (_access_token() or ""),
        "Content-Type": "application/json"})
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
    # Step 2: run the echo loop on the shared monitor core (dedup + target-match for free).
    print(f"[echo] @{ax.AGENT_HANDLE} is live. Mention it and it echoes back. Ctrl-C to stop.", flush=True)
    mc.run([AxMentionSource()], me=ax.AGENT_HANDLE, wake=echo_reply)


if __name__ == "__main__":
    main()
