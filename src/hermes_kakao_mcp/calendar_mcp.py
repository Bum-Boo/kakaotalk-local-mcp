from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
KOREA_TIMEZONE = "Asia/Seoul"
CANDIDATE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{8,128}$")
EVENT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{5,256}$")


class CalendarError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class CalendarSettings:
    token_path: Path
    calendar_id: str = "primary"
    timezone: str = KOREA_TIMEZONE

    @classmethod
    def from_environment(cls) -> CalendarSettings:
        profile_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser().resolve()
        return cls(token_path=profile_home / "google_token.json")


class CalendarGateway:
    """Primary-calendar boundary with no delete/update/attendee surface."""

    def __init__(
        self,
        settings: CalendarSettings,
        *,
        service_factory: Callable[[Path], Any] | None = None,
    ) -> None:
        self.settings = settings
        self._service_factory = service_factory or self._build_google_service
        self._service: Any | None = None

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "calendar_id": self.settings.calendar_id,
            "calendar_timezone": self.settings.timezone,
            "token_present": self.settings.token_path.is_file(),
            "write_surface": "create_only",
        }

    def list_busy(self, start: str, end: str, *, all_day: bool = False) -> dict[str, Any]:
        if not isinstance(all_day, bool):
            raise CalendarError("invalid_event_shape", "all_day must be true or false")
        if all_day:
            start_value, end_value = self._validate_all_day_range(start, end)
            query_start = f"{start_value}T00:00:00+09:00"
            query_end = f"{end_value}T00:00:00+09:00"
        else:
            start_value, end_value = self._validate_timed_range(start, end)
            query_start, query_end = start_value, end_value
        events = self._list_overlapping_events(query_start, query_end)
        return {
            "ok": True,
            "calendar_id": self.settings.calendar_id,
            "start": start_value,
            "end": end_value,
            "all_day": all_day,
            "busy": [self._busy_block(event) for event in events if self._is_busy(event)],
        }

    def get_event(self, calendar_event_id: str) -> dict[str, Any]:
        if not isinstance(calendar_event_id, str) or not EVENT_ID_PATTERN.fullmatch(calendar_event_id):
            raise CalendarError("invalid_event_id", "Calendar event id has an invalid shape")
        try:
            event = self._service_or_raise().events().get(
                calendarId=self.settings.calendar_id,
                eventId=calendar_event_id,
                fields="id,status,start,end,extendedProperties/private",
            ).execute()
        except CalendarError:
            raise
        except Exception as exc:
            raise CalendarError(
                "calendar_read_failed",
                "Could not read the requested calendar event",
            ) from exc
        return {"ok": True, **self._event_readback(event)}

    def create_event(
        self,
        *,
        candidate_id: str,
        summary: str,
        start: str,
        end: str,
        all_day: bool = False,
    ) -> dict[str, Any]:
        self._validate_candidate_id(candidate_id)
        title = self._validate_summary(summary)
        if not isinstance(all_day, bool):
            raise CalendarError("invalid_event_shape", "all_day must be true or false")
        if all_day:
            start_value, end_value = self._validate_all_day_range(start, end)
            conflict_start = f"{start_value}T00:00:00+09:00"
            conflict_end = f"{end_value}T00:00:00+09:00"
        else:
            start_value, end_value = self._validate_timed_range(start, end)
            conflict_start, conflict_end = start_value, end_value

        existing = self._find_by_candidate_id(candidate_id)
        if existing is not None:
            return {"ok": True, "created": False, **self._event_readback(existing)}

        conflicts = [
            event
            for event in self._list_overlapping_events(conflict_start, conflict_end)
            if self._is_busy(event)
        ]
        if conflicts:
            raise CalendarError("calendar_conflict", "A busy primary-calendar event overlaps this candidate")

        if all_day:
            body = {
                "summary": title,
                "start": {"date": start_value, "timeZone": self.settings.timezone},
                "end": {"date": end_value, "timeZone": self.settings.timezone},
                "extendedProperties": {"private": {"hermes_candidate_id": candidate_id}},
            }
        else:
            body = {
                "summary": title,
                "start": {"dateTime": start_value, "timeZone": self.settings.timezone},
                "end": {"dateTime": end_value, "timeZone": self.settings.timezone},
                "extendedProperties": {"private": {"hermes_candidate_id": candidate_id}},
            }

        try:
            created = self._service_or_raise().events().insert(
                calendarId=self.settings.calendar_id,
                body=body,
                sendUpdates="none",
            ).execute()
            event_id = created.get("id") if isinstance(created, dict) else None
            if not isinstance(event_id, str) or not EVENT_ID_PATTERN.fullmatch(event_id):
                raise CalendarError("calendar_readback_failed", "Calendar did not return a usable event id")
            readback = self._service_or_raise().events().get(
                calendarId=self.settings.calendar_id,
                eventId=event_id,
                fields="id,status,start,end,extendedProperties/private",
            ).execute()
        except CalendarError:
            raise
        except Exception as exc:
            raise CalendarError(
                "calendar_create_failed",
                "Could not create and read back the calendar event",
            ) from exc

        result = self._event_readback(readback)
        if result["calendar_event_id"] != event_id or result["status"] == "cancelled":
            raise CalendarError("calendar_readback_failed", "Created calendar event could not be verified")
        return {"ok": True, "created": True, **result}

    def _service_or_raise(self) -> Any:
        if self._service is not None:
            return self._service
        if not self.settings.token_path.is_file():
            raise CalendarError("calendar_auth_unavailable", "Primary calendar authentication is unavailable")
        try:
            self._service = self._service_factory(self.settings.token_path)
        except CalendarError:
            raise
        except Exception as exc:
            raise CalendarError(
                "calendar_auth_unavailable",
                "Primary calendar authentication is unavailable",
            ) from exc
        return self._service

    @staticmethod
    def _build_google_service(token_path: Path) -> Any:
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise CalendarError(
                "calendar_dependencies_unavailable",
                "Google Calendar client dependencies are unavailable",
            ) from exc
        credentials = Credentials.from_authorized_user_file(str(token_path), scopes=[CALENDAR_SCOPE])
        if not credentials.refresh_token:
            raise CalendarError("calendar_auth_unavailable", "Primary calendar authentication is unavailable")
        return build("calendar", "v3", credentials=credentials, cache_discovery=False)

    def _find_by_candidate_id(self, candidate_id: str) -> dict[str, Any] | None:
        try:
            results = self._service_or_raise().events().list(
                calendarId=self.settings.calendar_id,
                privateExtendedProperty=f"hermes_candidate_id={candidate_id}",
                showDeleted=False,
                singleEvents=True,
                maxResults=2,
                fields="items(id,status,start,end,extendedProperties/private)",
            ).execute()
        except CalendarError:
            raise
        except Exception as exc:
            raise CalendarError("calendar_read_failed", "Could not check calendar idempotency") from exc
        for event in results.get("items", []) if isinstance(results, dict) else []:
            if isinstance(event, dict) and event.get("status") != "cancelled":
                return event
        return None

    def _list_overlapping_events(self, start: str, end: str) -> list[dict[str, Any]]:
        try:
            results = self._service_or_raise().events().list(
                calendarId=self.settings.calendar_id,
                timeMin=start,
                timeMax=end,
                showDeleted=False,
                singleEvents=True,
                orderBy="startTime",
                maxResults=2500,
                fields="items(id,status,transparency,start,end,extendedProperties/private)",
            ).execute()
        except CalendarError:
            raise
        except Exception as exc:
            raise CalendarError("calendar_read_failed", "Could not read primary-calendar busy time") from exc
        items = results.get("items", []) if isinstance(results, dict) else []
        return [item for item in items if isinstance(item, dict)]

    @staticmethod
    def _is_busy(event: dict[str, Any]) -> bool:
        return event.get("status") != "cancelled" and event.get("transparency") != "transparent"

    @staticmethod
    def _boundary(value: Any) -> str:
        if not isinstance(value, dict):
            return ""
        date_time = value.get("dateTime")
        date_value = value.get("date")
        return date_time if isinstance(date_time, str) else date_value if isinstance(date_value, str) else ""

    def _busy_block(self, event: dict[str, Any]) -> dict[str, str]:
        return {"start": self._boundary(event.get("start")), "end": self._boundary(event.get("end"))}

    def _event_readback(self, event: Any) -> dict[str, str]:
        if not isinstance(event, dict):
            raise CalendarError("calendar_readback_failed", "Calendar event readback has an invalid shape")
        event_id = event.get("id")
        if not isinstance(event_id, str) or not EVENT_ID_PATTERN.fullmatch(event_id):
            raise CalendarError("calendar_readback_failed", "Calendar event readback has no usable event id")
        start = self._boundary(event.get("start"))
        end = self._boundary(event.get("end"))
        if not start or not end:
            raise CalendarError(
                "calendar_readback_failed",
                "Calendar event readback has no usable time range",
            )
        return {
            "calendar_event_id": event_id,
            "status": event.get("status") if isinstance(event.get("status"), str) else "",
            "start": start,
            "end": end,
        }

    @staticmethod
    def _validate_candidate_id(candidate_id: str) -> None:
        if not isinstance(candidate_id, str) or not CANDIDATE_ID_PATTERN.fullmatch(candidate_id):
            raise CalendarError("invalid_candidate_id", "Candidate id has an invalid shape")

    @staticmethod
    def _validate_summary(summary: str) -> str:
        if not isinstance(summary, str):
            raise CalendarError("invalid_event_shape", "Calendar event summary must be text")
        normalized = " ".join(summary.split())
        if not 1 <= len(normalized) <= 160:
            raise CalendarError("invalid_event_shape", "Calendar event summary must be 1 to 160 characters")
        return normalized

    @staticmethod
    def _validate_timed_range(start: str, end: str) -> tuple[str, str]:
        start_dt = CalendarGateway._parse_offset_datetime(start)
        end_dt = CalendarGateway._parse_offset_datetime(end)
        if end_dt <= start_dt or end_dt - start_dt > timedelta(days=2):
            raise CalendarError(
                "invalid_event_range",
                "Timed calendar event must end after it starts within two days",
            )
        return start_dt.isoformat(), end_dt.isoformat()

    @staticmethod
    def _parse_offset_datetime(value: str) -> datetime:
        if not isinstance(value, str):
            raise CalendarError("invalid_event_range", "Timed calendar value must be text")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CalendarError("invalid_event_range", "Timed calendar value must be ISO 8601") from exc
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(hours=9):
            raise CalendarError(
                "invalid_event_range",
                "Timed calendar values must use Korea Standard Time (+09:00)",
            )
        return parsed

    @staticmethod
    def _validate_all_day_range(start: str, end: str) -> tuple[str, str]:
        try:
            start_date = date.fromisoformat(start)
            end_date = date.fromisoformat(end)
        except (TypeError, ValueError) as exc:
            raise CalendarError("invalid_event_range", "All-day values must use YYYY-MM-DD") from exc
        if end_date <= start_date or end_date - start_date > timedelta(days=31):
            raise CalendarError("invalid_event_range", "All-day calendar event must be one to 31 days")
        return start_date.isoformat(), end_date.isoformat()


