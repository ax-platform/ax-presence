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
  - Intent-aware wake: after the NOTIFY, surface the sender's recent thread in a
    CONTEXT line so the agent reads the THROUGHLINE across their messages (people
    hint and repeat a theme), not just the single line that triggered the wake.
  - Sender presence: on each @mention, post an instant `thinking` ack, then a
    busy-keeper re-posts `working` (with dynamic activity the agent writes to a
    file) until this agent's reply lands -> `completed`. So a sender's progress
    bar never shows a black hole, and "completed" means a real response landed.
  - Resilience: never-halt reconnect with backoff; a circuit-breaker alert pages
    the sponsor on sustained failure or exit; a heartbeat file lets an external
    watchdog catch silent process death (crash/OOM/SIGKILL).
"""
import json, os, time, sys, threading, atexit, signal, fcntl
import urllib.request, urllib.parse, urllib.error

# --- Per-agent config (set these, or override via AX_* env vars) -------------
AGENT_HANDLE = os.environ.get("AX_AGENT_HANDLE", "your-agent")
AGENT_ID     = os.environ.get("AX_AGENT_ID", "<your-agent-uuid>")
SPACE_ID     = os.environ.get("AX_SPACE_ID", "")
SPONSOR      = os.environ.get("AX_SPONSOR", "@your-sponsor")  # gets failure alerts
TOKEN_FILE   = os.path.expanduser(
    os.environ.get("AX_TOKEN_FILE", f"~/.ax/{AGENT_HANDLE}-listener.json"))
ACTIVITY_FILE = os.path.expanduser(
    os.environ.get("AX_ACTIVITY_FILE", f"~/.ax/{AGENT_HANDLE}-activity"))  # agent writes activity here
ACTIVITY_JSON_FILE = os.path.expanduser(
    os.environ.get("AX_ACTIVITY_JSON_FILE", f"~/.ax/{AGENT_HANDLE}-activity.json"))  # structured {line,tool,step,total,unit}
REMINDERS_FILE = os.path.expanduser(
    os.environ.get("AX_REMINDERS_FILE", f"~/.ax/{AGENT_HANDLE}-reminders.json"))  # self-scheduled wakes
HEARTBEAT_FILE = os.path.expanduser(
    os.environ.get("AX_HEARTBEAT_FILE", f"~/.ax/{AGENT_HANDLE}-listener-heartbeat"))
HOME_FEED_FILE = os.path.expanduser(
    os.environ.get("AX_HOME_FEED_FILE", f"~/.ax/{AGENT_HANDLE}-home-feed.json"))  # rolling cross-space SSE activity
BUSY_MESSAGES_FILE = os.path.expanduser(
    os.environ.get("AX_BUSY_MESSAGES_FILE", f"~/.ax/{AGENT_HANDLE}-busy-messages.json"))  # customizable check-in lines
# OSS-safe, customizable "still working" check-in lines the waiting party sees
# while this agent works. Edit BUSY_MESSAGES_FILE (a JSON list) to personalize;
# these defaults should stay neutral, professional, and useful.
DEFAULT_BUSY = [
    "Still working — checking the details…",
    "Using tools to make progress…",
    "Reviewing context and current state…",
    "Running the next verification step…",
    "Preparing a concise update…",
    "Almost there — validating the result…",
]

BASE         = os.environ.get("AX_BASE", "https://paxai.app")
SSE_URL      = f"{BASE}/api/sse/messages"
TOKEN_URL    = f"{BASE}/oauth/token"   # aX-native; NOT the metadata-advertised /token (Cognito)
REGISTER_URL = f"{BASE}/oauth/register"          # device-code self-onboarding (--connect)
DEVICE_URL   = f"{BASE}/oauth/device/code"
RESOURCE     = f"{BASE}/mcp/agents/{AGENT_HANDLE}"  # named-agent route REQUIRED (base /mcp -> invalid_target)
SCOPE        = "openid offline_access ax-api/mcp:read ax-api/mcp:write"
MESSAGES_URL = f"{BASE}/api/v1/messages"
PROCESSING_URL = f"{BASE}/api/v1/agents/processing-status"
HEARTBEAT_URL = f"{BASE}/api/v1/agents/heartbeat"  # platform liveness (server TTL ~30s)
SIGNAL_URL   = f"{BASE}/internal/agent-signal"      # ALC signal-store (stack); X-API-Key auth, Redis 90s TTL
SIGNAL_API_KEY = os.environ.get("AX_INTERNAL_SIGNAL_KEY") or os.environ.get("INTERNAL_DISPATCH_API_KEY")

_refresh_lock = threading.Lock()
_seen_ids = set()       # dedup: same msg arrives as both 'message' and 'mention'
_alerted_exit = False   # exit alert fires at most once
_connected = False
_mentions_seen = 0
_replies_sent = 0          # productivity signal: replies/posts this listener has landed
_last_reply_at = 0         # epoch of the last successful reply/post (0 = none yet)
_currently_401 = False     # token currently rejected (mute/broken) — distinct from dormant
_pending = {}           # message_id -> threading.Event, set when this agent's reply lands
HOME_FEED_MAX = 200     # cap the rolling cross-space feed buffer so the file stays small
_home_feed = []         # in-memory rolling list of recent cross-space events (newest last)
_home_lock = threading.Lock()


def alert(text):
    """Out-of-band failure alert: surface in-session (stdout -> host monitor) AND
    to the sponsor (aX message, best-effort). Real degradation/exit only."""
    print(f"ALERT [listener] {text}", flush=True)
    try:
        at = current_access_token()
        body = json.dumps({"content": f"{SPONSOR} :warning: [{AGENT_HANDLE} listener] {text}",
                           "space_id": SPACE_ID, "channel": "main", "message_type": "text"}).encode()
        urllib.request.urlopen(urllib.request.Request(MESSAGES_URL, data=body,
            headers={"Authorization": "Bearer " + at, "Content-Type": "application/json"}), timeout=10)
    except Exception as e:
        print(f"[listener] alert POST failed (in-session wake still fired): {e!r}", file=sys.stderr, flush=True)


def derive_space_id_from_agent_record():
    """Return this agent's authoritative space_id from the aX agent record."""
    global SPACE_ID
    if SPACE_ID and SPACE_ID != "<your-space-uuid>":
        return SPACE_ID
    try:
        at = current_access_token()
        paths = []
        if AGENT_ID and AGENT_ID != "<your-agent-uuid>":
            paths.append(f"/api/v1/agents/{AGENT_ID}")
        paths.append("/api/v1/agents/me")
        for path in paths:
            req = urllib.request.Request(
                BASE + path,
                headers={"Authorization": "Bearer " + at, "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
            data = json.loads(raw.decode() if raw else "{}")
            agent = data.get("agent") if isinstance(data, dict) else None
            if isinstance(agent, dict):
                data = agent
            space = data.get("space_id") if isinstance(data, dict) else None
            if space:
                SPACE_ID = str(space)
                print(f"[listener] derived space_id for @{AGENT_HANDLE}: {SPACE_ID}", flush=True)
                return SPACE_ID
    except Exception as e:
        print(f"[listener] space_id lookup failed: {e!r}", file=sys.stderr, flush=True)
    return SPACE_ID


def build_processing_body(mid, status, activity=None, tool_name=None, progress=None, detail=None):
    """Build the processing-status body. Keep legacy status/activity behavior,
    but forward safe structured fields when available so Hermes agents can show
    tool/category + concise progress without leaking raw args or output."""
    body = {"message_id": mid, "status": status, "agent_name": AGENT_HANDLE}
    if activity:
        body["activity"] = activity
    if tool_name:
        body["tool_name"] = str(tool_name)
    if progress is not None:
        try:
            cur = int(progress.get("current"))
            tot = int(progress.get("total"))
            unit = progress.get("unit")
            body["progress"] = {"current": cur, "total": tot, "unit": str(unit) if unit else "steps"}
        except (AttributeError, TypeError, ValueError):
            pass
    if isinstance(detail, dict) and detail:
        body["detail"] = detail
    return body


def post_processing_status(mid, status, activity=None, tool_name=None, progress=None, detail=None):
    """Publish an agent_processing event so the SENDER's progress bar shows
    receipt/progress (no black hole). Best-effort — never blocks the wake."""
    try:
        at = current_access_token()
        body = build_processing_body(mid, status, activity, tool_name, progress, detail)
        urllib.request.urlopen(urllib.request.Request(PROCESSING_URL, data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + at, "Content-Type": "application/json",
                     "X-Agent-Id": AGENT_ID, "X-Space-Id": SPACE_ID}), timeout=10)
    except Exception as e:
        print(f"[listener] processing-status post failed: {e!r}", file=sys.stderr, flush=True)


def _extract_message_list(data):
    if isinstance(data, dict):
        return data.get("messages") or data.get("items") or []
    if isinstance(data, list):
        return data
    return []


def _recover_posted_message_id(at, content, space_id, parent_id=None):
    """Recover a POSTed message id when the deployed API returns an empty body.

    Some aX message POST paths successfully create the message but return HTTP 2xx
    with no JSON body. Treat that as an ambiguous send, not a failure: fetch recent
    destination-space messages and locate the exact content/parent pair.
    """
    try:
        qs = urllib.parse.urlencode({"space_id": space_id, "limit": 50})
        req = urllib.request.Request(f"{MESSAGES_URL}?{qs}",
            headers={"Authorization": "Bearer " + at,
                     "X-Agent-Id": AGENT_ID, "X-Space-Id": space_id})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
    except Exception as e:
        print(f"[send] recovery list failed: {e!r}", flush=True)
        return None
    for m in _extract_message_list(data):
        if (m.get("content") or "") != content:
            continue
        if parent_id and m.get("parent_id") != parent_id:
            continue
        return (m.get("message") or m).get("id")
    return None


def post_message(content, parent_id=None, space_id=None):
    """The easy 'respond to a side question / give an update' primitive.

    Posts a message (a threaded reply when parent_id is set), then RE-FETCHES to
    confirm it actually landed — a 2xx alone has silently lied before. Returns the
    new message id (or None). Drives the --reply / --say one-shots so an agent can
    answer a mention in one line instead of hand-rolling a REST call."""
    at = current_access_token()
    target_space = space_id or SPACE_ID
    payload = {"content": content, "space_id": target_space,
               "channel": "main", "message_type": "text"}
    if parent_id:
        payload["parent_id"] = parent_id
    try:
        req = urllib.request.Request(MESSAGES_URL, data=json.dumps(payload).encode(), method="POST",
            headers={"Authorization": "Bearer " + at, "Content-Type": "application/json",
                     "X-Agent-Id": AGENT_ID, "X-Space-Id": target_space})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
        if raw:
            d = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            mid = (d.get("message") or d).get("id")
        else:
            mid = _recover_posted_message_id(at, content, target_space, parent_id)
        if not mid:
            print("[send] failed: POST returned no readable message id", flush=True)
            return None
        globals()["_replies_sent"] += 1               # productivity signal
        globals()["_last_reply_at"] = int(time.time())
        globals()["_currently_401"] = False           # a landed post proves auth works
    except Exception as e:
        print(f"[send] failed: {e!r}", flush=True)
        return None
    try:                                  # verify it landed; never trust the 2xx alone
        req = urllib.request.Request(f"{MESSAGES_URL}/{mid}",
            headers={"Authorization": "Bearer " + at,
                     "X-Agent-Id": AGENT_ID, "X-Space-Id": target_space})
        urllib.request.urlopen(req, timeout=15)
        print(f"[send] delivered (id={mid})", flush=True)
    except Exception:
        print(f"[send] posted (id={mid}) but re-fetch failed — verify in app", flush=True)
    return mid


def _fmt_elapsed(secs):
    """Human 'how long': 45s, 2m10s, 1h03m."""
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60:02d}s"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


