from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


class KakaoAdapter(ABC):
    @abstractmethod
    def health(self) -> dict[str, Any]:
        """Return non-sensitive adapter and application health data."""

    @contextmanager
    def room_session(self, exact_room_title: str) -> Iterator[None]:
        """Keep one room available across a bounded stable-snapshot read."""
        del exact_room_title
        yield

    @abstractmethod
    def discover_room_titles(self) -> list[str]:
        """Discover locally visible room titles without exposing message content."""

    @abstractmethod
    def read_raw(self, exact_room_title: str) -> str:
        """Read the visible transcript from one exact room title."""

    @abstractmethod
    def send_text(self, exact_room_title: str, text: str) -> None:
        """Send one text message to one already-open exact room title."""
