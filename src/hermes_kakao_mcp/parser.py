from __future__ import annotations

import re
from typing import Final

from .models import ChatSnapshot, Message

MESSAGE_PATTERN: Final = re.compile(
    r"^\[(.+?)\]\s*\[(?:(오전|오후|AM|PM)\s*)?(\d{1,2}:\d{2})\]\s*(.*)$",
    re.IGNORECASE,
)
HEADER_PATTERNS: Final = (
    re.compile(r"^\[(.+?)\]\s*\[대화상대\s*(\d+)\s*명?\]?$"),
    re.compile(r"^\[(.+?)\]\s*\[(?:Chat participants|Participants)\s*(\d+)\]?$", re.I),
)
KOREAN_DATE_PATTERN: Final = re.compile(
    r"^-*\s*\d{4}년\s*\d{1,2}월\s*\d{1,2}일(?:\s*\S+요일)?\s*-*$"
)
ENGLISH_DATE_PATTERN: Final = re.compile(
    r"^-*\s*(?:\w+\s+)?\d{1,2}\s+\w+\s+\d{4}\s*-*$",
    re.I,
)
SELF_CHAT_UI_LINES: Final = {"지난 대화 보기", "View previous chats"}


def _kind(text: str) -> str:
    compact = text.strip()
    if compact in {"사진", "Photo"}:
        return "photo"
    if compact in {"동영상", "Video"}:
        return "video"
    if compact.startswith(("파일:", "File:")):
        return "file"
    return "text"


def _is_date_separator(line: str) -> bool:
    return bool(KOREAN_DATE_PATTERN.match(line) or ENGLISH_DATE_PATTERN.match(line))


def _parse_headerless_blocks(raw_text: str, sender: str) -> list[Message]:
    normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    messages: list[Message] = []
    for block in re.split(r"\n\s*\n+", normalized):
        lines: list[str] = []
        for original in block.split("\n"):
            line = original.strip()
            if not line or _is_date_separator(line) or line in SELF_CHAT_UI_LINES:
                continue
            if any(pattern.match(line) for pattern in HEADER_PATTERNS):
                continue
            lines.append(line)
        text = "\n".join(lines).strip()
        if text:
            messages.append(Message(sender=sender, time="", text=text, kind=_kind(text)))
    return messages


def parse_chat_text(raw_text: str, *, fallback_sender: str | None = None) -> ChatSnapshot:
    """Parse the text KakaoTalk places on the clipboard after Ctrl+A/Ctrl+C."""
    if not raw_text or not raw_text.strip():
        return ChatSnapshot(room_title=None, member_count=None, messages=())

    room_title: str | None = None
    member_count: int | None = None
    messages: list[Message] = []
    current: dict[str, str] | None = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        text = current["text"].strip()
        messages.append(
            Message(
                sender=current["sender"].strip(),
                time=current["time"].strip(),
                text=text,
                kind=_kind(text),
            )
        )
        current = None

    for original in raw_text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = original.strip()
        if not line or _is_date_separator(line):
            continue

        header_match = next((pattern.match(line) for pattern in HEADER_PATTERNS if pattern.match(line)), None)
        if header_match:
            room_title = header_match.group(1).strip()
            member_count = int(header_match.group(2))
            continue

        match = MESSAGE_PATTERN.match(line)
        if match:
            flush()
            marker = (match.group(2) or "").upper()
            clock = match.group(3)
            current = {
                "sender": match.group(1),
                "time": f"{marker} {clock}".strip(),
                "text": match.group(4),
            }
        elif current is not None:
            current["text"] += "\n" + line

    flush()
    if not messages and fallback_sender and fallback_sender.strip():
        messages = _parse_headerless_blocks(raw_text, fallback_sender.strip())
    return ChatSnapshot(room_title=room_title, member_count=member_count, messages=tuple(messages))
