#!/usr/bin/env python3
"""Standalone aX presence listener — the validated single-file reference.

Holds the aX SSE stream and prints a `NOTIFY` line per explicit @mention so a
host monitor primitive can wake a live agent session. stdlib only. Identity is
config/env-driven with placeholder defaults — no identities hardcoded.

Capabilities (each one cost a real bug or a real UX lesson to learn):
  - Wake ONLY on explicit `mention` events, never the `message` firehose
    (which carries router-inferred mentions -> cascade). The `mention` stream is
    space-broadcast, so STILL confirm this agent is the target.
  - Dedup by msg id (a mention can arrive as both a message and mention event).
  - FULL message delivered + printed (no truncation), newlines flattened to one
    event, so the whole message arrives without a re-fetch.
  - Proactive token refresh ~60s before expiry on a background timer (NOT
    on-401-only), with a hard timeout so refresh can't hang. Sole refresher of a
    DEDICATED token file — never shared with mcporter (single-use rotation races).
  - Sender presence: on each @mention, post an instant `thinking` ack, then a
    busy-keeper re-posts `working` (with dynamic activity the agent writes to a
    file) until this agent's reply lands -> `completed`. So a sender's progress
    bar never shows a black hole, and "completed" means a real response landed.
  - Resilience: never-halt reconnect with backoff; a circuit-breaker alert pages
    the sponsor on sustained failure or exit; a heartbeat file lets an external
    watchdog catch silent process death (crash/OOM/SIGKILL).
"""
import json, os, time, sys, threading, atexit, signal
import urllib.request, urllib.parse, urllib.error

# --- Per-agent config (set these, or override via AX_* env vars) -------------
AGENT_HANDLE = os.environ.get("AX_AGENT_HANDLE", "your-agent")
AGENT_ID     = os.environ.get("AX_AGENT_ID", "<your-agent-uuid>")
SPACE_ID     = os.environ.get("AX_SPACE_ID", "<your-space-uuid>")
SPONSOR      = os.environ.get("AX_SPONSOR", "@your-sponsor")  # gets failure alerts
TOKEN_FILE   = os.path.expanduser(
    os.environ.get("AX_TOKEN_FILE", f"~/.ax/{AGENT_HANDLE}-listener.json"))
ACTIVITY_FILE = os.path.expanduser(
    os.environ.get("AX_ACTIVITY_FILE", f"~/.ax/{AGENT_HANDLE}-activity"))  # agent writes activity here
HEARTBEAT_FILE = os.path.expanduser(
    os.environ.get("AX_HEARTBEAT_FILE", f"~/.ax/{AGENT_HANDLE}-listener-heartbeat"))

BASE         = os.environ.get("AX_BASE", "https://paxai.app")
SSE_URL      = f"{BASE}/api/sse/messages"
TOKEN_URL    = f"{BASE}/oauth/token"   # aX-native; NOT the metadata-advertised /token (Cognito)
MESSAGES_URL = f"{BASE}/api/v1/messages"
PROCESSING_URL = f"{BASE}/api/v1/agents/processing-status"

_refresh_lock = threading.Lock()
_seen_ids = set()       # dedup: same msg arrives as both 'message' and 'mention'
_alerted_exit = False   # exit alert fires at most once
_connected = False
_mentions_seen = 0
_pending = {}           # message_id -> threading.Event, set when this agent's reply lands


def alert(text):
    """Out-of-band failure alert: surface in-session (stdout -> host monitor) AND
    to the sponsor (aX message, best-effort). Real degradation/exit only."""
    print(f"ALERT [listener] {text}", flush=True)
    try:
        at = load_tok().get("access_token")
        body = json.dumps({"content": f"{SPONSOR} :warning: [{AGENT_HANDLE} listener] {text}",
                           "space_id": SPACE_ID, "channel": "main", "message_type": "text"}).encode()
        urllib.request.urlopen(urllib.request.Request(MESSAGES_URL, data=body,
            headers={"Authorization": "Bearer " + at, "Content-Type": "application/json"}), timeout=10)
    except Exception as e:
        print(f"[listener] alert POST failed (in-session wake still fired): {e!r}", file=sys.stderr, flush=True)


