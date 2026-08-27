from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ROOM_CATEGORIES, ROOM_TRACKING_POLICIES, Settings, load_settings
from .errors import ConfigurationError

_POLICY_VERSION = 1
_SCHEDULE_CATEGORIES = frozenset({"priority", "work_agency", "school_research"})
_CATEGORY_RULES = (
    (
        "work_agency",
        re.compile(
            r"업무|회사|프로젝트|고객|상담|컨설팅|work|project|client|meeting",
            re.IGNORECASE,
        ),
    ),
    (
        "school_research",
        re.compile(
            r"국민|대학원|대학|연구|랩|\blab\b|aid|석사|박사|논문|학회|조교|교수|세미나|수업",
            re.IGNORECASE,
        ),
    ),
    (
        "community",
        re.compile(
            r"단톡|모임|동문|동기|친목|오픈채팅|공지방|운영진|커뮤니티",
            re.IGNORECASE,
        ),
    ),
    (
        "service_notice",
        re.compile(
            r"알림톡|고객센터|택배|쿠팡|은행|카드|보험|증권|쇼핑|마켓|배송",
            re.IGNORECASE,
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class RoomPolicyPlan:
    room_count: int
    category_counts: dict[str, int]
    tracking_policy_counts: dict[str, int]
    backend_room_count: int
    changed: bool


def _classify_title(title: str, *, existing_backend: bool) -> str:
    for category, pattern in _CATEGORY_RULES:
        if pattern.search(title):
            return category
    return "priority" if existing_backend else "general"


def _managed_copy(data: dict[str, Any]) -> tuple[dict[str, Any], RoomPolicyPlan]:
    managed = json.loads(json.dumps(data, ensure_ascii=False))
    rooms = managed.get("rooms", [])
    if not isinstance(rooms, list):
        raise ConfigurationError("invalid_config", "rooms must be an array")
    backend = managed.get("backend_collector")
    existing_backend_ids = set()
    if isinstance(backend, dict) and isinstance(backend.get("room_ids"), list):
        existing_backend_ids = {
            room_id for room_id in backend["room_ids"] if isinstance(room_id, str)
        }

    category_counts: Counter[str] = Counter()
    policy_counts: Counter[str] = Counter()
    schedule_ids: list[str] = []
    for room in rooms:
        if not isinstance(room, dict):
            raise ConfigurationError("invalid_config", "Each room must be an object")
        room_id = room.get("id")
        title = room.get("title")
        if not isinstance(room_id, str) or not isinstance(title, str):
            raise ConfigurationError("invalid_config", "Managed rooms need local ids and titles")

        explicit_category = room.get("category")
        explicit_policy = room.get("tracking_policy")
        if (
            room.get("policy_source") == "manual"
            and explicit_category in ROOM_CATEGORIES
            and explicit_policy in ROOM_TRACKING_POLICIES
        ):
            category = explicit_category
            policy = explicit_policy
        else:
            existing_backend = room_id in existing_backend_ids
            category = _classify_title(title, existing_backend=existing_backend)
            policy = (
                "schedule"
                if existing_backend or category in _SCHEDULE_CATEGORIES
                else "on_demand"
            )

        room["category"] = category
        room["tracking_policy"] = policy
        room["schedule_watch_enabled"] = policy == "schedule"
        room["policy_source"] = (
            "manual" if room.get("policy_source") == "manual" else "auto_title_v1"
        )
        category_counts[category] += 1
        policy_counts[policy] += 1
        if room.get("enabled", True) and policy == "schedule":
            schedule_ids.append(room_id)

    if not 1 <= len(schedule_ids) <= 16:
        raise ConfigurationError(
            "managed_backend_scope_invalid",
            "Managed schedule tracking must contain 1 to 16 rooms",
            room_count=len(schedule_ids),
        )
    if not isinstance(backend, dict):
        raise ConfigurationError(
            "backend_required",
            "Room policy management requires the existing RAM-only backend collector",
        )
    backend["enabled"] = True
    backend["mode"] = "ram_only_v2"
    backend["room_ids"] = schedule_ids
    managed["backend_collector"] = backend
    managed["send_enabled"] = False
    managed["auto_reply_enabled"] = False
    managed["room_management"] = {
        "version": _POLICY_VERSION,
        "continuous_categories": sorted(_SCHEDULE_CATEGORIES),
        "default_policy": "on_demand",
    }

    canonical_before = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    canonical_after = json.dumps(managed, ensure_ascii=False, indent=2) + "\n"
    plan = RoomPolicyPlan(
        room_count=len(rooms),
        category_counts=dict(sorted(category_counts.items())),
        tracking_policy_counts=dict(sorted(policy_counts.items())),
        backend_room_count=len(schedule_ids),
        changed=canonical_before != canonical_after,
    )
    return managed, plan


def manage_room_policies(
    config_path: Path,
    *,
    apply: bool = False,
) -> tuple[RoomPolicyPlan, Settings | None, Path | None]:
    """Classify rooms locally and optionally apply a bounded tracking policy atomically."""
    config_path = config_path.expanduser().resolve()
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError("invalid_config", "Config file is missing or invalid JSON") from exc
    if not isinstance(data, dict):
        raise ConfigurationError("invalid_config", "Configuration root must be an object")

    managed, plan = _managed_copy(data)
    temporary = config_path.with_name(f"{config_path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    backup: Path | None = None
    try:
        temporary.write_text(
            json.dumps(managed, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        load_settings(temporary)
        if not apply:
            return plan, None, None
        backup = config_path.with_name(
            f"{config_path.name}.backup-room-policy-{uuid.uuid4().hex[:12]}"
        )
        shutil.copy2(config_path, backup)
        os.replace(temporary, config_path)
    finally:
        temporary.unlink(missing_ok=True)

    return plan, load_settings(config_path), backup
