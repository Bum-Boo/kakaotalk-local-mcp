from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .errors import ConfigurationError

ROOM_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
KAKAO_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+\.\d+$")
DEFAULT_BACKEND_KAKAO_VERSION = "26.7.1.5263"
ROOM_CATEGORIES = frozenset(
    {
        "vip_professor",
        "priority",
        "work_agency",
        "school_research",
        "community",
        "service_notice",
        "general",
    }
)
ROOM_TRACKING_POLICIES = frozenset({"schedule", "on_demand"})


@dataclass(frozen=True, slots=True)
class RoomConfig:
    room_id: str
    title: str
    my_name: str = ""
    enabled: bool = True
    self_chat: bool = False
    schedule_watch_enabled: bool = False
    category: str = "general"
    tracking_policy: str = "on_demand"
    source_label: str = ""
    source_people: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WebhookConfig:
    url: str
    secret_env: str
    event_type: str
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class BackendCollectorConfig:
    enabled: bool
    mode: str
    room_ids: tuple[str, ...]
    max_batch_rows: int
    bootstrap_retry_seconds: float
    expected_client_version: str = DEFAULT_BACKEND_KAKAO_VERSION


@dataclass(frozen=True, slots=True)
class Settings:
    config_path: Path
    adapter: str
    send_enabled: bool
    auto_reply_enabled: bool
    schedule_automation_enabled: bool
    max_message_chars: int
    operation_ttl_seconds: int
    readback_delay_seconds: float
    watch_interval_seconds: float
    event_retention_minutes: int
    schedule_candidate_retention_minutes: int
    state_path: Path
    rooms: dict[str, RoomConfig]
    mock_transcripts: dict[str, str]
    webhook: WebhookConfig | None
    backend_collector: BackendCollectorConfig | None


def default_config_path() -> Path:
    override = os.environ.get("HERMES_KAKAO_CONFIG")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return root / "HermesKakaoMCP" / "config.json"
    return Path.home() / ".config" / "hermes-kakao-mcp" / "config.json"


