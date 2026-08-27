from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .backend_v2 import OTHER_SENDER, BackendRecord, EphemeralV2Collector
from .config import Settings, load_settings
from .errors import KakaoBridgeError
from .models import Message
from .operations import opaque_hash
from .schedule_gate import detect_schedule_candidate
from .state import StateStore


def _iso_timestamp(timestamp: int) -> str:
    try:
        return datetime.fromtimestamp(timestamp, UTC).isoformat()
    except (OSError, OverflowError, ValueError):
        return datetime.now(UTC).isoformat()


def _candidate_context(records: tuple[BackendRecord, ...], source_index: int) -> list[dict[str, str]]:
    context = records[max(0, source_index - 3) : source_index + 1]
    return [{"sender_role": record.sender, "text": record.text} for record in context]


class BackendIngestionRunner:
    def __init__(
        self,
        settings: Settings,
        collector: EphemeralV2Collector,
        state: StateStore,
    ) -> None:
        self.settings = settings
        self.collector = collector
        self.state = state

    def observe_room(self, room_id: str) -> dict[str, Any]:
        runtime = self.collector.rooms[room_id]
        legacy_source_hash = getattr(runtime, "legacy_source_hash", None)
        if legacy_source_hash:
            self.state.migrate_backend_source_identity(
                room_id=room_id,
                legacy_source_hash=legacy_source_hash,
                source_hash=runtime.source_hash,
            )
        cursor = self.state.backend_cursor(room_id=room_id, source_hash=runtime.source_hash)
        if cursor is None:
            source_hash, maximum = self.collector.baseline(room_id)
            baseline = self.state.ensure_backend_cursor(
                room_id=room_id,
                source_hash=source_hash,
                baseline_log_id=maximum,
            )
            return {
                "ok": True,
                "room_id": room_id,
                "baseline": baseline["baseline"],
                "observed_new_count": 0,
                "schedule_candidate_count": 0,
            }

        backend = self.settings.backend_collector
        assert backend is not None
        records, observed_through, _signature = self.collector.read_since(
            room_id,
            cursor,
            backend.max_batch_rows,
        )
        if observed_through == cursor:
            return {
                "ok": True,
                "room_id": room_id,
                "baseline": False,
                "observed_new_count": 0,
                "schedule_candidate_count": 0,
            }

        room = self.settings.rooms[room_id]
        candidates: list[dict[str, Any]] = []
        new_text_count = 0
        for index, record in enumerate(records):
            if record.log_id <= cursor:
                continue
            new_text_count += 1
            if record.sender != OTHER_SENDER or not room.schedule_watch_enabled:
                continue
            detection = detect_schedule_candidate(
                Message(
                    sender=record.sender,
                    time=_iso_timestamp(record.sent_at),
                    text=record.text,
                )
            )
            if detection is None:
                continue
            source_fingerprint = opaque_hash(
                f"backend-source:{runtime.source_hash}:{record.log_id}"
            )
            candidates.append(
                {
                    "candidate_id": opaque_hash(
                        f"backend-schedule:{room_id}:{runtime.source_hash}:{record.log_id}"
                    ),
                    "source_fingerprint": source_fingerprint,
                    "payload": {
                        "source": "kakao_backend",
                        "source_class": room.category,
                        "priority": "vip" if room.category == "vip_professor" else "normal",
                        "source_label": room.source_label,
                        "source_people": list(room.source_people),
                        "received_at": _iso_timestamp(record.sent_at),
                        "context": _candidate_context(records, index),
                        "signals": {
                            "confidence": detection.confidence,
                            "date_expressions": list(detection.date_signals),
                            "time_expressions": list(detection.time_signals),
                            "intent_keywords": list(detection.keywords),
                            "needs_user_choice": detection.needs_user_choice,
                        },
                    },
                }
            )

        created = self.state.commit_backend_batch(
            room_id=room_id,
            source_hash=runtime.source_hash,
            expected_log_id=cursor,
            observed_through_log_id=observed_through,
            candidates=candidates,
        )
        self.state.purge_terminal_schedule_candidates(
            self.settings.schedule_candidate_retention_minutes
        )
        return {
            "ok": True,
            "room_id": room_id,
            "baseline": False,
            "observed_new_count": new_text_count,
            "schedule_candidate_count": created,
        }


