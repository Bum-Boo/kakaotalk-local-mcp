from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "kakao_schedule_candidate_trigger.py"


def load_trigger_module():
    spec = importlib.util.spec_from_file_location("kakao_schedule_candidate_trigger_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_trigger_is_disabled_without_automation_opt_in(tmp_path, monkeypatch) -> None:
    module = load_trigger_module()
    module.STATE_PATH = tmp_path / "trigger.json"
    created: list[bool] = []
    monkeypatch.setattr(
        module,
        "_bridge_status",
        lambda **_: {
            "ok": True,
            "pending_analysis_count": 2,
            "schedule_automation_enabled": False,
        },
    )
    monkeypatch.setattr(module, "_create_sweep_job", lambda: created.append(True))

    assert module.run() == {"ok": True, "action": "disabled"}
    assert created == []
    assert not module.STATE_PATH.exists()


def test_trigger_is_idle_without_pending_candidate_and_clears_stale_state(tmp_path, monkeypatch) -> None:
    module = load_trigger_module()
    module.STATE_PATH = tmp_path / "trigger.json"
    module.STATE_PATH.write_text('{"triggered_at": 1}\n', encoding="utf-8")
    monkeypatch.setattr(
        module,
        "_bridge_status",
        lambda **_: {
            "ok": True,
            "pending_analysis_count": 0,
            "schedule_automation_enabled": True,
        },
    )

    assert module.run() == {"ok": True, "action": "idle"}
    assert not module.STATE_PATH.exists()


def test_trigger_creates_one_job_then_coalesces_within_cooldown(tmp_path, monkeypatch) -> None:
    module = load_trigger_module()
    module.STATE_PATH = tmp_path / "trigger.json"
    created: list[bool] = []
    monkeypatch.setattr(
        module,
        "_bridge_status",
        lambda **_: {
            "ok": True,
            "pending_analysis_count": 1,
            "schedule_automation_enabled": True,
        },
    )
    monkeypatch.setattr(module, "_create_sweep_job", lambda: created.append(True))
    monkeypatch.setattr(module.time, "time", lambda: 1_000.0)

    assert module.run() == {"ok": True, "action": "triggered"}
    assert created == [True]
    state = json.loads(module.STATE_PATH.read_text(encoding="utf-8"))
    assert state["pending_analysis_count"] == 1
    assert state["vip_pending_analysis_count"] == 0

    assert module.run() == {"ok": True, "action": "coalesced"}
    assert created == [True]


def test_new_vip_candidate_bypasses_normal_cooldown_once(tmp_path, monkeypatch) -> None:
    module = load_trigger_module()
    module.STATE_PATH = tmp_path / "trigger.json"
    created: list[bool] = []
    status = {
        "ok": True,
        "pending_analysis_count": 1,
        "vip_pending_analysis_count": 0,
        "schedule_automation_enabled": True,
    }
    monkeypatch.setattr(module, "_bridge_status", lambda **_: dict(status))
    monkeypatch.setattr(module, "_create_sweep_job", lambda: created.append(True))
    monkeypatch.setattr(module.time, "time", lambda: 1_000.0)

    assert module.run()["action"] == "triggered"
    status["pending_analysis_count"] = 2
    status["vip_pending_analysis_count"] = 1
    assert module.run()["action"] == "triggered"
    assert module.run()["action"] == "coalesced"
    assert created == [True, True]


def test_trigger_dry_run_never_creates_job_or_state(tmp_path, monkeypatch) -> None:
    module = load_trigger_module()
    module.STATE_PATH = tmp_path / "trigger.json"
    created: list[bool] = []
    monkeypatch.setattr(
        module,
        "_bridge_status",
        lambda **_: {
            "ok": True,
            "pending_analysis_count": 3,
            "schedule_automation_enabled": True,
        },
    )
    monkeypatch.setattr(module, "_create_sweep_job", lambda: created.append(True))

    assert module.run(dry_run=True) == {
        "ok": True,
        "action": "would_trigger",
        "pending_analysis_count": 3,
    }
    assert created == []
    assert not module.STATE_PATH.exists()


def test_created_sweep_has_only_the_schedule_mcp_toolset(tmp_path, monkeypatch) -> None:
    module = load_trigger_module()
    captured: dict[str, object] = {}
    monkeypatch.setattr(module, "PROFILE_HOME", tmp_path / "schedulemanager")
    monkeypatch.setattr(module, "HERMES_PYTHON", Path("/safe/hermes-python"))

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    module._create_sweep_job()

    assert captured["command"] == [str(module.HERMES_PYTHON), "-c", module.SWEEP_CREATE_CODE]
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert environment["HERMES_HOME"] == str(module.PROFILE_HOME)
    request = json.loads(environment["KAKAO_SCHEDULE_SWEEP_REQUEST"])
    assert request["enabled_toolsets"] == ["kakao-schedule-ingest", "schedule-calendar"]
    assert "enabled_toolsets=request['enabled_toolsets']" in module.SWEEP_CREATE_CODE
    assert "Never use Kakao reply" in request["prompt"]
    assert "browser, terminal" in request["prompt"]
    assert "calendar_list_busy" in request["prompt"]
    assert "all_day=true" in request["prompt"]
    assert "calendar_create_candidate_event" in request["prompt"]
    assert "calendar_get_event" in request["prompt"]
    assert "Never mark registered without that readback" in request["prompt"]
    assert 'priority="vip"' in request["prompt"]
    assert "source_label" in request["prompt"]
    assert "source_people" in request["prompt"]
    assert "ask the user" in request["prompt"]
