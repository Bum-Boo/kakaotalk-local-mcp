from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def smoke() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="hermes-kakao-mcp-") as directory:
        root = Path(directory)
        config = root / "config.json"
        config.write_text(
            json.dumps(
                {
                    "adapter": "mock",
                    "send_enabled": False,
                    "state_path": str(root / "state.sqlite3"),
                    "rooms": [
                        {"id": "self-test", "title": "테스트방", "my_name": "나"}
                    ],
                    "mock_transcripts": {
                        "테스트방": "[테스트방] [대화상대 2명]\n[상대] [10:00] 안녕"
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["HERMES_KAKAO_CONFIG"] = str(config)
        env["PYTHONUTF8"] = "1"
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "hermes_kakao_mcp.server"],
            env=env,
        )
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(tool.name for tool in tools.tools)
            required = {
                "kakao_health",
                "kakao_allowed_rooms",
                "kakao_read_room",
                "kakao_poll_schedule_candidates",
                "kakao_get_schedule_candidate",
                "kakao_update_schedule_candidate",
                "kakao_prepare_reply",
                "kakao_commit_reply",
            }
            if not required.issubset(names):
                raise RuntimeError(f"Missing tools: {sorted(required - set(names))}")
            health = await session.call_tool("kakao_health", {})
            if health.isError:
                raise RuntimeError("kakao_health returned an MCP error")
            blocked = await session.call_tool(
                "kakao_commit_reply",
                {"operation_id": "missing", "confirmation_code": "00000000"},
            )
            return {
                "tool_count": len(names),
                "tools": names,
                "health_is_error": bool(health.isError),
                "commit_transport_is_error": bool(blocked.isError),
            }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(smoke()), ensure_ascii=False, indent=2))
