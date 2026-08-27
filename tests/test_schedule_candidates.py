import pytest

from hermes_kakao_mcp.errors import ConfigurationError
from hermes_kakao_mcp.state import StateStore


def test_schedule_candidate_queue_is_idempotent_and_keeps_bounded_payload(tmp_path) -> None:
    state = StateStore(tmp_path / "state.sqlite3")
    payload = {
        "context": [{"sender": "상대", "time": "10:00", "text": "9월 3일 회의"}],
        "signal": {"confidence": "high"},
    }

    assert state.append_schedule_candidate(
        candidate_id="candidate-a",
        room_id="room-a",
        source_fingerprint="a" * 64,
        payload=payload,
    )
    assert not state.append_schedule_candidate(
        candidate_id="candidate-a",
        room_id="room-a",
        source_fingerprint="a" * 64,
        payload=payload,
    )

    pending = state.pending_schedule_candidates()
    assert pending == [
        {
            "candidate_id": "candidate-a",
            "room_id": "room-a",
            "source_fingerprint": "a" * 64,
            "status": "pending_analysis",
            "calendar_event_id": None,
            **payload,
        }
    ]
    assert state.pending_schedule_candidate_count() == 1


def test_schedule_candidate_transition_prevents_invalid_status_and_terminal_replay(tmp_path) -> None:
    state = StateStore(tmp_path / "state.sqlite3")
    state.append_schedule_candidate(
        candidate_id="candidate-a",
        room_id="room-a",
        source_fingerprint="a" * 64,
        payload={"context": [], "signal": {}},
    )

    updated = state.update_schedule_candidate("candidate-a", "needs_user_choice")
    assert updated["status"] == "needs_user_choice"
    assert state.pending_schedule_candidates() == []
    assert state.get_schedule_candidate("candidate-a")["status"] == "needs_user_choice"

    registered = state.update_schedule_candidate("candidate-a", "registered", calendar_event_id="event-123")
    assert registered["status"] == "registered"
    assert registered["calendar_event_id"] == "event-123"
    assert state.pending_schedule_candidates() == []

    with pytest.raises(ConfigurationError, match="terminal"):
        state.update_schedule_candidate("candidate-a", "dismissed")
    with pytest.raises(ConfigurationError, match="invalid schedule candidate"):
        state.update_schedule_candidate("candidate-a", "not-a-status")


def test_terminal_schedule_candidate_purge_keeps_pending_work(tmp_path) -> None:
    state = StateStore(tmp_path / "state.sqlite3")
    for candidate_id in ("terminal", "pending"):
        state.append_schedule_candidate(
            candidate_id=candidate_id,
            room_id="room-a",
            source_fingerprint="a" * 64,
            payload={"context": [], "signals": {}},
        )
    state.update_schedule_candidate("terminal", "registered", calendar_event_id="calendar-id")
    with state._connection:
        state._connection.execute("UPDATE schedule_candidates SET updated_at = 0")

    assert state.purge_terminal_schedule_candidates(60) == 1
    assert [candidate["candidate_id"] for candidate in state.pending_schedule_candidates()] == ["pending"]
    assert state.pending_schedule_candidate_count() == 1