def _bounded_int(data: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigurationError(
            "invalid_config",
            f"{key} must be an integer between {low} and {high}",
        )
    return value


def _bounded_float(data: dict[str, Any], key: str, default: float, low: float, high: float) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= float(value) <= high:
        raise ConfigurationError(
            "invalid_config",
            f"{key} must be a number between {low} and {high}",
        )
    return float(value)


def load_settings(path: str | Path | None = None) -> Settings:
    config_path = Path(path).expanduser().resolve() if path else default_config_path()
    if not config_path.is_file():
        raise ConfigurationError(
            "config_not_found",
            "Kakao MCP configuration file was not found",
            path=str(config_path),
        )

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError("invalid_config", "Configuration is not valid UTF-8 JSON") from exc
    if not isinstance(data, dict):
        raise ConfigurationError("invalid_config", "Configuration root must be an object")

    adapter = data.get("adapter", "win32")
    if adapter not in {"win32", "mock"}:
        raise ConfigurationError("invalid_config", "adapter must be 'win32' or 'mock'")

    raw_rooms = data.get("rooms", [])
    if not isinstance(raw_rooms, list):
        raise ConfigurationError("invalid_config", "rooms must be an array")

    rooms: dict[str, RoomConfig] = {}
    titles: set[str] = set()
    for item in raw_rooms:
        if not isinstance(item, dict):
            raise ConfigurationError("invalid_config", "Each room must be an object")
        room_id = item.get("id")
        title = item.get("title")
        if not isinstance(room_id, str) or not ROOM_ID_PATTERN.fullmatch(room_id):
            raise ConfigurationError(
                "invalid_config",
                "Room id must match ^[a-z][a-z0-9_-]{1,63}$",
            )
        if not isinstance(title, str) or not title.strip():
            raise ConfigurationError("invalid_config", f"Room '{room_id}' needs an exact title")
        title = title.strip()
        if room_id in rooms or title in titles:
            raise ConfigurationError("invalid_config", "Room ids and exact titles must be unique")
        enabled = item.get("enabled", True)
        my_name = item.get("my_name", "")
        self_chat = item.get("self_chat", False)
        schedule_watch_enabled = item.get("schedule_watch_enabled", False)
        category = item.get("category", "general")
        tracking_policy = item.get(
            "tracking_policy",
            "schedule" if schedule_watch_enabled else "on_demand",
        )
        source_label = item.get("source_label", "")
        source_people = item.get("source_people", [])
        if (
            not isinstance(enabled, bool)
            or not isinstance(my_name, str)
            or not isinstance(self_chat, bool)
            or not isinstance(schedule_watch_enabled, bool)
            or category not in ROOM_CATEGORIES
            or tracking_policy not in ROOM_TRACKING_POLICIES
            or schedule_watch_enabled != (tracking_policy == "schedule")
            or not isinstance(source_label, str)
            or len(source_label.strip()) > 100
            or not isinstance(source_people, list)
            or len(source_people) > 8
            or not all(
                isinstance(person, str) and person.strip() and len(person.strip()) <= 50
                for person in source_people
            )
        ):
            raise ConfigurationError("invalid_config", f"Room '{room_id}' has invalid fields")
        my_name = my_name.strip()
        source_label = source_label.strip()
        source_people = tuple(dict.fromkeys(person.strip() for person in source_people))
        if self_chat and not my_name:
            my_name = title
        rooms[room_id] = RoomConfig(
            room_id=room_id,
            title=title,
            my_name=my_name,
            enabled=enabled,
            self_chat=self_chat,
            schedule_watch_enabled=schedule_watch_enabled,
            category=category,
            tracking_policy=tracking_policy,
            source_label=source_label,
            source_people=source_people,
        )
        titles.add(title)

    state_value = data.get("state_path", "state/hermes-kakao.sqlite3")
    if not isinstance(state_value, str) or not state_value.strip():
        raise ConfigurationError("invalid_config", "state_path must be a non-empty string")
    state_path = Path(state_value).expanduser()
    if not state_path.is_absolute():
        state_path = config_path.parent / state_path
    state_path = state_path.resolve()

    mock_transcripts = data.get("mock_transcripts", {})
    if not isinstance(mock_transcripts, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in mock_transcripts.items()
    ):
        raise ConfigurationError("invalid_config", "mock_transcripts must map strings to strings")

    send_enabled = data.get("send_enabled", False)
    if not isinstance(send_enabled, bool):
        raise ConfigurationError("invalid_config", "send_enabled must be true or false")
    auto_reply_enabled = data.get("auto_reply_enabled", False)
    if not isinstance(auto_reply_enabled, bool) or auto_reply_enabled:
        raise ConfigurationError(
            "invalid_config",
            "auto_reply_enabled must remain false; schedule ingestion never sends source-chat replies",
        )
    schedule_automation_enabled = data.get("schedule_automation_enabled", False)
    if not isinstance(schedule_automation_enabled, bool):
        raise ConfigurationError(
            "invalid_config",
            "schedule_automation_enabled must be true or false",
        )

    backend_collector: BackendCollectorConfig | None = None
    raw_backend = data.get("backend_collector")
    if raw_backend is not None:
        if not isinstance(raw_backend, dict):
            raise ConfigurationError("invalid_config", "backend_collector must be null or an object")
        backend_enabled = raw_backend.get("enabled", False)
        backend_mode = raw_backend.get("mode", "ram_only_v2")
        backend_room_ids = raw_backend.get("room_ids", [])
        expected_client_version = raw_backend.get(
            "expected_client_version", DEFAULT_BACKEND_KAKAO_VERSION
        )
        if not isinstance(backend_enabled, bool):
            raise ConfigurationError("invalid_config", "backend_collector.enabled must be boolean")
        if backend_mode != "ram_only_v2":
            raise ConfigurationError("invalid_config", "backend_collector.mode must be 'ram_only_v2'")
        if (
            not isinstance(expected_client_version, str)
            or not KAKAO_VERSION_PATTERN.fullmatch(expected_client_version)
        ):
            raise ConfigurationError(
                "invalid_config",
                "backend_collector.expected_client_version must use four numeric components",
            )
        if (
            not isinstance(backend_room_ids, list)
            or not backend_room_ids
            or len(backend_room_ids) > 16
            or not all(isinstance(room_id, str) for room_id in backend_room_ids)
            or len(set(backend_room_ids)) != len(backend_room_ids)
        ):
            raise ConfigurationError(
                "invalid_config",
                "backend_collector.room_ids must contain 1 to 16 unique room ids",
            )
        missing = [
            room_id
            for room_id in backend_room_ids
            if room_id not in rooms or not rooms[room_id].enabled
        ]
        if missing:
            raise ConfigurationError(
                "invalid_config",
                "backend_collector.room_ids must reference enabled allowlisted rooms",
            )
        if backend_enabled and adapter != "win32":
            raise ConfigurationError(
                "invalid_config",
                "An enabled backend_collector requires the win32 adapter",
            )
        if backend_enabled and send_enabled:
            raise ConfigurationError(
                "invalid_config",
                "An enabled backend_collector requires send_enabled=false",
            )
        backend_collector = BackendCollectorConfig(
            enabled=backend_enabled,
            mode=backend_mode,
            room_ids=tuple(backend_room_ids),
            max_batch_rows=_bounded_int(raw_backend, "max_batch_rows", 200, 1, 1000),
            bootstrap_retry_seconds=_bounded_float(
                raw_backend, "bootstrap_retry_seconds", 30.0, 5.0, 600.0
            ),
            expected_client_version=expected_client_version,
        )

    webhook: WebhookConfig | None = None
    raw_webhook = data.get("webhook")
    if raw_webhook is not None:
        if not isinstance(raw_webhook, dict):
            raise ConfigurationError("invalid_config", "webhook must be null or an object")
        url = raw_webhook.get("url", "")
        secret_env = raw_webhook.get("secret_env", "HERMES_KAKAO_WEBHOOK_SECRET")
        event_type = raw_webhook.get("event_type", "kakao.message")
        if not all(isinstance(value, str) and value for value in (url, secret_env, event_type)):
            raise ConfigurationError("invalid_config", "webhook string fields cannot be empty")
        parsed_url = urlparse(url)
        if parsed_url.scheme != "http" or parsed_url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ConfigurationError(
                "invalid_config",
                "webhook URL must be an HTTP loopback address",
            )
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,127}", secret_env):
            raise ConfigurationError("invalid_config", "webhook secret_env is invalid")
        webhook = WebhookConfig(
            url=url,
            secret_env=secret_env,
            event_type=event_type,
            timeout_seconds=_bounded_float(raw_webhook, "timeout_seconds", 5.0, 0.5, 30.0),
        )

    return Settings(
        config_path=config_path,
        adapter=adapter,
        send_enabled=send_enabled,
        auto_reply_enabled=auto_reply_enabled,
        schedule_automation_enabled=schedule_automation_enabled,
        max_message_chars=_bounded_int(data, "max_message_chars", 500, 1, 2000),
        operation_ttl_seconds=_bounded_int(data, "operation_ttl_seconds", 60, 10, 300),
        readback_delay_seconds=_bounded_float(data, "readback_delay_seconds", 1.0, 0.0, 10.0),
        watch_interval_seconds=_bounded_float(data, "watch_interval_seconds", 8.0, 3.0, 60.0),
        event_retention_minutes=_bounded_int(data, "event_retention_minutes", 60, 1, 1440),
        schedule_candidate_retention_minutes=_bounded_int(
            data, "schedule_candidate_retention_minutes", 10080, 60, 43200
        ),
        state_path=state_path,
        rooms=rooms,
        mock_transcripts=dict(mock_transcripts),
        webhook=webhook,
        backend_collector=backend_collector,
    )