def _safe_error(exc: Exception) -> str:
    return exc.code if isinstance(exc, KakaoBridgeError) else type(exc).__name__


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True), flush=True)


def run(*, settings: Settings, once: bool = False, adapter: Any | None = None) -> int:
    backend = settings.backend_collector
    if backend is None or not backend.enabled:
        _emit({"ok": False, "error": "backend_disabled"})
        return 2
    if settings.send_enabled or settings.auto_reply_enabled:
        _emit({"ok": False, "error": "backend_requires_manual_send"})
        return 2

    if adapter is None:
        from .adapters.win32 import Win32KakaoAdapter

        adapter = Win32KakaoAdapter()
    state = StateStore(settings.state_path)
    collector = EphemeralV2Collector(settings, adapter)
    watched_count = len(backend.room_ids)
    try:
        while True:
            try:
                state.update_backend_collector_status(
                    status="bootstrapping",
                    watched_room_count=watched_count,
                    mapped_room_count=0,
                )
                bootstrap = collector.bootstrap()
                runner = BackendIngestionRunner(settings, collector, state)
                results = [runner.observe_room(room_id) for room_id in backend.room_ids]
                candidate_count = sum(result["schedule_candidate_count"] for result in results)
                state.update_backend_collector_status(
                    status="running",
                    watched_room_count=watched_count,
                    mapped_room_count=len(collector.rooms),
                    last_candidate_count=candidate_count,
                )
                _emit(
                    {
                        "ok": True,
                        "mode": "ram_only_v2",
                        "bootstrap": bootstrap,
                        "baseline_room_count": sum(result["baseline"] for result in results),
                        "watched_room_count": watched_count,
                        "schedule_candidate_count": candidate_count,
                        "send_enabled": settings.send_enabled,
                        "auto_reply_enabled": settings.auto_reply_enabled,
                    }
                )
                if once:
                    return 0

                heartbeat_at = time.monotonic()
                first_cycle = True
                while True:
                    if first_cycle:
                        changed = backend.room_ids
                        first_cycle = False
                    else:
                        changed = collector.changed_room_ids()
                    cycle_results = [runner.observe_room(room_id) for room_id in changed]
                    candidate_count = sum(
                        result["schedule_candidate_count"] for result in cycle_results
                    )
                    now = time.monotonic()
                    if candidate_count or now - heartbeat_at >= 60:
                        state.update_backend_collector_status(
                            status="running",
                            watched_room_count=watched_count,
                            mapped_room_count=len(collector.rooms),
                            last_candidate_count=candidate_count,
                        )
                        heartbeat_at = now
                    if candidate_count:
                        _emit(
                            {
                                "ok": True,
                                "status": "candidate_created",
                                "observed_room_count": len(cycle_results),
                                "schedule_candidate_count": candidate_count,
                            }
                        )
                    time.sleep(settings.watch_interval_seconds)
            except KeyboardInterrupt:
                state.update_backend_collector_status(
                    status="stopped",
                    watched_room_count=watched_count,
                    mapped_room_count=len(collector.rooms),
                )
                return 130
            except Exception as exc:
                error_code = _safe_error(exc)
                state.update_backend_collector_status(
                    status="retrying",
                    watched_room_count=watched_count,
                    mapped_room_count=len(collector.rooms),
                    error_code=error_code,
                )
                _emit({"ok": False, "status": "retrying", "error": error_code})
                collector.close()
                if once:
                    return 2
                time.sleep(backend.bootstrap_retry_seconds)
    finally:
        collector.close()
        state.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RAM-only no-agent KakaoTalk backend watcher")
    parser.add_argument("--config")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.config:
        os.environ["HERMES_KAKAO_CONFIG"] = str(Path(args.config).expanduser().resolve())
    try:
        return run(settings=load_settings(), once=args.once)
    except KakaoBridgeError as exc:
        _emit({"ok": False, "error": exc.code})
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"hermes-kakao-backend-watch: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
