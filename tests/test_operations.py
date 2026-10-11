from dataclasses import replace

import pytest
from conftest import make_service

from hermes_kakao_mcp.errors import ConflictError
from hermes_kakao_mcp.operations import OperationStore


def test_identical_retries_keep_the_original_approval() -> None:
    store = OperationStore(60)
    payload = {
        "room_id": "room-a",
        "expected_fingerprint": "a" * 64,
        "text": "approved draft",
        "idempotency_key": "request-key",
    }
    original = store.prepare(**payload)
    retry = store.prepare(**payload)
    assert retry is original
    assert retry.public() == original.public()


@pytest.mark.parametrize(
    "changed",
    [{"room_id": "room-b"}, {"expected_fingerprint": "b" * 64}, {"text": "different draft"}],
)
def test_key_cannot_be_rebound_to_another_approval_payload(changed) -> None:
    store = OperationStore(60)
    payload = {
        "room_id": "room-a",
        "expected_fingerprint": "a" * 64,
        "text": "approved draft",
        "idempotency_key": "request-key",
    }
    original = store.prepare(**payload)
    before = original.public()
    with pytest.raises(ConflictError) as caught:
        store.prepare(**{**payload, **changed})
    assert caught.value.code == "idempotency_conflict"
    assert original.public() == before
    assert store.require_for_commit(original.operation_id, original.confirmation_code) is original
    assert store.prepare(**payload) is original


def test_service_rejects_changed_text_and_retains_identical_retry(tmp_path) -> None:
    service, adapter = make_service(tmp_path, send_enabled=True)
    fingerprint = service.read_room("self-test")["fingerprint"]
    original = service.prepare_reply("self-test", fingerprint, "approved draft", "request-key")
    with pytest.raises(ConflictError) as caught:
        service.prepare_reply("self-test", fingerprint, "different draft", "request-key")
    assert caught.value.code == "idempotency_conflict"
    assert adapter.sent == []
    assert service.prepare_reply("self-test", fingerprint, "approved draft", "request-key") == original
    service.commit_reply(original["operation_id"], original["confirmation_code"])
    assert adapter.sent == [("테스트방", "approved draft")]


def test_service_rejects_cross_room_key_reuse_without_sending(tmp_path) -> None:
    service, adapter = make_service(tmp_path, send_enabled=True)
    service.settings.rooms["room-b"] = replace(
        service.settings.rooms["self-test"], room_id="room-b", title="other-room"
    )
    adapter.set_transcript("other-room", adapter.read_raw("테스트방"))
    first = service.read_room("self-test")
    second = service.read_room("room-b")
    service.prepare_reply("self-test", first["fingerprint"], "approved draft", "request-key")
    with pytest.raises(ConflictError) as caught:
        service.prepare_reply("room-b", second["fingerprint"], "approved draft", "request-key")
    assert caught.value.code == "idempotency_conflict"
    assert adapter.sent == []


def test_service_rejects_same_key_against_a_changed_transcript(tmp_path) -> None:
    service, adapter = make_service(tmp_path, send_enabled=True)
    before = service.read_room("self-test")
    service.prepare_reply("self-test", before["fingerprint"], "approved draft", "request-key")
    adapter.set_transcript("테스트방", adapter.read_raw("테스트방") + "\n[상대] [오전 10:03] new message")
    after = service.read_room("self-test")
    assert after["fingerprint"] != before["fingerprint"]
    with pytest.raises(ConflictError) as caught:
        service.prepare_reply("self-test", after["fingerprint"], "approved draft", "request-key")
    assert caught.value.code == "idempotency_conflict"
    assert adapter.sent == []
