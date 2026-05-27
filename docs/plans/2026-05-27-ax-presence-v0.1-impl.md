# ax-presence v0.1 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship a working `ax-presence` v0.1 — a Python CLI for sponsored agent presence on aX, with OAuth bootstrap, SSE mention listener, idle heartbeat, on-disk liveness, never-halt circuit breakers, and an external watchdog.

**Architecture:** Pure-stdlib Python package (`urllib`, `threading`, `argparse`). Code is lifted from ax-backend PR #348's `scripts/headless_agent_sse_listener.py` and re-shaped into modules. Peach's never-halt + branched-error circuit-breaker model and the separate liveness file are added on top. Tests-as-spec for the invariants in `docs/plans/2026-05-27-ax-presence-design.md`.

**Tech Stack:** Python 3.11+, stdlib only at runtime, pytest for tests, GitHub Actions for CI.

**Reference docs (read first):**
- `docs/plans/2026-05-27-ax-presence-design.md` — the design this plan implements
- `/Users/jacob/claude_home/ax-backend-extract/scripts/headless_agent_sse_listener.py` — the canonical seed
- `/Users/jacob/claude_home/ax-backend-extract/docs/headless-agent-sse-listener.md` — the runbook
- `/Users/jacob/claude_home/ax-backend-extract/auth.md` — the OAuth contract

---

## Pre-flight

Before starting, verify the working directory is `~/claude_home/ax-presence` and the repo is clean:

```bash
cd ~/claude_home/ax-presence
git status              # expect clean
git log --oneline -3    # expect 3 commits ending at 3824cef
python3 --version       # expect 3.11+
```

Create a venv for development:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install pytest
```

---

## Task 1: Package skeleton + CI

**Files:**
- Create: `pyproject.toml`
- Create: `src/ax_presence/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/test_smoke.py`
- Create: `.github/workflows/ci.yml`

**Step 1: Write `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68", "setuptools-scm>=8"]
build-backend = "setuptools.build_meta"

[project]
name = "ax-presence"
description = "Durable headless presence for sponsored aX agents"
readme = "README.md"
license = { text = "MIT" }
requires-python = ">=3.11"
dynamic = ["version"]
authors = [{ name = "aX Platform" }]
classifiers = [
  "Development Status :: 3 - Alpha",
  "License :: OSI Approved :: MIT License",
  "Programming Language :: Python :: 3.11",
  "Programming Language :: Python :: 3.12",
]
dependencies = []

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[project.scripts]
ax-presence = "ax_presence.cli:main"

[project.urls]
Homepage = "https://github.com/ax-platform/ax-presence"
Issues = "https://github.com/ax-platform/ax-presence/issues"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools_scm]
fallback_version = "0.0.0+local"

