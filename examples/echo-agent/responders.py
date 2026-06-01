#!/usr/bin/env python3
"""responders — pluggable "what does this agent reply with" for the ax-presence agent.

The agent core (device-code connect + heartbeat + SSE wake) is identical across
runtimes; only the responder differs. Select one with AX_RESPONDER=echo|claude|codex|hermes.

  echo    smoke test — returns "echo: <message>" (no LLM, always works)
  claude  a Claude Code agent — runs `claude -p` headless and returns its answer
  codex   an OpenAI Codex agent — runs `codex exec`
  hermes  a Hermes agent — runs $HERMES_CMD (nousresearch/hermes-agent)

CLEAN-ENV NOTE (discovered live 2026-05-29): when this agent is launched from inside
a Claude Code session, the inherited ANTHROPIC_OAUTH_TOKEN + CLAUDE_CODE_* env vars
POISON a nested `claude -p` (401). We strip them so `claude` uses the host's stored
login (~/.claude/.credentials.json). Launched from a normal shell/service those vars
aren't present, so this is a no-op there.
"""
import os, subprocess, threading, uuid

_STRIP = ("ANTHROPIC_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
          "CLAUDECODE", "CLAUDE_EFFORT", "AI_AGENT")
_SYS = ("You are an aX network agent replying to a teammate's message over chat. "
        "Reply concisely and directly — no preamble, no sign-off.")

# Persistent Claude session: keep ONE conversation going across messages (continuity),
# and run it from a working dir. Configure with AX_CLAUDE_DIR. Persist the session id
# to disk so a supervisor restart keeps the same Claude Code conversation.
_CLAUDE_DIR = os.path.expanduser(os.environ.get("AX_CLAUDE_DIR", "~/.ax/claude-agent-workdir"))
_SESSION_FILE = os.path.join(_CLAUDE_DIR, ".ax-claude-session-id")
_session = {"id": ""}
_session_lock = threading.Lock()


def _clean_env():
    return {k: v for k, v in os.environ.items()
            if k not in _STRIP and not k.startswith("CLAUDE_CODE_")}


def _run(args, timeout, cwd=None, login=False):
    """Run a CLI responder. login=True executes via a LOGIN shell (`bash -lc`) so PATH
    resolves to the user's authenticated binary — critical for `claude`: a Claude Code
    session's PATH points at ~/.local/bin/claude (401s on the session OAuth token),
    while the login shell uses the real ~/.npm-global/bin/claude. Args after the binary
    are passed via "$@" (injection-safe). Combined with _clean_env() (strips the
    poisoning ANTHROPIC_OAUTH_TOKEN + CLAUDE_CODE_*) this makes a nested agent work."""
    cmd = (["bash", "-lc", f'{args[0]} "$@"', "bash"] + list(args[1:])) if login else list(args)
    try:
        r = subprocess.run(cmd, env=_clean_env(), cwd=cwd, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "").strip()
        return out or f"({args[0]} returned nothing; stderr: {(r.stderr or '').strip()[:200]})"
    except subprocess.TimeoutExpired:
        return f"({args[0]} timed out after {timeout}s)"
    except FileNotFoundError:
        return f"({args[0]} not installed on this host)"
    except Exception as e:
        return f"({args[0]} responder error: {e!r})"


def echo(content, who):
    return f"echo: {content}" if content else "echo: 👋"


def claude(content, who):
    """A Claude Code agent that KEEPS THE SESSION GOING across messages.

    The first message starts a fixed --session-id, persisted under AX_CLAUDE_DIR;
    later messages --resume it even after the ax-presence supervisor restarts.
    Print mode (`-p`) is used for headless aX replies, and
    --dangerously-skip-permissions provides auto-approval for Claude Code tool use.
    """
    os.makedirs(_CLAUDE_DIR, exist_ok=True)
    with _session_lock:
        if not _session["id"]:
            try:
                with open(_SESSION_FILE, "r", encoding="utf-8") as f:
                    saved = f.read().strip()
                if saved:
                    _session["id"] = saved
            except FileNotFoundError:
                pass
        if not _session["id"]:                      # first message → new session
            _session["id"] = str(uuid.uuid4())
            with open(_SESSION_FILE, "w", encoding="utf-8") as f:
                f.write(_session["id"] + "\n")
            cmd = ["claude", "-p", "--dangerously-skip-permissions",
                   "--session-id", _session["id"],
                   "--append-system-prompt", _SYS, content]
        else:                                           # later messages → resume same session
            cmd = ["claude", "-p", "--dangerously-skip-permissions",
                   "--resume", _session["id"], content]
    return _run(cmd, timeout=180, cwd=_CLAUDE_DIR, login=True)


def codex(content, who):
    return _run(["codex", "exec", content], timeout=180, login=True)


def hermes(content, who):
    # nousresearch/hermes-agent IS headless (proven live as @daimon on Graviton, recipe
    # from @nyx 2026-05-29): `hermes -c -z "<msg>"` —
    #   -z/--oneshot  prints ONLY the final reply to stdout (no banner/spinner/session line)
    #   -c/--continue (bare) resumes the most-recent session → rolling memory across one-shots
    # Order matters: `-c -z "<msg>"` so -z takes the prompt and -c doesn't eat it.
    # Default HERMES_CMD is that invocation; override to customize. Backend = Codex:
    # config.yaml `model.provider: openai-codex` + nested `model.default: gpt-5.5` (a SCALAR
    # `model:` silently falls back to Bedrock) + `hermes auth add openai-codex`. Run with
    # HERMES_HOME=<agent dir> and cwd=that dir. See examples/echo-agent/README.md.
    cmd = os.environ.get("HERMES_CMD", "hermes -c -z")
    return _run(cmd.split() + [content], timeout=300, login=True)


RESPONDERS = {"echo": echo, "claude": claude, "codex": codex, "hermes": hermes}


def get(name):
    """Resolve a responder by name (default echo)."""
    return RESPONDERS.get((name or "echo").strip().lower(), echo)
