from hermes_kakao_mcp.parser import parse_chat_text


def test_parse_korean_multiline_and_media() -> None:
    raw = """[테스트방] [대화상대 3명]
--------------- 2026년 8월 20일 목요일 ---------------
[철수] [오전 10:00] 첫 줄
둘째 줄
[영희] [오후 1:02] 사진
"""
    snapshot = parse_chat_text(raw)
    assert snapshot.room_title == "테스트방"
    assert snapshot.member_count == 3
    assert snapshot.messages[0].text == "첫 줄\n둘째 줄"
    assert snapshot.messages[1].kind == "photo"


def test_parse_english_24_hour_and_ampm() -> None:
    raw = """[Room] [Participants 2]
20 August 2026
[Alice] [14:03] hello
[Bob] [PM 2:04] Video
"""
    snapshot = parse_chat_text(raw)
    assert [message.time for message in snapshot.messages] == ["14:03", "PM 2:04"]
    assert snapshot.messages[1].kind == "video"


def test_empty_transcript() -> None:
    snapshot = parse_chat_text("  \n")
    assert snapshot.messages == ()
    assert snapshot.room_title is None


def test_headerless_self_chat_requires_explicit_sender_fallback() -> None:
    raw = """2026년 8월 20일 목요일

첫 메시지
둘째 줄

두 번째 메시지
"""
    assert parse_chat_text(raw).messages == ()

    snapshot = parse_chat_text(raw, fallback_sender="나")
    assert [message.sender for message in snapshot.messages] == ["나", "나"]
    assert [message.time for message in snapshot.messages] == ["", ""]
    assert [message.text for message in snapshot.messages] == [
        "첫 메시지\n둘째 줄",
        "두 번째 메시지",
    ]
