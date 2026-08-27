from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hermes_kakao_mcp.calendar_mcp import CalendarError, CalendarGateway, CalendarSettings


class Reply:
    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value

    def execute(self) -> dict[str, Any]:
        return self.value


class FakeEvents:
    def __init__(
        self,
        *,
        list_results: list[dict[str, Any]],
        insert_result: dict[str, Any] | None = None,
        get_results: list[dict[str, Any]] | None = None,
    ) -> None:
        self.list_results = list(list_results)
        self.insert_result = insert_result or {"id": "event-12345"}
        self.get_results = list(get_results or [])
        self.list_calls: list[dict[str, Any]] = []
        self.insert_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> Reply:
        self.list_calls.append(kwargs)
        return Reply(self.list_results.pop(0))

    def insert(self, **kwargs: Any) -> Reply:
        self.insert_calls.append(kwargs)
        return Reply(self.insert_result)

    def get(self, **kwargs: Any) -> Reply:
        self.get_calls.append(kwargs)
        return Reply(self.get_results.pop(0))


class FakeService:
    def __init__(self, events: FakeEvents) -> None:
        self._events = events

    def events(self) -> FakeEvents:
        return self._events


def event(
    event_id: str = "event-12345",
    *,
    status: str = "confirmed",
    transparency: str | None = None,
    start: str = "2026-09-03T10:00:00+09:00",
    end: str = "2026-09-03T11:00:00+09:00",
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": event_id,
        "status": status,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    if transparency is not None:
        value["transparency"] = transparency
    return value


def make_gateway(tmp_path: Path, events: FakeEvents) -> CalendarGateway:
    token_path = tmp_path / "google_token.json"
    token_path.write_text("{}", encoding="utf-8")
    return CalendarGateway(
        CalendarSettings(token_path=token_path),
        service_factory=lambda _: FakeService(events),
    )


def test_list_busy_returns_time_blocks_without_existing_event_content(tmp_path) -> None:
    events = FakeEvents(
        list_results=[
            {
                "items": [
                    event(),
                    event("transparent-123", transparency="transparent"),
                    event("cancelled-123", status="cancelled"),
                ]
            }
        ]
    )

    result = make_gateway(tmp_path, events).list_busy(
        "2026-09-03T09:00:00+09:00",
        "2026-09-03T12:00:00+09:00",
    )

    assert result["busy"] == [{"start": "2026-09-03T10:00:00+09:00", "end": "2026-09-03T11:00:00+09:00"}]
    assert "summary" not in events.list_calls[0]["fields"]
    assert "description" not in events.list_calls[0]["fields"]
    assert "attendees" not in events.list_calls[0]["fields"]


def test_list_busy_supports_explicit_all_day_ranges(tmp_path) -> None:
    events = FakeEvents(list_results=[{"items": []}])

    result = make_gateway(tmp_path, events).list_busy(
        "2026-09-03",
        "2026-09-04",
        all_day=True,
    )

    assert result["all_day"] is True
    assert result["start"] == "2026-09-03"
    assert result["end"] == "2026-09-04"
    assert events.list_calls[0]["timeMin"] == "2026-09-03T00:00:00+09:00"
    assert events.list_calls[0]["timeMax"] == "2026-09-04T00:00:00+09:00"


def test_create_returns_existing_candidate_event_without_another_insert(tmp_path) -> None:
    existing = event()
    events = FakeEvents(list_results=[{"items": [existing]}])

    result = make_gateway(tmp_path, events).create_event(
        candidate_id="candidate-123",
        summary="연구 회의",
        start="2026-09-03T10:00:00+09:00",
        end="2026-09-03T11:00:00+09:00",
    )

    assert result == {
        "ok": True,
        "created": False,
        "calendar_event_id": "event-12345",
        "status": "confirmed",
        "start": "2026-09-03T10:00:00+09:00",
        "end": "2026-09-03T11:00:00+09:00",
    }
    assert events.insert_calls == []
    assert events.list_calls[0]["privateExtendedProperty"] == "hermes_candidate_id=candidate-123"


def test_create_fails_closed_when_a_busy_event_overlaps(tmp_path) -> None:
    events = FakeEvents(list_results=[{"items": []}, {"items": [event()]}])
    gateway = make_gateway(tmp_path, events)

    with pytest.raises(CalendarError, match="overlaps") as error:
        gateway.create_event(
            candidate_id="candidate-123",
            summary="연구 회의",
            start="2026-09-03T10:00:00+09:00",
            end="2026-09-03T11:00:00+09:00",
        )

    assert error.value.code == "calendar_conflict"
    assert events.insert_calls == []


def test_create_is_candidate_bound_sends_no_updates_and_reads_back(tmp_path) -> None:
    readback = event()
    events = FakeEvents(
        list_results=[{"items": []}, {"items": []}],
        insert_result={"id": "event-12345"},
        get_results=[readback],
    )

    result = make_gateway(tmp_path, events).create_event(
        candidate_id="candidate-123",
        summary="  연구   회의  ",
        start="2026-09-03T10:00:00+09:00",
        end="2026-09-03T11:00:00+09:00",
    )

    assert result["created"] is True
    assert result["calendar_event_id"] == "event-12345"
    assert events.insert_calls[0]["calendarId"] == "primary"
    assert events.insert_calls[0]["sendUpdates"] == "none"
    assert events.insert_calls[0]["body"] == {
        "summary": "연구 회의",
        "start": {"dateTime": "2026-09-03T10:00:00+09:00", "timeZone": "Asia/Seoul"},
        "end": {"dateTime": "2026-09-03T11:00:00+09:00", "timeZone": "Asia/Seoul"},
        "extendedProperties": {"private": {"hermes_candidate_id": "candidate-123"}},
    }
    assert events.get_calls[0]["eventId"] == "event-12345"


def test_create_all_day_uses_korea_time_boundaries_for_conflict_check(tmp_path) -> None:
    events = FakeEvents(
        list_results=[{"items": []}, {"items": []}],
        get_results=[
            {
                "id": "event-12345",
                "status": "confirmed",
                "start": {"date": "2026-09-03"},
                "end": {"date": "2026-09-04"},
            }
        ],
    )

    result = make_gateway(tmp_path, events).create_event(
        candidate_id="candidate-123",
        summary="학사 일정",
        start="2026-09-03",
        end="2026-09-04",
        all_day=True,
    )

    assert result["created"] is True
    assert events.list_calls[1]["timeMin"] == "2026-09-03T00:00:00+09:00"
    assert events.list_calls[1]["timeMax"] == "2026-09-04T00:00:00+09:00"
    assert events.insert_calls[0]["body"]["start"] == {"date": "2026-09-03", "timeZone": "Asia/Seoul"}


def test_invalid_time_range_fails_before_any_calendar_call(tmp_path) -> None:
    events = FakeEvents(list_results=[])
    gateway = make_gateway(tmp_path, events)

    with pytest.raises(CalendarError, match="Korea Standard Time"):
        gateway.create_event(
            candidate_id="candidate-123",
            summary="연구 회의",
            start="2026-09-03T10:00:00Z",
            end="2026-09-03T11:00:00Z",
        )

    assert events.list_calls == []
    assert events.insert_calls == []
