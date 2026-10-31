"""In-process MCP protocol checks for the stdio servers.

These pin the tool surface that MCP clients see (names, argument schemas,
structured output) so that ``mcp`` SDK upgrades cannot change it silently.
The client session validates every structured result against the tool's
output schema, which is the path the SDK changed in 1.30.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import make_service
from mcp.client.session import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session

from hermes_kakao_mcp import calendar_mcp, server
from hermes_kakao_mcp.calendar_mcp import CalendarGateway, CalendarSettings

KAKAO_TOOL_ARGS = {
    "kakao_health": ([], []),
    "kakao_allowed_rooms": ([], []),
    "kakao_read_room": (["room_id"], ["max_messages"]),
    "kakao_observe_room": (["room_id"], []),
    "kakao_poll_events": ([], ["room_id", "limit"]),
    "kakao_poll_schedule_candidates": ([], ["limit"]),
    "kakao_get_schedule_candidate": (["candidate_id"], []),
    "kakao_update_schedule_candidate": (["candidate_id", "status"], ["calendar_event_id"]),
    "kakao_prepare_reply": (["room_id", "expected_fingerprint", "text"], ["idempotency_key"]),
    "kakao_commit_reply": (["operation_id", "confirmation_code"], []),
    "kakao_operation_status": (["operation_id"], []),
}

CALENDAR_TOOL_ARGS = {
    "calendar_health": ([], []),
    "calendar_list_busy": (["start", "end"], ["all_day"]),
    "calendar_get_event": (["calendar_event_id"], []),
    "calendar_create_candidate_event": (["candidate_id", "summary", "start", "end"], ["all_day"]),
}


def run_session(app: Any, body: Callable[[ClientSession], Awaitable[Any]]) -> Any:
    async def runner() -> Any:
        async with create_connected_server_and_client_session(app) as session:
            return await body(session)

    return asyncio.run(runner())


def structured(result: Any) -> dict[str, Any]:
    assert result.isError is False
    assert isinstance(result.structuredContent, dict)
    return result.structuredContent


@pytest.fixture
def kakao(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service, adapter = make_service(tmp_path)
    monkeypatch.setattr(server, "get_service", lambda: service)
    yield adapter
    service.state.close()


@pytest.mark.parametrize(
    ("app", "expected"),
    [(server.app, KAKAO_TOOL_ARGS), (calendar_mcp.app, CALENDAR_TOOL_ARGS)],
    ids=["kakao", "calendar"],
)
def test_tool_surface_is_stable(app: Any, expected: dict[str, tuple[list[str], list[str]]]) -> None:
    tools = run_session(app, lambda session: session.list_tools()).tools

    assert sorted(tool.name for tool in tools) == sorted(expected)
    for tool in tools:
        required, optional = expected[tool.name]
        schema = tool.inputSchema
        assert schema["type"] == "object"
        assert sorted(schema.get("required", [])) == sorted(required), tool.name
        assert sorted(schema["properties"]) == sorted(required + optional), tool.name
        assert tool.description
        assert tool.outputSchema is not None
        assert tool.outputSchema["type"] == "object"
        assert "$ref" not in tool.outputSchema


def test_kakao_tools_return_validated_structured_content(kakao) -> None:
    async def body(session: ClientSession) -> dict[str, Any]:
        await session.list_tools()
        return {
            "health": structured(await session.call_tool("kakao_health", {})),
            "rooms": structured(await session.call_tool("kakao_allowed_rooms", {})),
            "read": structured(
                await session.call_tool("kakao_read_room", {"room_id": "self-test", "max_messages": 5})
            ),
        }

    results = run_session(server.app, body)

    assert results["health"]["ok"] is True
    assert results["health"]["send_enabled"] is False
    assert [room["room_id"] for room in results["rooms"]["rooms"]] == ["self-test"]
    assert results["read"]["ok"] is True
    assert [message["text"] for message in results["read"]["messages"]] == ["안녕", "반가워"]
    assert "테스트방" not in repr(results)


def test_kakao_send_path_stays_fail_closed_over_mcp(kakao) -> None:
    async def body(session: ClientSession) -> tuple[dict[str, Any], dict[str, Any]]:
        await session.list_tools()
        read = structured(await session.call_tool("kakao_read_room", {"room_id": "self-test"}))
        prepared = structured(
            await session.call_tool(
                "kakao_prepare_reply",
                {"room_id": "self-test", "expected_fingerprint": read["fingerprint"], "text": "확인"},
            )
        )
        committed = structured(
            await session.call_tool(
                "kakao_commit_reply",
                {
                    "operation_id": prepared["operation_id"],
                    "confirmation_code": prepared["confirmation_code"],
                },
            )
        )
        return prepared, committed

    prepared, committed = run_session(server.app, body)

    assert prepared["ok"] is True
    assert committed == {
        "ok": False,
        "error": "sending_disabled",
        "message": "Sending is disabled in local configuration; no KakaoTalk input occurred",
    }
    assert kakao.sent == []


def test_kakao_invalid_arguments_are_rejected_by_the_sdk(kakao) -> None:
    async def body(session: ClientSession) -> Any:
        await session.list_tools()
        return await session.call_tool("kakao_read_room", {})

    result = run_session(server.app, body)

    assert result.isError is True
    assert kakao.sent == []


def test_calendar_health_over_mcp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = CalendarGateway(
        CalendarSettings(token_path=tmp_path / "missing-token.json"),
        service_factory=lambda _: pytest.fail("health must not build a calendar client"),
    )
    monkeypatch.setattr(calendar_mcp, "get_gateway", lambda: gateway)

    async def body(session: ClientSession) -> dict[str, Any]:
        await session.list_tools()
        return structured(await session.call_tool("calendar_health", {}))

    health = run_session(calendar_mcp.app, body)

    assert health["ok"] is True
    assert health["token_present"] is False
