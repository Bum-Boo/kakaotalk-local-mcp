from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Message:
    sender: str
    time: str
    text: str
    kind: str = "text"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ChatSnapshot:
    room_title: str | None
    member_count: int | None
    messages: tuple[Message, ...]

    def to_dict(self, *, max_messages: int | None = None) -> dict[str, Any]:
        messages = self.messages
        if max_messages is not None:
            messages = messages[-max_messages:]
        return {
            "room_title": self.room_title,
            "member_count": self.member_count,
            "messages": [message.to_dict() for message in messages],
        }
