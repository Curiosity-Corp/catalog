"""Regression checks for hardware profiles and the hardware-drivers tasks."""

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
TASKS = ROLE / "tasks"

# Rules at or below this number are processed before 73-seat-late, which is
# what turns TAG+="uaccess" into a per-seat ACL for the logged-in user.
MAX_UDEV_RULE_NUMBER = 70


def _profiles() -> dict:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    return defaults["hardware_profiles"]


def _drivers_tasks() -> list[dict]:
    return yaml.safe_load((TASKS / "hardware-drivers.yml").read_text())


def _flatten(tasks: list[dict]) -> list[dict]:
    flat = []
    for task in tasks:
        flat.append(task)
        for key in ("block", "rescue", "always"):
            flat.extend(_flatten(task.get(key, [])))
    return flat


def test_deskmeet_b760_profile_shape() -> None:
    profile = _profiles()["deskmeet-b760"]
    assert profile["description"]
    for package in (
        "solaar",
        "bluez",
        "bluez-tools",
        "linux-firmware",
        "intel-media-va-driver",
        "intel-gpu-tools",
        "vainfo",
        "libhidapi-libusb0",
        "alsa-scarlett-gui",
        "gphoto2",
        "entangle",
        "linux-modules-v4l2loopback-generic",
        "v4l2loopback-utils",
    ):
        assert package in profile["packages"]
    assert profile["services"] == ["bluetooth"]
    assert profile["pipx_packages"] == ["streamdeck-linux-gui"]
    assert profile["disable_sleep"] is True
    assert profile["performance_mode"] is True
    assert profile["pia_vpn"] is False
    assert profile["kernel_modules"] == ["v4l2loopback"]


def test_udev_rule_names_precede_seat_late_and_are_unique() -> None:
    for name, profile in _profiles().items():
        rule_names = [rule["name"] for rule in profile.get("udev_rules") or []]
        assert len(rule_names) == len(set(rule_names)), f"{name} repeats a rule name"
        for rule_name in rule_names:
            match = re.match(r"(\d+)-", rule_name)
            assert match, f"{name}: rule {rule_name!r} lacks a numeric prefix"
            assert int(match.group(1)) <= MAX_UDEV_RULE_NUMBER, (
                f"{name}: rule {rule_name!r} sorts after 73-seat-late"
            )


def test_deskmeet_b760_udev_rules_cover_the_desk_peripherals() -> None:
    rules = {
        rule["name"]: rule["content"]
        for rule in _profiles()["deskmeet-b760"]["udev_rules"]
    }
    joined = "\n".join(rules.values())
    # Stream Deck and Litra Beam need both the usb device and its hidraw node.
    for vendor in ("0fd9", "046d"):
        for subsystem in ("usb", "hidraw"):
            assert re.search(
                rf'SUBSYSTEM=="{subsystem}".*idVendor}}=="{vendor}".*uaccess',
                joined,
            ), f"missing {subsystem} rule for vendor {vendor}"
    assert 'idProduct}=="c901"' in joined
    canon = next(c for c in rules.values() if "04a9" in c)
    for fragment in (
        'idProduct}=="319b"',
        'MODE="0660"',
        'GROUP="plugdev"',
        'TAG+="uaccess"',
        'ENV{UDISKS_IGNORE}="1"',
    ):
        assert fragment in canon
    loopback = next(c for c in rules.values() if "video4linux" in c)
    assert 'KERNEL=="video42"' in loopback
    # ATTR{name} is not populated yet when the add event is processed.
    active = [l for l in loopback.splitlines() if not l.lstrip().startswith("#")]
    assert active and not any("ATTR{name}" in line for line in active)
    assert 'GROUP="video"' in loopback


def test_deskmeet_b760_loopback_options_match_the_udev_node() -> None:
    profile = _profiles()["deskmeet-b760"]
    (options,) = profile["modprobe_options"]
    assert re.fullmatch(r"[a-z0-9-]+", options["name"])
    assert (
        options["content"].strip()
        == 'options v4l2loopback video_nr=42 card_label="Canon EOS 50D" exclusive_caps=1'
    )


def test_modprobe_and_module_entries_are_well_formed() -> None:
    for name, profile in _profiles().items():
        for entry in profile.get("modprobe_options") or []:
            assert set(entry) == {"name", "content"}, f"{name}: bad modprobe entry"
        assert all(
            isinstance(module, str) and module
            for module in profile.get("kernel_modules") or []
        )


def test_hardware_drivers_tolerates_absent_profile_keys() -> None:
    text = (TASKS / "hardware-drivers.yml").read_text()
    for key in (
        "packages",
        "services",
        "pipx_packages",
        "udev_rules",
        "modprobe_options",
        "kernel_modules",
    ):
        uses = re.findall(rf"hardware_profiles\[hardware_profile\]\.{key}\b[^}}\n]*", text)
        assert uses, f"hardware-drivers.yml never reads {key}"
        assert all("default([])" in use for use in uses), (
            f"{key} must be read with | default([])"
        )


def test_udev_changes_reload_and_trigger_and_pipx_failures_are_not_swallowed() -> None:
    tasks = _flatten(_drivers_tasks())
    commands = [t["ansible.builtin.command"] for t in tasks if "ansible.builtin.command" in t]
    assert "udevadm control --reload" in commands
    assert "udevadm trigger" in commands
    for task in tasks:
        if "pipx" in task.get("name", "") or "community.general.pipx" in task:
            assert "failed_when" not in task
            assert "ignore_errors" not in task
    assert any("community.general.modprobe" in task for task in tasks)
    block = next(t for t in _drivers_tasks() if "kernel modules now" in t.get("name", ""))
    assert block["rescue"], "module load failures must be reported, not dropped"


@pytest.mark.skipif(
    shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed"
)
def test_hardware_drivers_runs_with_a_profile_that_only_has_a_description(
    tmp_path: Path,
) -> None:
    playbook = tmp_path / "bare.yml"
    playbook.write_text(
        textwrap.dedent(
            f"""\
            - hosts: localhost
              gather_facts: false
              connection: local
              vars:
                hardware_profile: bare
                hardware_profiles:
                  bare:
                    description: description only
              tasks:
                - ansible.builtin.include_tasks: {TASKS / "hardware-drivers.yml"}
            """
        )
    )
    result = subprocess.run(
        [shutil.which("ansible-playbook"), "-i", "localhost,", str(playbook)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "ANSIBLE_NOCOLOR": "1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "failed=0" in result.stdout
