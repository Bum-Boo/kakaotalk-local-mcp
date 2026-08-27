from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from conftest import make_settings

from hermes_kakao_mcp.backend_v2 import BackendRecord
from hermes_kakao_mcp.backend_watcher import BackendIngestionRunner
from hermes_kakao_mcp.config import BackendCollectorConfig, RoomConfig
from hermes_kakao_mcp.state import StateStore


class FakeCollector:
    def __init__(self) -> None:
        self.source_hash = "a" * 64
        self.rooms = {
            "self-test": SimpleNamespace(source_hash=self.source_hash),
        }

    def baseline(self, room_id: str) -> tuple[str, int]:
        assert room_id == "self-test"
        return self.source_hash, 10

    def read_since(self, room_id: str, after_log_id: int, limit: int):
        assert room_id == "self-test"
        assert limit == 200
        if after_log_id >= 13:
            return (), after_log_id, (0, 0, 0, 0)
        return (
            (
                BackendRecord(10, "other", 100, "이전 문맥"),
                BackendRecord(11, "self", 101, "9월 3일 내가 회의함"),
                BackendRecord(12, "other", 102, "9월 3일 오후 2시 조교 회의가 있습니다."),
                BackendRecord(13, "other", 103, "점심 먹었어?"),
            ),
            13,
            (1, 2, 3, 4),
        )


def backend_settings(tmp_path: Path):
    base = make_settings(tmp_path, my_name="", schedule_watch_enabled=True)
    room = RoomConfig(
        room_id="self-test",
        title="테스트방",
        my_name="",
        enabled=True,
        schedule_watch_enabled=True,
        category="vip_professor",
        tracking_policy="schedule",
        source_label="Professor A·Professor B room",
        source_people=("Professor A", "Professor B"),
    )
    return replace(
        base,
        rooms={"self-test": room},
        backend_collector=BackendCollectorConfig(
            enabled=True,
            mode="ram_only_v2",
            room_ids=("self-test",),
            max_batch_rows=200,
            bootstrap_retry_seconds=30.0,
        ),
    )


def test_backend_ingestion_baselines_filters_self_and_deduplicates(tmp_path) -> None:
    settings = backend_settings(tmp_path)
    state = StateStore(settings.state_path)
    runner = BackendIngestionRunner(settings, FakeCollector(), state)

    baseline = runner.observe_room("self-test")
    observed = runner.observe_room("self-test")
    repeated = runner.observe_room("self-test")
    pending = state.pending_schedule_candidates()

    assert baseline == {
        "ok": True,
        "room_id": "self-test",
        "baseline": True,
        "observed_new_count": 0,
        "schedule_candidate_count": 0,
    }
    assert observed["observed_new_count"] == 3
    assert observed["schedule_candidate_count"] == 2
    assert repeated["observed_new_count"] == 0
    assert repeated["schedule_candidate_count"] == 0
    assert len(pending) == 2
    assert {item["room_id"] for item in pending} == {"self-test"}
    assert {item["source_class"] for item in pending} == {"vip_professor"}
    assert {item["priority"] for item in pending} == {"vip"}
    assert {item["source_label"] for item in pending} == {"Professor A·Professor B room"}
    assert {tuple(item["source_people"]) for item in pending} == {
        ("Professor A", "Professor B")
    }
    assert [item["sender_role"] for item in pending[0]["context"]] == [
        "other",
        "self",
        "other",
    ]
    assert [item["sender_role"] for item in pending[1]["context"]] == [
        "other",
        "self",
        "other",
        "other",
    ]
    assert all(len(item["context"]) <= 4 for item in pending)
