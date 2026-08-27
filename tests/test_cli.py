from __future__ import annotations

import json

from hermes_kakao_mcp import cli


def test_validate_config_reports_independent_automation_guards(tmp_path, capsys, monkeypatch) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "adapter": "mock",
                "send_enabled": True,
                "auto_reply_enabled": False,
                "schedule_automation_enabled": True,
                "rooms": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_KAKAO_CONFIG", str(config_path))

    assert cli.main(["--config", str(config_path), "validate-config"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["send_enabled"] is True
    assert result["auto_reply_enabled"] is False
    assert result["schedule_automation_enabled"] is True
