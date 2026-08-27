from __future__ import annotations

from pathlib import Path

from hermes_kakao_mcp.adapters.mock import MockKakaoAdapter
from hermes_kakao_mcp.config import RoomConfig, Settings
from hermes_kakao_mcp.service import KakaoService

SAMPLE = """[테스트방] [대화상대 2명]
2026년 8월 20일 목요일
[상대] [오전 10:00] 안녕
[나] [오전 10:01] 반가워
"""


def make_settings(
    tmp_path: Path,
    *,
    send_enabled: bool = False,
    my_name: str = "나",
    adapter: str = "mock",
    self_chat: bool = False,
    schedule_watch_enabled: bool = False,
) -> Settings:
    return Settings(
        config_path=tmp_path / "config.json",
        adapter=adapter,
        send_enabled=send_enabled,
        auto_reply_enabled=False,
        schedule_automation_enabled=False,
        max_message_chars=500,
        operation_ttl_seconds=60,
        readback_delay_seconds=0.0,
        watch_interval_seconds=3.0,
        event_retention_minutes=60,
        schedule_candidate_retention_minutes=10080,
        state_path=tmp_path / "state.sqlite3",
        rooms={
            "self-test": RoomConfig(
                room_id="self-test",
                title="테스트방",
                my_name=my_name,
                enabled=True,
                self_chat=self_chat,
                schedule_watch_enabled=schedule_watch_enabled,
            )
        },
        mock_transcripts={"테스트방": SAMPLE},
        webhook=None,
        backend_collector=None,
    )


def make_service(tmp_path: Path, *, send_enabled: bool = False) -> tuple[KakaoService, MockKakaoAdapter]:
    settings = make_settings(tmp_path, send_enabled=send_enabled)
    adapter = MockKakaoAdapter(settings.mock_transcripts, self_sender="나")
    return KakaoService(settings, adapter), adapter
