import json
import sqlite3

import pytest

from hermes_kakao_mcp.errors import ConflictError
from hermes_kakao_mcp.models import Message
from hermes_kakao_mcp.state import StateStore


def test_snapshot_baseline_then_only_appended_messages(tmp_path) -> None:
    state = StateStore(tmp_path / "state.sqlite3")
    first = (Message("A", "10:00", "one"), Message("B", "10:01", "two"))
    baseline = state.update_snapshot("room-a", first)
    assert baseline["baseline"] is True
    assert baseline["new_messages"] == ()

    second = (*first, Message("A", "10:02", "three"))
    observed = state.update_snapshot("room-a", second)
    assert observed["baseline"] is False
    assert [message.text for message in observed["new_messages"]] == ["three"]


def test_snapshot_uses_longest_tail_overlap_when_last_message_repeats(tmp_path) -> None:
    state = StateStore(tmp_path / "state.sqlite3")
    first = (
        Message("A", "10:00", "one"),
        Message("B", "10:01", "two"),
        Message("C", "10:02", "repeat"),
    )
    state.update_snapshot("room-a", first)

    second = (
        first[1],
        first[2],
        Message("D", "10:03", "new-before-repeat"),
        first[2],
        Message("E", "10:04", "new-after-repeat"),
    )
    observed = state.update_snapshot("room-a", second)
    assert [message.text for message in observed["new_messages"]] == [
        "new-before-repeat",
        "repeat",
        "new-after-repeat",
    ]
    assert observed["uncertain_overlap"] is False


def test_room_state_persists_only_digests_not_transcript(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    state = StateStore(path)
    state.update_snapshot("room-a", (Message("A", "10:00", "sensitive text"),))
    connection = sqlite3.connect(path)
    row = connection.execute("SELECT tail_digests_json FROM room_state").fetchone()
    assert "sensitive text" not in row[0]
    assert len(json.loads(row[0])[0]) == 64


def test_events_are_at_most_once_per_poll(tmp_path) -> None:
    state = StateStore(tmp_path / "state.sqlite3")
    event_id = state.append_event(
        "room-a",
        "f" * 64,
        [Message("A", "10:00", "hello")],
        uncertain_overlap=False,
    )
    first = state.poll_events()
    second = state.poll_events()
    assert first[0]["event_id"] == event_id
    assert first[0]["messages"][0]["text"] == "hello"
    assert second == []


def test_backend_cursor_baselines_without_replay_and_commits_candidates_atomically(tmp_path) -> None:
    state = StateStore(tmp_path / "state.sqlite3")
    source_hash = "a" * 64

    baseline = state.ensure_backend_cursor(
        room_id="room-a",
        source_hash=source_hash,
        baseline_log_id=10,
    )
    created = state.commit_backend_batch(
        room_id="room-a",
        source_hash=source_hash,
        expected_log_id=10,
        observed_through_log_id=12,
        candidates=[
            {
                "candidate_id": "candidate-12",
                "source_fingerprint": "b" * 64,
                "payload": {"context": [{"sender_role": "other", "text": "9월 3일 회의"}]},
            }
        ],
    )
    resumed = state.ensure_backend_cursor(
        room_id="room-a",
        source_hash=source_hash,
        baseline_log_id=999,
    )

    assert baseline == {"baseline": True, "last_log_id": 10}
    assert created == 1
    assert resumed == {"baseline": False, "last_log_id": 12}
    assert state.pending_schedule_candidate_count() == 1

    with pytest.raises(ConflictError, match="concurrently"):
        state.commit_backend_batch(
            room_id="room-a",
            source_hash=source_hash,
            expected_log_id=10,
            observed_through_log_id=12,
            candidates=[],
        )


def test_backend_cursor_refuses_source_identity_change(tmp_path) -> None:
    state = StateStore(tmp_path / "state.sqlite3")
    state.ensure_backend_cursor(room_id="room-a", source_hash="a" * 64, baseline_log_id=4)

    with pytest.raises(ConflictError, match="source identity changed"):
        state.ensure_backend_cursor(room_id="room-a", source_hash="b" * 64, baseline_log_id=8)


def test_backend_cursor_migrates_legacy_source_hash_once_without_rebaseline(tmp_path) -> None:
    state = StateStore(tmp_path / "state.sqlite3")
    state.ensure_backend_cursor(room_id="room-a", source_hash="a" * 64, baseline_log_id=4)

    migrated = state.migrate_backend_source_identity(
        room_id="room-a",
        legacy_source_hash="a" * 64,
        source_hash="b" * 64,
    )

    assert migrated is True
    assert state.backend_cursor(room_id="room-a", source_hash="b" * 64) == 4
    assert (
        state.migrate_backend_source_identity(
            room_id="room-a",
            legacy_source_hash="a" * 64,
            source_hash="b" * 64,
        )
        is False
    )
    with pytest.raises(ConflictError, match="migration was refused"):
        state.migrate_backend_source_identity(
            room_id="room-a",
            legacy_source_hash="a" * 64,
            source_hash="c" * 64,
        )


def test_backend_collector_status_contains_no_message_payload(tmp_path) -> None:
    state = StateStore(tmp_path / "state.sqlite3")
    state.update_backend_collector_status(
        status="running",
        watched_room_count=4,
        mapped_room_count=4,
        last_candidate_count=0,
    )

    assert state.backend_collector_status() == {
        "status": "running",
        "mode": "ram_only_v2",
        "watched_room_count": 4,
        "mapped_room_count": 4,
        "last_cycle_at": pytest.approx(state.backend_collector_status()["last_cycle_at"]),
        "error_code": None,
        "last_candidate_count": 0,
    }
