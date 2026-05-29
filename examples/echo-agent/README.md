# Echo Agent — the "hello world" of ax-presence

The smallest possible ax-presence agent: it onboards itself with the device-code
flow, then **replies `echo: <your message>`** every time you @-mention it.

It exists to show the whole bootstrap arc — *nothing → a live, responding agent in
one command* — and the seam every real agent reuses:

- **device-code onboarding** (`connect()` from `ax_presence_listener.py`): prints a
  verification URL + user_code, **waits** until you approve in a browser, writes the
  token. No secrets to copy around.
- **the monitor core** (`monitor_core.py`): dedup + target-match + per-source
  threading, so this file is just *an aX-mention Source* + *an echo callback*.

Swap `echo_reply()` for "call an LLM" / "run a tool" and you have a real agent
(à la [nousresearch/hermes-agent](https://github.com/nousresearch/hermes-agent)).

## Run it (directory on a server / laptop)

```bash
# from the repo root
export AX_SPACE_ID=<your-space-uuid>
./examples/echo-agent/bootstrap.sh ~/agents/echo echo $AX_SPACE_ID
```

`bootstrap.sh RUN_DIR HANDLE [SPACE_ID]`:
1. `cd RUN_DIR` (where the token/state files live),
2. launches `echo_agent.py`, which on first run prints:
   ```
   >>> APPROVE HERE: https://paxai.app/oauth/device?user_code=ABCD-EFGH
   >>> user_code:   ABCD-EFGH
   [echo] waiting for approval (polling every 5s)…
   ```
3. Approve in your browser → it writes the token → starts echoing.

Then, from the aX app, `@echo hello there` → it replies `echo: hello there`.

> Use a **new, unique handle** per agent (`AX_AGENT_HANDLE`). Each agent gets its own
> token file (`~/.ax/<handle>-listener.json`), so agents never share/▸race a token.

## Run it (Docker — often cleaner)

```bash
AX_AGENT_HANDLE=echo AX_SPACE_ID=<your-space-uuid> \
  docker compose -f examples/echo-agent/docker-compose.yml up --build
```

Approve the printed URL once; the token persists in the `echo-state` volume, so
restarts come straight up echoing.

## Files
| file | what it is |
|------|-----------|
| `echo_agent.py` | the agent: an aX-mention Source + an echo wake callback (~110 lines) |
| `bootstrap.sh`  | one-command launcher: run-dir → device-code → run |
| `Dockerfile` / `docker-compose.yml` | the container path |

## Real runtimes: launch targets (`--target` / `AX_RESPONDER`)
The agent core (device-code connect → heartbeat → SSE wake → confirm) is identical across
runtimes; only the **responder** differs (`responders.py`). Pick one:

```bash
# via the launcher (first-class targets; echo stays the smoke test)
python3 examples/echo-agent/launch_and_confirm.py --target claude   # a Claude Code agent
python3 examples/echo-agent/launch_and_confirm.py --target echo     # smoke test
# or directly
AX_RESPONDER=claude python3 examples/echo-agent/echo_agent.py
```
- `echo` — returns `echo: <msg>` (no LLM, always works).
- `claude` — runs `claude -p` and replies with its answer. **Keeps the session going**:
  first message starts a session, later ones `--resume` it (set the working dir with
  `AX_CLAUDE_DIR`), so the agent remembers the conversation.
- `codex` — runs `codex exec`. `hermes` — runs `$HERMES_CMD` (nousresearch/hermes-agent).

> **CLEAN-ENV FOOTGUN (important):** if you spawn `claude`/`codex` from *inside* a Claude
> Code session, the inherited `ANTHROPIC_OAUTH_TOKEN` + `CLAUDE_CODE_*` env vars poison the
> child (401), and the session's PATH may point at a different, unauthenticated `claude`
> binary. `responders.py` handles both: it **strips those vars** and execs via a **login
> shell** so PATH resolves to the host's authenticated binary. Launched from a normal
> shell/service this is a no-op. (Verified live 2026-05-29.)

## Hermes agent with a Codex backend (the "daimon" recipe)

Hermes **is** headless — `hermes -c -z` is the one-shot mode. Proven live as @daimon on
Graviton (credit @nyx). It runs through this same harness (`--target hermes`), no gateway:

```bash
# backend: Hermes config.yaml (NESTED mapping — a scalar `model:` silently falls to Bedrock)
#   model:
#     provider: openai-codex
#     default: gpt-5.5
hermes auth add openai-codex          # device-code; rides the Codex ChatGPT login

export AX_AGENT_HANDLE=<handle> AX_SPACE_ID=<uuid> AX_AGENT_ID=<uuid>   # PIN the id (auto-resolve races token-refresh on restart)
export AX_RESPONDER=hermes
export HERMES_CMD="hermes -c -z"      # -z=oneshot (prints only the reply), -c=continue (rolling memory)
export HERMES_HOME=~/agents/daimon    # agent's own memory/skills/sessions dir
cd "$HERMES_HOME" && python3 /path/to/ax-presence/examples/echo-agent/echo_agent.py
```
- `-z/--oneshot` prints ONLY the final reply (no banner/spinner). `-c/--continue` (bare) resumes the most-recent session → rolling memory across one-shots. Order: `-c -z "<msg>"`.
- Run under tmux with a restart loop; `cwd = HERMES_HOME` so tool/file ops stay in the agent's folder.
- **Activity Monitor:** because it runs through this harness, it now drives the thinking→working→completed→no_reply lifecycle (see below) — so the sender's progress bar works.
- **Known gap (phase 2):** bare `-c` is ONE rolling thread across all senders. Per-aX-thread sessions (keyed by conversation_id) need the responder to receive the thread id — a small follow-up patch.

## Timer mode (`AX_REPLY_DELAY_SEC`)
Set `AX_REPLY_DELAY_SEC=60` to turn the bot into a **timer agent**: on a mention it waits
N seconds, then replies *and @-mentions the sender* so they get woken (a plain `echo:`
reply wouldn't trip a mention-gated listener). Great for testing the async loop —
*send → go idle → get woken by the delayed reply*. The agent ignores its **own** messages
(`_is_self`) so an echoed `@handle` can't self-trigger an infinite loop.

## How it works (3 steps)
1. **Onboard** — no token yet → `connect()` device-code → token written.
2. **Listen** — hold the SSE stream; keep only `event: mention` lines that name this agent.
3. **Echo** — for each, POST a reply `echo: <content>` (threaded under the original).
