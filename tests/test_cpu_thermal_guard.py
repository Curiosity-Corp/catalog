"""CPU thermal guard: intel_pstate max_perf_pct cap at boot, plus the nct6775 sensors.

deskmeet1's BIOS locks RAPL (MSR 0x610 bit 63), so the OS cap is
intel_pstate/max_perf_pct. The cap is opt-in (empty by default), tunable per
host, applied by a oneshot unit, and skips cleanly without intel_pstate.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import jinja2
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
TASKS = ROLE / "tasks"
HELPER = ROLE / "files/curiosity-cpu-max-perf"
UNIT_TEMPLATE = ROLE / "templates/curiosity-cpu-max-perf.service.j2"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
PROFILES = DEFAULTS["hardware_profiles"]
VARIABLE = "developer_workstation_cpu_max_perf_pct"


def _tasks(name: str) -> list[dict]:
    return yaml.safe_load((TASKS / name).read_text())


def _flatten(tasks: list[dict]) -> list[dict]:
    flat = []
    for task in tasks:
        flat.append(task)
        for key in ("block", "rescue", "always"):
            flat.extend(_flatten(task.get(key) or []))
    return flat


def _derived(profile: str) -> str:
    env = jinja2.Environment()
    return env.from_string(DEFAULTS[VARIABLE]).render(
        hardware_profiles=PROFILES, hardware_profile=profile
    ).strip()


def _run(args: list[str], knob_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(HELPER), *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CURIOSITY_INTEL_PSTATE_DIR": str(knob_dir)},
    )


# --- variable and profile ---------------------------------------------------


def test_the_cap_is_off_unless_the_profile_sets_it() -> None:
    assert _derived("deskmeet-b760") == "70"
    for name in set(PROFILES) - {"deskmeet-b760"} | {"no-such-profile"}:
        assert _derived(name) == "", name
        assert "cpu_max_perf_pct" not in PROFILES.get(name, {}), name


def test_deskmeet_loads_the_nct6775_hwmon_module_at_boot() -> None:
    assert "nct6775" in PROFILES["deskmeet-b760"]["kernel_modules"]
    assert "v4l2loopback" in PROFILES["deskmeet-b760"]["kernel_modules"]
    for name, profile in PROFILES.items():
        if name != "deskmeet-b760":
            assert "nct6775" not in (profile.get("kernel_modules") or []), name
    # hardware-drivers.yml writes kernel_modules to modules-load.d and loads them now.
    text = (TASKS / "hardware-drivers.yml").read_text()
    assert "/etc/modules-load.d/curiosity-hardware.conf" in text
    assert "community.general.modprobe" in text


# --- helper script ----------------------------------------------------------


def test_helper_writes_the_percentage(tmp_path: Path) -> None:
    knob = tmp_path / "max_perf_pct"
    knob.write_text("100\n")
    result = _run(["70"], tmp_path)
    assert result.returncode == 0, result.stderr
    assert knob.read_text().strip() == "70"
    assert "max_perf_pct is 70" in result.stdout


def test_helper_restore_lifts_the_cap(tmp_path: Path) -> None:
    knob = tmp_path / "max_perf_pct"
    knob.write_text("70\n")
    assert _run(["--restore"], tmp_path).returncode == 0
    assert knob.read_text().strip() == "100"


def test_helper_skips_cleanly_without_intel_pstate(tmp_path: Path) -> None:
    result = _run(["70"], tmp_path / "absent")
    assert result.returncode == 0, result.stderr
    assert "nothing to do" in result.stdout
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("bad", ["0", "101", "-5", "abc", "7.5", ""])
def test_helper_rejects_a_bad_percentage(tmp_path: Path, bad: str) -> None:
    knob = tmp_path / "max_perf_pct"
    knob.write_text("100\n")
    result = _run([bad], tmp_path)
    assert result.returncode == 2
    assert knob.read_text().strip() == "100"


def test_helper_without_arguments_is_an_error(tmp_path: Path) -> None:
    assert _run([], tmp_path).returncode == 2


def test_helper_reports_a_failed_write_as_an_error(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can write a read-only file")
    # A directory in place of the knob is writable by test(1) yet cannot be written.
    (tmp_path / "max_perf_pct").mkdir()
    result = _run(["70"], tmp_path)
    assert result.returncode == 1
    assert "cannot write" in result.stderr


# --- unit and tasks ---------------------------------------------------------


def _unit(pct: str = "70") -> str:
    return jinja2.Environment().from_string(UNIT_TEMPLATE.read_text()).render(
        developer_workstation_cpu_max_perf_pct=pct
    )


def test_unit_is_a_boot_oneshot_that_skips_without_intel_pstate() -> None:
    unit = _unit()
    assert "Type=oneshot" in unit
    assert "RemainAfterExit=yes" in unit
    assert "ConditionPathExists=/sys/devices/system/cpu/intel_pstate/max_perf_pct" in unit
    assert "ExecStart=/usr/local/sbin/curiosity-cpu-max-perf 70" in unit
    # Stopping the unit (the role removes it when the setting is emptied) lifts the cap.
    assert "ExecStop=/usr/local/sbin/curiosity-cpu-max-perf --restore" in unit
    assert "WantedBy=multi-user.target" in unit
    assert "ExecStart=/usr/local/sbin/curiosity-cpu-max-perf 55" in _unit("55")


def test_install_tasks_only_run_when_the_cap_is_set() -> None:
    tasks = _tasks("cpu-thermal-guard.yml")
    install = next(t for t in tasks if t.get("name") == "Install the CPU max performance cap")
    assert install["when"] == "(developer_workstation_cpu_max_perf_pct | string | trim) | length > 0"
    destinations = {
        t["ansible.builtin.copy"]["dest"] for t in install["block"] if "ansible.builtin.copy" in t
    } | {
        t["ansible.builtin.template"]["dest"] for t in install["block"] if "ansible.builtin.template" in t
    }
    assert destinations == {
        "/usr/local/sbin/curiosity-cpu-max-perf",
        "/etc/systemd/system/curiosity-cpu-max-perf.service",
    }
    enable = next(t for t in install["block"] if "ansible.builtin.systemd" in t)
    assert enable["ansible.builtin.systemd"]["enabled"] is True


def test_emptying_the_setting_stops_and_removes_the_unit() -> None:
    tasks = _tasks("cpu-thermal-guard.yml")
    removal = next(t for t in tasks if str(t.get("name", "")).startswith("Remove the CPU max performance cap"))
    assert removal["when"] == "(developer_workstation_cpu_max_perf_pct | string | trim) | length == 0"
    flat = _flatten(removal["block"])
    stop = next(t for t in flat if "ansible.builtin.systemd" in t)["ansible.builtin.systemd"]
    assert stop["state"] == "stopped" and stop["enabled"] is False
    files = next(t for t in flat if "ansible.builtin.file" in t)
    assert files["ansible.builtin.file"]["state"] == "absent"
    assert set(files["loop"]) == {
        "/etc/systemd/system/curiosity-cpu-max-perf.service",
        "/usr/local/sbin/curiosity-cpu-max-perf",
    }


@pytest.mark.parametrize(
    ("value", "valid"),
    [("", True), ("70", True), ("100", True), ("1", True), ("0", False), ("101", False), ("7x", False), ("-3", False), ("070", False)],
)
def test_validation_accepts_empty_or_1_to_100(value: str, valid: bool) -> None:
    assert_task = _tasks("cpu-thermal-guard.yml")[0]
    expression = assert_task["ansible.builtin.assert"]["that"][0]
    pattern = re.search(r"match\('([^']+)'\)", expression).group(1)
    assert bool(re.match(pattern, value)) is valid


def test_main_includes_the_guard_with_tags_applied() -> None:
    main = yaml.safe_load((TASKS / "main.yml").read_text())
    include = next(t for t in main if t.get("name") == "Include the CPU thermal guard")
    assert include["ansible.builtin.include_tasks"]["file"] == "cpu-thermal-guard.yml"
    assert include["ansible.builtin.include_tasks"]["apply"]["tags"] == include["tags"]
    assert "workstation_profile in ['desktop', 'thin-client', 'byod-kiosk']" in include["when"]


def test_handler_applies_the_cap_when_the_unit_changes() -> None:
    handlers = yaml.safe_load((ROLE / "handlers/main.yml").read_text())
    names = {h["name"] for h in handlers}
    assert "Apply the CPU max performance cap" in names
    notified = {
        n
        for t in _flatten(_tasks("cpu-thermal-guard.yml"))
        for n in (t.get("notify") if isinstance(t.get("notify"), list) else [t.get("notify")])
        if n
    }
    assert notified <= names


def test_helper_never_touches_rapl_or_powercap() -> None:
    code = "\n".join(
        line for line in HELPER.read_text().splitlines() if not line.lstrip().startswith("#")
    )
    for forbidden in ("powercap", "constraint_", "wrmsr", "/dev/cpu"):
        assert forbidden not in code, forbidden


def test_modules_load_file_has_one_module_per_line(tmp_path: Path) -> None:
    """join('\\n') in a YAML block wrote a literal backslash-n ("v4l2loopback\\nnct6775"),
    which modules-load cannot load; a second module in a profile exposed it.

    Runs the real task through ansible-playbook with only the destination moved.
    """
    ansible_playbook = shutil.which("ansible-playbook")
    if ansible_playbook is None:
        pytest.skip("ansible-playbook is not installed")
    task = next(
        t for t in _tasks("hardware-drivers.yml")
        if str(t.get("name", "")).startswith("Load hardware-specific kernel modules at boot")
    )
    copy = dict(task["ansible.builtin.copy"])
    destination = tmp_path / "curiosity-hardware.conf"
    copy.update(dest=str(destination))
    copy.pop("owner", None)
    copy.pop("group", None)
    playbook = tmp_path / "play.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "connection": "local",
                    "gather_facts": False,
                    "vars": {
                        "hardware_profile": "deskmeet-b760",
                        "hardware_profiles": {
                            "deskmeet-b760": {
                                "kernel_modules": PROFILES["deskmeet-b760"]["kernel_modules"]
                            }
                        },
                    },
                    "tasks": [{"name": "write", "ansible.builtin.copy": copy}],
                }
            ]
        )
    )
    result = subprocess.run(
        [ansible_playbook, "-i", "localhost,", str(playbook)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "ANSIBLE_NOCOLOR": "1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    lines = destination.read_text().splitlines()
    assert lines[0].startswith("# Managed by the curiosity workstation role")
    assert [line for line in lines[1:] if line.strip()] == ["v4l2loopback", "nct6775"]
    assert "\\n" not in destination.read_text()
