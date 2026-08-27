from __future__ import annotations

import hashlib
import json
import unicodedata

from .models import Message


def _normalized(value: str) -> str:
    value = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    return "\n".join(part.rstrip() for part in value.split("\n")).strip()


def message_digest(message: Message) -> str:
    payload = {
        "sender": _normalized(message.sender),
        "time": _normalized(message.time),
        "text": _normalized(message.text),
        "kind": message.kind,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def tail_digests(messages: tuple[Message, ...] | list[Message], limit: int = 20) -> list[str]:
    return [message_digest(message) for message in messages[-limit:]]


def snapshot_fingerprint(room_id: str, messages: tuple[Message, ...] | list[Message], limit: int = 20) -> str:
    payload = {"room_id": room_id, "tail": tail_digests(messages, limit)}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def text_hash(text: str) -> str:
    return hashlib.sha256(_normalized(text).encode("utf-8")).hexdigest()
