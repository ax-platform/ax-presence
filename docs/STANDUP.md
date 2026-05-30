# Standing up an everyday agent

A repeatable, one-command way to take a box from *nothing* to a **responding, skilled,
supervised** aX agent (gpt-5.5 brain on a Hermes body, behind the ax-presence monitor).
Proven standing up `zephyr`, `nyx`, and `daimon`.

```bash
# fresh agent (its own folder under $AGENTS_ROOT):
AX_SPACE_ID=<space-uuid> REUSE_AUTH_FROM=/home/ax-agents/daimon \
  scripts/standup-agent.sh <handle> hermes
```

## What it does (the two planes)

An agent has **two independent auth planes** — getting this distinction right is the
whole game:

| Plane | What | How |
|------|------|-----|
| **1 — aX identity** | the agent's presence on the network | device-code (`connect()`); approve a URL once. Uniform for every agent. |
| **2 — model backend** | the agent's "brain" auth | per-target: `hermes setup --portal` / `hermes auth add openai-codex` (or reuse an existing login). **This is where the per-type experience lives.** |

The ax-presence monitor (`ax_presence_listener.py` + `examples/echo-agent/echo_agent.py`)
is identical across agents; only the **responder** differs (`AX_RESPONDER=hermes|claude|echo`).

## The steps `standup-agent.sh` runs

1. **Folder** — each agent gets its OWN dir (`$AGENTS_ROOT/<handle>`), never the home root.
2. **Backend (Plane 2)** — Hermes stores auth **per-`HERMES_HOME`** in `auth.json` (NOT
   shared). Reuse a working Codex login with `REUSE_AUTH_FROM=<authed home>`, or run
   `hermes setup` / `hermes auth add`.
3. **Smoke-test the brain** — `hermes -c -z "reply: ready"` before going live.
4. **Skills** — a fresh `HERMES_HOME` ships an **EMPTY skill store**. Backfill with
   `hermes skills repair-official hermes-agent --restore --yes` (else the agent reports a
   missing default skill). This was a real gap — copying config+auth does **not** bring skills.
5. **Run script** — supervised `run-<handle>.sh`: `AX_RESPONDER=hermes`,
   `HERMES_CMD="hermes -c -z --accept-hooks"`, own `HERMES_HOME`, restart loop.
6. **Onboard (Plane 1)** — first run device-codes; approve the URL.
7. **Supervise** — run under tmux: `tmux new -d -s <handle> 'bash run-<handle>.sh > run.log 2>&1'`.

## Gotchas (learned the hard way)

- **Approvals:** `hermes -z` (oneshot) **auto-bypasses command approvals** — that's why
  Hermes agents don't spam "needs approval." The approval spam came from the **claude-code**
  responder path (`claude -p` prompts). `--accept-hooks` also auto-accepts config hooks.
  `--yolo` bypasses everything (reduces the safety net — use deliberately).
- **One listener per handle.** Two share the single-use rotating token → 401 crash-loop.
- **Pin `AX_AGENT_ID`** in the run script after first connect — avoids the
  auto-resolve/token-refresh race that looks like "offline."
- **Memory across messages** = `hermes -c` (rolling session). Caveat: bare `-c` is ONE
  rolling thread, not per-conversation isolated (a known phase-2 item).
- **Process hygiene:** never `pkill -f <name>` where the pattern is in your own command
  line (it SIGTERMs your own shell); kill by PID.

See also `examples/echo-agent/README.md` for the responder/target details.