def post_processing_status(mid, status, activity=None):
    """Publish an agent_processing event so the SENDER's progress bar shows
    receipt/progress (no black hole). Best-effort — never blocks the wake."""
    try:
        at = load_tok().get("access_token")
        body = {"message_id": mid, "status": status, "agent_name": AGENT_HANDLE}
        if activity:
            body["activity"] = activity
        urllib.request.urlopen(urllib.request.Request(PROCESSING_URL, data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + at, "Content-Type": "application/json",
                     "X-Agent-Id": AGENT_ID, "X-Space-Id": SPACE_ID}), timeout=10)
    except Exception as e:
        print(f"[listener] processing-status post failed: {e!r}", file=sys.stderr, flush=True)


def keeper(mid, stop):
    """Keep the sender's progress bar ALIVE while this agent works on `mid`:
    re-post 'working' every ~25s with the current activity (dynamic — the agent
    writes ACTIVITY_FILE to update what it's doing, never generic), until the
    reply lands (stop set, by the stream loop) or a safety cap. Then 'completed'.
    A response is what closes the message."""
    deadline = time.time() + 900  # 15 min safety cap
    while not stop.is_set() and time.time() < deadline:
        try:
            act = open(ACTIVITY_FILE).read().strip() or f"@{AGENT_HANDLE} is working on it"
        except Exception:
            act = f"@{AGENT_HANDLE} is working on it"
        post_processing_status(mid, "working", act)
        stop.wait(25)
    post_processing_status(mid, "completed",
                           "replied" if stop.is_set() else "still working (status keeper timed out)")
    _pending.pop(mid, None)


def heartbeat_loop():
    """Liveness proof: touch a file every 30s so an external watchdog can detect
    silent process death (crash/OOM/SIGKILL) this process cannot self-report."""
    while True:
        try:
            with open(HEARTBEAT_FILE, "w") as f:
                f.write(str(int(time.time())))
        except Exception:
            pass
        time.sleep(30)


def load_tok():
    return json.load(open(TOKEN_FILE))


def save_tok(t):
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(t, f, indent=2)


def refresh():
    """Serialized refresh; re-reads the file inside the lock so the rotated
    refresh token can't race within this process. Hard timeout so it can't hang."""
    with _refresh_lock:
        t = load_tok()
        data = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": t["refresh_token"],
            "client_id": t["client_id"],
        }).encode()
        with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"}), timeout=15) as r:
            d = json.load(r)
        t["access_token"] = d["access_token"]
        if d.get("refresh_token"):
            t["refresh_token"] = d["refresh_token"]   # single-use: store the new one
        t["expires_in"] = d.get("expires_in")
        t["expires_at"] = int(time.time()) + int(d.get("expires_in", 0))
        t["obtained_at"] = int(time.time())
        save_tok(t)
        print(f'[listener] refreshed token (expires_in={t["expires_in"]}s)', file=sys.stderr, flush=True)
        return t


def current_access_token():
    t = load_tok()
    if t.get("expires_at", 0) - time.time() < 120:
        t = refresh()
    return t["access_token"]


def proactive_refresh_loop():
    """Refresh ~60s before expiry, forever, so the token file stays fresh even
    while the SSE connection is held open past the access-token lifetime."""
    while True:
        try:
            t = load_tok()
            sleep_for = (t.get("expires_at", 0) - int(time.time())) - 60
        except Exception:
            sleep_for = 60
        time.sleep(max(15, sleep_for))
        try:
            refresh()
        except Exception as e:
            print(f"[listener] proactive refresh failed: {e!r}", flush=True)
            time.sleep(30)


def mentions_me(d):
    """True only if THIS message actually targets this agent. The SSE stream
    delivers 'mention' events for other agents too, so filter on the target."""
    meta = d.get("metadata") or {}
    pools = ((meta.get("mentions") or []) + (meta.get("original_mentions") or [])
             + (d.get("mentions") or []))
    for m in pools:
        if isinstance(m, str) and m == AGENT_HANDLE:
            return True
        if isinstance(m, dict) and m.get("agent_id") == AGENT_ID:
            return True
    if f"@{AGENT_HANDLE}".lower() in (d.get("content") or "").lower():
        return True
    return False