def adopt_single_open_room(
    config_path: Path,
    *,
    room_id: str,
    exact_title: str,
    my_name: str = "",
    self_chat: bool = False,
) -> Settings:
    """Atomically adopt one already-open room while forcing read-only mode."""
    config_path = config_path.expanduser().resolve()
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError("invalid_config", "Config file is missing or invalid JSON") from exc
    if not isinstance(data, dict):
        raise ConfigurationError("invalid_config", "Config root must be an object")

    rooms = data.get("rooms", [])
    if not isinstance(rooms, list):
        raise ConfigurationError("invalid_config", "rooms must be a list")
    if self_chat and not my_name.strip():
        my_name = exact_title
    adopted = {
        "id": room_id,
        "title": exact_title,
        "my_name": my_name,
        "enabled": True,
        "self_chat": self_chat,
        "schedule_watch_enabled": False,
        "category": "general",
        "tracking_policy": "on_demand",
        "policy_source": "manual",
    }
    updated_rooms = []
    replaced = False
    for room in rooms:
        if isinstance(room, dict) and room.get("id") == room_id:
            updated_rooms.append(adopted)
            replaced = True
        else:
            updated_rooms.append(room)
    if not replaced:
        updated_rooms.append(adopted)

    data["rooms"] = updated_rooms
    data["send_enabled"] = False
    data["auto_reply_enabled"] = False
    data["schedule_automation_enabled"] = False
    temporary = config_path.with_name(f"{config_path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        load_settings(temporary)
        os.replace(temporary, config_path)
    finally:
        temporary.unlink(missing_ok=True)
    return load_settings(config_path)


def merge_discovered_rooms(
    config_path: Path,
    exact_titles: list[str],
) -> tuple[Settings, int, Path]:
    """Atomically merge locally discovered titles while forcing manual-send mode."""
    config_path = config_path.expanduser().resolve()
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError("invalid_config", "Config file is missing or invalid JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("rooms", []), list):
        raise ConfigurationError("invalid_config", "Config root and rooms must be valid containers")

    titles = sorted({title.strip() for title in exact_titles if isinstance(title, str) and title.strip()})
    rooms = data.get("rooms", [])
    by_title = {
        room.get("title"): room
        for room in rooms
        if isinstance(room, dict) and isinstance(room.get("title"), str)
    }
    used_ids = {
        room.get("id")
        for room in rooms
        if isinstance(room, dict) and isinstance(room.get("id"), str)
    }
    added = 0
    for title in titles:
        existing = by_title.get(title)
        if existing is not None:
            existing["enabled"] = True
            continue
        room_id = ""
        while not room_id or room_id in used_ids:
            room_id = f"auto_{uuid.uuid4().hex[:16]}"
        discovered = {
            "id": room_id,
            "title": title,
            "my_name": "",
            "enabled": True,
            "self_chat": False,
            "schedule_watch_enabled": False,
            "category": "general",
            "tracking_policy": "on_demand",
            "policy_source": "unclassified",
        }
        rooms.append(discovered)
        by_title[title] = discovered
        used_ids.add(room_id)
        added += 1

    data["rooms"] = rooms
    data["send_enabled"] = False
    data["auto_reply_enabled"] = False
    backup = config_path.with_name(f"{config_path.name}.backup-discovery-{uuid.uuid4().hex[:12]}")
    shutil.copy2(config_path, backup)
    temporary = config_path.with_name(f"{config_path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        load_settings(temporary)
        os.replace(temporary, config_path)
    finally:
        temporary.unlink(missing_ok=True)
    return load_settings(config_path), added, backup


def enable_backend_for_manual_rooms(config_path: Path) -> tuple[Settings, Path]:
    """Enable RAM-only v2 watching for the pre-existing hand-selected room set."""
    config_path = config_path.expanduser().resolve()
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError("invalid_config", "Config file is missing or invalid JSON") from exc
    rooms = data.get("rooms", []) if isinstance(data, dict) else []
    if not isinstance(data, dict) or not isinstance(rooms, list):
        raise ConfigurationError("invalid_config", "Config root and rooms must be valid containers")
    selected_ids = [
        room.get("id")
        for room in rooms
        if isinstance(room, dict)
        and isinstance(room.get("id"), str)
        and not room["id"].startswith("auto_")
    ]
    if not 1 <= len(selected_ids) <= 16 or len(set(selected_ids)) != len(selected_ids):
        raise ConfigurationError(
            "manual_backend_scope_invalid",
            "Expected 1 to 16 unique hand-selected room ids",
        )
    selected = set(selected_ids)
    for room in rooms:
        if isinstance(room, dict) and isinstance(room.get("id"), str):
            is_selected = room["id"] in selected
            room["schedule_watch_enabled"] = is_selected
            room["tracking_policy"] = "schedule" if is_selected else "on_demand"
            room.setdefault("category", "general")
    data["rooms"] = rooms
    data["send_enabled"] = False
    data["auto_reply_enabled"] = False
    data["backend_collector"] = {
        "enabled": True,
        "mode": "ram_only_v2",
        "room_ids": selected_ids,
        "max_batch_rows": 200,
        "bootstrap_retry_seconds": 30.0,
        "expected_client_version": DEFAULT_BACKEND_KAKAO_VERSION,
    }
    backup = config_path.with_name(f"{config_path.name}.backup-backend-{uuid.uuid4().hex[:12]}")
    shutil.copy2(config_path, backup)
    temporary = config_path.with_name(f"{config_path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        load_settings(temporary)
        os.replace(temporary, config_path)
    finally:
        temporary.unlink(missing_ok=True)
    return load_settings(config_path), backup
