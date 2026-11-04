"""Crash diagnostics: a hard freeze becomes a logged panic and an automatic reboot.

deskmeet1 froze hard seven times in three weeks with nothing left to read
(catalog#139). The task group is opt-in per hardware profile, never makes an
Oops fatal, arms the hardware watchdog only when its device exists, and removes
everything again when the key is dropped.
"""

from pathlib import Path

import jinja2
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
TASKS_FILE = ROLE / "tasks/crash-diagnostics.yml"
SYSCTL_TEMPLATE = ROLE / "templates/curiosity-crash-diagnostics-sysctl.conf.j2"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
PROFILES = DEFAULTS["hardware_profiles"]
VARIABLE = "developer_workstation_crash_diagnostics"


def _flatten(tasks: list[dict]) -> list[dict]:
    flat = []
    for task in tasks:
        flat.append(task)
        for key in ("block", "rescue", "always"):
            flat.extend(_flatten(task.get(key) or []))
    return flat


TASKS = yaml.safe_load(TASKS_FILE.read_text())
FLAT = _flatten(TASKS)


def _task(name: str) -> dict:
    matches = [t for t in FLAT if t.get("name") == name]
    assert len(matches) == 1, name
    return matches[0]


def _derived(profile: str) -> object:
    env = jinja2.Environment()
    rendered = env.from_string(DEFAULTS[VARIABLE]).render(hardware_profiles=PROFILES, hardware_profile=profile)
    return yaml.safe_load(rendered)


def _sysctl(settings: dict) -> dict[str, str]:
    text = jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(SYSCTL_TEMPLATE.read_text()).render(
        **{VARIABLE: settings}
    )
    pairs = {}
    for line in text.splitlines():
        if line.strip() and not line.startswith("#"):
            key, value = (part.strip() for part in line.split("=", 1))
            pairs[key] = value
    return pairs


# --- profile and variable -----------------------------------------------------


def test_off_by_default_for_every_profile_but_deskmeet() -> None:
    assert _derived("no-such-profile") == {}
    on = [name for name, profile in PROFILES.items() if profile.get("crash_diagnostics")]
    assert on == ["deskmeet-b760"]


def test_deskmeet_arms_the_pch_tco_watchdog_and_reboots_after_a_panic() -> None:
    settings = PROFILES["deskmeet-b760"]["crash_diagnostics"]
    assert settings["watchdog_module"] == "iTCO_wdt"
    assert 60 <= settings["watchdog_seconds"] <= 300
    assert 10 <= settings["panic_reboot_seconds"] <= 120


def test_the_profile_key_is_documented_in_the_profile_reference() -> None:
    text = (ROLE / "defaults/main.yml").read_text()
    assert "# crash_diagnostics (mapping, default unset" in text


# --- sysctls -----------------------------------------------------------------


def test_lockups_panic_with_all_cpu_backtraces_and_reboot() -> None:
    pairs = _sysctl({"panic_reboot_seconds": 30})
    assert pairs == {
        "kernel.nmi_watchdog": "1",
        "kernel.hardlockup_panic": "1",
        "kernel.softlockup_panic": "1",
        "kernel.hardlockup_all_cpu_backtrace": "1",
        "kernel.softlockup_all_cpu_backtrace": "1",
        "kernel.panic": "30",
    }


def test_an_oops_is_never_made_fatal() -> None:
    """On deskmeet1 an Oops (2026-10-25 02:56) was followed by 14 h of normal use."""
    assert "panic_on_oops" not in SYSCTL_TEMPLATE.read_text().replace("Oops is deliberately", "")
    assert "kernel.panic_on_oops" not in _sysctl({})
    assert _sysctl({})["kernel.panic"] == "30"


# --- watchdog ----------------------------------------------------------------


def test_the_watchdog_is_armed_only_when_its_device_exists() -> None:
    for name in ("Configure the systemd hardware watchdog for the next boot", "Arm the systemd hardware watchdog now"):
        assert "_crash_watchdog_dev.stat.exists | default(false)" in _task(name)["when"]


def test_arming_is_live_over_dbus_without_reexecuting_pid1() -> None:
    text = TASKS_FILE.read_text()
    assert "daemon-reexec" not in text
    arm = _task("Arm the systemd hardware watchdog now")
    assert "RuntimeWatchdogUSec t {{ (_crash_watchdog_seconds | int) * 1000000 }}" in arm["ansible.builtin.command"]
    assert any("_crash_watchdog_running.stdout" in cond for cond in arm["when"])  # idempotent


def test_the_watchdog_driver_is_loaded_by_name_not_unblacklisted() -> None:
    text = TASKS_FILE.read_text()
    assert "/etc/modules-load.d/curiosity-watchdog.conf" in text
    assert "modprobe.d" not in text.replace("# Ubuntu blacklists", "")


def test_settings_are_validated() -> None:
    assert_task = _task("Validate the crash diagnostics settings")
    conditions = " ".join(assert_task["ansible.builtin.assert"]["that"])
    for key in ("panic_reboot_seconds", "watchdog_seconds", "journal_sync_seconds", "watchdog_module"):
        assert key in conditions


@pytest.mark.parametrize(
    ("value", "ok"),
    [("30", True), ("1", True), ("0", False), ("-5", False), ("abc", False), ("99999", False)],
)
def test_panic_seconds_pattern(value: str, ok: bool) -> None:
    import re

    assert bool(re.match(r"^[1-9][0-9]{0,3}$", value)) is ok


# --- removal -----------------------------------------------------------------


def test_dropping_the_key_removes_every_file_and_disarms() -> None:
    install = TASKS_FILE.read_text().split("- name: Remove crash diagnostics when they are not configured")[0]
    removal = _task("Remove crash diagnostics when they are not configured")
    removed = set(_task("Remove the crash diagnostics files")["loop"])
    removed.add(_task("Remove the journald sync interval")["ansible.builtin.file"]["path"])
    for path in (
        "/etc/sysctl.d/60-curiosity-crash-diagnostics.conf",
        "/etc/systemd/journald.conf.d/60-curiosity-crash-diagnostics.conf",
        "/etc/modules-load.d/curiosity-watchdog.conf",
        "/etc/systemd/system.conf.d/60-curiosity-watchdog.conf",
    ):
        assert path in install and path in removed
    assert removal["when"] == "developer_workstation_crash_diagnostics | length == 0"
    assert "RuntimeWatchdogUSec t 0" in _task("Disarm the systemd hardware watchdog")["ansible.builtin.command"]
    assert "kernel.hardlockup_panic=0" in _task("Restore the kernel's lockup defaults")["ansible.builtin.command"]


def test_the_task_group_is_included_for_physical_desktops_only() -> None:
    main = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
    include = [
        t for t in main
        if isinstance(t.get("ansible.builtin.include_tasks"), dict)
        and t["ansible.builtin.include_tasks"].get("file") == "crash-diagnostics.yml"
    ]
    assert len(include) == 1
    assert any("ansible_virtualization_type" in cond for cond in include[0]["when"])
