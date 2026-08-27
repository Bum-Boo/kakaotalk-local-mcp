import json

import pytest

from hermes_kakao_mcp.config import (
    adopt_single_open_room,
    enable_backend_for_manual_rooms,
    load_settings,
    merge_discovered_rooms,
)
from hermes_kakao_mcp.errors import ConfigurationError
from hermes_kakao_mcp.room_policy import manage_room_policies


def test_config_is_fail_closed_by_default(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"adapter": "mock", "rooms": []}), encoding="utf-8")
    settings = load_settings(path)
    assert settings.send_enabled is False
    assert settings.auto_reply_enabled is False
    assert settings.schedule_automation_enabled is False
    assert settings.backend_collector is None
    assert settings.rooms == {}


def test_config_rejects_duplicate_exact_titles(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "adapter": "mock",
                "rooms": [
                    {"id": "room-one", "title": "같은방"},
                    {"id": "room-two", "title": "같은방"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="unique"):
        load_settings(path)


def test_config_rejects_unbounded_values(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"adapter": "mock", "rooms": [], "max_message_chars": 9000}))
    with pytest.raises(ConfigurationError, match="max_message_chars"):
        load_settings(path)


def test_config_rejects_non_loopback_webhook(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "adapter": "mock",
                "rooms": [],
                "webhook": {"url": "https://example.com/webhooks/kakao"},
            }
        )
    )
    with pytest.raises(ConfigurationError, match="loopback"):
        load_settings(path)


def test_adopt_room_is_atomic_and_forces_read_only(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"adapter": "mock", "send_enabled": True, "rooms": []}),
        encoding="utf-8",
    )
    settings = adopt_single_open_room(
        path,
        room_id="self-test",
        exact_title="private local title",
        self_chat=True,
    )
    assert settings.send_enabled is False
    assert settings.schedule_automation_enabled is False
    assert settings.rooms["self-test"].title == "private local title"
    assert settings.rooms["self-test"].my_name == "private local title"
    assert settings.rooms["self-test"].self_chat is True
    assert not list(tmp_path.glob("config.json.*.tmp"))


def test_merge_discovered_rooms_is_idempotent_and_forces_manual_send(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "adapter": "mock",
                "send_enabled": True,
                "auto_reply_enabled": False,
                "schedule_automation_enabled": True,
                "rooms": [{"id": "known", "title": "known title", "enabled": False}],
            }
        ),
        encoding="utf-8",
    )

    settings, added, backup = merge_discovered_rooms(
        path,
        ["known title", "new title", "new title"],
    )

    assert added == 1
    assert backup.is_file()
    assert settings.send_enabled is False
    assert settings.auto_reply_enabled is False
    assert settings.schedule_automation_enabled is True
    assert settings.rooms["known"].enabled is True
    new_rooms = [room for room in settings.rooms.values() if room.title == "new title"]
    assert len(new_rooms) == 1
    assert new_rooms[0].room_id.startswith("auto_")
    assert new_rooms[0].schedule_watch_enabled is False

    repeated, repeated_added, _ = merge_discovered_rooms(path, ["known title", "new title"])
    assert repeated_added == 0
    assert len(repeated.rooms) == 2


