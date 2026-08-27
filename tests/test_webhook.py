from __future__ import annotations

import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from hermes_kakao_mcp.config import WebhookConfig
from hermes_kakao_mcp.webhook import deliver_event, signed_headers


def test_v2_signature_covers_timestamp_and_body() -> None:
    body = b'{"event":"hello"}'
    headers = signed_headers(body, "secret", timestamp=1234, request_id="request-1")
    expected = hmac.new(b"secret", b"1234." + body, hashlib.sha256).hexdigest()
    assert headers["X-Webhook-Signature-V2"] == expected
    assert headers["X-Webhook-Timestamp"] == "1234"
    assert headers["X-Request-ID"] == "request-1"


def test_deliver_event_posts_signed_json_to_loopback() -> None:
    captured: dict[str, object] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
            length = int(self.headers["Content-Length"])
            captured["body"] = self.rfile.read(length)
            captured["signature"] = self.headers["X-Webhook-Signature-V2"]
            captured["timestamp"] = self.headers["X-Webhook-Timestamp"]
            captured["request_id"] = self.headers["X-Request-ID"]
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    try:
        config = WebhookConfig(
            url=f"http://127.0.0.1:{server.server_port}/webhooks/kakao",
            secret_env="TEST_WEBHOOK_SECRET",
            event_type="kakao.message",
            timeout_seconds=2.0,
        )
        event = {
            "event_id": 7,
            "room_id": "self-test",
            "fingerprint": "a" * 64,
            "messages": [{"sender": "상대", "time": "10:00", "text": "안녕", "kind": "text"}],
            "uncertain_overlap": False,
            "created_at": 123.0,
        }
        result = deliver_event(config, event, "secret")
    finally:
        server.server_close()
        thread.join(timeout=2)

    assert result["status"] == 200
    body = captured["body"]
    assert isinstance(body, bytes)
    decoded = json.loads(body)
    assert decoded["room_id"] == "self-test"
    assert decoded["event_type"] == "kakao.message"
    signed = str(captured["timestamp"]).encode() + b"." + body
    expected = hmac.new(b"secret", signed, hashlib.sha256).hexdigest()
    assert captured["signature"] == expected
    assert str(captured["request_id"]).startswith("kakao-7-")