def parse_activity(json_text=None, legacy_text=None):
    """Parse structured Hermes activity ({line,tool,step,total,unit}) with a
    legacy string fallback. This intentionally exposes only concise labels/progress,
    not raw command arguments, environment, tokens, or tool output."""
    if json_text:
        try:
            d = json.loads(json_text)
            if isinstance(d, dict):
                out: dict[str, object] = {"line": str(d.get("line") or "").strip()}
                if d.get("tool"):
                    out["tool"] = str(d["tool"])
                step, total = d.get("step"), d.get("total")
                if isinstance(step, str) and "/" in step:
                    a, _, b = step.partition("/")
                    step, total = a, b
                if step is not None:
                    try:
                        out["step"] = int(step)
                    except (TypeError, ValueError):
                        pass
                if total is not None:
                    try:
                        out["total"] = int(total)
                    except (TypeError, ValueError):
                        pass
                if d.get("unit"):
                    out["unit"] = str(d["unit"])
                return out
        except Exception:
            pass
    return {"line": (legacy_text or json_text or "").strip()}


def read_activity():
    """Read current activity, preferring structured JSON and falling back to the
    legacy bare-string file for Claude Code/Codex/older agent compatibility."""
    jt = lt = None
    try:
        jt = open(ACTIVITY_JSON_FILE).read()
    except Exception:
        jt = None
    if not jt:
        try:
            lt = open(ACTIVITY_FILE).read()
        except Exception:
            lt = None
    return parse_activity(jt, lt)


