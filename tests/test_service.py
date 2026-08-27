from contextlib import contextmanager
from dataclasses import replace

import pytest
from conftest import make_service, make_settings

from hermes_kakao_mcp.adapters.mock import MockKakaoAdapter
from hermes_kakao_mcp.errors import AdapterError, ApprovalError, ConflictError
from hermes_kakao_mcp.service import KakaoService


def test_first_observe_baselines_without_replay(tmp_path) -> None:
    service, adapter = make_service(tmp_path)
    first = service.observe_room("self-test")
    assert first["baseline"] is True
    assert first["eligible_new_count"] == 0
    assert service.poll_events()["event_count"] == 0

    adapter.set_transcript(
        "테스트방",
        adapter.read_raw("테스트방") + "\n[상대] [오전 10:02] 새 메시지",
    )
    second = service.observe_room("self-test")
    assert second["eligible_new_count"] == 1
    events = service.poll_events()
    assert events["events"][0]["messages"][0]["text"] == "새 메시지"


def test_self_chat_headerless_transcript_baselines_without_events(tmp_path) -> None:
    settings = make_settings(tmp_path, my_name="나", self_chat=True)
    adapter = MockKakaoAdapter({"테스트방": "첫 메시지\n\n두 번째 메시지"})
    service = KakaoService(settings, adapter)

    read = service.read_room("self-test")
    assert [message["text"] for message in read["messages"]] == ["첫 메시지", "두 번째 메시지"]
    observed = service.observe_room("self-test")
    assert observed["baseline"] is True
    assert observed["eligible_new_count"] == 0


def test_snapshot_requires_two_consecutive_matching_fingerprints(tmp_path) -> None:
    class SequenceAdapter(MockKakaoAdapter):
        def __init__(self, transcripts: list[str]) -> None:
            super().__init__({"테스트방": transcripts[-1]})
            self.transcripts = iter(transcripts)

        def read_raw(self, exact_room_title: str) -> str:
            try:
                return next(self.transcripts)
            except StopIteration:
                return super().read_raw(exact_room_title)

    short = """[테스트방] [대화상대 2명]
[상대] [오전 10:00] 먼저 보인 메시지
"""
    stable = short + "[상대] [오전 10:01] 늦게 렌더된 메시지\n"
    settings = make_settings(tmp_path)
    adapter = SequenceAdapter([short, stable, stable])
    service = KakaoService(settings, adapter)

    result = service.read_room("self-test")

    assert [message["text"] for message in result["messages"]] == [
        "먼저 보인 메시지",
        "늦게 렌더된 메시지",
    ]


def test_stable_snapshot_uses_one_room_session_for_all_reads(tmp_path) -> None:
    class SessionAdapter(MockKakaoAdapter):
        def __init__(self) -> None:
            super().__init__({"테스트방": "[상대] [오전 10:00] 안녕"})
            self.events: list[str] = []

        @contextmanager
        def room_session(self, exact_room_title: str):
            self.events.append(f"enter:{exact_room_title}")
            try:
                yield
            finally:
                self.events.append(f"exit:{exact_room_title}")

        def read_raw(self, exact_room_title: str) -> str:
            self.events.append(f"read:{exact_room_title}")
            return super().read_raw(exact_room_title)

    settings = make_settings(tmp_path)
    adapter = SessionAdapter()
    service = KakaoService(settings, adapter)

    service.read_room("self-test")

    assert adapter.events == [
        "enter:테스트방",
        "read:테스트방",
        "read:테스트방",
        "exit:테스트방",
    ]


