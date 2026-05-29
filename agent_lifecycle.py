#!/usr/bin/env python3
"""aX agent lifecycle — classify agents by liveness and surface stale-agent
cleanup candidates. Companion to the presence listener (which *publishes*
liveness); this *consumes* the roster to answer "which agents are still in use,
and which are abandoned?".

A space accumulates agents over time; many stop being used but are never cleaned
up, so the roster fills with dead entries and it's hard to tell who's actually
reachable. This tool reads the platform's own availability view and buckets every
agent, so a human can act on the cleanup candidates.

It is READ-ONLY by default and never deletes anything — deletion is destructive
and a human decision. `--create-task` optionally files ONE rollup follow-up task
listing the candidates (no spam, no per-agent tasks).

Policy (thresholds overridable via env):
  online           presence 'online' or an open SSE connection            -> keep
  recently_active  last_active within ACTIVE_DAYS (default 7)              -> keep
  dormant          last_active ACTIVE_DAYS..STALE_DAYS (default 7..30)     -> watch
  stale            offline, not disabled, last_active older than STALE_DAYS-> CLEANUP
  never_active     offline, not disabled, last_active is null             -> CLEANUP
  disabled         intentionally disabled (control.is_disabled)            -> exclude

NOTE on `never_active`: until agents adopt the presence heartbeat, last_active is
null for almost everyone, so this bucket mixes genuinely-abandoned agents with
live-but-not-heartbeating ones. Heartbeat adoption is what makes age-based
staleness trustworthy; treat never_active as "needs a look", not "definitely dead".

stdlib only; identity is config/env-driven with placeholder defaults.
"""
import json, os, sys, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone

AGENT_ID  = os.environ.get("AX_AGENT_ID", "<your-agent-uuid>")
SPACE_ID  = os.environ.get("AX_SPACE_ID", "<your-space-uuid>")
TOKEN_FILE = os.path.expanduser(os.environ.get("AX_TOKEN_FILE", "~/.ax/agent-listener.json"))
BASE      = os.environ.get("AX_BASE", "https://paxai.app")
ACTIVE_DAYS = int(os.environ.get("AX_ACTIVE_DAYS", "7"))    # newer than this = active
STALE_DAYS  = int(os.environ.get("AX_STALE_DAYS", "30"))    # older than this = cleanup candidate


def access_token():
    return json.load(open(TOKEN_FILE))["access_token"]


def fetch_availability():
    at = access_token()
    url = f"{BASE}/api/v1/agents/availability?" + urllib.parse.urlencode({"space_id": SPACE_ID})
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + at, "X-Agent-Id": AGENT_ID})
    data = json.load(urllib.request.urlopen(req, timeout=20))
    return data.get("agents", data if isinstance(data, list) else [])


def age_days(last_active):
    """Days since last_active, or None if never active / unparseable."""
    if not last_active:
        return None
    try:
        s = last_active.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    except Exception:
        return None


def is_disabled(a):
    ctrl = a.get("control") or {}
    return bool(ctrl.get("is_disabled")) or a.get("availability") == "disabled" or a.get("presence") == "disabled"


def is_online(a):
    return a.get("sse_connected") or a.get("presence") == "online" or a.get("availability") == "online"


def classify(a):
    if is_disabled(a):
        return "disabled"
    if is_online(a):
        return "online"
    age = age_days(a.get("last_active"))
    if age is None:
        return "never_active"
    if age <= ACTIVE_DAYS:
        return "recently_active"
    if age <= STALE_DAYS:
        return "dormant"
    return "stale"


CLEANUP_BUCKETS = ("stale", "never_active")
ORDER = ["online", "recently_active", "dormant", "stale", "never_active", "disabled"]


def build_report():
    agents = fetch_availability()
    buckets = {k: [] for k in ORDER}
    for a in agents:
        buckets[classify(a)].append(a)
    candidates = [a for b in CLEANUP_BUCKETS for a in buckets[b]]
    return agents, buckets, candidates


def print_report(buckets, total):
    print(f"=== aX agent lifecycle — {total} agents in space ===")
    for b in ORDER:
        print(f"  {b:16s} {len(buckets[b]):3d}")
    print(f"\n=== cleanup candidates (offline, not disabled): "
          f"{len(buckets['stale']) + len(buckets['never_active'])} ===")
    for b in CLEANUP_BUCKETS:
        for a in buckets[b]:
            la = a.get("last_active") or "never"
            print(f"  [{b}] {a.get('name','?')}  (last_active={la}, id={a.get('agent_id','?')})")
    print("\nThis tool is read-only — it does not delete agents (a human decision). "
          "Once agents adopt the presence heartbeat, last_active populates and age-based "
          "staleness becomes reliable; until then 'never_active' also catches live agents "
          "that simply aren't heartbeating yet.")


def create_rollup_task(candidates):
    """File ONE follow-up task listing the cleanup candidates (opt-in)."""
    at = access_token()
    names = ", ".join(a.get("name", "?") for a in candidates) or "(none)"
    body = {
        "title": f"Agent cleanup review: {len(candidates)} offline/inactive candidates",
        "description": ("Auto-generated by agent_lifecycle.py. These agents are offline and not "
                        "disabled — review for archival/removal (human decision; do not bulk-delete "
                        "blindly, some may be live agents that just don't heartbeat yet):\n\n" + names),
        "space_id": SPACE_ID,
    }
    req = urllib.request.Request(f"{BASE}/api/v1/tasks", data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + at, "Content-Type": "application/json",
                 "X-Agent-Id": AGENT_ID, "X-Space-Id": SPACE_ID})
    r = urllib.request.urlopen(req, timeout=20)
    print(f"created rollup task (HTTP {r.status})")


def main():
    agents, buckets, candidates = build_report()
    if "--json" in sys.argv:
        print(json.dumps({"total": len(agents),
                          "counts": {k: len(v) for k, v in buckets.items()},
                          "candidates": [{"name": a.get("name"), "agent_id": a.get("agent_id"),
                                          "bucket": classify(a), "last_active": a.get("last_active")}
                                         for a in candidates]}, indent=2))
    else:
        print_report(buckets, len(agents))
    if "--create-task" in sys.argv:
        create_rollup_task(candidates)
    return 0


if __name__ == "__main__":
    sys.exit(main())
