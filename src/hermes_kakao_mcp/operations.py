from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass

from .errors import ApprovalError
from .fingerprint import text_hash


def opaque_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class PreparedOperation:
    operation_id: str
    confirmation_code: str
    room_id: str
    expected_fingerprint: str
    text: str
    text_hash: str
    idempotency_hash: str
    created_at: float
    expires_at: float
    status: str = "prepared"

    def public(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "confirmation_code": self.confirmation_code,
            "room_id": self.room_id,
            "expected_fingerprint": self.expected_fingerprint,
            "draft_sha256": self.text_hash,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status,
        }


class OperationStore:
    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds
        self._operations: dict[str, PreparedOperation] = {}
        self._idempotency: dict[str, str] = {}
        self._lock = threading.RLock()

    def _purge(self) -> None:
        now = time.time()
        expired = [
            operation_id
            for operation_id, operation in self._operations.items()
            if operation.expires_at < now and operation.status == "prepared"
        ]
        for operation_id in expired:
            operation = self._operations.pop(operation_id)
            self._idempotency.pop(operation.idempotency_hash, None)

    def prepare(
        self,
        *,
        room_id: str,
        expected_fingerprint: str,
        text: str,
        idempotency_key: str,
    ) -> PreparedOperation:
        with self._lock:
            self._purge()
            idempotency_hash = opaque_hash(idempotency_key)
            existing_id = self._idempotency.get(idempotency_hash)
            if existing_id:
                return self._operations[existing_id]

            operation_id = secrets.token_urlsafe(24)
            draft_hash = text_hash(text)
            confirmation_code = opaque_hash(f"{operation_id}:{draft_hash}")[:8].upper()
            now = time.time()
            operation = PreparedOperation(
                operation_id=operation_id,
                confirmation_code=confirmation_code,
                room_id=room_id,
                expected_fingerprint=expected_fingerprint,
                text=text,
                text_hash=draft_hash,
                idempotency_hash=idempotency_hash,
                created_at=now,
                expires_at=now + self.ttl_seconds,
            )
            self._operations[operation_id] = operation
            self._idempotency[idempotency_hash] = operation_id
            return operation

    def require_for_commit(self, operation_id: str, confirmation_code: str) -> PreparedOperation:
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                raise ApprovalError("operation_not_found", "Prepared operation was not found or expired")
            if operation.expires_at < time.time():
                operation.status = "expired"
                raise ApprovalError("operation_expired", "Prepared operation has expired")
            if operation.status not in {"prepared", "sent_verified", "sent_unverified"}:
                raise ApprovalError("operation_not_pending", "Prepared operation is no longer pending")
            if not secrets.compare_digest(operation.confirmation_code, confirmation_code.upper()):
                raise ApprovalError("confirmation_mismatch", "Confirmation code does not match the draft")
            return operation

    def mark(self, operation_id: str, status: str) -> None:
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation:
                operation.status = status

    def status(self, operation_id: str) -> dict[str, object]:
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                raise ApprovalError("operation_not_found", "Prepared operation was not found")
            if operation.expires_at < time.time() and operation.status == "prepared":
                operation.status = "expired"
            return operation.public()
