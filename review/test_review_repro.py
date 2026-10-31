"""Synthetic reproductions for docs/cloud-20261031-review.md.

All data is synthetic. No KakaoTalk UI, real account, or third-party network is used;
the webhook check only binds 127.0.0.1 listeners.

Each test asserts the CURRENT (defective) behavior so the owner can re-run it.
When a fix lands, the matching test is expected to fail and should be inverted
or moved into tests/.

Run:  pytest review -q -p no:cacheprovider
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from hermes_kakao_mcp.adapters.mock import MockKakaoAdapter
from hermes_kakao_mcp.config import RoomConfig, Settings, WebhookConfig
from hermes_kakao_mcp.errors import AdapterError, ApprovalError
from hermes_kakao_mcp.service import KakaoService
from hermes_kakao_mcp.webhook import deliver_event

ROOM_A = "[가상방A] [대화상대 2명]\n[상대A] [오전 10:00] 안녕A\n"
ROOM_B = "[가상방B] [대화상대 2명]\n[상대B] [오전 10:00] 안녕B\n"


def _settings(tmp_path, **overrides) -> Settings:
    values = dict(
        config_path=tmp_path / "config.json",
        adapter="mock",
        send_enabled=True,
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
            "room-a": RoomConfig(room_id="room-a", title="가상방A", my_name="나"),
            "room-b": RoomConfig(room_id="room-b", title="가상방B", my_name="나"),
        },
        mock_transcripts={"가상방A": ROOM_A, "가상방B": ROOM_B},
        webhook=None,
        backend_collector=None,
    )
    values.update(overrides)
    return Settings(**values)


# R1 -------------------------------------------------------------------------
def test_r1_reused_idempotency_key_returns_other_room_operation(tmp_path) -> None:
    """prepare(room-b, "Y", key=K) silently returns the earlier room-a/"X" operation."""
    settings = _settings(tmp_path)
    adapter = MockKakaoAdapter(settings.mock_transcripts, self_sender="나")
    service = KakaoService(settings, adapter)

    fp_a = service.read_room("room-a")["fingerprint"]
    fp_b = service.read_room("room-b")["fingerprint"]
    first = service.prepare_reply("room-a", fp_a, "A방 초안 X", "shared-key")
    second = service.prepare_reply("room-b", fp_b, "B방 초안 Y", "shared-key")

    # Observed: same operation, still bound to room-a and text X.
    assert second["operation_id"] == first["operation_id"]
    assert second["room_id"] == "room-a"

    # The user approved "Y to room-b", but committing the returned op sends X to room-a.
    service.commit_reply(second["operation_id"], second["confirmation_code"])
    assert adapter.sent == [("가상방A", "A방 초안 X")]


# R2 -------------------------------------------------------------------------
def test_r2_confirmation_code_is_returned_to_the_caller(tmp_path) -> None:
    """The 'approval' secret is handed to the same MCP caller that commits."""
    settings = _settings(tmp_path)
    adapter = MockKakaoAdapter(settings.mock_transcripts, self_sender="나")
    service = KakaoService(settings, adapter)

    fp = service.read_room("room-a")["fingerprint"]
    prepared = service.prepare_reply("room-a", fp, "사용자 승인 없이")
    assert "confirmation_code" in prepared
    # No human step is needed: the caller can commit immediately.
    result = service.commit_reply(prepared["operation_id"], prepared["confirmation_code"])
    assert result["status"] == "sent_verified"
    assert adapter.sent == [("가상방A", "사용자 승인 없이")]


# R3 -------------------------------------------------------------------------
class AutoOpenAdapter(MockKakaoAdapter):
    """Mimics Win32KakaoAdapter: a closed room is opened by room_session and closed after."""

    def __init__(self, transcripts: dict[str, str]) -> None:
        super().__init__(transcripts, self_sender="나")
        self.open_titles: set[str] = set()

    @contextmanager
    def room_session(self, exact_room_title: str):
        opened_here = exact_room_title not in self.open_titles
        self.open_titles.add(exact_room_title)
        try:
            yield
        finally:
            if opened_here:
                self.open_titles.discard(exact_room_title)

    def read_raw(self, exact_room_title: str) -> str:
        if exact_room_title not in self.open_titles:
            raise AdapterError("room_not_open", "synthetic: not open")
        return super().read_raw(exact_room_title)

    def send_text(self, exact_room_title: str, text: str) -> None:
        # Win32KakaoAdapter.send_text calls _find_exact_room (no auto-open).
        if exact_room_title not in self.open_titles:
            raise AdapterError("room_not_open", "Exactly one already-open allowed room window is required")
        super().send_text(exact_room_title, text)


def test_r3_commit_to_closed_room_is_recorded_send_unknown_without_sending(tmp_path) -> None:
    settings = _settings(tmp_path)
    adapter = AutoOpenAdapter(settings.mock_transcripts)
    service = KakaoService(settings, adapter)

    fp = service.read_room("room-a")["fingerprint"]  # works: room_session auto-opens
    prepared = service.prepare_reply("room-a", fp, "닫힌 방 전송", "closed-room-key")
    with pytest.raises(AdapterError) as excinfo:
        service.commit_reply(prepared["operation_id"], prepared["confirmation_code"])

    assert excinfo.value.code == "room_not_open"
    assert adapter.sent == []  # nothing was typed
    assert service.operation_status(prepared["operation_id"])["status"] == "send_unknown"
    # The idempotency key is now burned although no input occurred.
    fp_again = service.read_room("room-a")["fingerprint"]
    with pytest.raises(ApprovalError) as again:
        service.prepare_reply("room-a", fp_again, "닫힌 방 전송", "closed-room-key")
    assert again.value.code == "already_committed"


# R4 -------------------------------------------------------------------------
def test_r4_loopback_webhook_honours_http_proxy_env(monkeypatch) -> None:
    """With HTTP_PROXY set and no NO_PROXY, message bodies go to the proxy, not loopback."""
    received: dict[str, list[bytes]] = {"proxy": [], "target": []}

    def make_handler(name: str):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                received[name].append(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(200)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        return Handler

    proxy = HTTPServer(("127.0.0.1", 0), make_handler("proxy"))
    target = HTTPServer(("127.0.0.1", 0), make_handler("target"))
    for server in (proxy, target):
        threading.Thread(target=server.handle_request, daemon=True).start()

    for key in ("NO_PROXY", "no_proxy", "http_proxy", "HTTP_PROXY", "REQUEST_METHOD"):
        monkeypatch.delenv(key, raising=False)
    # Stand-in for a corporate/system proxy; here it is just another loopback port.
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy.server_port}")

    config = WebhookConfig(
        url=f"http://127.0.0.1:{target.server_port}/hooks/kakao",
        secret_env="HERMES_KAKAO_WEBHOOK_SECRET",
        event_type="kakao.message",
        timeout_seconds=2.0,
    )
    event = {
        "event_id": 1,
        "room_id": "room-a",
        "fingerprint": "f" * 64,
        "created_at": 1.0,
        "messages": [{"sender": "상대", "time": "", "text": "가상 비밀 본문", "kind": "text"}],
        "uncertain_overlap": False,
    }
    try:
        deliver_event(config, event, "synthetic-secret")
    finally:
        proxy.server_close()
        target.server_close()

    assert received["target"] == []
    assert len(received["proxy"]) == 1
    assert json.loads(received["proxy"][0])["messages"][0]["text"] == "가상 비밀 본문"


# R5 -------------------------------------------------------------------------
def test_r5_disabled_room_candidates_starve_allowed_ones(tmp_path) -> None:
    settings = _settings(
        tmp_path,
        rooms={
            "room-a": RoomConfig(
                room_id="room-a", title="가상방A", my_name="나",
                schedule_watch_enabled=True, tracking_policy="schedule",
            ),
            "room-b": RoomConfig(
                room_id="room-b", title="가상방B", my_name="나", enabled=False,
                schedule_watch_enabled=True, tracking_policy="schedule",
            ),
        },
    )
    service = KakaoService(settings, MockKakaoAdapter(settings.mock_transcripts))
    for index in range(3):  # older rows from a room that was later disabled
        service.state.append_schedule_candidate(
            candidate_id=f"old-{index}", room_id="room-b", source_fingerprint="x", payload={}
        )
    service.state.append_schedule_candidate(
        candidate_id="new-allowed", room_id="room-a", source_fingerprint="y", payload={}
    )

    result = service.poll_schedule_candidates(limit=3)
    assert result["candidate_count"] == 0  # allowed candidate hidden behind filtered rows
    assert service.schedule_candidate_count()["pending_analysis_count"] == 1