def test_snapshot_refuses_to_advance_state_when_transcript_never_stabilizes(tmp_path) -> None:
    class AlternatingAdapter(MockKakaoAdapter):
        def __init__(self) -> None:
            super().__init__({"테스트방": ""})
            self.index = 0

        def read_raw(self, exact_room_title: str) -> str:
            self.index += 1
            return (
                "[테스트방] [대화상대 2명]\n[상대] [오전 10:00] A\n"
                if self.index % 2
                else "[테스트방] [대화상대 2명]\n[상대] [오전 10:01] B\n"
            )

    settings = make_settings(tmp_path)
    service = KakaoService(settings, AlternatingAdapter())

    with pytest.raises(AdapterError, match="did not stabilize"):
        service.observe_room("self-test")

    assert service.state.pending_events() == []


def test_self_and_media_messages_do_not_enqueue(tmp_path) -> None:
    service, adapter = make_service(tmp_path)
    service.observe_room("self-test")
    adapter.set_transcript(
        "테스트방",
        adapter.read_raw("테스트방")
        + "\n[나] [오전 10:02] 내 메시지\n[상대] [오전 10:03] 사진",
    )
    result = service.observe_room("self-test")
    assert result["eligible_new_count"] == 0


def test_schedule_watch_enqueues_only_new_schedule_candidate(tmp_path) -> None:
    settings = make_settings(tmp_path, schedule_watch_enabled=True)
    settings = replace(
        settings,
        rooms={
            "self-test": replace(
                settings.rooms["self-test"],
                category="vip_professor",
                tracking_policy="schedule",
                source_label="Professor A·Professor B room",
                source_people=("Professor A", "Professor B"),
            )
        },
    )
    adapter = MockKakaoAdapter(settings.mock_transcripts, self_sender="나")
    service = KakaoService(settings, adapter)
    assert service.health()["approved_source_aliases"] == [
        {
            "source_class": "vip_professor",
            "source_label": "Professor A·Professor B room",
            "source_people": ["Professor A", "Professor B"],
            "room_count": 1,
        }
    ]

    baseline = service.observe_room("self-test")
    assert baseline["schedule_candidate_count"] == 0

    adapter.set_transcript(
        "테스트방",
        adapter.read_raw("테스트방") + "\n[상대] [오전 10:02] 9월 3일 오후 2시 조교 회의가 있습니다.",
    )
    observed = service.observe_room("self-test")

    assert observed["schedule_candidate_count"] == 1
    candidates = service.poll_schedule_candidates()
    assert candidates["candidate_count"] == 1
    candidate = candidates["candidates"][0]
    assert candidate["room_id"] == "self-test"
    assert candidate["signals"]["confidence"] == "high"
    assert candidate["source_class"] == "vip_professor"
    assert candidate["priority"] == "vip"
    assert candidate["source_label"] == "Professor A·Professor B room"
    assert candidate["source_people"] == ["Professor A", "Professor B"]
    assert len(candidate["context"]) <= 4
    counts = service.schedule_candidate_count()
    assert counts["pending_analysis_count"] == 1
    assert counts["vip_pending_analysis_count"] == 1

    choice = service.update_schedule_candidate(candidate["candidate_id"], "needs_user_choice")
    assert choice["status"] == "needs_user_choice"
    assert service.poll_schedule_candidates()["candidate_count"] == 0
    assert service.schedule_candidate_count()["pending_analysis_count"] == 0
    assert service.schedule_candidate_count()["vip_pending_analysis_count"] == 0
    held = service.get_schedule_candidate(candidate["candidate_id"])
    assert held["status"] == "needs_user_choice"
    assert held["candidate_id"] == candidate["candidate_id"]
    registered = service.update_schedule_candidate(
        candidate["candidate_id"],
        "registered",
        calendar_event_id="calendar-readback-id",
    )
    assert registered["status"] == "registered"
    assert service.poll_schedule_candidates()["candidate_count"] == 0


