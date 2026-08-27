from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .config import (
    adopt_single_open_room,
    enable_backend_for_manual_rooms,
    load_settings,
    merge_discovered_rooms,
)
from .errors import ConfigurationError, KakaoBridgeError
from .room_policy import manage_room_policies
from .service import build_service


def _json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _set_config(path: str | None) -> None:
    if path:
        os.environ["HERMES_KAKAO_CONFIG"] = str(Path(path).expanduser().resolve())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restricted Hermes KakaoTalk MCP bridge")
    parser.add_argument("--config", help="Path to local JSON config")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="Check config, bridge and KakaoTalk process")
    subparsers.add_parser("schedule-candidate-count", help="Print only the pending schedule candidate count")
    subparsers.add_parser("backend-status", help="Read RAM-only backend watcher status without content")
    subparsers.add_parser(
        "enable-backend-manual-rooms",
        help="Atomically enable RAM-only backend watch for hand-selected rooms",
    )
    subparsers.add_parser("validate-config", help="Validate config without reading KakaoTalk")
    policy_parser = subparsers.add_parser(
        "classify-room-policies",
        help="Classify local rooms and manage bounded tracking without printing titles",
    )
    policy_parser.add_argument(
        "--apply",
        action="store_true",
        help="Back up and atomically apply the locally classified room policy",
    )
    subparsers.add_parser("serve", help="Run stdio MCP server")
    adopt_parser = subparsers.add_parser(
        "adopt-open-room",
        help="Adopt exactly one open chat locally without printing its title",
    )
    adopt_parser.add_argument("--room-id", default="self-test")
    adopt_parser.add_argument("--my-name", default="")
    adopt_parser.add_argument(
        "--self-chat",
        action="store_true",
        help="Treat the one open room as KakaoTalk's self chat",
    )
    discover_parser = subparsers.add_parser(
        "discover-all-rooms",
        help="Discover every visible local chat room without printing titles",
    )
    discover_parser.add_argument(
        "--apply",
        action="store_true",
        help="Merge discovered rooms into the private config in manual-send mode",
    )
    watch_parser = subparsers.add_parser("watch", help="Run local no-AI watcher")
    watch_parser.add_argument("--room", action="append", dest="rooms")
    watch_parser.add_argument("--once", action="store_true")
    backend_watch_parser = subparsers.add_parser(
        "backend-watch", help="Run RAM-only no-agent encrypted-DB watcher"
    )
    backend_watch_parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    _set_config(args.config)

    try:
        if args.command == "serve":
            from .server import main as serve

            serve()
            return 0

        settings = load_settings()
        if args.command == "validate-config":
            _json(
                {
                    "ok": True,
                    "config_path": str(settings.config_path),
                    "adapter": settings.adapter,
                    "send_enabled": settings.send_enabled,
                    "auto_reply_enabled": settings.auto_reply_enabled,
                    "schedule_automation_enabled": settings.schedule_automation_enabled,
                    "allowed_room_count": sum(room.enabled for room in settings.rooms.values()),
                    "backend_collector_enabled": bool(
                        settings.backend_collector and settings.backend_collector.enabled
                    ),
                    "backend_room_count": len(settings.backend_collector.room_ids)
                    if settings.backend_collector
                    else 0,
                }
            )
            return 0

        if args.command == "doctor":
            _json(build_service(settings).health())
            return 0

        if args.command == "classify-room-policies":
            plan, updated, backup = manage_room_policies(
                settings.config_path,
                apply=args.apply,
            )
            _json(
                {
                    "ok": True,
                    "applied": bool(args.apply),
                    "changed": plan.changed,
                    "room_count": plan.room_count,
                    "category_counts": plan.category_counts,
                    "tracking_policy_counts": plan.tracking_policy_counts,
                    "backend_room_count": plan.backend_room_count,
                    "send_enabled": updated.send_enabled if updated else settings.send_enabled,
                    "auto_reply_enabled": (
                        updated.auto_reply_enabled if updated else settings.auto_reply_enabled
                    ),
                    "backup_created": bool(backup and backup.is_file()),
                }
            )
            return 0

        if args.command == "schedule-candidate-count":
            _json(build_service(settings).schedule_candidate_count())
            return 0

        if args.command == "backend-status":
            status = build_service(settings).state.backend_collector_status()
            _json({"ok": True, "backend_collector": status})
            return 0

        if args.command == "enable-backend-manual-rooms":
            updated, backup = enable_backend_for_manual_rooms(settings.config_path)
            backend = updated.backend_collector
            _json(
                {
                    "ok": True,
                    "backend_collector_enabled": bool(backend and backend.enabled),
                    "backend_room_count": len(backend.room_ids) if backend else 0,
                    "schedule_watch_room_count": sum(
                        room.schedule_watch_enabled for room in updated.rooms.values()
                    ),
                    "send_enabled": updated.send_enabled,
                    "auto_reply_enabled": updated.auto_reply_enabled,
                    "backup_created": backup.is_file(),
                }
            )
            return 0

        if args.command == "adopt-open-room":
            if settings.adapter != "win32":
                raise ConfigurationError("windows_required", "Room adoption requires the Win32 adapter")
            from .adapters.win32 import Win32KakaoAdapter

            title = Win32KakaoAdapter().single_open_room_title()
            updated = adopt_single_open_room(
                settings.config_path,
                room_id=args.room_id,
                exact_title=title,
                my_name=args.my_name,
                self_chat=args.self_chat,
            )
            room = updated.rooms[args.room_id]
            _json(
                {
                    "ok": True,
                    "room_id": room.room_id,
                    "configured": True,
                    "watch_ready": bool(room.my_name),
                    "self_chat": room.self_chat,
                    "send_enabled": updated.send_enabled,
                }
            )
            return 0

        if args.command == "discover-all-rooms":
            if settings.adapter != "win32":
                raise ConfigurationError("windows_required", "Room discovery requires Win32")
            from .adapters.win32 import Win32KakaoAdapter

            titles = Win32KakaoAdapter().discover_room_titles()
            added = 0
            backup_created = False
            updated = settings
            if args.apply:
                updated, added, backup = merge_discovered_rooms(settings.config_path, titles)
                backup_created = backup.is_file()
            _json(
                {
                    "ok": True,
                    "applied": bool(args.apply),
                    "discovered_room_count": len(titles),
                    "added_room_count": added,
                    "configured_room_count": sum(room.enabled for room in updated.rooms.values()),
                    "schedule_watch_room_count": sum(
                        room.schedule_watch_enabled for room in updated.rooms.values()
                    ),
                    "send_enabled": updated.send_enabled,
                    "auto_reply_enabled": updated.auto_reply_enabled,
                    "backup_created": backup_created,
                }
            )
            return 0

        if args.command == "watch":
            from .watcher import run

            return run(settings=settings, room_ids=args.rooms, once=args.once)
        if args.command == "backend-watch":
            from .backend_watcher import run

            return run(settings=settings, once=args.once)
    except KakaoBridgeError as exc:
        _json(exc.to_dict())
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"hermes-kakao-mcp: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