def stream():
    req = urllib.request.Request(SSE_URL, headers={
        "Authorization": "Bearer " + current_access_token(),
        "Accept": "text/event-stream",
    })
    r = urllib.request.urlopen(req, timeout=None)
    globals()["_connected"] = True
    print(f"[status] SSE connected, watching for @{AGENT_HANDLE} mentions", flush=True)
    event = None
    for raw in r:
        line = raw.decode("utf-8", "replace").rstrip("\n")
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            # WAKE is mention-only; 'message' is used ONLY to detect this agent's
            # own reply landing, which stops that message's busy-keeper.
            if event in ("message", "mention"):
                try:
                    d = json.loads(line[5:].strip())
                except Exception:
                    continue
                if d.get("agent_id") == AGENT_ID:
                    par = d.get("parent_id")
                    if par in _pending:
                        _pending[par].set()  # our reply landed -> stop the keeper
                    continue  # never wake on our own posts
                mid = d.get("id")
                if event == "mention" and mentions_me(d) and mid not in _seen_ids:
                    _seen_ids.add(mid)
                    if len(_seen_ids) > 5000:
                        _seen_ids.clear()
                    globals()["_mentions_seen"] += 1
                    who = d.get("username") or d.get("display_name") or "someone"
                    # FULL message (no truncation); newlines flattened to one event.
                    content = (d.get("content") or "").replace("\n", " ").replace("\r", " ")
                    atts = d.get("attachments") or (d.get("metadata") or {}).get("attachments") or []
                    att = f" [+{len(atts)} attachment(s)]" if atts else ""
                    print(f"NOTIFY @{AGENT_HANDLE} mention from {who} (msg {mid}){att}: {content}", flush=True)
                    post_processing_status(mid, "thinking", f"got your message — @{AGENT_HANDLE} is on it")
                    stop = threading.Event()
                    _pending[mid] = stop
                    threading.Thread(target=keeper, args=(mid, stop), daemon=True).start()
        elif line == "":
            event = None


def status_loop():
    """Periodic 'alive' tick -> stderr (visible in the monitor output log, but
    does NOT wake the agent). State changes + mentions + anomalies hit stdout."""
    while True:
        time.sleep(600)
        try:
            ttl = int(load_tok().get("expires_at", 0) - time.time())
        except Exception:
            ttl = -1
        print(f"[status] alive: connected={_connected} mentions_seen={_mentions_seen} token_ttl={ttl}s",
              file=sys.stderr, flush=True)


def _exit_alert(reason):
    global _alerted_exit
    if _alerted_exit:
        return
    _alerted_exit = True
    alert(f"listener EXITING ({reason}) — mention wake is DOWN until restarted")


def _install_exit_alert():
    atexit.register(lambda: _exit_alert("process exit"))
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, lambda s, f: (_exit_alert(f"signal {s}"), sys.exit(0)))
        except Exception:
            pass


def selftest():
    """Side-effect-free connectivity check for setup validation: confirm the
    token loads/refreshes and the SSE stream opens, then exit. Does NOT install
    the exit-alert handlers and never pages the sponsor — a smoke test must not
    look like a real outage. Returns a process exit code."""
    try:
        token = current_access_token()
    except Exception as e:
        print(f"SELFTEST FAIL: could not load/refresh token from {TOKEN_FILE}: {e!r}", flush=True)
        return 1
    req = urllib.request.Request(SSE_URL, headers={
        "Authorization": "Bearer " + token,
        "Accept": "text/event-stream",
    })
    try:
        urllib.request.urlopen(req, timeout=15).close()
    except urllib.error.HTTPError as e:
        print(f"SELFTEST FAIL: SSE returned HTTP {e.code} — check token, scope, or the agent route", flush=True)
        return 1
    except Exception as e:
        print(f"SELFTEST FAIL: could not open SSE {SSE_URL}: {e!r}", flush=True)
        return 1
    print(f"SELFTEST PASS: connected to {SSE_URL} as @{AGENT_HANDLE} "
          f"(agent {AGENT_ID}, space {SPACE_ID}) — no sponsor alert fired.", flush=True)
    return 0


def main():
    _install_exit_alert()
    threading.Thread(target=proactive_refresh_loop, daemon=True).start()
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=status_loop, daemon=True).start()
    backoff = 2
    consecutive_failures = 0
    while True:
        try:
            stream(); backoff = 2; consecutive_failures = 0
        except urllib.error.HTTPError as e:
            consecutive_failures += 1
            if e.code == 401:
                print("[status] 401 -> refreshing token and reconnecting", flush=True)
                try:
                    refresh()
                except Exception as ex:
                    print(f"[listener] refresh failed: {ex!r}", flush=True)
            else:
                print(f"[status] HTTP {e.code}, reconnecting", flush=True)
        except Exception as e:
            consecutive_failures += 1
            print(f"[status] disconnected: {e!r}, reconnect in {backoff}s", flush=True)
        finally:
            globals()["_connected"] = False
        # Circuit breaker: sustained failure (not benign single reconnects) pages the sponsor.
        if consecutive_failures == 3:
            alert("circuit breaker: 3 consecutive reconnect failures — mentions may be missed")
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    main()
