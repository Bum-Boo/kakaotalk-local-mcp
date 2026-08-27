from hermes_kakao_mcp.models import Message
from hermes_kakao_mcp.schedule_gate import detect_schedule_candidate


def test_detects_academic_schedule_with_date_time_and_keyword() -> None:
    message = Message(
        sender="조교",
        time="오전 10:00",
        text="9월 3일 오후 2시 조교 회의가 301호에서 진행됩니다.",
    )

    result = detect_schedule_candidate(message)

    assert result is not None
    assert result.confidence == "high"
    assert "9월 3일" in result.date_signals
    assert "오후 2시" in result.time_signals
    assert "회의" in result.keywords


def test_marks_choice_or_ambiguous_schedule_for_user_review() -> None:
    message = Message(
        sender="학과",
        time="오전 10:00",
        text="세미나는 화요일 14시 또는 수요일 16시 중 가능한 시간에 참석해 주세요.",
    )

    result = detect_schedule_candidate(message)

    assert result is not None
    assert result.confidence == "needs_review"
    assert result.needs_user_choice is True


def test_forwards_substantive_text_without_content_exclusion_list() -> None:
    for text in (
        "[광고] 오늘만 특가 쿠폰",
        "점심 먹었어?",
        "회의록은 나중에 공유할게",
    ):
        result = detect_schedule_candidate(Message("상대", "", text))
        assert result is not None
        assert result.confidence == "needs_review"


def test_ignores_greeting_only_but_keeps_greeting_with_substantive_body() -> None:
    for text in ("안녕", "안녕하세요", "안녕하세요!", "반갑습니다.", "반가워요ㅎㅎ"):
        assert detect_schedule_candidate(Message("상대", "", text)) is None

    result = detect_schedule_candidate(
        Message("상대", "", "안녕하세요. 다음 주 화요일 조교 회의가 있습니다.")
    )
    assert result is not None
    assert "다음 주" in result.date_signals
    assert "회의" in result.keywords