def activity_to_progress(act):
    """Turn parsed step/total activity into the backend progress shape."""
    if act.get("step") is not None and act.get("total") is not None:
        p = {"current": act["step"], "total": act["total"]}
        if act.get("unit"):
            p["unit"] = act["unit"]
        return p
    return None


def load_busy_messages():
    """Customizable 'still working' check-in lines (JSON list at BUSY_MESSAGES_FILE);
    falls back to DEFAULT_BUSY. Re-read per message so edits take effect live."""
    try:
        msgs = json.load(open(BUSY_MESSAGES_FILE))
        if isinstance(msgs, list) and msgs:
            return [str(m) for m in msgs]
    except Exception:
        pass
    return DEFAULT_BUSY


def keeper(mid, stop):
    """Keep the requester's check-in / progress bar ALIVE while this agent works on
    `mid`: re-post 'working' every ~25s until the reply lands (stop set, by the stream
    loop) or a safety cap, then 'completed'. The status carries (a) HOW LONG the agent
    has been working (elapsed), and (b) either real activity (Hermes/tool progress
    when available) or a rotating neutral, customizable 'still working' line. This is
    the agent-to-agent check-in: the waiting party sees useful progress, not a black
    hole. A response is what closes the message."""
    start = time.time()
    deadline = start + 900  # 15 min safety cap
    busy = load_busy_messages()
    i = 0
    while not stop.is_set() and time.time() < deadline:
        parsed = read_activity()          # structured JSON preferred, legacy string fallback
        act = parsed.get("line")
        tool = parsed.get("tool")
        progress = activity_to_progress(parsed)
        if not act:                       # no real activity reported -> rotate a neutral check-in
            act = busy[i % len(busy)]
            i += 1
        post_processing_status(mid, "working", f"{act} (⏱ {_fmt_elapsed(time.time() - start)})",
                               tool_name=tool, progress=progress)
        stop.wait(25)
    total = _fmt_elapsed(time.time() - start)
    post_processing_status(mid, "completed",
                           f"replied (took {total})" if stop.is_set() else f"still working (keeper timed out after {total})")
    _pending.pop(mid, None)


