"""The telemetry policy says the role reports nothing; hold the code to it."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
POLICY = (ROOT / "docs/telemetry-and-evidence.md").read_text()


def test_no_telemetry_or_analytics_variable_in_role_defaults() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    flagged = [
        name
        for name in defaults
        if re.search(r"telemetry|analytics|usage_report|phone_home|crash_report", name)
        and not name.startswith("streamstack_openvino_telemetry_")
    ]
    assert flagged == []


def test_only_outbound_alert_hook_is_empty_by_default() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    hooks = sorted(name for name in defaults if name.endswith(("_webhook_url", "_webhook")))
    assert hooks == ["failed_sweep_webhook_url"]
    assert defaults["failed_sweep_webhook_url"] == ""
    assert "failed_sweep_webhook_url" in POLICY


def test_policy_states_the_boundaries() -> None:
    lowered = POLICY.lower()
    for phrase in (
        "there is no telemetry",
        "no-network mode",
        "deletion",
        "workstation_update_state_dir",
        "has not yet been reviewed",
    ):
        assert phrase in lowered, phrase
    state_dir = yaml.safe_load((ROLE / "defaults/main.yml").read_text())["workstation_update_state_dir"]
    assert state_dir in POLICY


def test_policy_keeps_personal_data_out_of_the_repository() -> None:
    assert "Personal data is never committed to this repository." in POLICY