[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py"]
```

**Step 2: Write `src/ax_presence/__init__.py`**

```python
"""ax-presence — durable headless presence for sponsored aX agents."""

__version__ = "0.1.0.dev0"
```

**Step 3: Write `tests/__init__.py`** (empty file)

```python
```

**Step 4: Write `tests/test_smoke.py`**

```python
def test_package_imports():
    import ax_presence
    assert ax_presence.__version__
```

**Step 5: Install editable + run smoke test**

```bash
pip install -e ".[dev]"
pytest tests/test_smoke.py -v
```

Expected: 1 passed.

**Step 6: Write `.github/workflows/ci.yml`**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python ${{ matrix.python-version }}
        uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - name: Install
        run: |
          python -m pip install --upgrade pip
          pip install -e ".[dev]"
      - name: Test
        run: pytest -v
```

**Step 7: Commit**

```bash
git add pyproject.toml src/ tests/ .github/
git commit -m "Add package skeleton, pytest smoke test, GitHub Actions CI"
git push
```

Verify CI passes: `gh run watch` (or check https://github.com/ax-platform/ax-presence/actions).

---

## Task 2: Port TokenStore (TDD)

**Files:**
- Create: `src/ax_presence/tokens.py`
- Create: `tests/test_tokens.py`

The TokenStore lifts directly from the canonical seed at
`/Users/jacob/claude_home/ax-backend-extract/scripts/headless_agent_sse_listener.py`
lines 44–168. Attribute in module docstring.

**Step 1: Write failing tests for TokenStore loaders**

`tests/test_tokens.py`:

```python
import json
import time
from pathlib import Path

import pytest

from ax_presence.tokens import TokenStore


def write_token(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload))


def test_flat_token_file_loads(tmp_path: Path) -> None:
    p = tmp_path / "t.json"
    write_token(p, {
        "access_token": "A",
        "refresh_token": "R",
        "expires_at": int(time.time()) + 3600,
        "client_id": "C",
    })
    store = TokenStore(p, token_url="https://example/oauth/token", refresh_skew_seconds=60)
    assert store.access_token(refresh_window_seconds=60) == "A"


def test_nested_token_file_loads(tmp_path: Path) -> None:
    p = tmp_path / "t.json"
    write_token(p, {
        "tokens": {
            "access_token": "A",
            "refresh_token": "R",
            "expires_at": int(time.time()) + 3600,
        },
        "clientInfo": {"client_id": "C"},
    })
    store = TokenStore(p, token_url="https://example/oauth/token", refresh_skew_seconds=60)
    assert store.access_token(refresh_window_seconds=60) == "A"


def test_mcporter_vault_rejected(tmp_path: Path) -> None:
    p = tmp_path / "t.json"
    write_token(p, {"entries": {"foo": {"access_token": "A"}}})
    store = TokenStore(p, token_url="https://example/oauth/token", refresh_skew_seconds=60)
    with pytest.raises(RuntimeError, match="MCPorter"):
        store.access_token(refresh_window_seconds=60)


def test_missing_access_token_raises(tmp_path: Path) -> None:
    p = tmp_path / "t.json"
    write_token(p, {"refresh_token": "R", "client_id": "C", "expires_at": int(time.time()) + 3600})
    store = TokenStore(p, token_url="https://example/oauth/token", refresh_skew_seconds=60)
    with pytest.raises(RuntimeError, match="access_token"):
        store.access_token(refresh_window_seconds=60)
```

**Step 2: Run, verify FAIL with "No module named 'ax_presence.tokens'"**

```bash
pytest tests/test_tokens.py -v
```

**Step 3: Write `src/ax_presence/tokens.py`**

Copy the `TokenStore` class verbatim from
`/Users/jacob/claude_home/ax-backend-extract/scripts/headless_agent_sse_listener.py`
(lines 44–168, the entire `class TokenStore` definition through `proactive_refresh_loop`).

Add this module docstring at the top:

```python
"""TokenStore — manages OAuth tokens for the listener.

Lifted from ax-backend PR #348 (scripts/headless_agent_sse_listener.py)
with no semantic changes. Two-shape loader (flat + nested) and refusal
to read MCPorter shared vaults are preserved as invariants.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
```

Then paste the `TokenStore` class body unchanged.

**Step 4: Run tests**

```bash
pytest tests/test_tokens.py -v
```

Expected: 4 passed.

**Step 5: Commit**

```bash
git add src/ax_presence/tokens.py tests/test_tokens.py
git commit -m "Port TokenStore from ax-backend PR #348

Two-shape loader (flat / nested with clientInfo) + explicit refusal of
MCPorter shared vaults. Four tests cover the loader contract."
git push
```

---

## Task 3: Port SSE helpers (TDD)

**Files:**
- Create: `src/ax_presence/sse.py`
- Create: `tests/test_sse.py`

**Step 1: Write failing tests**

`tests/test_sse.py`:

```python
import io

from ax_presence.sse import iter_sse_events, mention_candidates, mention_matches, should_notify


def make_stream(lines: list[str]) -> io.BytesIO:
    return io.BytesIO("\n".join(lines).encode("utf-8") + b"\n")


def test_iter_sse_parses_event_and_data() -> None:
    stream = make_stream([
        "event: mention",
        "data: {\"id\": \"m1\"}",
        "",
    ])
    events = list(iter_sse_events(stream))
    assert events == [("mention", '{"id": "m1"}')]


def test_iter_sse_default_event_is_message() -> None:
    stream = make_stream([
        "data: hello",
        "",
    ])
    events = list(iter_sse_events(stream))
    assert events == [("message", "hello")]


def test_mention_matches_string_forms() -> None:
    assert mention_matches("claude_prime", "claude_prime", None) is True
    assert mention_matches("@claude_prime", "claude_prime", None) is True
    assert mention_matches("uuid-123", "claude_prime", "uuid-123") is True
    assert mention_matches("other", "claude_prime", None) is False


def test_mention_matches_dict_forms() -> None:
    assert mention_matches({"agent_name": "claude_prime"}, "claude_prime", None) is True
    assert mention_matches({"handle": "claude_prime"}, "claude_prime", None) is True
    assert mention_matches({"agent_id": "uuid-123"}, "claude_prime", "uuid-123") is True
    assert mention_matches({"agent_name": "other"}, "claude_prime", "uuid-123") is False


def test_mention_candidates_collects_all_sources() -> None:
    payload = {
        "mentioned_agent": "claude_prime",
        "mentioned_agent_id": "u1",
        "mentions": ["other"],
        "metadata": {
            "mentions": [{"agent_name": "claude_prime"}],
            "original_mentions": ["@claude_prime"],
            "mentioned_agents": [{"agent_id": "u1"}],
        },
    }
    items = mention_candidates(payload)
    assert "claude_prime" in items
    assert "@claude_prime" in items
    assert {"agent_name": "claude_prime"} in items


def test_should_notify_mention_event_only() -> None:
    payload = {"mentioned_agent": "claude_prime"}
    assert should_notify("mention", payload, "claude_prime", None) is True
    assert should_notify("message", payload, "claude_prime", None) is False
```

**Step 2: Run, verify FAIL**

```bash
pytest tests/test_sse.py -v
```

**Step 3: Write `src/ax_presence/sse.py`**

Copy lines 171–223 from the canonical seed (the `iter_sse_events`,
`mention_matches`, `mention_candidates`, `should_notify`, `sender_label`
functions). Add this module header:

```python
"""SSE parsing and mention-target gating.

Lifted from ax-backend PR #348 (scripts/headless_agent_sse_listener.py).
The wake contract — mention-event-only + target match + dedup — is
enforced by callers using these helpers.
"""

from __future__ import annotations

from typing import Any
```

**Step 4: Run tests**

```bash
pytest tests/test_sse.py -v
```

Expected: 6 passed.

**Step 5: Commit**

```bash
git add src/ax_presence/sse.py tests/test_sse.py
git commit -m "Port SSE helpers from ax-backend PR #348

iter_sse_events parser plus mention_matches/mention_candidates/
should_notify/sender_label. Six tests cover the parser and the
mention-target wake contract."
git push
```

---

## Task 4: Codify the design invariants as failing tests

**Files:**
- Create: `tests/test_invariants.py`

This task establishes the "tests-as-spec" file. All ten tests should
PASS where the behavior is already provided by Tasks 2–3 and FAIL where
behavior comes later. Failures here are the to-do list for Tasks 5+.

**Step 1: Write `tests/test_invariants.py`**

```python
"""Tests-as-spec for the v0.1 design invariants.

Each test's name is the invariant. If anyone refactors and breaks one,
the test name tells the story. See:
docs/plans/2026-05-27-ax-presence-design.md#tests-as-spec-invariants
"""

import json
import time
from pathlib import Path

import pytest

from ax_presence.sse import should_notify
from ax_presence.tokens import TokenStore


def test_wake_only_on_mention_events() -> None:
    """Invariant 1: SSE 'message' events with mention-shaped payloads must not wake."""
    payload = {"mentioned_agent": "claude_prime", "id": "m1"}
    assert should_notify("message", payload, "claude_prime", None) is False
    assert should_notify("mention", payload, "claude_prime", None) is True


def test_target_match_required() -> None:
    """Invariant 2: a mention for another agent must not wake us."""
    payload = {"mentioned_agent": "someone_else"}
    assert should_notify("mention", payload, "claude_prime", None) is False


def test_msg_id_dedup_is_callers_responsibility() -> None:
    """Invariant 3: dedup is enforced in the listener loop (Task 6)."""
    # Will be re-implemented as a listen-loop integration test once Task 6 lands.
    pytest.skip("Implemented in tests/test_listen.py once Task 6 lands")


def test_rejects_mcporter_vault(tmp_path: Path) -> None:
    """Invariant 4: refuse MCPorter shared vault shape."""
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"entries": {"foo": {"access_token": "A"}}}))
    store = TokenStore(p, token_url="https://example/oauth/token", refresh_skew_seconds=60)
    with pytest.raises(RuntimeError, match="MCPorter"):
        store.access_token(refresh_window_seconds=60)


def test_proactive_refresh_runs_before_expiry(tmp_path: Path) -> None:
    """Invariant 5: refresh decision is based on expires_at, not on 401."""
    p = tmp_path / "t.json"
    p.write_text(json.dumps({
        "access_token": "A",
        "refresh_token": "R",
        "expires_at": int(time.time()) + 30,
        "client_id": "C",
    }))
    store = TokenStore(p, token_url="https://example/oauth/token", refresh_skew_seconds=60)
    # seconds_until_refresh should be negative because expiry is within the skew window
    token = store.load()
    assert store.seconds_until_refresh(token) < 0


def test_uses_oauth_token_endpoint() -> None:
    """Invariant 6: the configured refresh URL must be the aX-native /oauth/token."""
    from ax_presence.tokens import TokenStore
    # The default constant lives in listen.py once Task 6 lands; cross-check here.
    pytest.skip("Implemented in tests/test_listen.py once Task 6 lands")


def test_circuit_open_never_halts() -> None:
    """Invariant 7: opened circuits slow-retry forever, they do not halt."""
    pytest.skip("Implemented in tests/test_circuit.py once Task 9 lands")


def test_refresh_terminal_errors_open_immediately() -> None:
    """Invariant 8: invalid_grant/invalid_client are terminal — open on first failure."""
    pytest.skip("Implemented in tests/test_circuit.py once Task 10 lands")


def test_circuit_open_fires_out_of_band_alert() -> None:
    """Invariant 9: opens must invoke the alert-cmd, not just log."""
    pytest.skip("Implemented in tests/test_circuit.py once Task 11 lands")


def test_liveness_file_is_touched_by_main_loop() -> None:
    """Invariant 10: the liveness file mtime advances from the main loop only."""
    pytest.skip("Implemented in tests/test_liveness.py once Task 8 lands")
```

**Step 2: Run**

```bash
pytest tests/test_invariants.py -v
```

Expected: 5 passed, 5 skipped.

**Step 3: Commit**

```bash
git add tests/test_invariants.py
git commit -m "Add tests-as-spec invariants file (5 active, 5 skipped pending later tasks)

Skipped tests act as a written punch list — each skip references the
task that will activate it."
git push
```

---

## Task 5: `auth refresh` subcommand (the simpler one first)

**Files:**
- Create: `src/ax_presence/auth.py`
- Create: `src/ax_presence/cli.py`
- Create: `tests/test_auth.py`

**Step 1: Write failing test**

`tests/test_auth.py`:

```python
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from ax_presence.auth import refresh


def test_refresh_calls_oauth_token_endpoint(tmp_path: Path) -> None:
    token_path = tmp_path / "t.json"
    token_path.write_text(json.dumps({
        "access_token": "old",
        "refresh_token": "R",
        "expires_at": int(time.time()) - 1,
        "client_id": "C",
    }))

    captured: dict = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["data"] = request.data.decode()
        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *a): return None
            def read(self): return json.dumps({
                "access_token": "new",
                "refresh_token": "R2",
                "expires_in": 900,
                "scope": "openid",
                "token_type": "Bearer",
            }).encode()
        # json.load reads from .read() on Python's stdlib path — but our
        # TokenStore uses json.load(response) which iterates. Make it a
        # file-like wrapper instead:
        import io
        return io.BytesIO(json.dumps({
            "access_token": "new",
            "refresh_token": "R2",
            "expires_in": 900,
            "scope": "openid",
            "token_type": "Bearer",
        }).encode())

    # Patch where TokenStore actually calls urlopen
    with patch("ax_presence.tokens.urllib.request.urlopen", side_effect=fake_urlopen):
        refresh(token_path=token_path, token_url="https://paxai.app/oauth/token")

    assert captured["url"] == "https://paxai.app/oauth/token"
    assert "grant_type=refresh_token" in captured["data"]
    assert "client_id=C" in captured["data"]

    updated = json.loads(token_path.read_text())
    assert updated["access_token"] == "new"
    assert updated["refresh_token"] == "R2"
```

**Step 2: Run, verify FAIL with import error**

```bash
pytest tests/test_auth.py -v
```

**Step 3: Write `src/ax_presence/auth.py` — minimal `refresh()` wrapper**

```python
"""OAuth bootstrap and refresh for ax-presence.

bootstrap() runs the device-code flow against paxai.app's aX-native
/oauth/register and /oauth/device/code endpoints, then writes a token
file. refresh() invokes the existing TokenStore.refresh() once.
"""

from __future__ import annotations

from pathlib import Path

from .tokens import TokenStore


DEFAULT_TOKEN_URL = "https://paxai.app/oauth/token"


def refresh(token_path: Path, token_url: str = DEFAULT_TOKEN_URL) -> None:
    """One-shot refresh of an existing token file."""
    store = TokenStore(token_path, token_url=token_url, refresh_skew_seconds=60)
    store.refresh()
```

**Step 4: Run tests**

```bash
pytest tests/test_auth.py -v
```

Expected: 1 passed.

**Step 5: Write minimal `src/ax_presence/cli.py`**

```python
"""ax-presence CLI router."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .auth import DEFAULT_TOKEN_URL, refresh as auth_refresh


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ax-presence", description=__doc__)
    parser.add_argument("--version", action="version", version=f"ax-presence {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", help="OAuth bootstrap and refresh")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)

    refresh_p = auth_sub.add_parser("refresh", help="Refresh an existing token file")
    refresh_p.add_argument("--token-file", type=Path, required=True)
    refresh_p.add_argument("--token-url", default=DEFAULT_TOKEN_URL)

    args = parser.parse_args(argv)

    if args.command == "auth" and args.auth_command == "refresh":
        auth_refresh(token_path=args.token_file, token_url=args.token_url)
        return 0

    print(f"Unhandled command: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
```

**Step 6: Smoke the CLI**

```bash
pip install -e ".[dev]"      # rebuild entry point
ax-presence --version
ax-presence auth refresh --help
```

Expected: version printed, help text shown.

**Step 7: Commit**

```bash
git add src/ax_presence/auth.py src/ax_presence/cli.py tests/test_auth.py
git commit -m "Add 'ax-presence auth refresh' subcommand

Wraps TokenStore.refresh() in a CLI entry point. One test confirms
the request hits /oauth/token (aX-native, not Cognito /token)."
git push
```

---

## Task 6: `auth bootstrap` subcommand (DCR + device-code)

**Files:**
- Modify: `src/ax_presence/auth.py`
- Modify: `src/ax_presence/cli.py`
- Modify: `tests/test_auth.py`

**Step 1: Write failing test for the bootstrap flow against fixtures**

Append to `tests/test_auth.py`:

```python
def test_bootstrap_writes_token_file(tmp_path: Path, monkeypatch) -> None:
    """Bootstrap runs DCR → device-code → token-exchange and writes a file."""
    from unittest.mock import patch
    import io

    responses = [
        # POST /oauth/register response
        io.BytesIO(json.dumps({"client_id": "CID"}).encode()),
        # POST /oauth/device/code response
        io.BytesIO(json.dumps({
            "device_code": "DC",
            "user_code": "UCODE",
            "verification_uri_complete": "https://paxai.app/device?code=UCODE",
            "interval": 0,
            "expires_in": 600,
        }).encode()),
        # POST /oauth/token response (success on first poll)
        io.BytesIO(json.dumps({
            "access_token": "A",
            "refresh_token": "R",
            "expires_in": 900,
            "scope": "openid offline_access ax-api/mcp:read ax-api/mcp:write",
            "token_type": "Bearer",
        }).encode()),
    ]

    def fake_urlopen(request, timeout):
        return responses.pop(0)

    token_path = tmp_path / "tokens.json"

    from ax_presence.auth import bootstrap
    with patch("ax_presence.auth.urllib.request.urlopen", side_effect=fake_urlopen):
        bootstrap(
            handle="claude_prime",
            token_path=token_path,
            base_url="https://paxai.app",
            poll_interval_floor=0,
        )

    data = json.loads(token_path.read_text())
    assert data["access_token"] == "A"
    assert data["refresh_token"] == "R"
    assert data["client_id"] == "CID"
    assert data["expires_at"] > time.time()
```

**Step 2: Run, verify FAIL**

**Step 3: Add `bootstrap()` to `src/ax_presence/auth.py`**

```python
import json
import os
import sys
import time
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "https://paxai.app"
DEFAULT_SCOPE = "openid offline_access ax-api/mcp:read ax-api/mcp:write"


def _post_form(url: str, fields: dict, timeout: int = 30) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _post_json(url: str, body: dict, timeout: int = 30) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def bootstrap(
    handle: str,
    token_path: Path,
    base_url: str = DEFAULT_BASE_URL,
    scope: str = DEFAULT_SCOPE,
    poll_interval_floor: int = 1,
) -> None:
    """Run DCR + device-code OAuth and write a token file (mode 0600).

    Uses the aX-native endpoints (/oauth/register, /oauth/device/code,
    /oauth/token). The advertised /token endpoint is Cognito and rejects
    aX DCR clients with invalid_client.
    """
    base_url = base_url.rstrip("/")
    resource = f"{base_url}/mcp/agents/{handle}"

    # 1. Dynamic client registration
    dcr = _post_json(f"{base_url}/oauth/register", {
        "client_name": f"ax-presence/{handle}",
        "redirect_uris": [],
        "grant_types": [
            "urn:ietf:params:oauth:grant-type:device_code",
            "refresh_token",
        ],
        "response_types": [],
        "token_endpoint_auth_method": "none",
        "scope": scope,
    })
    client_id = dcr["client_id"]

    # 2. Device authorization request
    dev = _post_form(f"{base_url}/oauth/device/code", {
        "client_id": client_id,
        "resource": resource,
        "scope": scope,
    })

    print("", file=sys.stderr)
    print(f"  Open: {dev['verification_uri_complete']}", file=sys.stderr)
    print(f"  Code: {dev['user_code']}", file=sys.stderr)
    print("  Waiting for sponsor approval…", file=sys.stderr)
    print("", file=sys.stderr)

    interval = max(poll_interval_floor, int(dev.get("interval") or 5))
    deadline = time.time() + int(dev.get("expires_in") or 600)
    device_code = dev["device_code"]

    # 3. Poll the token endpoint
    while True:
        if time.time() > deadline:
            raise RuntimeError("device-code expired before approval")
        try:
            tok = _post_form(f"{base_url}/oauth/token", {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
                "client_id": client_id,
            })
            break
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if '"authorization_pending"' in body or '"slow_down"' in body:
                time.sleep(interval)
                continue
            raise RuntimeError(f"token poll failed: {e.code} {body}") from e
        time.sleep(interval)

    now = int(time.time())
    payload = {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token"),
        "token_type": tok.get("token_type", "Bearer"),
        "scope": tok.get("scope", scope),
        "expires_in": int(tok.get("expires_in") or 0),
        "expires_at": now + int(tok.get("expires_in") or 0),
        "obtained_at": now,
        "client_id": client_id,
    }
    token_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle_file:
        json.dump(payload, handle_file, indent=2)
        handle_file.write("\n")
```

Also at the top of `auth.py`, add the missing import:

```python
import urllib.error
```

**Step 4: Wire bootstrap into the CLI**

In `cli.py`, after the `refresh_p` block:

```python
    bootstrap_p = auth_sub.add_parser("bootstrap", help="Run device-code OAuth and write a token file")
    bootstrap_p.add_argument("--handle", required=True, help="Agent handle (without @)")
    bootstrap_p.add_argument("--token-file", type=Path, required=True)
    bootstrap_p.add_argument("--base-url", default="https://paxai.app")
```

And in the dispatch logic:

```python
    if args.command == "auth" and args.auth_command == "bootstrap":
        from .auth import bootstrap as auth_bootstrap
        auth_bootstrap(handle=args.handle, token_path=args.token_file, base_url=args.base_url)
        return 0
```

**Step 5: Run tests**

```bash
pytest tests/test_auth.py -v
```

Expected: 2 passed.

**Step 6: Commit**

```bash
git add src/ax_presence/auth.py src/ax_presence/cli.py tests/test_auth.py
git commit -m "Add 'ax-presence auth bootstrap' device-code subcommand

DCR + device-code + poll against /oauth/{register,device/code,token}
(aX-native, not the metadata-advertised Cognito /token). Writes a
mode-0600 token file the listener can consume."
git push
```

---

## Task 7: `listen` subcommand — baseline (no heartbeat, no liveness, no breakers yet)

**Files:**
- Create: `src/ax_presence/listen.py`
- Create: `tests/test_listen.py`
- Modify: `src/ax_presence/cli.py`

**Step 1: Write a failing test against an in-process fake SSE stream**

`tests/test_listen.py`:

```python
import io
import json
from pathlib import Path
from unittest.mock import patch

from ax_presence.listen import run_once


def fake_sse_bytes(events: list[tuple[str, dict]]) -> bytes:
    out = []
    for event, payload in events:
        out.append(f"event: {event}")
        out.append(f"data: {json.dumps(payload)}")
        out.append("")
    return ("\n".join(out) + "\n").encode()


def test_listen_emits_notify_on_matching_mention(tmp_path: Path, capsys) -> None:
    # Token file
    tp = tmp_path / "t.json"
    tp.write_text(json.dumps({
        "access_token": "A", "refresh_token": "R",
        "expires_at": 9999999999, "client_id": "C",
    }))

    body = fake_sse_bytes([
        ("mention", {"id": "m1", "mentioned_agent": "claude_prime", "content": "hi", "sender_name": "peach"}),
        ("message", {"id": "m2", "mentioned_agent": "claude_prime", "content": "ignored"}),
    ])

    def fake_urlopen(request, timeout):
        return io.BytesIO(body)

    with patch("ax_presence.listen.urllib.request.urlopen", side_effect=fake_urlopen):
        run_once(
            sse_url="https://example/sse",
            token_file=tp,
            handle="claude_prime",
            agent_id=None,
            max_content_chars=80,
            connect_refresh_window_seconds=120,
        )

    out = capsys.readouterr().out
    assert "NOTIFY @claude_prime mention from peach (msg m1): hi" in out
    # The 'message' event must NOT produce a NOTIFY even though payload looks like a mention
    assert "msg m2" not in out


def test_listen_dedups_repeated_msg_id(tmp_path: Path, capsys) -> None:
    tp = tmp_path / "t.json"
    tp.write_text(json.dumps({
        "access_token": "A", "refresh_token": "R",
        "expires_at": 9999999999, "client_id": "C",
    }))
    body = fake_sse_bytes([
        ("mention", {"id": "same", "mentioned_agent": "claude_prime", "content": "hi"}),
        ("mention", {"id": "same", "mentioned_agent": "claude_prime", "content": "hi again"}),
    ])
    with patch("ax_presence.listen.urllib.request.urlopen", return_value=io.BytesIO(body)):
        run_once("https://example/sse", tp, "claude_prime", None, 80, 120)
    out = capsys.readouterr().out
    assert out.count("NOTIFY") == 1
```

**Step 2: Run, verify FAIL**

**Step 3: Write `src/ax_presence/listen.py`**

```python
"""SSE listener loop.

Adapted from ax-backend PR #348's stream_once + run functions. The
wake contract (mention-only + target match + msg-id dedup) is enforced
here. Idle heartbeat, liveness file, and circuit breakers land in
Tasks 8, 9, and 11.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path
from typing import Optional

from .sse import iter_sse_events, sender_label, should_notify
from .tokens import TokenStore


DEFAULT_SSE_URL = "https://paxai.app/api/sse/messages"


def run_once(
    sse_url: str,
    token_file: Path,
    handle: str,
    agent_id: Optional[str],
    max_content_chars: int,
    connect_refresh_window_seconds: int,
) -> None:
    """Stream SSE once, emitting NOTIFY lines on matching mentions. Returns when stream ends."""
    store = TokenStore(token_file, token_url="https://paxai.app/oauth/token", refresh_skew_seconds=60)
    req = urllib.request.Request(
        sse_url,
        headers={
            "Authorization": "Bearer " + store.access_token(connect_refresh_window_seconds),
            "Accept": "text/event-stream",
        },
    )
    with urllib.request.urlopen(req, timeout=None) as resp:
        notified: set[str] = set()
        for event, data in iter_sse_events(resp):
            if event != "mention":
                continue
            try:
                import json
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not should_notify(event, payload, handle, agent_id):
                continue
            msg_id = payload.get("id") or "unknown"
            if msg_id != "unknown":
                if msg_id in notified:
                    continue
                notified.add(msg_id)
            content = (payload.get("content") or "")[:max_content_chars]
            print(
                f"NOTIFY @{handle} mention from {sender_label(payload)} (msg {msg_id}): {content}",
                flush=True,
            )
```

**Step 4: Wire into CLI**

Add to `cli.py`:

```python
    listen_p = sub.add_parser("listen", help="Stream SSE and emit NOTIFY lines on mentions")
    listen_p.add_argument("--handle", required=True)
    listen_p.add_argument("--token-file", type=Path, required=True)
    listen_p.add_argument("--agent-id", default=None)
    listen_p.add_argument("--sse-url", default="https://paxai.app/api/sse/messages")
    listen_p.add_argument("--max-content-chars", type=int, default=500)
```

In dispatch:

```python
    if args.command == "listen":
        from .listen import run_once
        run_once(
            sse_url=args.sse_url,
            token_file=args.token_file,
            handle=args.handle,
            agent_id=args.agent_id,
            max_content_chars=args.max_content_chars,
            connect_refresh_window_seconds=120,
        )
        return 0
```

**Step 5: Run tests**

```bash
pytest tests/test_listen.py -v
```

Expected: 2 passed.

**Step 6: Activate the previously-skipped invariant test**

Edit `tests/test_invariants.py`: remove the `pytest.skip` from `test_msg_id_dedup_is_callers_responsibility` and replace its body with:

```python
def test_msg_id_dedup_is_callers_responsibility(tmp_path: Path, capsys) -> None:
    # Re-uses the listen-loop test as canonical proof of dedup.
    from tests.test_listen import test_listen_dedups_repeated_msg_id
    test_listen_dedups_repeated_msg_id(tmp_path, capsys)
```

Run `pytest tests/test_invariants.py -v` — expect 6 passed, 4 skipped.

**Step 7: Commit**

```bash
git add src/ax_presence/listen.py src/ax_presence/cli.py tests/test_listen.py tests/test_invariants.py
git commit -m "Add 'ax-presence listen' subcommand baseline

Stream SSE, gate on mention events, enforce target + msg-id dedup,
emit NOTIFY lines on stdout. No heartbeat, liveness, or circuit
breakers yet — those land in Tasks 8–11."
git push
```

---

## Task 8: Idle heartbeat + liveness file

**Files:**
- Modify: `src/ax_presence/listen.py`
- Create: `src/ax_presence/liveness.py`
- Create: `tests/test_heartbeat.py`
- Create: `tests/test_liveness.py`

**Step 1: Write failing heartbeat test using a fake clock**

`tests/test_heartbeat.py`:

```python
import time
from unittest.mock import MagicMock

from ax_presence.listen import HeartbeatTicker


def test_heartbeat_fires_when_idle() -> None:
    clock = [1000.0]
    def now(): return clock[0]

    emit = MagicMock()
    ticker = HeartbeatTicker(interval_seconds=60, jitter_seconds=0, handle="x", emit=emit, clock=now)
    ticker.note_activity()
    assert ticker.due(now()) is False
    clock[0] += 61
    assert ticker.due(now()) is True
    ticker.fire(now())
    emit.assert_called_once()
    # After firing, due is False again
    assert ticker.due(now()) is False


def test_heartbeat_resets_on_activity() -> None:
    clock = [1000.0]
    def now(): return clock[0]
    emit = MagicMock()
    ticker = HeartbeatTicker(interval_seconds=60, jitter_seconds=0, handle="x", emit=emit, clock=now)
    clock[0] += 30
    ticker.note_activity()
    clock[0] += 40    # 70s since start, but only 40s since last activity
    assert ticker.due(now()) is False
```

**Step 2: Run, verify FAIL**

**Step 3: Add `HeartbeatTicker` to `listen.py`**

```python
import random
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class HeartbeatTicker:
    interval_seconds: float
    jitter_seconds: float
    handle: str
    emit: Callable[[str], None]
    clock: Callable[[], float] = field(default_factory=lambda: time.monotonic)
    _next_due: float = field(init=False)
    _last_activity: float = field(init=False)

    def __post_init__(self) -> None:
        now = self.clock()
        self._last_activity = now
        self._next_due = now + self._jittered_interval()

    def _jittered_interval(self) -> float:
        if self.jitter_seconds <= 0:
            return self.interval_seconds
        return self.interval_seconds + random.uniform(-self.jitter_seconds, self.jitter_seconds)

    def note_activity(self) -> None:
        now = self.clock()
        self._last_activity = now
        self._next_due = now + self._jittered_interval()

    def due(self, now: float) -> bool:
        return now >= self._next_due

    def fire(self, now: float) -> None:
        idle = now - self._last_activity
        self.emit(f"HEARTBEAT @{self.handle} idle {int(idle)}s — running self-check")
        self._next_due = now + self._jittered_interval()
```

Add `import time` to the imports at top.

**Step 4: Run heartbeat tests**

```bash
pytest tests/test_heartbeat.py -v
```

Expected: 2 passed.

**Step 5: Write failing liveness tests**

`tests/test_liveness.py`:

```python
import time
from pathlib import Path

from ax_presence.liveness import LivenessFile, watchdog_is_stale


def test_touch_updates_mtime(tmp_path: Path) -> None:
    p = tmp_path / "alive"
    lf = LivenessFile(p)
    lf.touch()
    assert p.exists()
    first = p.stat().st_mtime
    time.sleep(0.05)
    lf.touch()
    assert p.stat().st_mtime > first


def test_watchdog_is_stale(tmp_path: Path) -> None:
    p = tmp_path / "alive"
    p.touch()
    import os
    old = time.time() - 200
    os.utime(p, (old, old))
    assert watchdog_is_stale(p, max_age_seconds=90) is True


def test_watchdog_fresh(tmp_path: Path) -> None:
    p = tmp_path / "alive"
    p.touch()
    assert watchdog_is_stale(p, max_age_seconds=90) is False
```

**Step 6: Write `src/ax_presence/liveness.py`**

```python
"""Liveness file + watchdog.

The liveness file is touched by the listener's main loop and watched
by an external supervisor (cron/systemd/launchd) or the bundled
`ax-presence watchdog` subcommand. Stale mtime → process is dead →
out-of-band alert.
"""

from __future__ import annotations

import os
import time
from pathlib import Path


class LivenessFile:
    def __init__(self, path: Path) -> None:
        self.path = path

    def touch(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Create if absent, then update mtime
        self.path.touch(mode=0o644, exist_ok=True)
        now = time.time()
        os.utime(self.path, (now, now))


def watchdog_is_stale(path: Path, max_age_seconds: float) -> bool:
    if not path.exists():
        return True
    age = time.time() - path.stat().st_mtime
    return age > max_age_seconds
```

**Step 7: Run liveness tests**

```bash
pytest tests/test_liveness.py -v
```

Expected: 3 passed.

**Step 8: Activate invariant 10**

In `tests/test_invariants.py`, replace `test_liveness_file_is_touched_by_main_loop` body:

```python
def test_liveness_file_is_touched_by_main_loop(tmp_path: Path) -> None:
    """Invariant 10: the liveness file mtime advances from the main loop only."""
    from ax_presence.liveness import LivenessFile
    p = tmp_path / "alive"
    lf = LivenessFile(p)
    lf.touch()
    first = p.stat().st_mtime
    import time as _t; _t.sleep(0.05)
    lf.touch()
    assert p.stat().st_mtime > first
```

(The "main loop only, not refresh thread" rule is enforced by review and integration tests in Task 11; this test confirms the basic touch contract.)

**Step 9: Commit**

```bash
git add src/ax_presence/listen.py src/ax_presence/liveness.py tests/test_heartbeat.py tests/test_liveness.py tests/test_invariants.py
git commit -m "Add idle heartbeat + on-disk liveness file

HeartbeatTicker emits a HEARTBEAT line after an idle window with jitter
and resets on activity. LivenessFile is touched by callers; watchdog_is_stale
detects death. Wired into listen.py in Task 11 once breakers land."
git push
```

---

## Task 9: Circuit breaker (never-halt, branched-error)

**Files:**
- Create: `src/ax_presence/circuit.py`
- Create: `tests/test_circuit.py`

**Step 1: Write failing tests for the never-halt and branched-error invariants**

`tests/test_circuit.py`:

```python
import time
from unittest.mock import MagicMock

from ax_presence.circuit import CircuitBreaker, ErrorClass


def test_circuit_open_still_calls_action_at_cap() -> None:
    """Invariant 7: open never halts — slow-retry forever at cap."""
    calls = []
    alert = MagicMock()
    cb = CircuitBreaker(
        name="sse",
        failure_threshold=2,
        window_seconds=60,
        backoff_cap_seconds=0,    # zero so the test doesn't sleep
        alert=alert,
        clock=lambda: 0.0,
    )

    def action():
        calls.append("call")
        raise RuntimeError("boom")

    # Two failures opens the breaker; the third call still happens.
    for _ in range(3):
        try:
            cb.run(action, classify=lambda e: ErrorClass.TRANSIENT)
        except RuntimeError:
            pass

    assert len(calls) == 3, "open breaker must still call the action"
    assert cb.is_open() is True
    assert alert.called


def test_terminal_error_opens_immediately() -> None:
    """Invariant 8: terminal error opens on first failure."""
    alert = MagicMock()
    cb = CircuitBreaker(
        name="refresh",
        failure_threshold=99,   # high; would not trip on transient
        window_seconds=300,
        backoff_cap_seconds=0,
        alert=alert,
        clock=lambda: 0.0,
    )

    def action():
        raise RuntimeError("invalid_grant")

    try:
        cb.run(action, classify=lambda e: ErrorClass.TERMINAL)
    except RuntimeError:
        pass

    assert cb.is_open() is True
    assert alert.called


def test_success_closes_circuit() -> None:
    alert = MagicMock()
    cb = CircuitBreaker(
        name="sse",
        failure_threshold=1,
        window_seconds=60,
        backoff_cap_seconds=0,
        alert=alert,
        clock=lambda: 0.0,
    )
    try:
        cb.run(lambda: (_ for _ in ()).throw(RuntimeError("boom")), classify=lambda e: ErrorClass.TRANSIENT)
    except RuntimeError:
        pass
    assert cb.is_open() is True
    cb.run(lambda: "ok", classify=lambda e: ErrorClass.TRANSIENT)
    assert cb.is_open() is False
```

**Step 2: Run, verify FAIL**

**Step 3: Write `src/ax_presence/circuit.py`**

```python
"""Circuit breaker — never-halt, branched-error, out-of-band-alert.

Per peach's tonight-live tuning (2026-05-27):

- "Open" never means stop. It means slow-retry at backoff cap.
- Every open fires an out-of-band sponsor alert.
- Refresh ring branches on error class: terminal errors open
  immediately and stay open; transient errors use the threshold/window.

See docs/plans/2026-05-27-ax-presence-design.md#circuit-breakers.
"""

from __future__ import annotations

import enum
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque


class ErrorClass(enum.Enum):
    TRANSIENT = "transient"
    TERMINAL = "terminal"


@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int
    window_seconds: float
    backoff_cap_seconds: float
    alert: Callable[[str, str], None]    # (name, message) -> None
    clock: Callable[[], float] = field(default_factory=lambda: time.monotonic)
    _failures: Deque[float] = field(default_factory=deque)
    _open: bool = False
    _last_alert_at: float = 0.0
    _re_alert_interval_seconds: float = 300.0    # 5 min

    def is_open(self) -> bool:
        return self._open

    def run(self, action: Callable[[], object], classify: Callable[[Exception], ErrorClass]) -> object:
        if self._open and self.backoff_cap_seconds > 0:
            time.sleep(self.backoff_cap_seconds)
        try:
            result = action()
            self._on_success()
            return result
        except Exception as e:
            ec = classify(e)
            self._on_failure(e, ec)
            raise

    def _on_success(self) -> None:
        self._failures.clear()
        if self._open:
            self._open = False
            self.alert(self.name, "circuit closed (recovered)")

    def _on_failure(self, error: Exception, ec: ErrorClass) -> None:
        now = self.clock()
        if ec is ErrorClass.TERMINAL:
            if not self._open:
                self._open = True
                self.alert(self.name, f"terminal error: {error!r}")
                self._last_alert_at = now
            elif now - self._last_alert_at > self._re_alert_interval_seconds:
                self.alert(self.name, f"still terminal: {error!r}")
                self._last_alert_at = now
            return

        # transient
        self._failures.append(now)
        while self._failures and now - self._failures[0] > self.window_seconds:
            self._failures.popleft()

        if not self._open and len(self._failures) >= self.failure_threshold:
            self._open = True
            self.alert(self.name, f"opened after {len(self._failures)} failures: {error!r}")
            self._last_alert_at = now
        elif self._open and now - self._last_alert_at > self._re_alert_interval_seconds:
            self.alert(self.name, f"still open: {error!r}")
            self._last_alert_at = now
```

**Step 4: Run tests**

```bash
pytest tests/test_circuit.py -v
```

Expected: 3 passed.

**Step 5: Activate invariants 7 and 8 in `tests/test_invariants.py`**

Replace the skipped bodies:

```python
def test_circuit_open_never_halts() -> None:
    from tests.test_circuit import test_circuit_open_still_calls_action_at_cap
    test_circuit_open_still_calls_action_at_cap()


def test_refresh_terminal_errors_open_immediately() -> None:
    from tests.test_circuit import test_terminal_error_opens_immediately
    test_terminal_error_opens_immediately()
```

**Step 6: Commit**

```bash
git add src/ax_presence/circuit.py tests/test_circuit.py tests/test_invariants.py
git commit -m "Add CircuitBreaker (never-halt, branched-error model)

Per peach's tonight-live tuning: 'open' means slow-retry at cap, never
stop. Terminal errors (invalid_grant/invalid_client) open immediately
and stay open with periodic re-alerts. Three tests cover the
never-halt, terminal-immediate-open, and success-close invariants."
git push
```

---

## Task 10: Out-of-band alerts (`alert-cmd`)

**Files:**
- Create: `src/ax_presence/alerts.py`
- Create: `tests/test_alerts.py`
- Modify: `tests/test_invariants.py`

**Step 1: Failing test**

`tests/test_alerts.py`:

```python
from unittest.mock import patch

from ax_presence.alerts import shell_alert


def test_shell_alert_invokes_subprocess() -> None:
    with patch("ax_presence.alerts.subprocess.run") as run:
        shell_alert("/bin/true", ring="sse", message="opened after 3 fails")
        assert run.called
        args = run.call_args.args[0]
        # The command is invoked with the message and ring as env vars
        env = run.call_args.kwargs.get("env") or {}
        assert env.get("AX_PRESENCE_RING") == "sse"
        assert env.get("AX_PRESENCE_MESSAGE") == "opened after 3 fails"


def test_shell_alert_no_cmd_is_noop() -> None:
    with patch("ax_presence.alerts.subprocess.run") as run:
        shell_alert(None, ring="sse", message="ignored")
        assert run.called is False
```

**Step 2: Run, verify FAIL**

**Step 3: Write `src/ax_presence/alerts.py`**

```python
"""Out-of-band sponsor alerts.

Every CIRCUIT-OPEN must fire one of these — a log line alone is
invisible on a headless box at exactly the moment the sponsor needs to
be paged. The default backend is a shell command; users can write any
wrapper they like (send-to-aX-as-DM, SMS, webhook, etc.).
"""

from __future__ import annotations

import os
import subprocess
from typing import Optional


def shell_alert(cmd: Optional[str], ring: str, message: str) -> None:
    """Invoke `cmd` (via /bin/sh -c) with ring+message in env. No-op if cmd is None."""
    if not cmd:
        return
    env = os.environ.copy()
    env["AX_PRESENCE_RING"] = ring
    env["AX_PRESENCE_MESSAGE"] = message
    subprocess.run(["/bin/sh", "-c", cmd], env=env, check=False)
```

**Step 4: Run tests**

```bash
pytest tests/test_alerts.py -v
```

Expected: 2 passed.

**Step 5: Activate invariant 9**

In `tests/test_invariants.py`:

```python
def test_circuit_open_fires_out_of_band_alert() -> None:
    from unittest.mock import MagicMock
    from ax_presence.circuit import CircuitBreaker, ErrorClass

    alert = MagicMock()
    cb = CircuitBreaker(
        name="sse",
        failure_threshold=1,
        window_seconds=60,
        backoff_cap_seconds=0,
        alert=alert,
        clock=lambda: 0.0,
    )
    try:
        cb.run(lambda: (_ for _ in ()).throw(RuntimeError("boom")), classify=lambda e: ErrorClass.TRANSIENT)
    except RuntimeError:
        pass
    assert alert.called, "open must call the alert callable, not just log"
```

**Step 6: Commit**

```bash
git add src/ax_presence/alerts.py tests/test_alerts.py tests/test_invariants.py
git commit -m "Add shell-based out-of-band alert dispatcher

shell_alert invokes a user-supplied command with the ring and message
in env vars. Users wire it into circuit-open events via the listen
subcommand's --alert-cmd flag (Task 11)."
git push
```

---

## Task 11: Wire heartbeat + liveness + circuit breakers into the listen loop

**Files:**
- Modify: `src/ax_presence/listen.py`
- Modify: `src/ax_presence/cli.py`
- Modify: `tests/test_listen.py`

This is the integration task. The baseline `run_once` becomes the new
`run_forever` that wraps it with breakers, heartbeats, and liveness.

**Step 1: Refactor `listen.py`** — keep the existing `run_once` (rename to `_stream_once`), add a `run_forever` orchestrator:

```python
import time
import urllib.error
from .alerts import shell_alert
from .circuit import CircuitBreaker, ErrorClass
from .liveness import LivenessFile


DEFAULT_TOKEN_URL = "https://paxai.app/oauth/token"


def _classify_sse_error(error: Exception) -> ErrorClass:
    if isinstance(error, urllib.error.HTTPError) and error.code == 401:
        return ErrorClass.TRANSIENT    # refresh will handle 401
    return ErrorClass.TRANSIENT


def _classify_refresh_error(error: Exception) -> ErrorClass:
    msg = str(error)
    if "invalid_grant" in msg or "invalid_client" in msg:
        return ErrorClass.TERMINAL
    return ErrorClass.TRANSIENT


def run_forever(
    sse_url: str,
    token_file: Path,
    handle: str,
    agent_id: Optional[str],
    max_content_chars: int = 500,
    idle_heartbeat_seconds: Optional[int] = None,
    idle_heartbeat_jitter: int = 0,
    liveness_file: Optional[Path] = None,
    liveness_interval_seconds: int = 30,
    alert_cmd: Optional[str] = None,
) -> None:
    store = TokenStore(token_file, token_url=DEFAULT_TOKEN_URL, refresh_skew_seconds=60)

    def alert(ring: str, message: str) -> None:
        print(f"[ax-presence] CIRCUIT-{ring.upper()}: {message}", file=sys.stderr, flush=True)
        shell_alert(alert_cmd, ring=ring, message=message)

    sse_cb = CircuitBreaker(
        name="sse",
        failure_threshold=3,
        window_seconds=60,
        backoff_cap_seconds=30,
        alert=alert,
    )

    ticker = None
    if idle_heartbeat_seconds:
        ticker = HeartbeatTicker(
            interval_seconds=idle_heartbeat_seconds,
            jitter_seconds=idle_heartbeat_jitter,
            handle=handle,
            emit=lambda line: print(line, flush=True),
        )

    liveness = LivenessFile(liveness_file) if liveness_file else None
    next_liveness_at = time.monotonic()

    def stream():
        nonlocal next_liveness_at
        req = urllib.request.Request(
            sse_url,
            headers={
                "Authorization": "Bearer " + store.access_token(refresh_window_seconds=120),
                "Accept": "text/event-stream",
            },
        )
        with urllib.request.urlopen(req, timeout=None) as resp:
            notified: set[str] = set()
            print("[ax-presence] SSE connected", file=sys.stderr, flush=True)
            for event, data in iter_sse_events(resp):
                # Liveness + heartbeat tick on every event (and on no-op events too,
                # but iter_sse_events only yields on full event blocks; that's fine —
                # the SSE keepalive pings are events.)
                now = time.monotonic()
                if liveness and now >= next_liveness_at:
                    liveness.touch()
                    next_liveness_at = now + liveness_interval_seconds
                if ticker and ticker.due(now):
                    ticker.fire(now)

                if event != "mention":
                    continue
                try:
                    import json
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not should_notify(event, payload, handle, agent_id):
                    continue
                msg_id = payload.get("id") or "unknown"
                if msg_id != "unknown":
                    if msg_id in notified:
                        continue
                    notified.add(msg_id)
                content = (payload.get("content") or "")[:max_content_chars]
                print(
                    f"NOTIFY @{handle} mention from {sender_label(payload)} (msg {msg_id}): {content}",
                    flush=True,
                )
                if ticker:
                    ticker.note_activity()

    while True:
        try:
            sse_cb.run(stream, classify=_classify_sse_error)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"[ax-presence] stream error: {e!r}", file=sys.stderr, flush=True)
            time.sleep(2)
```

**Step 2: Update CLI**

Add to the `listen` subparser:

```python
    listen_p.add_argument("--idle-heartbeat", type=str, default=None,
                          help="e.g. 1h, 30m, 5m")
    listen_p.add_argument("--idle-heartbeat-jitter", type=str, default="0s")
    listen_p.add_argument("--liveness-file", type=Path, default=None)
    listen_p.add_argument("--liveness-interval", type=int, default=30)
    listen_p.add_argument("--alert-cmd", default=None)
```

And a tiny duration parser at the top of `cli.py`:

```python
def _parse_duration(spec: Optional[str]) -> Optional[int]:
    if not spec:
        return None
    spec = spec.strip().lower()
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(spec[-1], 1)
    return int(spec[:-1] if spec[-1].isalpha() else spec) * multiplier
```

Dispatch:

```python
    if args.command == "listen":
        from .listen import run_forever
        run_forever(
            sse_url=args.sse_url,
            token_file=args.token_file,
            handle=args.handle,
            agent_id=args.agent_id,
            max_content_chars=args.max_content_chars,
            idle_heartbeat_seconds=_parse_duration(args.idle_heartbeat),
            idle_heartbeat_jitter=_parse_duration(args.idle_heartbeat_jitter) or 0,
            liveness_file=args.liveness_file,
            liveness_interval_seconds=args.liveness_interval,
            alert_cmd=args.alert_cmd,
        )
        return 0
```

**Step 3: Add an integration test that verifies liveness mtime advances during a `run_forever` against a fake SSE**

Append to `tests/test_listen.py`:

```python
def test_run_forever_touches_liveness_on_main_loop(tmp_path: Path) -> None:
    import threading
    from ax_presence.listen import run_forever
    # synthesize a short stream then close
    body = fake_sse_bytes([
        ("mention", {"id": "m1", "mentioned_agent": "claude_prime", "content": "hi"}),
    ])
    tp = tmp_path / "t.json"
    tp.write_text(json.dumps({
        "access_token": "A", "refresh_token": "R",
        "expires_at": 9999999999, "client_id": "C",
    }))
    alive = tp.parent / "alive"

    def short_urlopen(req, timeout=None):
        return io.BytesIO(body)

    with patch("ax_presence.listen.urllib.request.urlopen", side_effect=short_urlopen):
        # Run for one stream cycle then break out via KeyboardInterrupt
        def runner():
            try:
                run_forever(
                    sse_url="https://example/sse",
                    token_file=tp,
                    handle="claude_prime",
                    agent_id=None,
                    liveness_file=alive,
                    liveness_interval_seconds=0,    # touch on every event
                )
            except KeyboardInterrupt:
                pass

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        t.join(timeout=2)

    assert alive.exists()
```

**Step 4: Run all tests**

```bash
pytest -v
```

Expected: all pass, no skips.

**Step 5: Commit**

```bash
git add src/ax_presence/listen.py src/ax_presence/cli.py tests/test_listen.py
git commit -m "Wire heartbeat + liveness + circuit breakers into listen loop

run_forever() is the new entry point. SSE failures go through a
3-fail/60s breaker; refresh errors branch on terminal vs transient.
Every open fires shell_alert. Liveness file is touched from the main
loop on every event. Idle heartbeat fires after the configured window."
git push
```

---

## Task 12: `watchdog` subcommand

**Files:**
- Modify: `src/ax_presence/liveness.py`
- Modify: `src/ax_presence/cli.py`
- Modify: `tests/test_liveness.py`

**Step 1: Failing test**

Append to `tests/test_liveness.py`:

```python
def test_watchdog_alerts_once_per_stale_event(tmp_path: Path) -> None:
    from ax_presence.liveness import watchdog_loop_once
    p = tmp_path / "alive"
    p.touch()
    import os
    os.utime(p, (time.time() - 200, time.time() - 200))
    calls = []
    def alert(ring, message):
        calls.append((ring, message))
    watchdog_loop_once(p, max_age_seconds=90, alert=alert, state={"alerted": False})
    assert len(calls) == 1
    # Second tick while still stale → no duplicate alert
    state = {"alerted": True}
    watchdog_loop_once(p, max_age_seconds=90, alert=alert, state=state)
    assert len(calls) == 1
```

**Step 2: Extend `src/ax_presence/liveness.py`**

```python
from typing import Callable


def watchdog_loop_once(
    path: Path,
    max_age_seconds: float,
    alert: Callable[[str, str], None],
    state: dict,
) -> None:
    """Run one watchdog tick. `state` is a caller-owned dict for one-shot alerting."""
    stale = watchdog_is_stale(path, max_age_seconds)
    if stale and not state.get("alerted"):
        alert("liveness", f"liveness file {path} is stale (>{max_age_seconds}s)")
        state["alerted"] = True
    elif not stale and state.get("alerted"):
        alert("liveness", "liveness file recovered (fresh mtime)")
        state["alerted"] = False
```

**Step 3: CLI wiring**

```python
    watchdog_p = sub.add_parser("watchdog", help="External liveness watchdog")
    watchdog_p.add_argument("--liveness-file", type=Path, required=True)
    watchdog_p.add_argument("--max-age", default="90s")
    watchdog_p.add_argument("--alert", required=True, help="Shell command invoked on stale")
    watchdog_p.add_argument("--interval", default="30s")
```

Dispatch:

```python
    if args.command == "watchdog":
        from .liveness import watchdog_loop_once
        from .alerts import shell_alert
        import time
        state = {"alerted": False}
        max_age = _parse_duration(args.max_age)
        interval = _parse_duration(args.interval)
        def alert(ring, message):
            shell_alert(args.alert, ring=ring, message=message)
        while True:
            watchdog_loop_once(args.liveness_file, max_age, alert, state)
            time.sleep(interval)
```

**Step 4: Run tests**

```bash
pytest tests/test_liveness.py -v
```

Expected: 4 passed.

**Step 5: Commit**

```bash
git add src/ax_presence/liveness.py src/ax_presence/cli.py tests/test_liveness.py
git commit -m "Add 'ax-presence watchdog' subcommand

One-shot watchdog ticker plus a long-running CLI wrapper. Alerts once
per stale-event and once on recovery. The external systemd/cron model
remains recommended."
git push
```

---

## Task 13: Live smoke against `claude_prime` on paxai.app

This is the manual validation step. There is no automated test that
hits prod — the smoke is intentionally manual to avoid burning the OAuth
flow in CI.

**Step 1: Bootstrap a token**

```bash
ax-presence auth bootstrap \
  --handle claude_prime \
  --token-file ~/.config/ax-presence/tokens/claude_prime.json
```

Open the printed URL in a browser, sign in if needed, approve.
Expected: file written at the path, mode 0600.

**Step 2: Start the listener**

In one terminal:

```bash
ax-presence listen \
  --handle claude_prime \
  --token-file ~/.config/ax-presence/tokens/claude_prime.json \
  --idle-heartbeat 2m \
  --idle-heartbeat-jitter 10s \
  --liveness-file ~/.config/ax-presence/state/claude_prime.alive \
  --liveness-interval 30s \
  --alert-cmd 'echo "[ALERT] $AX_PRESENCE_RING: $AX_PRESENCE_MESSAGE" >> ~/.config/ax-presence/alerts.log'
```

Expected on stderr: `[ax-presence] SSE connected`.
Expected file: `~/.config/ax-presence/state/claude_prime.alive` exists with fresh mtime.

**Step 3: Trigger a mention**

From a separate aX-connected session (e.g., from the codex_supervisor
identity), send: `@claude_prime smoke test ping`.

Expected on stdout in terminal 1:
```
NOTIFY @claude_prime mention from <sender> (msg <id>): smoke test ping
```

**Step 4: Verify heartbeat fires**

Wait ~2 minutes of silence. Expected on stdout:
```
HEARTBEAT @claude_prime idle 120s — running self-check
```

**Step 5: Start the watchdog**

In a third terminal:

```bash
ax-presence watchdog \
  --liveness-file ~/.config/ax-presence/state/claude_prime.alive \
  --max-age 90s \
  --interval 15s \
  --alert 'echo "[WATCHDOG] $AX_PRESENCE_MESSAGE" >> ~/.config/ax-presence/alerts.log'
```

Kill the listener (`Ctrl+C` in terminal 1). Within ~90s, the watchdog
should write a stale-alert line to the log. Restart the listener; the
watchdog should write a recovery line.

**Step 6: Document any surprises**

If anything doesn't match expectations, capture stderr/stdout and open
an issue at https://github.com/ax-platform/ax-presence/issues. Do not
"fix and move on" silently — surprises here are exactly the kind of
thing peach warned about.

**Step 7: Commit the smoke evidence**

Once the smoke is clean:

```bash
cat > docs/SMOKE-2026-05-XX.md <<'EOF'
# Smoke test — 2026-05-XX

- Environment: paxai.app prod
- Agent: claude_prime
- Sponsor: madtank
- Bootstrap: clean
- Listen: NOTIFY on mention from <sender> at <time>
- Heartbeat: fired after <X>s idle
- Liveness: file touched every 30s, watchdog detected kill in <X>s
- Recovery: watchdog detected restart in <X>s

No surprises.
EOF
git add docs/SMOKE-*.md
git commit -m "Smoke v0.1: bootstrap → listen → mention → heartbeat → watchdog all clean"
git push
```

---

## Task 14: Tag v0.1 and notify peach

**Step 1: Bump version**

Edit `src/ax_presence/__init__.py`:

```python
__version__ = "0.1.0"
```

**Step 2: Tag**

```bash
git add src/ax_presence/__init__.py
git commit -m "Release v0.1.0"
git tag -a v0.1.0 -m "v0.1.0 — first working release with peach's never-halt circuit-breaker model"
git push --tags
git push
```

**Step 3: Open a GitHub release**

```bash
gh release create v0.1.0 --title "v0.1.0" --notes-file docs/plans/2026-05-27-ax-presence-design.md
```

(The design doc is acceptable release notes for v0.1 — it documents what shipped.)

**Step 4: Send peach the review ping**

From the running Claude Code session, send via `mcp__ax__messages`:

```
@peach v0.1 is real code now — tagged at https://github.com/ax-platform/ax-presence/releases/tag/v0.1.0

Smoke clean on paxai.app: bootstrap → listen → mention → heartbeat → watchdog,
all four of your v0.1-review corrections shipped. Would love your
hands-on review pass when you have a window. Madtank will set up
contributor access.

Reference set as you offered would be great: listener invariants,
wake-bridge model, circuit-breaker tuning, and the ack-on-receipt /
custom-status / attachment-read patterns from your tonight's work.
```

---

## Task 15 (deferred — out of v0.1): auth.md citation PR

Once peach signs off on the v0.1 review, open a PR against ax-backend's
`auth.md` adding a section near "Use The Credential For Live Events"
that points to `https://github.com/ax-platform/ax-presence` as the
recommended monitor.

Do not do this in v0.1. The PR is only earned after peach's review pass.

---

## Done criteria for v0.1

- [ ] All 15 tasks committed and pushed
- [ ] `pytest -v` shows all tests pass, zero skips
- [ ] `gh run watch` shows green CI on main
- [ ] Smoke document exists at `docs/SMOKE-YYYY-MM-DD.md`
- [ ] v0.1.0 tag exists, GitHub release created
- [ ] Peach has been pinged for review
- [ ] No half-finished files, no `# TODO` comments left as questions to the next reader

## What v0.2 will add (out of scope, listed so they're not forgotten)

- `bridge` subcommand with tmux / fifo / Claude Code session backends
- `daemon` subcommand: refresh + listen + bridge in one supervised process
- `httpx` for SSE if `urllib` proves limiting under real load
- PyPI release
- auth.md citation PR (Task 15)
- Optional: peach-flavored status messages (emoji + custom display) per madtank's earlier riff