def _presence_beat():
    """One platform heartbeat POST. The access token can be long-expired by the
    time a tick runs (host suspend pauses the refresh timer but not wall-clock
    expiry), so always fetch via current_access_token(); on a 401 force one
    refresh and retry once instead of spamming with a dead token."""
    def _post(at):
        req = urllib.request.Request(HEARTBEAT_URL, data=b"{}",
            headers={"Authorization": "Bearer " + at, "Content-Type": "application/json",
                     "X-Agent-Id": AGENT_ID, "X-Space-Id": SPACE_ID})
        urllib.request.urlopen(req, timeout=10)
    try:
        _post(current_access_token())
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
        try:
            _post(refresh()["access_token"])
        except Exception:
            globals()["_currently_401"] = True
            raise
    globals()["_currently_401"] = False


def presence_loop():
    """Publish liveness to the PLATFORM: POST /api/v1/agents/heartbeat every ~20s
    (server TTL ~30s) so this agent shows 'online' + responsive in the agents
    presence/availability views. The endpoints already exist server-side; the
    common gap is that agents simply never call them, so everyone reads 'offline'.
    Calling this is what makes an agent discoverable as alive."""
    derive_space_id_from_agent_record()
    while True:
        try:
            _presence_beat()
        except Exception as e:
            print(f"[listener] platform heartbeat failed: {e!r}", file=sys.stderr, flush=True)
        time.sleep(20)


