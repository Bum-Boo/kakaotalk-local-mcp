from __future__ import annotations

from datetime import datetime
from threading import RLock
from typing import Any

from ..errors import AdapterError
from .base import KakaoAdapter


class MockKakaoAdapter(KakaoAdapter):
    """Deterministic adapter used for tests and MCP protocol smoke checks."""

    def __init__(self, transcripts: dict[str, str] | None = None, self_sender: str = "나") -> None:
        self._transcripts = dict(transcripts or {})
        self._self_sender = self_sender
        self._lock = RLock()
        self.sent: list[tuple[str, str]] = []

    def health(self) -> dict[str, Any]:
        return {"running": True, "adapter": "mock", "open_chat_count": len(self._transcripts)}

    def discover_room_titles(self) -> list[str]:
        with self._lock:
            return sorted(self._transcripts)

    def read_raw(self, exact_room_title: str) -> str:
        with self._lock:
            try:
                return self._transcripts[exact_room_title]
            except KeyError as exc:
                raise AdapterError(
                    "room_not_open",
                    "Allowed room is not available in the mock adapter",
                ) from exc

    def send_text(self, exact_room_title: str, text: str) -> None:
        with self._lock:
            if exact_room_title not in self._transcripts:
                raise AdapterError("room_not_open", "Allowed room is not open")
            marker = datetime.now().strftime("%H:%M")
            suffix = f"\n[{self._self_sender}] [{marker}] {text}"
            self._transcripts[exact_room_title] = self._transcripts[exact_room_title].rstrip() + suffix
            self.sent.append((exact_room_title, text))

    def set_transcript(self, exact_room_title: str, raw_text: str) -> None:
        with self._lock:
            self._transcripts[exact_room_title] = raw_text
