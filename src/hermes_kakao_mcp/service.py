from __future__ import annotations

import threading
import time
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from . import __version__
from .adapters.base import KakaoAdapter
from .adapters.mock import MockKakaoAdapter
from .config import RoomConfig, Settings
from .errors import AdapterError, ApprovalError, ConfigurationError, ConflictError, KakaoBridgeError
from .fingerprint import message_digest, snapshot_fingerprint, text_hash
from .operations import OperationStore, opaque_hash
from .parser import parse_chat_text
from .schedule_gate import detect_schedule_candidate
from .state import StateStore


class KakaoService:
    def __init__(
        self,
        settings: Settings,
        adapter: KakaoAdapter,
        state: StateStore | None = None,
        operations: OperationStore | None = None,
    ) -> None:
        self.settings = settings
        self.adapter = adapter
        self.state = state or StateStore(settings.state_path)
        self.operations = operations or OperationStore(settings.operation_ttl_seconds)
        self._commit_lock = threading.RLock()

    def _room(self, room_id: str) -> RoomConfig:
        room = self.settings.rooms.get(room_id)
        if room is None or not room.enabled:
            raise ConfigurationError(
                "room_not_allowed",
                "Room id is not present and enabled in the local allowlist",
            )
        return room

    def _snapshot_once(self, room: RoomConfig) -> tuple[Any, str]:
        raw = self.adapter.read_raw(room.title)
        fallback_sender = room.my_name if room.self_chat else None
        snapshot = parse_chat_text(raw, fallback_sender=fallback_sender)
        fingerprint = snapshot_fingerprint(room.room_id, snapshot.messages)
        return snapshot, fingerprint

    def _snapshot(self, room: RoomConfig) -> tuple[Any, str]:
        """Return a transcript only after two consecutive parsed tails agree.

        KakaoTalk virtualizes long transcripts, so the first Ctrl+A/C result can
        be a stale partial window. A non-convergent view must not move a room
        baseline, generate an event, or authorize a reply.
        """
        with self.adapter.room_session(room.title):
            previous_fingerprint: str | None = None
            latest_snapshot: Any | None = None
            for _attempt in range(1, 6):
                snapshot, fingerprint = self._snapshot_once(room)
                if fingerprint == previous_fingerprint:
                    return snapshot, fingerprint
                previous_fingerprint = fingerprint
                latest_snapshot = snapshot
            raise AdapterError(
                "snapshot_unstable",
                "KakaoTalk transcript did not stabilize after bounded consecutive reads",
                attempts=5,
                last_message_count=len(latest_snapshot.messages) if latest_snapshot is not None else 0,
            )

    def health(self) -> dict[str, Any]:
        adapter_health = self.adapter.health()
        backend_status = self.state.backend_collector_status()
        enabled_rooms = [room for room in self.settings.rooms.values() if room.enabled]
        approved_alias_counts = Counter(
            (room.category, room.source_label, room.source_people)
            for room in enabled_rooms
            if room.source_label
        )
        return {
            "ok": True,
            "service": "hermes-kakao-mcp",
            "version": __version__,
            "send_enabled": self.settings.send_enabled,
            "auto_reply_enabled": self.settings.auto_reply_enabled,
            "schedule_automation_enabled": self.settings.schedule_automation_enabled,
            "allowed_room_count": len(enabled_rooms),
            "room_category_counts": dict(
                sorted(Counter(room.category for room in enabled_rooms).items())
            ),
            "tracking_policy_counts": dict(
                sorted(Counter(room.tracking_policy for room in enabled_rooms).items())
            ),
            "approved_source_aliases": [
                {
                    "source_class": source_class,
                    "source_label": source_label,
                    "source_people": list(source_people),
                    "room_count": room_count,
                }
                for (source_class, source_label, source_people), room_count in sorted(
                    approved_alias_counts.items()
                )
            ],
            "backend_collector_enabled": bool(
                self.settings.backend_collector and self.settings.backend_collector.enabled
            ),
            "backend_collector": backend_status,
            **adapter_health,
        }

    def allowed_rooms(self) -> dict[str, Any]:
        rooms = [
            {
                "room_id": room.room_id,
                "enabled": room.enabled,
                "watch_ready": bool(room.my_name),
                "category": room.category,
                "tracking_policy": room.tracking_policy,
                "schedule_watch_enabled": room.schedule_watch_enabled,
                "backend_watch_enabled": bool(
                    self.settings.backend_collector
                    and self.settings.backend_collector.enabled
                    and room.room_id in self.settings.backend_collector.room_ids
                ),
            }
            for room in self.settings.rooms.values()
            if room.enabled
        ]
        return {"ok": True, "rooms": rooms, "send_enabled": self.settings.send_enabled}

    def read_room(self, room_id: str, max_messages: int = 10) -> dict[str, Any]:
        room = self._room(room_id)
        max_messages = max(1, min(max_messages, 50))
        snapshot, fingerprint = self._snapshot(room)
        return {
            "ok": True,
            "room_id": room_id,
            "fingerprint": fingerprint,
            "member_count": snapshot.member_count,
            "messages": [message.to_dict() for message in snapshot.messages[-max_messages:]],
        }

    @staticmethod
    def _bounded_schedule_context(
        room: RoomConfig,
        messages: tuple[Any, ...],
        source: Any,
    ) -> list[dict[str, str]]:
        source_index = len(messages) - 1
        for index in range(len(messages) - 1, -1, -1):
            if messages[index] == source:
                source_index = index
                break
        context = messages[max(0, source_index - 3) : source_index + 1]
        return [
            {
                "sender_role": "self" if room.my_name and message.sender == room.my_name else "other",
                "text": message.text,
            }
            for message in context
        ]

    def _enqueue_schedule_candidates(
        self,
        room: RoomConfig,
        snapshot: Any,
        fingerprint: str,
        messages: tuple[Any, ...],
    ) -> int:
        if not room.schedule_watch_enabled:
            return 0
        created = 0
        for message in messages:
            detection = detect_schedule_candidate(message)
            if detection is None:
                continue
            candidate_id = opaque_hash(
                f"schedule:{room.room_id}:{fingerprint}:{message_digest(message)}"
            )
            payload = {
                "source": "kakao",
                "source_class": room.category,
                "priority": "vip" if room.category == "vip_professor" else "normal",
                "source_label": room.source_label,
                "source_people": list(room.source_people),
                "received_at": datetime.now(UTC).isoformat(),
                "context": self._bounded_schedule_context(room, snapshot.messages, message),
                "signals": {
                    "confidence": detection.confidence,
                    "date_expressions": list(detection.date_signals),
                    "time_expressions": list(detection.time_signals),
                    "intent_keywords": list(detection.keywords),
                    "needs_user_choice": detection.needs_user_choice,
                },
            }
            if self.state.append_schedule_candidate(
                candidate_id=candidate_id,
                room_id=room.room_id,
                source_fingerprint=fingerprint,
                payload=payload,
            ):
                created += 1
        return created

    def observe_room(self, room_id: str) -> dict[str, Any]:
        room = self._room(room_id)
        if not room.my_name:
            raise ConfigurationError(
                "my_name_required",
                "my_name is required for watcher self-message filtering",
            )
        snapshot, _ = self._snapshot(room)
        observation = self.state.update_snapshot(room_id, snapshot.messages)
        eligible = tuple(
            message
            for message in observation["new_messages"]
            if message.kind == "text" and message.sender != room.my_name
        )
        event_id: int | None = None
        if eligible:
            event_id = self.state.append_event(
                room_id,
                observation["fingerprint"],
                eligible,
                uncertain_overlap=observation["uncertain_overlap"],
            )
        schedule_candidate_count = self._enqueue_schedule_candidates(
            room,
            snapshot,
            observation["fingerprint"],
            eligible,
        )
        self.state.purge_events(self.settings.event_retention_minutes)
        self.state.purge_terminal_schedule_candidates(self.settings.schedule_candidate_retention_minutes)
        return {
            "ok": True,
            "room_id": room_id,
            "baseline": observation["baseline"],
            "fingerprint": observation["fingerprint"],
            "eligible_new_count": len(eligible),
            "event_id": event_id,
            "uncertain_overlap": observation["uncertain_overlap"],
            "schedule_candidate_count": schedule_candidate_count,
        }

    def poll_events(self, room_id: str | None = None, limit: int = 20) -> dict[str, Any]:
        if room_id is not None:
            self._room(room_id)
        events = self.state.poll_events(room_id=room_id, limit=limit)
        allowed = {room.room_id for room in self.settings.rooms.values() if room.enabled}
        events = [event for event in events if event["room_id"] in allowed]
        return {"ok": True, "event_count": len(events), "events": events}

    def poll_schedule_candidates(self, limit: int = 20) -> dict[str, Any]:
        candidates = self.state.pending_schedule_candidates(limit=limit)
        allowed = {
            room.room_id
            for room in self.settings.rooms.values()
            if room.enabled and room.schedule_watch_enabled
        }
        candidates = [candidate for candidate in candidates if candidate["room_id"] in allowed]
        return {"ok": True, "candidate_count": len(candidates), "candidates": candidates}

    def schedule_candidate_count(self) -> dict[str, Any]:
        """Return an allowlisted pending count without exposing candidate content."""
        allowed = tuple(
            sorted(
                room.room_id
                for room in self.settings.rooms.values()
                if room.enabled and room.schedule_watch_enabled
            )
        )
        vip_rooms = tuple(
            sorted(
                room.room_id
                for room in self.settings.rooms.values()
                if room.enabled
                and room.schedule_watch_enabled
                and room.category == "vip_professor"
            )
        )
        return {
            "ok": True,
            "pending_analysis_count": self.state.pending_schedule_candidate_count(allowed),
            "vip_pending_analysis_count": self.state.pending_schedule_candidate_count(vip_rooms),
            "schedule_automation_enabled": self.settings.schedule_automation_enabled,
            "auto_reply_enabled": self.settings.auto_reply_enabled,
        }

    def get_schedule_candidate(self, candidate_id: str) -> dict[str, Any]:
        """Read one allowlisted candidate, including a user-choice hold.

        This is deliberately id-addressed rather than a broad status search so
        the scheduling owner can resume one Telegram question without gaining
        general room/transcript access.
        """
        candidate = self.state.get_schedule_candidate(candidate_id)
        room = self._room(candidate["room_id"])
        if not room.schedule_watch_enabled:
            raise ConfigurationError(
                "schedule_watch_disabled",
                "Schedule candidate room is no longer enabled for scheduling",
            )
        return {"ok": True, **candidate}

    def update_schedule_candidate(
        self,
        candidate_id: str,
        status: str,
        calendar_event_id: str = "",
    ) -> dict[str, Any]:
        existing = self.state.get_schedule_candidate(candidate_id)
        room = self._room(existing["room_id"])
        if not room.schedule_watch_enabled:
            raise ConfigurationError(
                "schedule_watch_disabled",
                "Schedule candidate room is no longer enabled for scheduling",
            )
        candidate = self.state.update_schedule_candidate(
            candidate_id,
            status,
            calendar_event_id=calendar_event_id or None,
        )
        return {"ok": True, **candidate}

    def prepare_reply(
        self,
        room_id: str,
        expected_fingerprint: str,
        text: str,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        room = self._room(room_id)
        text = text.strip()
        if not text:
            raise ApprovalError("empty_message", "Reply text cannot be empty")
        if len(text) > self.settings.max_message_chars:
            raise ApprovalError(
                "message_too_long",
                "Reply exceeds the configured character limit",
                max_chars=self.settings.max_message_chars,
            )
        if len(expected_fingerprint) != 64 or any(
            character not in "0123456789abcdefABCDEF" for character in expected_fingerprint
        ):
            raise ConflictError("invalid_fingerprint", "Expected fingerprint must be a SHA-256 hex string")

        _, current_fingerprint = self._snapshot(room)
        if current_fingerprint != expected_fingerprint:
            raise ConflictError(
                "stale_draft",
                "Room transcript changed before reply preparation",
                current_fingerprint=current_fingerprint,
            )

        idempotency_key = idempotency_key.strip() or (
            f"{room_id}:{expected_fingerprint}:{text_hash(text)}"
        )
        if len(idempotency_key) > 256:
            raise ApprovalError("invalid_idempotency_key", "Idempotency key is too long")
        idempotency_hash = opaque_hash(idempotency_key)
        previous = self.state.committed_for_idempotency(idempotency_hash)
        if previous:
            raise ApprovalError(
                "already_committed",
                "This idempotency key was already committed; no new operation was created",
                status=previous["status"],
            )

        operation = self.operations.prepare(
            room_id=room_id,
            expected_fingerprint=expected_fingerprint,
            text=text,
            idempotency_key=idempotency_key,
        )
        return {"ok": True, "send_enabled": self.settings.send_enabled, **operation.public()}

    def commit_reply(self, operation_id: str, confirmation_code: str) -> dict[str, Any]:
        with self._commit_lock:
            return self._commit_reply_locked(operation_id, confirmation_code)

    def _commit_reply_locked(self, operation_id: str, confirmation_code: str) -> dict[str, Any]:
        if not self.settings.send_enabled:
            raise ApprovalError(
                "sending_disabled",
                "Sending is disabled in local configuration; no KakaoTalk input occurred",
            )

        operation = self.operations.require_for_commit(operation_id, confirmation_code)
        operation_hash = opaque_hash(operation.operation_id)
        previous = self.state.committed_operation(operation_hash)
        if previous:
            return {
                "ok": True,
                "status": previous["status"],
                "verified": bool(previous["verified"]),
                "already_committed": True,
                "retry_allowed": False,
            }

        room = self._room(operation.room_id)
        _, current_fingerprint = self._snapshot(room)
        if current_fingerprint != operation.expected_fingerprint:
            self.operations.mark(operation.operation_id, "stale")
            raise ConflictError(
                "stale_draft",
                "Room transcript changed after preparation; no message was sent",
                current_fingerprint=current_fingerprint,
            )

        try:
            self.adapter.send_text(room.title, operation.text)
        except Exception as exc:
            status = "send_unknown"
            self.operations.mark(operation.operation_id, status)
            self.state.record_commit(
                operation_hash=operation_hash,
                idempotency_hash=operation.idempotency_hash,
                room_id=operation.room_id,
                text_hash_value=operation.text_hash,
                status=status,
                verified=False,
            )
            if isinstance(exc, KakaoBridgeError):
                raise
            raise AdapterError(
                "send_unknown",
                "KakaoTalk delivery outcome is unknown; do not retry this operation",
            ) from exc
        self.operations.mark(operation.operation_id, "sent_pending_readback")
        if self.settings.readback_delay_seconds:
            time.sleep(self.settings.readback_delay_seconds)

        verified = False
        try:
            readback, readback_fingerprint = self._snapshot(room)
            latest = readback.messages[-1] if readback.messages else None
            verified = bool(
                readback_fingerprint != current_fingerprint
                and latest is not None
                and text_hash(latest.text) == operation.text_hash
                and (not room.my_name or latest.sender == room.my_name)
            )
        except Exception:
            verified = False

        status = "sent_verified" if verified else "sent_unverified"
        self.operations.mark(operation.operation_id, status)
        self.state.record_commit(
            operation_hash=operation_hash,
            idempotency_hash=operation.idempotency_hash,
            room_id=operation.room_id,
            text_hash_value=operation.text_hash,
            status=status,
            verified=verified,
        )
        return {
            "ok": True,
            "status": status,
            "verified": verified,
            "already_committed": False,
            "retry_allowed": False,
        }

    def operation_status(self, operation_id: str) -> dict[str, Any]:
        return {"ok": True, **self.operations.status(operation_id)}


def build_adapter(settings: Settings) -> KakaoAdapter:
    if settings.adapter == "mock":
        return MockKakaoAdapter(settings.mock_transcripts)
    from .adapters.win32 import Win32KakaoAdapter

    return Win32KakaoAdapter()


def build_service(settings: Settings) -> KakaoService:
    return KakaoService(settings=settings, adapter=build_adapter(settings))
