from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

WINDOWS_CMD = "/mnt/c/Windows/System32/cmd.exe"
WINDOWS_ROOT = os.environ.get("KAKAO_MCP_WINDOWS_ROOT", r"C:\path\to\kakaotalk-local-mcp")
WINDOWS_BRIDGE = WINDOWS_ROOT + r"\scripts\run-mcp.cmd"


def decode_result(result: object) -> dict[str, object]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        if isinstance(structured.get("result"), dict):
            return structured["result"]
        return structured
    for item in getattr(result, "content", []):
        text = getattr(item, "text", None)
        if isinstance(text, str):
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                return decoded
    raise RuntimeError("MCP result did not contain a JSON object")


async def smoke() -> dict[str, object]:
    if not Path(WINDOWS_CMD).is_file():
        raise RuntimeError("Windows cmd.exe is unavailable from WSL")
    parameters = StdioServerParameters(
        command=WINDOWS_CMD,
        args=["/d", "/s", "/c", WINDOWS_BRIDGE],
    )
    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        names = sorted(tool.name for tool in tools.tools)
        health = decode_result(await session.call_tool("kakao_health", {}))
        candidates = decode_result(await session.call_tool("kakao_poll_schedule_candidates", {}))
        if len(names) != 11:
            raise RuntimeError(f"Expected 11 tools, got {len(names)}")
        if "kakao_get_schedule_candidate" not in names:
            raise RuntimeError("Candidate hold lookup tool is missing")
        if health.get("running") is not True or health.get("adapter") != "win32":
            raise RuntimeError(f"Unexpected live health: {health}")
        if health.get("auto_reply_enabled") is not False:
            raise RuntimeError(f"Automatic reply must stay disabled: {health}")
        if not isinstance(candidates.get("candidate_count"), int):
            raise RuntimeError(f"Candidate queue result is invalid: {candidates}")
        return {
            "tool_count": len(names),
            "adapter": health.get("adapter"),
            "kakaotalk_running": health.get("running"),
            "allowed_room_count": health.get("allowed_room_count"),
            "send_enabled": health.get("send_enabled"),
            "auto_reply_enabled": health.get("auto_reply_enabled"),
            "pending_schedule_candidate_count": candidates.get("candidate_count"),
        }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(smoke()), ensure_ascii=False, indent=2))
