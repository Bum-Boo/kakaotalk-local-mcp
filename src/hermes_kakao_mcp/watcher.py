from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .config import Settings, load_settings
from .errors import KakaoBridgeError
from .service import KakaoService, build_service
from .webhook import deliver_event


def _deliver_pending(service: KakaoService, settings: Settings) -> dict[str, object] | None:
    if settings.webhook is None:
        return None
    secret = os.environ.get(settings.webhook.secret_env, "")
    if not secret:
        return {"enabled": True, "delivered": 0, "error": "webhook_secret_missing"}

    delivered: list[int] = []
    for event in service.state.pending_events(limit=20):
        try:
            deliver_event(settings.webhook, event, secret)
        except KakaoBridgeError as exc:
            return {
                "enabled": True,
                "delivered": len(delivered),
                "error": exc.code,
            }
        if service.state.mark_event_delivered(event["event_id"]):
            delivered.append(event["event_id"])
    return {"enabled": True, "delivered": len(delivered), "event_ids": delivered}


def run(*, settings: Settings, room_ids: list[str] | None = None, once: bool = False) -> int:
    service = build_service(settings)
    selected = room_ids or [
        room.room_id
        for room in settings.rooms.values()
        if room.enabled and room.my_name
    ]
    if not selected:
        print("No watch-ready allowed rooms are configured", file=sys.stderr)
        return 2
    for room_id in selected:
        room = settings.rooms.get(room_id)
        if room is None or not room.enabled or not room.my_name:
            print(f"Room is not watch-ready: {room_id}", file=sys.stderr)
            return 2

    while True:
        cycle = []
        for room_id in selected:
            try:
                result = service.observe_room(room_id)
            except KakaoBridgeError as exc:
                result = exc.to_dict()
                result["room_id"] = room_id
            cycle.append(result)
        delivery = _deliver_pending(service, settings)
        output = {"observed": cycle}
        if delivery is not None:
            output["webhook"] = delivery
        print(json.dumps(output, ensure_ascii=False), flush=True)
        if once:
            return 0 if all(item.get("ok") for item in cycle) else 2
        time.sleep(settings.watch_interval_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="No-AI KakaoTalk change watcher")
    parser.add_argument("--config")
    parser.add_argument("--room", action="append", dest="rooms")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.config:
        os.environ["HERMES_KAKAO_CONFIG"] = str(Path(args.config).expanduser().resolve())
    try:
        return run(settings=load_settings(), room_ids=args.rooms, once=args.once)
    except KakaoBridgeError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
