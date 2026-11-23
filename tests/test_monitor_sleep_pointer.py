"""Monitor sleep (monitor_off_minutes) and pointer speed for named mice (pointer_accel_products)."""

import re
from pathlib import Path

import jinja2
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
MAIN = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
SLEEP = yaml.safe_load((ROLE / "tasks/monitor-sleep.yml").read_text())
POINTER = yaml.safe_load((ROLE / "tasks/pointer-accel.yml").read_text())
LOCK = yaml.safe_load((ROLE / "tasks/minimal-desktop-locking-user.yml").read_text())


def _environment() -> jinja2.Environment:
    from ansible.plugins.filter.core import FilterModule
    from ansible.plugins.test.core import TestModule

    env = jinja2.Environment(trim_blocks=True)
    env.filters.update(FilterModule().filters())
    env.tests.update(TestModule().tests())
    return env


def _render(template: str, **context: object) -> str:
    return _environment().from_string(template).render(**context)


def _derived(name: str, profile: str) -> str:
    return _render(
        DEFAULTS[name], hardware_profiles=DEFAULTS["hardware_profiles"], hardware_profile=profile
    ).strip()


def _expr(expression: str, **context: object) -> str:
    return _render("{{ " + expression + " }}", **context).strip()


def _task(tasks: list, name: str) -> dict:
    for task in tasks:
        if task.get("name") == name:
            return task
        found = [t for t in task.get("block", []) if t.get("name") == name]
        if found:
            return found[0]
    raise AssertionError(name)


def _include(name: str) -> dict:
    for task in MAIN:
        include = task.get("ansible.builtin.include_tasks")
        if isinstance(include, dict) and include.get("file") == name:
            return task
    raise AssertionError(name)


def test_profiles_default_off_and_deskmeet_sleeps_after_an_hour() -> None:
    for name in DEFAULTS["hardware_profiles"]:
        expected = "60" if name == "deskmeet-b760" else "0"
        assert _derived("monitor_sleep_minutes", name) == expected, name
    assert _derived("monitor_sleep_minutes", "missing-profile") == "0"
    # The console blanks and the monitors sleep when the screen locks.
    assert _derived("minimal_desktop_idle_lock_minutes", "deskmeet-b760") == "60"


def test_deskmeet_names_the_kensington_and_installs_openrgb() -> None:
    profile = DEFAULTS["hardware_profiles"]["deskmeet-b760"]
    assert profile["pointer_accel_products"] == ["MG Pro Fit Ergo Keyboard & Mouse"]
    assert "openrgb" in profile["packages"]
    assert _derived("pointer_accel_products", "generic") == "[]"


def test_monitor_sleep_validation() -> None:
    checks = _task(SLEEP, "Validate the monitor sleep period")["ansible.builtin.assert"]["that"]
    for value, ok in [("0", True), ("60", True), ("1440", True), ("1441", False), ("-5", False), ("abc", False), ("12345", False)]:
        results = [_expr(check, monitor_sleep_minutes=value) == "True" for check in checks]
        assert all(results) is ok, (value, results)


def test_console_blank_dropin_appends_seconds() -> None:
    task = _task(SLEEP, "Install the console blank GRUB drop-in")
    content = _render(task["ansible.builtin.copy"]["content"], monitor_sleep_minutes="60")
    line = [l for l in content.splitlines() if l.startswith("GRUB_CMDLINE_LINUX_DEFAULT=")]
    assert line == ['GRUB_CMDLINE_LINUX_DEFAULT="${GRUB_CMDLINE_LINUX_DEFAULT:+$GRUB_CMDLINE_LINUX_DEFAULT }consoleblank=3600"']
    assert DEFAULTS["monitor_sleep_console_dropin_path"].startswith("/etc/default/grub.d/")


def test_zero_removes_the_dropin() -> None:
    remove = _task(SLEEP, "Remove the console blank GRUB drop-in when monitor sleep is off")
    assert remove["ansible.builtin.file"]["state"] == "absent"
    assert _expr(remove["when"], monitor_sleep_minutes="0") == "True"
    assert _expr(remove["when"], monitor_sleep_minutes="60") == "False"


def _xscreensaver(minutes: str) -> list[str]:
    block = _task(LOCK, "Configure XScreenSaver to blank and lock after the configured idle period")
    rendered = _render(
        block["ansible.builtin.blockinfile"]["block"],
        minimal_desktop_idle_lock_minutes="60",
        monitor_sleep_minutes=minutes,
    )
    return [line for line in rendered.splitlines() if line.strip()]


def test_xscreensaver_dpms_follows_monitor_sleep() -> None:
    lines = _xscreensaver("60")
    assert "dpmsEnabled: True" in lines
    assert "dpmsQuickOff: False" in lines
    for key in ("dpmsStandby", "dpmsSuspend", "dpmsOff"):
        assert f"{key}: 1:00:00" in lines
    assert "dpmsOff: 0:05:00" in _xscreensaver("5")
    assert all(re.match(r"^[A-Za-z]+: ", line) for line in lines), lines


def test_xscreensaver_without_monitor_sleep_is_unchanged() -> None:
    assert _xscreensaver("0") == [
        "timeout: 1:00:00",
        "lock: True",
        "lockTimeout: 0:00:00",
        "mode: blank",
        "splash: False",
    ]


def test_pointer_inputclass_per_product() -> None:
    task = _task(POINTER, "Install the pointer speed InputClass")
    content = _render(
        task["ansible.builtin.copy"]["content"],
        pointer_accel_products=["MG Pro Fit Ergo Keyboard & Mouse"],
        logitech_g604_accel_speed="0.6",
    )
    assert content.count('Section "InputClass"') == 1
    assert 'MatchProduct "MG Pro Fit Ergo Keyboard & Mouse"' in content
    assert 'Option "AccelSpeed" "0.6"' in content
    assert 'MatchIsPointer "on"' in content


def test_pointer_validation() -> None:
    checks = _task(POINTER, "Validate the pointer speed and the product names")["ansible.builtin.assert"]["that"]

    def ok(products: object, speed: str) -> bool:
        return all(_expr(c, pointer_accel_products=products, logitech_g604_accel_speed=speed) == "True" for c in checks)

    assert ok(["MG Pro Fit Ergo Keyboard & Mouse"], "0.6")
    assert ok([], "")
    assert ok(["Mouse"], "-1")
    assert not ok(['Bad "quote"'], "0.6")
    assert not ok("MG Pro", "0.6")
    assert not ok(["Mouse"], "1.5")


def test_pointer_file_removed_without_products_or_speed() -> None:
    remove = _task(POINTER, "Remove the pointer speed InputClass when no product or speed is set")
    assert _expr(remove["when"], pointer_accel_products=[], logitech_g604_accel_speed="0.6") == "True"
    assert _expr(remove["when"], pointer_accel_products=["M"], logitech_g604_accel_speed="") == "True"
    assert _expr(remove["when"], pointer_accel_products=["M"], logitech_g604_accel_speed="0.6") == "False"


def test_main_includes_both_on_desktop_profiles() -> None:
    sleep = _include("monitor-sleep.yml")
    pointer = _include("pointer-accel.yml")
    for task in (sleep, pointer):
        assert "workstation_profile in ['desktop', 'thin-client', 'byod-kiosk']" in task["when"]
    assert "monitor-sleep" in sleep["tags"]
    assert "pointer-accel" in pointer["tags"]
