from __future__ import annotations

from typing import Any


class KakaoBridgeError(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"ok": False, "error": self.code, "message": self.message}
        if self.details:
            result["details"] = self.details
        return result


class ConfigurationError(KakaoBridgeError):
    pass


class AdapterError(KakaoBridgeError):
    pass


class ConflictError(KakaoBridgeError):
    pass


class ApprovalError(KakaoBridgeError):
    pass
