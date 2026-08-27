from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Message

# The gate forwards substantive incoming text from explicitly watched rooms to
# the scheduling owner. It never turns natural language into a calendar event
# and never invokes a model.
_DATE_PATTERNS = (
    re.compile(r"\b\d{4}[./-]\s*\d{1,2}[./-]\s*\d{1,2}\b"),
    re.compile(r"\b\d{1,2}\s*월\s*\d{1,2}\s*일?\b"),
    re.compile(r"\b(?:오늘|내일|모레|이번\s*주|다음\s*주)\b"),
    re.compile(r"\b(?:월|화|수|목|금|토|일)요일\b"),
)
_TIME_PATTERNS = (
    re.compile(r"\b(?:오전|오후)\s*\d{1,2}\s*시(?:\s*\d{1,2}\s*분)?\b"),
    re.compile(r"\b(?:AM|PM|am|pm)\s*\d{1,2}(?::\d{2})?\b"),
    re.compile(r"\b\d{1,2}:\d{2}\b"),
)
_KEYWORDS = (
    "회의",
    "수업",
    "세미나",
    "면담",
    "제출",
    "마감",
    "신청",
    "근무",
    "보강",
    "휴강",
    "행사",
    "오리엔테이션",
    "시험",
    "강의",
    "조교",
    "일정",
    "예약",
)
_GREETING_ONLY_PATTERN = re.compile(
    r"^(?:안녕(?:하세요|하십니까)?|반갑습니다|반가워요)[\s!?.~ㅋㅎ]*$"
)
_CHOICE_PATTERN = re.compile(
    r"(?:또는|혹은|중\s*(?:가능|선택)|가능한\s*시간|참석\s*(?:가능|여부)|신청\s*(?:가능|여부)|원하(?:는|시면))"
)


@dataclass(frozen=True, slots=True)
class ScheduleCandidateDetection:
    confidence: str
    date_signals: tuple[str, ...]
    time_signals: tuple[str, ...]
    keywords: tuple[str, ...]
    needs_user_choice: bool


def _matches(patterns: tuple[re.Pattern[str], ...], text: str) -> tuple[str, ...]:
    found: list[str] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            value = match.group(0).strip()
            if value and value not in found:
                found.append(value)
    return tuple(found)


def detect_schedule_candidate(message: Message) -> ScheduleCandidateDetection | None:
    """Return a bounded local schedule signal, or ``None`` for ordinary chat.

    A candidate is not a calendar decision. Ambiguous time/attendance wording
    stays ``needs_review`` so the Schedule Manager can ask the user instead of
    assuming intent.
    """
    if message.kind != "text":
        return None
    text = message.text.strip()
    if not text or _GREETING_ONLY_PATTERN.fullmatch(text):
        return None

    date_signals = _matches(_DATE_PATTERNS, text)
    time_signals = _matches(_TIME_PATTERNS, text)
    keywords = tuple(keyword for keyword in _KEYWORDS if keyword in text)
    needs_user_choice = bool(_CHOICE_PATTERN.search(text)) or len(time_signals) > 1
    deadline_like = bool({"마감", "제출", "신청"}.intersection(keywords))
    confidence = (
        "high"
        if keywords and not needs_user_choice and (bool(time_signals) or deadline_like)
        else "needs_review"
    )
    return ScheduleCandidateDetection(
        confidence=confidence,
        date_signals=date_signals,
        time_signals=time_signals,
        keywords=keywords,
        needs_user_choice=needs_user_choice,
    )
