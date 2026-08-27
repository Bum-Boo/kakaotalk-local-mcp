from hermes_kakao_mcp.fingerprint import message_digest, snapshot_fingerprint
from hermes_kakao_mcp.models import Message


def test_unicode_fingerprint_is_normalized() -> None:
    composed = Message("상대", "오전 1:00", "café")
    decomposed = Message("상대", "오전 1:00", "cafe\u0301")
    assert message_digest(composed) == message_digest(decomposed)


def test_snapshot_fingerprint_is_room_scoped() -> None:
    messages = (Message("상대", "10:00", "안녕"),)
    assert snapshot_fingerprint("room-a", messages) != snapshot_fingerprint("room-b", messages)