app = FastMCP(
    "schedule-calendar",
    instructions=(
        "Primary Google Calendar boundary. Read busy blocks, create idempotent candidate-bound events, "
        "and read them back. Never use it to update, delete, invite attendees, or expose event details."
    ),
)


@lru_cache(maxsize=1)
def get_gateway() -> CalendarGateway:
    return CalendarGateway(CalendarSettings.from_environment())


def _call(method: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        return method(*args, **kwargs)
    except CalendarError as exc:
        return exc.to_dict()
    except Exception:
        return {
            "ok": False,
            "error": "calendar_internal_error",
            "message": "Unexpected primary-calendar boundary error; no sensitive details were exposed",
        }


@app.tool()
def calendar_health() -> dict[str, Any]:
    """Report only primary-calendar boundary readiness; never read events."""
    return _call(get_gateway().health)


@app.tool()
def calendar_list_busy(start: str, end: str, all_day: bool = False) -> dict[str, Any]:
    """Return busy time blocks only, without existing event titles, descriptions, attendees, or links.

    Use ``all_day=true`` only with YYYY-MM-DD start/end values; it queries the full
    Korea-time day range without exposing existing event content.
    """
    return _call(get_gateway().list_busy, start, end, all_day=all_day)


@app.tool()
def calendar_get_event(calendar_event_id: str) -> dict[str, Any]:
    """Read a just-created event's id/status/time range for registration readback only."""
    return _call(get_gateway().get_event, calendar_event_id)


@app.tool()
def calendar_create_candidate_event(
    candidate_id: str,
    summary: str,
    start: str,
    end: str,
    all_day: bool = False,
) -> dict[str, Any]:
    """Create one primary-calendar event bound to an opaque schedule candidate.

    Use only after explicit facts, busy-time conflict check, and the user's approved auto-registration policy.
    The same candidate id returns the original event instead of creating a duplicate.
    """
    return _call(
        get_gateway().create_event,
        candidate_id=candidate_id,
        summary=summary,
        start=start,
        end=end,
        all_day=all_day,
    )


def main() -> None:
    app.run(transport="stdio")


if __name__ == "__main__":
    main()
