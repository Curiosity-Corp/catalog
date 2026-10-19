"""Regression checks for the no-sleep and performance policy (tasks/system-tuning.yml).

Hardware profiles declare `disable_sleep` and `performance_mode`. Both used to
be gated on laptop chassis types, so always-on desktops such as the DeskMeet
B760 profile declared them and never got them.
"""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
TASKS = ROLE / "tasks"


def _tasks() -> list[dict]:
    return yaml.safe_load((TASKS / "system-tuning.yml").read_text())


def _task(name: str) -> dict:
    for task in _tasks():
        if task.get("name") == name:
            return task
    raise AssertionError(f"system-tuning.yml has no task named {name!r}")


def test_no_sleep_policy_applies_to_any_chassis() -> None:
    for name in (
        "Ensure no-sleep logind drop-in directory exists",
        "Deploy no-sleep policy",
        "Mask sleep and hibernate targets",
    ):
        when = _task(name)["when"]
        assert "_is_laptop" not in when, f"{name!r} is still laptop-only"
        assert any("disable_sleep" in cond for cond in when)


def test_no_sleep_policy_ignores_suspend_and_idle() -> None:
    content = _task("Deploy no-sleep policy")["ansible.builtin.copy"]["content"]
    for line in ("HandleSuspendKey=ignore", "HandleHibernateKey=ignore", "IdleAction=ignore"):
        assert line in content


def test_every_sleep_target_is_masked() -> None:
    task = _task("Mask sleep and hibernate targets")
    assert task["ansible.builtin.systemd"]["masked"] is True
    assert set(task["loop"]) == {
        "sleep.target",
        "suspend.target",
        "hibernate.target",
        "hybrid-sleep.target",
        "suspend-then-hibernate.target",
    }


def test_desktop_performance_mode_uses_power_profiles_daemon() -> None:
    block = _task("Install and enable power-profiles-daemon (desktops)")
    assert "not _is_laptop" in block["when"]
    assert any("performance_mode" in cond for cond in block["when"])
    inner = {t["name"]: t for t in block["block"]}
    select = inner["Select the performance power profile"]
    assert select["ansible.builtin.command"]["argv"] == ["powerprofilesctl", "set", "performance"]
    assert "performance" in select["when"]


def test_deskmeet_profile_opts_in() -> None:
    profiles = yaml.safe_load((ROLE / "defaults/main.yml").read_text())["hardware_profiles"]
    assert profiles["deskmeet-b760"]["disable_sleep"] is True
    assert profiles["deskmeet-b760"]["performance_mode"] is True
