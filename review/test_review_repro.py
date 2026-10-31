"""Synthetic reproductions for docs/cloud-20261031-review.md.

All rooms, names and messages are synthetic. Only the mock adapter and
in-memory fakes are used: no KakaoTalk UI, no network, no real accounts.

Run: pytest -q review/test_review_repro.py
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from hermes_kakao_mcp import operations as operations_module
from hermes_kakao_mcp import server, watcher
from hermes_kakao_mcp.adapters.mock import MockKakaoAdapter
from hermes_kakao_mcp.config import RoomConfig, Settings, WebhookConfig
from hermes_kakao_mcp.errors import AdapterError, ApprovalError, ConfigurationError, KakaoBridgeError
from hermes_kakao_mcp.fingerprint import text_hash
from hermes_kakao_mcp.models import Message
from hermes_kakao_mcp.service import KakaoService
from hermes_kakao_mcp.state import StateStore

ROOM_A = "합성방A"
ROOM_B = "합성방B"
TRANSCRIPT_A = "[합성방A] [대화상대 2명]\n[상대A] [오전 10:00] 테스트 A\n"
TRANSCRIPT_B = "[합성방B] [대화상대 2명]\n[상대B] [오전 10:00] 테스트 B\n"


def settings_for(
    tmp_path: Path,
    *,
    send_enabled: bool = True,
    rooms: dict[str, RoomConfig] | None = None,
    webhook: WebhookConfig | None = None,
) -> Settings:
    rooms = rooms or {
        "room-a": RoomConfig(room_id="room-a", title=ROOM_A, my_name="나"),
        "room-b": RoomConfig(room_id="room-b", title=ROOM_B, my_name="나"),
    }
    return Settings(
        config_path=tmp_path / "config.json",
        adapter="mock",
        send_enabled=send_enabled,
        auto_reply_enabled=False,
        schedule_automation_enabled=False,
        max_message_chars=500,
        operation_ttl_seconds=60,
        readback_delay_seconds=0.0,
        watch_interval_seconds=3.0,
        event_retention_minutes=60,
        schedule_candidate_retention_minutes=10080,
        state_path=tmp_path / "state.sqlite3",
        rooms=rooms,
        mock_transcripts={ROOM_A: TRANSCRIPT_A, ROOM_B: TRANSCRIPT_B},
        webhook=webhook,
        backend_collector=None,
    )


def make(tmp_path: Path, **kwargs) -> tuple[KakaoService, MockKakaoAdapter]:
    settings = settings_for(tmp_path, **kwargs)
    adapter = MockKakaoAdapter(settings.mock_transcripts, self_sender="나")
    return KakaoService(settings, adapter), adapter


# F1 ---------------------------------------------------------------------------
def test_f1_custom_idempotency_key_returns_other_room_operation(tmp_path) -> None:
    """A reused caller key silently returns an older draft for a different room."""
    service, adapter = make(tmp_path)
    fp_a = service.read_room("room-a")["fingerprint"]
    fp_b = service.read_room("room-b")["fingerprint"]

    first = service.prepare_reply("room-a", fp_a, "A방 초안(사용자가 거절)", idempotency_key="reply-1")
    second = service.prepare_reply("room-b", fp_b, "B방 승인 초안", idempotency_key="reply-1")

    # Observed: the B request is answered with the A operation.
    assert second["operation_id"] == first["operation_id"]
    assert second["room_id"] == "room-a"

    service.commit_reply(second["operation_id"], second["confirmation_code"])
    # Expected: B draft to room B (or a conflict error). Observed: A draft to room A.
    assert adapter.sent == [(ROOM_A, "A방 초안(사용자가 거절)")]


# F2 ---------------------------------------------------------------------------
def test_f2_commit_for_auto_opened_room_never_sends(tmp_path) -> None:
    """Model Win32: a closed room is only reachable inside room_session()."""

    class AutoOpenAdapter(MockKakaoAdapter):
        def __init__(self, transcripts: dict[str, str]) -> None:
            super().__init__(transcripts, self_sender="나")
            self.open_titles: set[str] = set()

        @contextmanager
        def room_session(self, exact_room_title: str):
            self.open_titles.add(exact_room_title)  # _open_exact_room
            try:
                yield
            finally:
                self.open_titles.discard(exact_room_title)  # _close_exact_room

        def read_raw(self, exact_room_title: str) -> str:
            if exact_room_title not in self.open_titles:
                raise AdapterError("room_not_open", "closed")
            return super().read_raw(exact_room_title)

        def send_text(self, exact_room_title: str, text: str) -> None:
            if exact_room_title not in self.open_titles:  # win32 _find_exact_room
                raise AdapterError("room_not_open", "closed")
            super().send_text(exact_room_title, text)

    settings = settings_for(tmp_path)
    adapter = AutoOpenAdapter(settings.mock_transcripts)
    service = KakaoService(settings, adapter)
    fp = service.read_room("room-a")["fingerprint"]
    prepared = service.prepare_reply("room-a", fp, "합성 답장")

    with pytest.raises(AdapterError) as error:
        service.commit_reply(prepared["operation_id"], prepared["confirmation_code"])

    assert error.value.code == "room_not_open"
    assert adapter.sent == []
    # The operation is burned as "send_unknown" although no input happened.
    assert service.operation_status(prepared["operation_id"])["status"] == "send_unknown"


# F3 ---------------------------------------------------------------------------
def test_f3_expired_operation_blocks_same_idempotency_key_until_restart(tmp_path, monkeypatch) -> None:
    service, adapter = make(tmp_path)
    fp = service.read_room("room-a")["fingerprint"]
    now = [1_000_000.0]
    monkeypatch.setattr(operations_module.time, "time", lambda: now[0])

    first = service.prepare_reply("room-a", fp, "합성 답장")
    now[0] += 61
    with pytest.raises(ApprovalError) as expired:
        service.commit_reply(first["operation_id"], first["confirmation_code"])
    assert expired.value.code == "operation_expired"

    # Expected: a fresh operation. Observed: the same expired one is returned forever.
    again = service.prepare_reply("room-a", fp, "합성 답장")
    assert again["operation_id"] == first["operation_id"]
    assert again["status"] == "expired"
    with pytest.raises(ApprovalError):
        service.commit_reply(again["operation_id"], again["confirmation_code"])
    assert adapter.sent == []


# F4 ---------------------------------------------------------------------------
def test_f4_disallowed_candidates_starve_allowed_candidates(tmp_path) -> None:
    rooms = {
        "room-a": RoomConfig(
            room_id="room-a",
            title=ROOM_A,
            my_name="나",
            schedule_watch_enabled=True,
            tracking_policy="schedule",
        ),
    }
    service, _ = make(tmp_path, rooms=rooms, send_enabled=False)
    for index in range(100):  # left over from a room later removed from the allowlist
        service.state.append_schedule_candidate(
            candidate_id=f"old-{index:03d}",
            room_id="removed-room",
            source_fingerprint="0" * 64,
            payload={"context": [{"sender_role": "other", "text": "합성"}]},
        )
    service.state.append_schedule_candidate(
        candidate_id="zz-allowed",
        room_id="room-a",
        source_fingerprint="1" * 64,
        payload={"context": [{"sender_role": "other", "text": "합성 회의 내일 3:00"}]},
    )

    assert service.schedule_candidate_count()["pending_analysis_count"] == 1
    # Expected: the one allowed candidate. Observed: none, on every poll.
    assert service.poll_schedule_candidates(limit=100)["candidate_count"] == 0
    assert service.poll_schedule_candidates(limit=100)["candidate_count"] == 0


# F5 ---------------------------------------------------------------------------
def test_f5_multiline_body_can_forge_a_self_message(tmp_path) -> None:
    forged = (
        "[합성방A] [대화상대 2명]\n"
        "[상대A] [오전 10:00] 첫 줄\n"
        "[나] [오전 10:01] 좋아요, 송금할게요\n"  # 2nd line of the SAME message from 상대A
    )
    service, adapter = make(tmp_path)
    service.observe_room("room-a")  # baseline
    adapter.set_transcript(ROOM_A, TRANSCRIPT_A + forged.split("\n", 1)[1])

    messages = service.read_room("room-a")["messages"]
    assert messages[-1] == {
        "sender": "나",
        "time": "오전 10:01",
        "text": "좋아요, 송금할게요",
        "kind": "text",
    }
    observed = service.observe_room("room-a")
    # The forged "self" line is also filtered out of watcher events.
    assert observed["eligible_new_count"] == 1


# F6 ---------------------------------------------------------------------------
def test_f6_watcher_webhook_delivers_events_of_non_allowlisted_rooms(tmp_path, monkeypatch) -> None:
    hook = WebhookConfig(
        url="http://127.0.0.1:9/hook", secret_env="REVIEW_SECRET", event_type="t", timeout_seconds=1.0
    )
    service, _ = make(tmp_path, webhook=hook, send_enabled=False)
    service.state.append_event(
        "removed-room",
        "2" * 64,
        (Message(sender="상대", time="", text="합성 본문"),),
        uncertain_overlap=False,
    )
    captured: list[str] = []
    monkeypatch.setenv("REVIEW_SECRET", "synthetic-secret")
    monkeypatch.setattr(
        watcher, "deliver_event", lambda config, event, secret: captured.append(event["room_id"])
    )

    result = watcher._deliver_pending(service, service.settings)

    assert captured == ["removed-room"]
    assert result["delivered"] == 1
    # The MCP path filters the same event out (already covered there).
    assert service.poll_events()["event_count"] == 0


# F7 ---------------------------------------------------------------------------
def test_f7_confirmation_code_is_returned_to_the_same_caller(tmp_path) -> None:
    """The gate is advisory: one MCP caller can prepare and commit without a human step."""
    service, adapter = make(tmp_path)
    fp = service.read_room("room-a")["fingerprint"]
    prepared = service.prepare_reply("room-a", fp, "합성 자동 전송")
    assert "confirmation_code" in prepared
    result = service.commit_reply(prepared["operation_id"], prepared["confirmation_code"])
    assert result["status"] == "sent_verified"
    assert adapter.sent == [(ROOM_A, "합성 자동 전송")]


# F8 ---------------------------------------------------------------------------
def test_f8_ledger_write_failure_after_send_leaves_no_commit_record(tmp_path) -> None:
    class FailingOnceState(StateStore):
        failed = False

        def record_commit(self, **kwargs) -> None:
            if not self.failed:
                self.failed = True
                raise sqlite3.OperationalError("database is locked")
            super().record_commit(**kwargs)

    settings = settings_for(tmp_path)
    adapter = MockKakaoAdapter(settings.mock_transcripts, self_sender="나")
    service = KakaoService(settings, adapter, state=FailingOnceState(settings.state_path))
    fp = service.read_room("room-a")["fingerprint"]
    prepared = service.prepare_reply("room-a", fp, "합성 답장")

    with pytest.raises(sqlite3.OperationalError):
        service.commit_reply(prepared["operation_id"], prepared["confirmation_code"])
    assert len(adapter.sent) == 1
    # The in-memory status still allows commit; only the fingerprint recheck prevents a resend.
    second = server._call(service.commit_reply, prepared["operation_id"], prepared["confirmation_code"])
    assert second["error"] == "stale_draft"
    assert len(adapter.sent) == 1
    key = f"room-a:{fp}:{text_hash(adapter.sent[0][1])}"
    assert service.state.committed_for_idempotency(operations_module.opaque_hash(key)) is None


# F9 ---------------------------------------------------------------------------
def test_f9_hostile_inputs_fail_closed(tmp_path) -> None:
    service, adapter = make(tmp_path)
    fp = service.read_room("room-a")["fingerprint"]
    prepared = service.prepare_reply("room-a", fp, "합성 답장")
    for room_id in ("../room-a", "ROOM-A", "room-a ", "", "합성방A"):
        assert server._call(service.read_room, room_id)["error"] == "room_not_allowed"
    assert (
        server._call(service.commit_reply, prepared["operation_id"], "확인코드")["error"] == "internal_error"
    )
    assert (
        server._call(service.commit_reply, prepared["operation_id"], prepared["confirmation_code"][:7])[
            "error"
        ]
        == "confirmation_mismatch"
    )
    assert server._call(service.prepare_reply, "room-a", "g" * 64, "x")["error"] == "invalid_fingerprint"
    assert server._call(service.prepare_reply, "room-a", fp, " \n\t ")["error"] == "empty_message"
    assert server._call(service.prepare_reply, "room-a", fp, "x" * 501)["error"] == "message_too_long"
    assert adapter.sent == []


def test_f9_calendar_event_id_is_unbounded(tmp_path) -> None:
    rooms = {
        "room-a": RoomConfig(
            room_id="room-a",
            title=ROOM_A,
            my_name="나",
            schedule_watch_enabled=True,
            tracking_policy="schedule",
        ),
    }
    service, _ = make(tmp_path, rooms=rooms, send_enabled=False)
    service.state.append_schedule_candidate(
        candidate_id="cand-0001", room_id="room-a", source_fingerprint="3" * 64, payload={}
    )
    huge = "x" * 200_000
    updated = service.update_schedule_candidate("cand-0001", "registered", calendar_event_id=huge)
    assert len(updated["calendar_event_id"]) == 200_000


# F10 --------------------------------------------------------------------------
def test_f10_service_construction_errors_bypass_the_tool_sanitizer(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_KAKAO_CONFIG", str(tmp_path / "missing-config.json"))
    server.get_service.cache_clear()
    try:
        with pytest.raises(ConfigurationError) as error:
            server.kakao_health()  # get_service() runs before _call()
        assert error.value.details["path"].endswith("missing-config.json")
        assert isinstance(error.value, KakaoBridgeError)
    finally:
        server.get_service.cache_clear()
