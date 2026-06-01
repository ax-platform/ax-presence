from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "agent-move-and-verify.sh"
DOC = ROOT / "docs" / "AGENT-MOVE-AND-VERIFY.md"
SETUP_DOC = ROOT / "docs" / "standard-agent-setup.md"


def test_move_script_encodes_full_destination_roundtrip_gate():
    text = SCRIPT.read_text()

    # Presence/SSE is necessary, but not enough.
    assert "/api/v1/agents/availability?space_id=" in text
    assert '"sse_connected"' in text

    # Synthetic smoke must prove destination routing, gateway dispatch for the
    # exact smoke message, and a real reply from the routed target agent.
    assert 'routing_story") or {}).get("targets")' in text
    assert "routing_target_found" in text
    assert "verify_log_dispatch" in text
    assert "aX inbound dispatch:" in text
    assert "reply_message_id" in text
    assert 'm.get("parent_id") != mid' in text

    # The restart must be scoped to the gateway wrapper, not broad fleet sessions.
    assert 'TMUX_SESSION="${TMUX_SESSION:-$HANDLE-gw}"' in text
    assert 'tmux kill-session -t "$TMUX_SESSION"' in text


def test_move_docs_state_presence_is_not_ready_and_require_reply_proof():
    combined = DOC.read_text() + "\n" + SETUP_DOC.read_text()

    assert "Presence is necessary but **not sufficient**" in combined
    assert "Presence/SSE alone is not enough" in combined
    assert "destination-space mention → target gateway inbound dispatch → target reply" in combined
    assert "Require a real post-check reply authored by that agent" in combined
    assert "restart only that handle's gateway listener" in combined
