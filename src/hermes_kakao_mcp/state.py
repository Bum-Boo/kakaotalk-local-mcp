"""Bounded local state for events, operations, and schedule candidates.

Opaque local room IDs and fail-closed state boundaries were inspired by
https://github.com/channprj/kmsg. This module is an original implementation;
see THIRD_PARTY_NOTICES.md for attribution details.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .errors import ConfigurationError, ConflictError
from .fingerprint import message_digest, snapshot_fingerprint
from .models import Message

SCHEDULE_CANDIDATE_STATUSES = frozenset(
    {
        "pending_analysis",
        "needs_user_choice",
        "registered",
        "dismissed",
        "stale",
        "failed",
    }
)
TERMINAL_SCHEDULE_CANDIDATE_STATUSES = frozenset({"registered", "dismissed", "stale", "failed"})


def _longest_tail_overlap_end(previous: list[str], current: list[str]) -> int | None:
    """Return the safest end index for the longest previous suffix in current.

    The latest occurrence wins when identical digest sequences repeat. That can
    conservatively miss an ambiguous duplicate, but it avoids replaying an old
    message as new and accidentally triggering a duplicate reply.
    """
    for size in range(min(len(previous), len(current)), 0, -1):
        target = previous[-size:]
        for start in range(len(current) - size, -1, -1):
            if current[start : start + size] == target:
                return start + size
    return None


class StateStore:
    """Small SQLite state store.

    Transcript history is never persisted. Room state keeps only hashes; event
    payload text is retained for the configured short window and then purged.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS room_state (
                    room_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    tail_digests_json TEXT NOT NULL,
                    observed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    delivered_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_events_pending
                    ON events(delivered_at, created_at);
                CREATE TABLE IF NOT EXISTS schedule_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL,
                    source_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    calendar_event_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_schedule_candidates_pending
                    ON schedule_candidates(status, created_at);
                CREATE TABLE IF NOT EXISTS backend_room_state (
                    room_id TEXT PRIMARY KEY,
                    source_hash TEXT NOT NULL,
                    last_log_id INTEGER NOT NULL,
                    baseline_at REAL NOT NULL,
                    observed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS backend_collector_status (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    status TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    watched_room_count INTEGER NOT NULL,
                    mapped_room_count INTEGER NOT NULL,
                    last_cycle_at REAL NOT NULL,
                    error_code TEXT,
                    last_candidate_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS commits (
                    operation_hash TEXT PRIMARY KEY,
                    idempotency_hash TEXT NOT NULL UNIQUE,
                    room_id TEXT NOT NULL,
                    text_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    verified INTEGER NOT NULL,
                    committed_at REAL NOT NULL
                );
                """
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def backend_cursor(self, *, room_id: str, source_hash: str) -> int | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT source_hash, last_log_id FROM backend_room_state WHERE room_id = ?",
                (room_id,),
            ).fetchone()
        if row is None:
            return None
        if row["source_hash"] != source_hash:
            raise ConflictError(
                "backend_source_changed",
                "The backend source identity changed; automatic re-baselining was refused",
            )
        return int(row["last_log_id"])

    def migrate_backend_source_identity(
        self,
        *,
        room_id: str,
        legacy_source_hash: str,
        source_hash: str,
    ) -> bool:
        if len(legacy_source_hash) != 64 or len(source_hash) != 64:
            raise ConfigurationError("invalid_backend_cursor", "backend source identity is invalid")
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT source_hash FROM backend_room_state WHERE room_id = ?",
                (room_id,),
            ).fetchone()
            if row is None or row["source_hash"] == source_hash:
                return False
            if row["source_hash"] != legacy_source_hash:
                raise ConflictError(
                    "backend_source_changed",
                    "The backend source identity changed; migration was refused",
                )
            updated = self._connection.execute(
                """
                UPDATE backend_room_state
                SET source_hash = ?
                WHERE room_id = ? AND source_hash = ?
                """,
                (source_hash, room_id, legacy_source_hash),
            )
            if updated.rowcount != 1:
                raise ConflictError("backend_cursor_race", "backend cursor changed concurrently")
        return True

    def ensure_backend_cursor(
        self,
        *,
        room_id: str,
        source_hash: str,
        baseline_log_id: int,
    ) -> dict[str, Any]:
        if baseline_log_id < 0 or len(source_hash) != 64:
            raise ConfigurationError("invalid_backend_cursor", "backend cursor input is invalid")
        now = time.time()
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT source_hash, last_log_id FROM backend_room_state WHERE room_id = ?",
                (room_id,),
            ).fetchone()
            if row is not None:
                if row["source_hash"] != source_hash:
                    raise ConflictError(
                        "backend_source_changed",
                        "The backend source identity changed; automatic re-baselining was refused",
                    )
                return {
                    "baseline": False,
                    "last_log_id": int(row["last_log_id"]),
                }
            self._connection.execute(
                """
                INSERT INTO backend_room_state(
                    room_id, source_hash, last_log_id, baseline_at, observed_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (room_id, source_hash, baseline_log_id, now, now),
            )
        return {"baseline": True, "last_log_id": baseline_log_id}

    def commit_backend_batch(
        self,
        *,
        room_id: str,
        source_hash: str,
        expected_log_id: int,
        observed_through_log_id: int,
        candidates: list[dict[str, Any]],
    ) -> int:
        if observed_through_log_id < expected_log_id:
            raise ConfigurationError("invalid_backend_cursor", "backend cursor cannot move backwards")
        now = time.time()
        created = 0
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT source_hash, last_log_id FROM backend_room_state WHERE room_id = ?",
                (room_id,),
            ).fetchone()
            if row is None or row["source_hash"] != source_hash:
                raise ConflictError("backend_cursor_missing", "backend cursor is missing or changed")
            if int(row["last_log_id"]) != expected_log_id:
                raise ConflictError("backend_cursor_race", "backend cursor changed concurrently")
            for candidate in candidates:
                payload_json = json.dumps(
                    candidate["payload"], ensure_ascii=False, separators=(",", ":")
                )
                cursor = self._connection.execute(
                    """
                    INSERT OR IGNORE INTO schedule_candidates(
                        candidate_id, room_id, source_fingerprint, status,
                        payload_json, calendar_event_id, created_at, updated_at
                    ) VALUES (?, ?, ?, 'pending_analysis', ?, NULL, ?, ?)
                    """,
                    (
                        candidate["candidate_id"],
                        room_id,
                        candidate["source_fingerprint"],
                        payload_json,
                        now,
                        now,
                    ),
                )
                created += cursor.rowcount
            update = self._connection.execute(
                """
                UPDATE backend_room_state
                SET last_log_id = ?, observed_at = ?
                WHERE room_id = ? AND source_hash = ? AND last_log_id = ?
                """,
                (observed_through_log_id, now, room_id, source_hash, expected_log_id),
            )
            if update.rowcount != 1:
                raise ConflictError("backend_cursor_race", "backend cursor changed concurrently")
        return created

    def update_backend_collector_status(
        self,
        *,
        status: str,
        watched_room_count: int,
        mapped_room_count: int,
        error_code: str | None = None,
        last_candidate_count: int = 0,
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO backend_collector_status(
                    singleton, status, mode, watched_room_count, mapped_room_count,
                    last_cycle_at, error_code, last_candidate_count
                ) VALUES (1, ?, 'ram_only_v2', ?, ?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    status = excluded.status,
                    watched_room_count = excluded.watched_room_count,
                    mapped_room_count = excluded.mapped_room_count,
                    last_cycle_at = excluded.last_cycle_at,
                    error_code = excluded.error_code,
                    last_candidate_count = excluded.last_candidate_count
                """,
                (
                    status,
                    watched_room_count,
                    mapped_room_count,
                    time.time(),
                    error_code,
                    last_candidate_count,
                ),
            )

    def backend_collector_status(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM backend_collector_status WHERE singleton = 1"
            ).fetchone()
        if row is None:
            return None
        return {
            "status": row["status"],
            "mode": row["mode"],
            "watched_room_count": int(row["watched_room_count"]),
            "mapped_room_count": int(row["mapped_room_count"]),
            "last_cycle_at": float(row["last_cycle_at"]),
            "error_code": row["error_code"],
            "last_candidate_count": int(row["last_candidate_count"]),
        }

    def update_snapshot(
        self,
        room_id: str,
        messages: tuple[Message, ...],
    ) -> dict[str, Any]:
        now = time.time()
        current_digests = [message_digest(message) for message in messages[-20:]]
        current_fingerprint = snapshot_fingerprint(room_id, messages)

        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT fingerprint, tail_digests_json FROM room_state WHERE room_id = ?",
                (room_id,),
            ).fetchone()
            baseline = row is None
            uncertain_overlap = False
            new_messages: tuple[Message, ...] = ()

            if row is not None and row["fingerprint"] != current_fingerprint and messages:
                previous_digests = json.loads(row["tail_digests_json"])
                overlap_end = _longest_tail_overlap_end(previous_digests, current_digests)
                if overlap_end is None:
                    uncertain_overlap = True
                    new_messages = messages[-1:]
                else:
                    offset = len(messages) - len(current_digests)
                    new_messages = messages[offset + overlap_end :]

            self._connection.execute(
                """
                INSERT INTO room_state(room_id, fingerprint, tail_digests_json, observed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(room_id) DO UPDATE SET
                    fingerprint = excluded.fingerprint,
                    tail_digests_json = excluded.tail_digests_json,
                    observed_at = excluded.observed_at
                """,
                (room_id, current_fingerprint, json.dumps(current_digests), now),
            )

        return {
            "baseline": baseline,
            "fingerprint": current_fingerprint,
            "new_messages": new_messages,
            "uncertain_overlap": uncertain_overlap,
        }

    def append_event(
        self,
        room_id: str,
        fingerprint: str,
        messages: tuple[Message, ...] | list[Message],
        *,
        uncertain_overlap: bool,
    ) -> int:
        payload = {
            "messages": [message.to_dict() for message in messages],
            "uncertain_overlap": uncertain_overlap,
        }
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO events(room_id, fingerprint, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    room_id,
                    fingerprint,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    time.time(),
                ),
            )
            return int(cursor.lastrowid)

    def poll_events(self, room_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        events = self.pending_events(room_id=room_id, limit=limit)
        for event in events:
            self.mark_event_delivered(event["event_id"])
        return events

    def pending_events(self, room_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        query = "SELECT * FROM events WHERE delivered_at IS NULL"
        params: list[Any] = []
        if room_id is not None:
            query += " AND room_id = ?"
            params.append(room_id)
        query += " ORDER BY id LIMIT ?"
        params.append(limit)

        with self._lock:
            rows = self._connection.execute(query, params).fetchall()

        events = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            events.append(
                {
                    "event_id": row["id"],
                    "room_id": row["room_id"],
                    "fingerprint": row["fingerprint"],
                    "created_at": row["created_at"],
                    **payload,
                }
            )
        return events

    def mark_event_delivered(self, event_id: int) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE events SET delivered_at = ? WHERE id = ? AND delivered_at IS NULL",
                (time.time(), event_id),
            )
            return cursor.rowcount == 1

    def purge_events(self, retention_minutes: int) -> int:
        cutoff = time.time() - retention_minutes * 60
        with self._lock, self._connection:
            cursor = self._connection.execute("DELETE FROM events WHERE created_at < ?", (cutoff,))
        return cursor.rowcount

    def append_schedule_candidate(
        self,
        *,
        candidate_id: str,
        room_id: str,
        source_fingerprint: str,
        payload: dict[str, Any],
    ) -> bool:
        """Insert one idempotent, bounded schedule candidate.

        The caller owns candidate-id construction. A duplicate source must be a
        no-op so watcher restarts cannot produce duplicate calendar work.
        """
        now = time.time()
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO schedule_candidates(
                    candidate_id, room_id, source_fingerprint, status,
                    payload_json, calendar_event_id, created_at, updated_at
                ) VALUES (?, ?, ?, 'pending_analysis', ?, NULL, ?, ?)
                """,
                (candidate_id, room_id, source_fingerprint, payload_json, now, now),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _schedule_candidate_from_row(row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(row["payload_json"])
        return {
            "candidate_id": row["candidate_id"],
            "room_id": row["room_id"],
            "source_fingerprint": row["source_fingerprint"],
            "status": row["status"],
            "calendar_event_id": row["calendar_event_id"],
            **payload,
        }

    def pending_schedule_candidates(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return only candidates awaiting first scheduling analysis.

        `needs_user_choice` has already been surfaced to the user and must not
        be consumed again by a periodic worker; terminal records stay available
        only by id for audit/readback until retention purges them.
        """
        limit = max(1, min(limit, 100))
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM schedule_candidates
                WHERE status = 'pending_analysis'
                ORDER BY created_at, candidate_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._schedule_candidate_from_row(row) for row in rows]

    def pending_schedule_candidate_count(self, room_ids: tuple[str, ...] | None = None) -> int:
        """Count only first-pass candidates without decoding their payloads."""
        query = "SELECT COUNT(*) AS count FROM schedule_candidates WHERE status = 'pending_analysis'"
        params: list[Any] = []
        if room_ids is not None:
            if not room_ids:
                return 0
            query += " AND room_id IN (" + ",".join("?" for _ in room_ids) + ")"
            params.extend(room_ids)
        with self._lock:
            row = self._connection.execute(query, params).fetchone()
        assert row is not None
        return int(row["count"])

    def get_schedule_candidate(self, candidate_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM schedule_candidates WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
        if row is None:
            raise ConfigurationError("schedule_candidate_not_found", "schedule candidate was not found")
        return self._schedule_candidate_from_row(row)

    def update_schedule_candidate(
        self,
        candidate_id: str,
        status: str,
        *,
        calendar_event_id: str | None = None,
    ) -> dict[str, Any]:
        if status not in SCHEDULE_CANDIDATE_STATUSES:
            raise ConfigurationError("invalid_schedule_candidate_status", "invalid schedule candidate status")
        if status == "registered" and not (calendar_event_id or "").strip():
            raise ConfigurationError(
                "calendar_readback_required",
                "registered schedule candidates require a calendar event readback id",
            )

        allowed_transitions = {
            "pending_analysis": {"needs_user_choice", "registered", "dismissed", "stale", "failed"},
            "needs_user_choice": {"registered", "dismissed", "stale", "failed"},
        }
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT * FROM schedule_candidates WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
            if row is None:
                raise ConfigurationError("schedule_candidate_not_found", "schedule candidate was not found")
            current_status = row["status"]
            if current_status in TERMINAL_SCHEDULE_CANDIDATE_STATUSES:
                raise ConfigurationError(
                    "schedule_candidate_terminal",
                    "schedule candidate is already terminal and cannot change",
                )
            if status not in allowed_transitions.get(current_status, set()):
                raise ConfigurationError(
                    "invalid_schedule_candidate_transition",
                    "invalid schedule candidate status transition",
                )
            self._connection.execute(
                """
                UPDATE schedule_candidates
                SET status = ?, calendar_event_id = ?, updated_at = ?
                WHERE candidate_id = ?
                """,
                (status, calendar_event_id.strip() if calendar_event_id else None, time.time(), candidate_id),
            )
            updated = self._connection.execute(
                "SELECT * FROM schedule_candidates WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
        assert updated is not None
        return self._schedule_candidate_from_row(updated)

    def purge_terminal_schedule_candidates(self, retention_minutes: int) -> int:
        cutoff = time.time() - retention_minutes * 60
        terminal = tuple(TERMINAL_SCHEDULE_CANDIDATE_STATUSES)
        placeholders = ",".join("?" for _ in terminal)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                f"""
                DELETE FROM schedule_candidates
                WHERE status IN ({placeholders}) AND updated_at < ?
                """,
                (*terminal, cutoff),
            )
            return cursor.rowcount

    def committed_for_idempotency(self, idempotency_hash: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM commits WHERE idempotency_hash = ?",
                (idempotency_hash,),
            ).fetchone()
        return dict(row) if row else None

    def committed_operation(self, operation_hash: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM commits WHERE operation_hash = ?",
                (operation_hash,),
            ).fetchone()
        return dict(row) if row else None

    def record_commit(
        self,
        *,
        operation_hash: str,
        idempotency_hash: str,
        room_id: str,
        text_hash_value: str,
        status: str,
        verified: bool,
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO commits(
                    operation_hash, idempotency_hash, room_id, text_hash,
                    status, verified, committed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_hash,
                    idempotency_hash,
                    room_id,
                    text_hash_value,
                    status,
                    int(verified),
                    time.time(),
                ),
            )