def heartbeat_loop():
    """Liveness proof: touch a file every 30s so an external watchdog can detect
    silent process death (crash/OOM/SIGKILL) this process cannot self-report."""
    while True:
        try:
            os.makedirs(os.path.dirname(HEARTBEAT_FILE) or ".", mode=0o700, exist_ok=True)
            # 0600 (not the umask default 664) — no world-readable agent files on a shared box (@nyx).
            fd = os.open(HEARTBEAT_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(str(int(time.time())))
            # Productivity signal record for the agent-lifecycle sweep: the
            # CURRENTLY-functional dimension last_active_at (backward-looking) can't see.
            # Lets the ladder route 'present-but-mute/401-broken' to FIX, not archive.
            sig = {"handle": AGENT_HANDLE, "ts": int(time.time()), "connected": _connected,
                   "currently_401": _currently_401, "mentions_seen": _mentions_seen,
                   "replies_sent": _replies_sent, "last_reply_at": _last_reply_at,
                   "responsiveness_ratio": (round(_replies_sent / _mentions_seen, 3)
                                            if _mentions_seen else None)}
            sp = os.path.expanduser(f"~/.ax/{AGENT_HANDLE}-signal.json")
            sfd = os.open(sp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(sfd, "w") as sf:
                json.dump(sig, sf)
        except Exception:
            pass
        # Push the same signal to the backend signal-store (the ALC sweep reads THIS — a
        # backend on another box can't read the local file). Separate X-API-Key auth, NOT
        # the agent's rotating token, so a 401-broken agent can STILL report currently_401
        # (resolves the chicken-egg). Best-effort; 404s harmlessly until the endpoint deploys.
        if SIGNAL_API_KEY:
            try:
                sbody = json.dumps({"agent_id": AGENT_ID, "currently_401": _currently_401,
                    "responsiveness_ratio": (round(_replies_sent / _mentions_seen, 3) if _mentions_seen else None),
                    "mentions_seen": _mentions_seen, "replies_sent": _replies_sent,
                    "last_reply_at": _last_reply_at}).encode()
                urllib.request.urlopen(urllib.request.Request(SIGNAL_URL, data=sbody, method="POST",
                    headers={"X-API-Key": SIGNAL_API_KEY, "Content-Type": "application/json"}), timeout=10)
            except Exception:
                pass  # never block the heartbeat
        time.sleep(30)


def load_tok():
    return json.load(open(TOKEN_FILE))


def save_tok(t):
    # Ensure the token dir exists (a truly fresh box has no ~/.ax) BEFORE O_CREAT, else
    # os.open raises FileNotFoundError *after* the user already burned the device code.
    # (caught live on the Graviton fresh box by @nyx, 2026-05-29.)
    os.makedirs(os.path.dirname(TOKEN_FILE) or ".", mode=0o700, exist_ok=True)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(t, f, indent=2)


def _form_post(url, fields):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:        # device-code errors come back as 400 JSON
        try:
            return json.load(e)
        except Exception:
            return {"error": f"http_{e.code}"}


def connect():
    """Self-onboarding via device-code OAuth: the agent creates a verification URL,
    hands it to the human, and WAITS (polls) until they approve — then writes its own
    token file and returns. This IS the 'device-code wait' as a startup step, so a
    brand-new agent goes from nothing -> connected with one command (`--connect`),
    then proceeds to stay present. Uses the aX-native /oauth/* endpoints (NOT the
    Cognito /.well-known metadata). No deps beyond stdlib."""
    if AGENT_HANDLE in ("", "your-agent"):
        print("connect: set AX_AGENT_HANDLE first (it picks your named-agent route).", flush=True)
        return 1
    print(f"[connect] registering a public client for @{AGENT_HANDLE}…", flush=True)
    req = urllib.request.Request(REGISTER_URL,
        data=json.dumps({
            "client_name": f"{AGENT_HANDLE} listener",
            "grant_types": ["urn:ietf:params:oauth:grant-type:device_code", "refresh_token"],
            "token_endpoint_auth_method": "none", "redirect_uris": [], "scope": SCOPE,
        }).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            client_id = json.load(r).get("client_id")
    except Exception as e:
        print(f"[connect] register failed: {e!r}", flush=True)
        return 1
    if not client_id:
        print("[connect] register returned no client_id", flush=True)
        return 1

    dc = _form_post(DEVICE_URL, {"client_id": client_id, "resource": RESOURCE, "scope": SCOPE})
    if not dc.get("device_code"):
        print(f"[connect] device/code failed: {dc}", flush=True)
        return 1
    interval = int(dc.get("interval", 5))
    print("\n  >>> APPROVE HERE: " + str(dc.get("verification_uri_complete")), flush=True)
    print("  >>> user_code:   " + str(dc.get("user_code")) + "\n", flush=True)
    print(f"[connect] waiting for approval (polling every {interval}s)…", flush=True)

    deadline = time.time() + int(dc.get("expires_in", 600))
    while time.time() < deadline:
        time.sleep(interval)
        resp = _form_post(TOKEN_URL, {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": dc["device_code"], "client_id": client_id,
        })
        err = resp.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        if err:
            print(f"[connect] token error: {err}", flush=True)
            return 1
        now = int(time.time())                  # success — no error field
        save_tok({
            "access_token": resp["access_token"], "refresh_token": resp.get("refresh_token"),
            "client_id": client_id, "token_type": resp.get("token_type", "Bearer"),
            "scope": resp.get("scope", SCOPE), "expires_in": resp.get("expires_in"),
            "expires_at": now + int(resp.get("expires_in", 900)), "obtained_at": now,
        })
        print(f"[connect] connected — token written to {TOKEN_FILE}", flush=True)
        return 0
    print("[connect] device code expired before approval — re-run --connect.", flush=True)
    return 1


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


def _proactive_tick(now=None):
    """One refresh-timer decision; returns seconds to sleep, never more than 60.
    time.sleep does not advance during host suspend (macOS lid-close), so one
    long sleep-until-expiry strands the timer while wall-clock expiry passes
    (the 2026-06-12 401 storm). Short slices re-read the file every tick, so a
    wake refreshes within one slice."""
    now = int(time.time()) if now is None else now
    try:
        remaining = load_tok().get("expires_at", 0) - now
    except Exception:
        return 60
    if remaining > 60:
        return min(60, remaining - 60)
    try:
        refresh()
    except Exception as e:
        print(f"[listener] proactive refresh failed: {e!r}", flush=True)
        return 30
    return 15


def proactive_refresh_loop():
    """Refresh ~60s before expiry, forever, so the token file stays fresh even
    while the SSE connection is held open past the access-token lifetime."""
    while True:
        time.sleep(_proactive_tick())


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


def record_home_event(d, event):
    """Accumulate every space-tagged SSE event into a rolling cross-space feed.
    The SSE stream is token-scoped (delivers ALL the agent's spaces), so this is
    the only live source of cross-space activity — REST messages reads only the
    current space. Persisted to HOME_FEED_FILE so the one-shot `--home` view
    (a separate process) renders real recent activity, not just a snapshot."""
    rec = {
        "ts": d.get("created_at") or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "space_id": d.get("space_id") or "?",
        "who": d.get("username") or d.get("display_name") or d.get("agent_name") or d.get("sender_name") or "?",
        "kind": event,
        "mine": d.get("agent_id") == AGENT_ID,
        "text": (d.get("content") or "").replace("\n", " ").replace("\r", " ")[:140],
    }
    with _home_lock:
        _home_feed.append(rec)
        if len(_home_feed) > HOME_FEED_MAX:
            del _home_feed[:len(_home_feed) - HOME_FEED_MAX]
        snapshot = list(_home_feed)
    try:
        tmp = HOME_FEED_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snapshot, f)
        os.replace(tmp, HOME_FEED_FILE)  # atomic so --home never reads a half-write
    except Exception:
        pass
def _sender_key(m):
    """Stable identity for a message author across messages: prefer agent_id,
    else fall back to a human/display name. Used to gather one sender's thread."""
    return (m.get("agent_id") or m.get("sender_id") or m.get("user_id")
            or m.get("username") or m.get("display_name") or m.get("sender_name"))


def emit_sender_context(d):
    """Intent-aware wake: after the NOTIFY, fetch the SENDER's recent messages in
    this space and print a CONTEXT line so the agent reads the THROUGHLINE across
    their messages, not just the single literal line that triggered the wake.
    People hint and repeat a theme; the signal is the thread, not one message.
    Runs in a thread so it never delays the instant wake/ack. Best-effort.
    Especially valuable for the daemon shape (a freshly-spawned agent sees only
    the wake line, so the prior-message context would otherwise be lost)."""
    try:
        key = _sender_key(d)
        cur = d.get("id")
        sp = d.get("space_id") or SPACE_ID
        at = current_access_token()
        url = MESSAGES_URL + "?" + urllib.parse.urlencode({"space_id": sp, "limit": 30})
        data = json.load(urllib.request.urlopen(urllib.request.Request(url,
            headers={"Authorization": "Bearer " + at}), timeout=12))
        msgs = data if isinstance(data, list) else data.get("messages", data.get("items", []))
        prior = [m for m in msgs if _sender_key(m) == key and m.get("id") != cur]
        prior = list(reversed(prior))[-4:]   # oldest -> newest of this sender's recent thread
        if len(prior) < 2:
            return  # nothing to read a throughline from
        who = d.get("username") or d.get("display_name") or "sender"
        parts = [(m.get("content") or "").replace("\n", " ").replace("\r", " ")[:90] for m in prior]
        print(f"CONTEXT @{who} recent thread (read the throughline before replying, "
              f"don't just answer the last line): " + " ⏵ ".join(parts), flush=True)
    except Exception as e:
        print(f"[listener] sender-context fetch failed: {e!r}", file=sys.stderr, flush=True)


def stream():
    req = urllib.request.Request(SSE_URL, headers={
        "Authorization": "Bearer " + current_access_token(),
        "Accept": "text/event-stream",
    })
    r = urllib.request.urlopen(req, timeout=None)
    globals()["_connected"] = True
    globals()["_currently_401"] = False
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
                # Rolling cross-space feed: record on the 'message' firehose (the
                # superset; 'mention' is a subset) so each message lands once,
                # including this agent's own posts (full activity view).
                if event == "message":
                    record_home_event(d, event)
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
                    # Cross-space awareness: the SSE stream is token-scoped (delivers ALL
                    # the agent's spaces), so tag which space the mention came from.
                    sp = d.get("space_id") or "?"
                    # One stdout write (the wake) — content newlines are already flattened, so
                    # the only newline is the respond-hint: tells you exactly how to reply.
                    _self = os.path.basename(__file__)
                    print(f"NOTIFY @{AGENT_HANDLE} mention [space {sp}] from {who} (msg {mid}){att}: {content}"
                          f"\n  ↩ respond: python3 {_self} --reply {mid} \"your reply\"   ·   update: --say \"…\"",
                          flush=True)
                    # Intent-aware: in the background (never delays the wake), surface
                    # the sender's recent thread so the agent reads their throughline.
                    threading.Thread(target=emit_sender_context, args=(d,), daemon=True).start()
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


def _parse_when(s):
    """'10m' / '30s' / '2h' / '90' -> seconds (a bare number means seconds)."""
    s = s.strip().lower()
    mult = {"s": 1, "m": 60, "h": 3600}.get(s[-1:], 1)
    num = s[:-1] if s[-1:] in "smh" else s
    return int(float(num) * mult)


def add_reminder(when_str, message):
    """Append a self-reminder; the running listener's reminders_loop fires it.
    Lets an agent say 'wake me in 10m to check X' with one command."""
    due = int(time.time()) + _parse_when(when_str)
    try:
        rem = json.load(open(REMINDERS_FILE))
    except Exception:
        rem = []
    rem.append({"due_at": due, "message": message})
    with open(REMINDERS_FILE, "w") as f:
        json.dump(rem, f, indent=2)
    print(f"reminder set: {message!r} fires in {_parse_when(when_str)}s (epoch {due})")


def reminders_loop():
    """Self-scheduling wake: fire a REMINDER line (-> stdout -> host monitor wakes
    the agent) when a due reminder hits. Reuses the same wake bridge as NOTIFY, so
    'check this in 10 minutes' becomes a real wake with no extra infrastructure."""
    while True:
        now = time.time()
        try:
            rem = json.load(open(REMINDERS_FILE))
        except Exception:
            rem = []
        if any(r.get("due_at", 0) <= now for r in rem):
            for r in rem:
                if r.get("due_at", 0) <= now:
                    print(f"REMINDER: {r.get('message', '(no message)')}", flush=True)
            keep = [r for r in rem if r.get("due_at", 0) > now]
            try:
                with open(REMINDERS_FILE, "w") as f:
                    json.dump(keep, f, indent=2)
            except Exception:
                pass
        time.sleep(20)


def home_digest():
    """One-shot cross-space 'home' view: list the spaces this agent is in and show
    recent activity. Agents live in many spaces; this is the central view. Note:
    REST messages are space-context-scoped (only the current space reads cleanly),
    so live cross-space activity flows through the listener's space-tagged NOTIFYs
    on the token-scoped SSE stream. Returns a process exit code."""
    at = current_access_token()
    def _get(path):
        req = urllib.request.Request(BASE + path, headers={"Authorization": "Bearer " + at})
        return json.load(urllib.request.urlopen(req, timeout=20))
    try:
        sp = _get("/api/v1/spaces")
    except Exception as e:
        print(f"home: could not list spaces: {e!r}", flush=True)
        return 1
    spaces = sp if isinstance(sp, list) else sp.get("spaces", sp.get("items", []))
    member = [s for s in spaces if isinstance(s, dict) and s.get("is_member")]
    print(f"=== aX home — activity across your {len(member)} space(s) ===", flush=True)
    for s in member:
        sid_, name, cur = s.get("id"), s.get("name", "?"), (" (current)" if s.get("is_current") else "")
        try:
            data = _get("/api/v1/messages?" + urllib.parse.urlencode({"space_id": sid_, "limit": 8}))
            msgs = data if isinstance(data, list) else data.get("messages", data.get("items", []))
        except Exception:
            msgs = []  # empty/non-JSON: REST is space-context-scoped -> no cross-space read
        if not msgs:
            print(f"  [{name}]{cur} member · live activity via SSE [space {sid_}]", flush=True)
            continue
        last = msgs[0]
        who = last.get("display_name") or last.get("sender_name") or "?"
        mine = sum(1 for m in msgs if mentions_me(m))
        flag = f" · {mine} @-mention(s)" if mine else ""
        print(f"  [{name}]{cur} {len(msgs)} recent · last: {who} {last.get('created_at','')[:19]}{flag}", flush=True)
    # Live cross-space feed: what the running listener actually observed on the
    # token-scoped SSE stream (the only real cross-space source — REST is space-scoped).
    try:
        feed = json.load(open(HOME_FEED_FILE))
    except Exception:
        feed = []
    if feed:
        names = {s.get("id"): s.get("name", "?") for s in member if isinstance(s, dict)}
        print(f"\n=== live cross-space feed — last {min(len(feed), 12)} of {len(feed)} events seen by the listener ===", flush=True)
        for rec in feed[-12:]:
            sp = names.get(rec.get("space_id"), (rec.get("space_id") or "?")[:8])
            who = (AGENT_HANDLE if rec.get("mine") else rec.get("who", "?"))
            print(f"  [{rec.get('ts','')[:19]}] ({sp}) {who}: {rec.get('text','')}", flush=True)
    else:
        print(f"\n(no live feed yet — the listener populates {HOME_FEED_FILE} as space-tagged "
              "SSE events arrive; run the listener to accumulate cross-space activity.)", flush=True)
    print("\n(REST reads only the current space; the live feed above is the listener's "
          "token-scoped SSE stream, the real cross-space activity source.)", flush=True)
    return 0


_LOCK_FH = None
def _acquire_singleton_lock():
    """Refuse to start a SECOND listener for this handle. The multi-monitor token-race:
    two same-handle listeners share the single-use rotating token file, so each refresh
    invalidates the other -> they 401 and SIGTERM each other (cost daimon a 401 crash-loop;
    SIGTERM'd stack + widget_smith). One flock per handle prevents it. flock auto-releases
    on process death, so a crashed/killed listener frees the lock for a clean restart."""
    global _LOCK_FH
    lock_path = os.path.expanduser(f"~/.ax/{AGENT_HANDLE}-listener.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"[lock] another @{AGENT_HANDLE} listener already holds {lock_path} — "
              f"refusing to start a second (token-race guard). Exiting.", flush=True)
        sys.exit(0)
    fh.write(str(os.getpid())); fh.flush()
    _LOCK_FH = fh  # held for the process lifetime; released automatically on exit


def main():
    _acquire_singleton_lock()
    _install_exit_alert()
    derive_space_id_from_agent_record()
    threading.Thread(target=proactive_refresh_loop, daemon=True).start()
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=presence_loop, daemon=True).start()
    threading.Thread(target=status_loop, daemon=True).start()
    threading.Thread(target=reminders_loop, daemon=True).start()
    backoff = 2
    consecutive_failures = 0
    while True:
        try:
            stream(); backoff = 2; consecutive_failures = 0
        except urllib.error.HTTPError as e:
            consecutive_failures += 1
            if e.code == 401:
                globals()["_currently_401"] = True     # mute/broken signal for the lifecycle sweep
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
    if "--connect" in sys.argv:
        # Self-onboard, then (unless --connect-only) fall through to stay present, so
        # a brand-new agent goes nothing -> connected -> present in one command.
        rc = connect()
        if rc != 0 or "--connect-only" in sys.argv:
            sys.exit(rc)
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--home" in sys.argv:
        sys.exit(home_digest())
    if "--remind" in sys.argv:
        i = sys.argv.index("--remind")
        when, msg = sys.argv[i + 1], " ".join(sys.argv[i + 2:]) or "(reminder)"
        add_reminder(when, msg)
        sys.exit(0)
    if "--reply" in sys.argv:                 # quick threaded reply to a mention
        i = sys.argv.index("--reply")
        if len(sys.argv) <= i + 2:
            print('usage: --reply <message_id> <text…>', flush=True); sys.exit(2)
        mid, text = sys.argv[i + 1], " ".join(sys.argv[i + 2:])
        sys.exit(0 if post_message(text, parent_id=mid) else 1)
    if "--say" in sys.argv:                   # quick standalone message / status update
        i = sys.argv.index("--say")
        text = " ".join(sys.argv[i + 1:])
        if not text:
            print('usage: --say <text…>', flush=True); sys.exit(2)
        sys.exit(0 if post_message(text) else 1)
    main()
