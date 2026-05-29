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
# and run it from a working dir. Configure with AX_CLAUDE_DIR.
_CLAUDE_DIR = os.path.expanduser(os.environ.get("AX_CLAUDE_DIR", "~/.ax/claude-agent-workdir"))
_session = {"id": None}
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
    """A Claude Code agent that KEEPS THE SESSION GOING across messages: the first
    message starts a session (fixed --session-id), each later message --resumes it, all
    run from AX_CLAUDE_DIR. So the agent remembers the conversation."""
    os.makedirs(_CLAUDE_DIR, exist_ok=True)
    with _session_lock:
        if _session["id"] is None:                      # first message → new session
            _session["id"] = str(uuid.uuid4())
            cmd = ["claude", "-p", "--session-id", _session["id"],
                   "--append-system-prompt", _SYS, content]
        else:                                           # later messages → resume same session
            cmd = ["claude", "-p", "--resume", _session["id"], content]
    return _run(cmd, timeout=180, cwd=_CLAUDE_DIR, login=True)


def codex(content, who):
    return _run(["codex", "exec", content], timeout=180, login=True)


def hermes(content, who):
    cmd = os.environ.get("HERMES_CMD")
    if not cmd:
        return "(hermes not configured — set HERMES_CMD to the hermes-agent run command)"
    return _run(cmd.split() + [content], timeout=300, login=True)


RESPONDERS = {"echo": echo, "claude": claude, "codex": codex, "hermes": hermes}


def get(name):
    """Resolve a responder by name (default echo)."""
    return RESPONDERS.get((name or "echo").strip().lower(), echo)