def test_schedule_watch_never_enqueues_when_disabled(tmp_path) -> None:
    service, adapter = make_service(tmp_path)
    service.observe_room("self-test")
    adapter.set_transcript(
        "테스트방",
        adapter.read_raw("테스트방") + "\n[상대] [오전 10:02] 9월 3일 오후 2시 조교 회의가 있습니다.",
    )

    observed = service.observe_room("self-test")

    assert observed["eligible_new_count"] == 1
    assert observed["schedule_candidate_count"] == 0
    assert service.state.pending_schedule_candidates() == []


def test_prepare_commit_and_readback_exactly_once(tmp_path) -> None:
    service, adapter = make_service(tmp_path, send_enabled=True)
    current = service.read_room("self-test")
    prepared = service.prepare_reply(
        "self-test",
        current["fingerprint"],
        "테스트 답장",
        "request-0001",
    )
    committed = service.commit_reply(prepared["operation_id"], prepared["confirmation_code"])
    assert committed["status"] == "sent_verified"
    assert committed["verified"] is True
    assert adapter.sent == [("테스트방", "테스트 답장")]

    duplicate = service.commit_reply(prepared["operation_id"], prepared["confirmation_code"])
    assert duplicate["already_committed"] is True
    assert adapter.sent == [("테스트방", "테스트 답장")]


def test_commit_is_blocked_when_sending_disabled(tmp_path) -> None:
    service, adapter = make_service(tmp_path, send_enabled=False)
    current = service.read_room("self-test")
    prepared = service.prepare_reply("self-test", current["fingerprint"], "초안")
    with pytest.raises(ApprovalError, match="disabled"):
        service.commit_reply(prepared["operation_id"], prepared["confirmation_code"])
    assert adapter.sent == []


def test_adapter_failure_makes_operation_terminal(tmp_path) -> None:
    class FailingAdapter(MockKakaoAdapter):
        def send_text(self, exact_room_title: str, text: str) -> None:
            raise AdapterError("send_unconfirmed", "delivery outcome is unknown")

    settings = make_settings(tmp_path, send_enabled=True)
    adapter = FailingAdapter(settings.mock_transcripts)
    service = KakaoService(settings, adapter)
    current = service.read_room("self-test")
    prepared = service.prepare_reply("self-test", current["fingerprint"], "한 번만")

    with pytest.raises(AdapterError, match="unknown"):
        service.commit_reply(prepared["operation_id"], prepared["confirmation_code"])
    assert service.operation_status(prepared["operation_id"])["status"] == "send_unknown"
    with pytest.raises(ApprovalError, match="no longer pending"):
        service.commit_reply(prepared["operation_id"], prepared["confirmation_code"])


def test_readback_requires_transcript_change_not_only_matching_old_text(tmp_path) -> None:
    class NoOpAdapter(MockKakaoAdapter):
        def send_text(self, exact_room_title: str, text: str) -> None:
            return None

    settings = make_settings(tmp_path, send_enabled=True)
    adapter = NoOpAdapter(settings.mock_transcripts)
    service = KakaoService(settings, adapter)
    current = service.read_room("self-test")
    prepared = service.prepare_reply("self-test", current["fingerprint"], "반가워")
    result = service.commit_reply(prepared["operation_id"], prepared["confirmation_code"])
    assert result["status"] == "sent_unverified"
    assert result["verified"] is False


def test_stale_commit_does_not_send(tmp_path) -> None:
    service, adapter = make_service(tmp_path, send_enabled=True)
    current = service.read_room("self-test")
    prepared = service.prepare_reply("self-test", current["fingerprint"], "오래된 답장")
    adapter.set_transcript(
        "테스트방",
        adapter.read_raw("테스트방") + "\n[상대] [오전 10:03] 추가 메시지",
    )
    with pytest.raises(ConflictError, match="changed"):
        service.commit_reply(prepared["operation_id"], prepared["confirmation_code"])
    assert adapter.sent == []


def test_prepare_rejects_unknown_room(tmp_path) -> None:
    service, _ = make_service(tmp_path)
    with pytest.raises(Exception, match="allowlist"):
        service.read_room("not-allowed")
