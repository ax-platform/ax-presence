#!/usr/bin/env python3
"""launch_and_confirm — stand up an agent and PROVE it's connected.

This is the ax-presence "monitor launches an agent and confirms connectivity"
capability (and the Graviton standup command):

  1. LAUNCH the agent as a subprocess (default: examples/echo-agent/echo_agent.py).
     Its first run does the device-code bootstrap (auth.md) — prints a verification
     URL + user_code and waits for browser approval. No PAT files in the shipped path.
  2. WAIT for the agent to write its token (bootstrap complete).
  3. CONFIRM CONNECTED by polling the platform presence API until the agent shows
     presence=online + responsive (i.e. it is heartbeating), or a timeout.
  4. Print CONNECTED ✅ (with last_heartbeat) or FAILED with a diagnosis, then keep
     the agent running (Ctrl-C to stop).

Usage:
    export AX_AGENT_HANDLE=echo
    export AX_SPACE_ID=<your-space-uuid>
    python3 launch_and_confirm.py                 # launches echo_agent.py
    python3 launch_and_confirm.py -- python3 my_hermes_agent.py   # launch any agent cmd
Env: AX_CONNECT_TIMEOUT (default 240s), AX_TOKEN_FILE (defaults per handle).
"""
import os, sys, json, time, threading, subprocess, urllib.request, urllib.parse, urllib.error

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
sys.path.insert(0, _ROOT)

# First-class launch targets: select the runtime BEFORE importing ax (which reads
# AX_AGENT_HANDLE at import). Each target = a responder + a default handle. echo stays
# the smoke test; claude/codex/hermes are real runtimes. Override the handle with
# AX_AGENT_HANDLE=... to point a target at a specific agent identity.
_TARGET_HANDLE = {"echo": "echo", "claude": "cc", "codex": "cx", "hermes": "hermes"}
if "--target" in sys.argv:
    _t = sys.argv[sys.argv.index("--target") + 1].strip().lower()
    os.environ["AX_RESPONDER"] = _t
    os.environ.setdefault("AX_AGENT_HANDLE", _TARGET_HANDLE.get(_t, _t))

import ax_presence_listener as ax            # config: BASE, SPACE_ID, TOKEN_FILE, HANDLE

BASE = ax.BASE
PRESENCE_URL = f"{BASE}/api/v1/agents/presence"
WHOAMI_URL = f"{BASE}/api/v1/agents/me"
HANDLE = ax.AGENT_HANDLE
SPACE_ID = ax.SPACE_ID
TOKEN_FILE = ax.TOKEN_FILE
TIMEOUT = int(os.environ.get("AX_CONNECT_TIMEOUT", "240"))

DEFAULT_CMD = [sys.executable, os.path.join(_HERE, "echo_agent.py")]


def _tok():
    try:
        return json.load(open(TOKEN_FILE)).get("access_token")
    except Exception:
        return None


def _agent_id(token):
    env = os.environ.get("AX_AGENT_ID", "")
    if env and not env.startswith("<"):
        return env
    try:
        req = urllib.request.Request(WHOAMI_URL, headers={"Authorization": "Bearer " + token})
        d = json.load(urllib.request.urlopen(req, timeout=15))
        return d.get("id") or d.get("agent_id")
    except Exception:
        return None


def _presence(token, aid):
    """Return this agent's presence record, or None."""
    try:
        url = PRESENCE_URL + "?" + urllib.parse.urlencode({"space_id": SPACE_ID})
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
        d = json.load(urllib.request.urlopen(req, timeout=15))
        for a in (d.get("agents") or d.get("items") or []):
            if a.get("agent_id") == aid or a.get("name") == HANDLE:
                return a
    except Exception as e:
        print(f"[launch] presence query failed (will retry): {e!r}", flush=True)
    return None


def confirm(child):
    """Wait for bootstrap, then poll presence until online+responsive or timeout."""
    deadline = time.time() + TIMEOUT
    print(f"[launch] waiting for @{HANDLE} to bootstrap + heartbeat (timeout {TIMEOUT}s)…", flush=True)
    # 1) wait for token (device-code approval done)
    while time.time() < deadline:
        if child.poll() is not None:
            print(f"\n❌ FAILED: agent exited (code {child.returncode}) before connecting.", flush=True)
            return False
        if _tok():
            break
        time.sleep(2)
    token = _tok()
    if not token:
        print(f"\n❌ FAILED: no token after {TIMEOUT}s — device-code never approved?", flush=True)
        return False
    aid = _agent_id(token)
    if not aid:
        print("\n❌ FAILED: bootstrapped but couldn't resolve agent_id (set AX_AGENT_ID).", flush=True)
        return False
    # 2) poll presence until online + responsive
    while time.time() < deadline:
        if child.poll() is not None:
            print(f"\n❌ FAILED: agent exited (code {child.returncode}) before going online.", flush=True)
            return False
        rec = _presence(token, aid)
        if rec and rec.get("presence") == "online":
            print(f"\n✅ CONNECTED: @{HANDLE} is online"
                  f"{' + responsive' if rec.get('responsive') else ''} "
                  f"(agent_id={aid[:8]}…, last_heartbeat={rec.get('last_heartbeat')}).", flush=True)
            print("   It is heartbeating; mention it to see it echo. Ctrl-C to stop the agent.", flush=True)
            return True
        time.sleep(3)
    print(f"\n❌ FAILED: bootstrapped (agent_id={aid[:8]}…) but never showed online within {TIMEOUT}s "
          f"— is the heartbeat thread running / X-Agent-Id correct?", flush=True)
    return False


def main():
    cmd = DEFAULT_CMD
    if "--" in sys.argv:
        cmd = sys.argv[sys.argv.index("--") + 1:]
    print(f"[launch] launching agent: {' '.join(cmd)}", flush=True)
    print(f"[launch] (first run shows a device-code URL to approve in your browser)", flush=True)
    child = subprocess.Popen(cmd, env=os.environ.copy())   # inherit stdio: device-code prompt + logs stream
    t = threading.Thread(target=confirm, args=(child,), daemon=True)
    t.start()
    try:
        child.wait()
    except KeyboardInterrupt:
        print("\n[launch] stopping agent…", flush=True)
        child.terminate()
        try: child.wait(timeout=10)
        except Exception: child.kill()


if __name__ == "__main__":
    main()
