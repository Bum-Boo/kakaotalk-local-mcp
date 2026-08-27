from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import load_settings
from .errors import KakaoBridgeError
from .service import KakaoService, build_service

app = FastMCP(
    "hermes-kakao-mcp",
    instructions=(
        "Fail-closed KakaoTalk PC bridge. Use only room_id values from kakao_allowed_rooms. "
        "Never call kakao_commit_reply without explicit current-turn user approval."
    ),
)


@lru_cache(maxsize=1)
def get_service() -> KakaoService:
    return build_service(load_settings())


def _call(method: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        return method(*args, **kwargs)
    except KakaoBridgeError as exc:
        return exc.to_dict()
    except Exception:
        return {
            "ok": False,
            "error": "internal_error",
            "message": "Unexpected local bridge error; no sensitive details were exposed",
        }


@app.tool()
def kakao_health() -> dict[str, Any]:
    """Check health and approved source aliases without reading conversation content."""
    return _call(get_service().health)


@app.tool()
def kakao_allowed_rooms() -> dict[str, Any]:
    """List only opaque local room IDs from the fail-closed allowlist; titles are never exposed."""
    return _call(get_service().allowed_rooms)


@app.tool()
def kakao_read_room(room_id: str, max_messages: int = 10) -> dict[str, Any]:
    """Read up to 50 recent messages from one exact allowlisted room.

    On Windows a closed room is searched and opened through the visible KakaoTalk UI,
    title-verified, and closed after the stable read. The previous foreground and text
    clipboard are restored. Non-text clipboards fail closed without mutation.
    """
    return _call(get_service().read_room, room_id, max_messages)


@app.tool()
def kakao_observe_room(room_id: str) -> dict[str, Any]:
    """Update a local hash baseline and enqueue only new incoming text for one allowed room.

    The first call creates a baseline and never replays old messages. This function does not call an AI.
    """
    return _call(get_service().observe_room, room_id)


@app.tool()
def kakao_poll_events(room_id: str | None = None, limit: int = 20) -> dict[str, Any]:
    """Drain watcher events already stored locally; this call does not touch the KakaoTalk UI."""
    return _call(get_service().poll_events, room_id, limit)


@app.tool()
def kakao_poll_schedule_candidates(limit: int = 20) -> dict[str, Any]:
    """Read pending local schedule candidates without touching KakaoTalk or a calendar.

    Candidates contain only an opaque room id, source fingerprint, and bounded
    context. This tool never replies to a KakaoTalk source chat.
    """
    return _call(get_service().poll_schedule_candidates, limit)


@app.tool()
def kakao_get_schedule_candidate(candidate_id: str) -> dict[str, Any]:
    """Read one opaque schedule candidate, including a user-choice hold.

    Only use a candidate ID already presented by the schedule workflow. This
    does not enumerate rooms or read the KakaoTalk UI.
    """
    return _call(get_service().get_schedule_candidate, candidate_id)


@app.tool()
def kakao_update_schedule_candidate(
    candidate_id: str,
    status: str,
    calendar_event_id: str = "",
) -> dict[str, Any]:
    """Record a scheduling decision for one candidate.

    Use ``registered`` only after the calendar owner performed a successful
    write and read back its real calendar event id. This tool cannot send any
    KakaoTalk message.
    """
    return _call(
        get_service().update_schedule_candidate,
        candidate_id,
        status,
        calendar_event_id,
    )


@app.tool()
def kakao_prepare_reply(
    room_id: str,
    expected_fingerprint: str,
    text: str,
    idempotency_key: str = "",
) -> dict[str, Any]:
    """Prepare an expiring one-time reply after re-reading and matching the transcript hash.

    Preparation never sends a message. It returns a confirmation code bound to the exact draft.
    """
    return _call(
        get_service().prepare_reply,
        room_id,
        expected_fingerprint,
        text,
        idempotency_key,
    )


@app.tool()
def kakao_commit_reply(operation_id: str, confirmation_code: str) -> dict[str, Any]:
    """Send one prepared reply exactly once.

    HARD GATE: call only after explicit current-turn user approval. Local send_enabled must also be true.
    The transcript is rechecked before input and read back after sending; automatic retry is forbidden.
    """
    return _call(get_service().commit_reply, operation_id, confirmation_code)


@app.tool()
def kakao_operation_status(operation_id: str) -> dict[str, Any]:
    """Return non-content status for one in-memory prepared operation."""
    return _call(get_service().operation_status, operation_id)


def main() -> None:
    app.run(transport="stdio")


if __name__ == "__main__":
    main()