def test_config_rejects_non_boolean_self_chat(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "adapter": "mock",
                "rooms": [{"id": "self-test", "title": "private", "self_chat": "yes"}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="invalid fields"):
        load_settings(path)


def test_room_policy_classifies_locally_and_dry_run_does_not_change_config(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "adapter": "win32",
                "send_enabled": False,
                "auto_reply_enabled": False,
                "schedule_automation_enabled": True,
                "rooms": [
                    {"id": "manual-room", "title": "가까운 사람"},
                    {"id": "school-room", "title": "국민대 연구 세미나"},
                    {"id": "work-room", "title": "프레스티지 업무"},
                    {"id": "service-room", "title": "택배 알림톡"},
                    {"id": "general-room", "title": "일반 대화"},
                ],
                "backend_collector": {
                    "enabled": True,
                    "mode": "ram_only_v2",
                    "room_ids": ["manual-room"],
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    before = path.read_bytes()

    plan, settings, backup = manage_room_policies(path, apply=False)

    assert settings is None
    assert backup is None
    assert path.read_bytes() == before
    assert plan.room_count == 5
    assert plan.backend_room_count == 3
    assert plan.category_counts == {
        "general": 1,
        "priority": 1,
        "school_research": 1,
        "service_notice": 1,
        "work_agency": 1,
    }
    assert plan.tracking_policy_counts == {"on_demand": 2, "schedule": 3}


def test_room_policy_apply_is_atomic_preserves_manual_send_and_is_idempotent(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "adapter": "win32",
                "send_enabled": True,
                "auto_reply_enabled": False,
                "rooms": [
                    {"id": "manual-room", "title": "가까운 사람"},
                    {"id": "school-room", "title": "AID LAB 조교"},
                    {"id": "community-room", "title": "동문 모임"},
                ],
                "backend_collector": {
                    "enabled": True,
                    "mode": "ram_only_v2",
                    "room_ids": ["manual-room"],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    plan, settings, backup = manage_room_policies(path, apply=True)

    assert plan.backend_room_count == 2
    assert settings is not None
    assert backup is not None and backup.is_file()
    assert settings.send_enabled is False
    assert settings.auto_reply_enabled is False
    assert settings.backend_collector is not None
    assert settings.backend_collector.room_ids == ("manual-room", "school-room")
    assert settings.rooms["manual-room"].category == "priority"
    assert settings.rooms["school-room"].tracking_policy == "schedule"
    assert settings.rooms["community-room"].tracking_policy == "on_demand"

    repeated, _, _ = manage_room_policies(path, apply=False)
    assert repeated.changed is False


def test_room_policy_preserves_manual_vip_professor_override(tmp_path) -> None:
    path = tmp_path / "vip-config.json"
    path.write_text(
        json.dumps(
            {
                "adapter": "win32",
                "send_enabled": False,
                "auto_reply_enabled": False,
                "rooms": [
                    {
                        "id": "vip-room",
                        "title": "private",
                        "enabled": True,
                        "category": "vip_professor",
                        "tracking_policy": "schedule",
                        "schedule_watch_enabled": True,
                        "policy_source": "manual",
                        "source_label": "Professor A·Professor B room",
                        "source_people": ["Professor A", "Professor B"],
                    }
                ],
                "backend_collector": {
                    "enabled": True,
                    "mode": "ram_only_v2",
                    "room_ids": ["vip-room"],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    plan, _, _ = manage_room_policies(path, apply=False)

    assert plan.category_counts == {"vip_professor": 1}
    assert plan.tracking_policy_counts == {"schedule": 1}
    assert plan.backend_room_count == 1
    settings = load_settings(path)
    assert settings.rooms["vip-room"].source_label == "Professor A·Professor B room"
    assert settings.rooms["vip-room"].source_people == ("Professor A", "Professor B")


def test_config_rejects_mismatched_room_tracking_policy(tmp_path) -> None:
    path = tmp_path / "bad-room-policy.json"
    path.write_text(
        json.dumps(
            {
                "adapter": "mock",
                "rooms": [
                    {
                        "id": "room-one",
                        "title": "private",
                        "category": "general",
                        "tracking_policy": "on_demand",
                        "schedule_watch_enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="invalid fields"):
        load_settings(path)


def test_config_keeps_auto_reply_disabled_and_schedule_watch_opt_in(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "adapter": "mock",
                "send_enabled": True,
                "auto_reply_enabled": False,
                "schedule_automation_enabled": True,
                "rooms": [
                    {
                        "id": "schedule-room",
                        "title": "private local title",
                        "schedule_watch_enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    settings = load_settings(path)

    assert settings.send_enabled is True
    assert settings.auto_reply_enabled is False
    assert settings.schedule_automation_enabled is True
    assert settings.rooms["schedule-room"].schedule_watch_enabled is True


def test_config_rejects_enabled_auto_reply_and_non_boolean_schedule_options(tmp_path) -> None:
    auto_reply = tmp_path / "auto-reply.json"
    auto_reply.write_text(json.dumps({"adapter": "mock", "auto_reply_enabled": True, "rooms": []}))
    with pytest.raises(ConfigurationError, match="auto_reply_enabled"):
        load_settings(auto_reply)

    bad_watch = tmp_path / "bad-watch.json"
    bad_watch.write_text(
        json.dumps(
            {
                "adapter": "mock",
                "rooms": [{"id": "room-one", "title": "private", "schedule_watch_enabled": "yes"}],
            }
        )
    )
    with pytest.raises(ConfigurationError, match="invalid fields"):
        load_settings(bad_watch)

    bad_automation = tmp_path / "bad-automation.json"
    bad_automation.write_text(
        json.dumps({"adapter": "mock", "schedule_automation_enabled": "yes", "rooms": []})
    )
    with pytest.raises(ConfigurationError, match="schedule_automation_enabled"):
        load_settings(bad_automation)


def test_backend_collector_accepts_only_explicit_win32_manual_send_rooms(tmp_path) -> None:
    path = tmp_path / "backend.json"
    path.write_text(
        json.dumps(
            {
                "adapter": "win32",
                "send_enabled": False,
                "rooms": [{"id": "room-one", "title": "private"}],
                "backend_collector": {
                    "enabled": True,
                    "mode": "ram_only_v2",
                    "room_ids": ["room-one"],
                    "max_batch_rows": 250,
                },
            }
        )
    )

    settings = load_settings(path)

    assert settings.backend_collector is not None
    assert settings.backend_collector.enabled is True
    assert settings.backend_collector.room_ids == ("room-one",)
    assert settings.backend_collector.max_batch_rows == 250
    assert settings.backend_collector.expected_client_version == "26.7.1.5263"


def test_backend_collector_rejects_malformed_client_version_pin(tmp_path) -> None:
    path = tmp_path / "bad-version.json"
    path.write_text(
        json.dumps(
            {
                "adapter": "win32",
                "rooms": [{"id": "room-one", "title": "private"}],
                "backend_collector": {
                    "enabled": True,
                    "room_ids": ["room-one"],
                    "expected_client_version": "latest",
                },
            }
        )
    )

    with pytest.raises(ConfigurationError, match="expected_client_version"):
        load_settings(path)


@pytest.mark.parametrize(
    ("adapter", "send_enabled", "room_ids", "match"),
    [
        ("mock", False, ["room-one"], "win32"),
        ("win32", True, ["room-one"], "send_enabled=false"),
        ("win32", False, ["missing"], "enabled allowlisted"),
    ],
)
def test_backend_collector_rejects_unsafe_scope(
    tmp_path, adapter, send_enabled, room_ids, match
) -> None:
    path = tmp_path / f"bad-{match}.json"
    path.write_text(
        json.dumps(
            {
                "adapter": adapter,
                "send_enabled": send_enabled,
                "rooms": [{"id": "room-one", "title": "private"}],
                "backend_collector": {
                    "enabled": True,
                    "room_ids": room_ids,
                },
            }
        )
    )

    with pytest.raises(ConfigurationError, match=match):
        load_settings(path)


def test_enable_backend_for_manual_rooms_is_atomic_and_exact_scope(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "adapter": "win32",
                "send_enabled": True,
                "auto_reply_enabled": False,
                "rooms": [
                    {"id": "room-one", "title": "private one"},
                    {"id": "room-two", "title": "private two"},
                    {
                        "id": "auto_discovered",
                        "title": "private discovered",
                        "schedule_watch_enabled": True,
                    },
                ],
            }
        )
    )

    settings, backup = enable_backend_for_manual_rooms(path)

    assert backup.is_file()
    assert settings.send_enabled is False
    assert settings.auto_reply_enabled is False
    assert settings.backend_collector is not None
    assert settings.backend_collector.room_ids == ("room-one", "room-two")
    assert settings.backend_collector.expected_client_version == "26.7.1.5263"
    assert settings.rooms["room-one"].schedule_watch_enabled is True
    assert settings.rooms["room-two"].schedule_watch_enabled is True
    assert settings.rooms["auto_discovered"].schedule_watch_enabled is False
    assert not list(tmp_path.glob("config.json.*.tmp"))
