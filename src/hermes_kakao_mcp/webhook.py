from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from typing import Any

from .config import WebhookConfig
from .errors import KakaoBridgeError


def encode_event(config: WebhookConfig, event: dict[str, Any]) -> bytes:
    payload = {
        "event_type": config.event_type,
        "event_id": event["event_id"],
        "room_id": event["room_id"],
        "fingerprint": event["fingerprint"],
        "messages": event["messages"],
        "uncertain_overlap": event["uncertain_overlap"],
        "observed_at": event["created_at"],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def signed_headers(body: bytes, secret: str, *, timestamp: int, request_id: str) -> dict[str, str]:
    timestamp_text = str(timestamp)
    signed = timestamp_text.encode("ascii") + b"." + body
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json; charset=utf-8",
        "X-Webhook-Timestamp": timestamp_text,
        "X-Webhook-Signature-V2": digest,
        "X-Request-ID": request_id,
        "User-Agent": "hermes-kakao-mcp/0.1",
    }


def deliver_event(config: WebhookConfig, event: dict[str, Any], secret: str) -> dict[str, Any]:
    if not secret:
        raise KakaoBridgeError("webhook_secret_missing", "Webhook secret environment variable is empty")
    body = encode_event(config, event)
    request_id = f"kakao-{event['event_id']}-{str(event['fingerprint'])[:12]}"
    headers = signed_headers(body, secret, timestamp=int(time.time()), request_id=request_id)
    request = urllib.request.Request(config.url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        raise KakaoBridgeError(
            "webhook_http_error",
            "Hermes webhook rejected the signed event",
            status=exc.code,
        ) from exc
    except (OSError, urllib.error.URLError) as exc:
        raise KakaoBridgeError("webhook_unreachable", "Hermes webhook is not reachable") from exc
    if not 200 <= status < 300:
        raise KakaoBridgeError(
            "webhook_http_error",
            "Hermes webhook returned a non-success status",
            status=status,
        )
    return {"ok": True, "status": status, "request_id": request_id}
