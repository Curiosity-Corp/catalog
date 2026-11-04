"""CPU thermal logger: coretemp, throttle counters and fans to the journal (catalog#141).

The logger reads a sysfs tree, so these tests build a small fake one and run
the real script against it through CURIOSITY_SYSFS_ROOT.
"""

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import jinja2
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
SCRIPT = ROLE / "files/curiosity-thermal-log"
UNIT = ROLE / "templates/curiosity-thermal-log.service.j2"
TASKS = ROLE / "tasks/cpu-thermal-guard.yml"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
PROFILES = DEFAULTS["hardware_profiles"]
VARIABLE = "developer_workstation_cpu_thermal_log"


def _load_module():
    loader = importlib.machinery.SourceFileLoader("curiosity_thermal_log", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module  # dataclasses resolve annotations through sys.modules
    loader.exec_module(module)
    return module


tl = _load_module()


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{value}\n")


def _fake_sysfs(
    root: Path,
    package_mc: int = 50000,
    cores_mc: dict[int, int] | None = None,
    core_throttles: dict[int, int] | None = None,
    package_throttles: int = 2,
) -> Path:
    """Two cores, each with two hyperthreads (cpu0/1 -> core 0, cpu2/3 -> core 20)."""
    cores_mc = cores_mc or {0: 48000, 20: 61000}
    core_throttles = core_throttles or {0: 0, 20: 2}
    coretemp = root / "class/hwmon/hwmon2"
    _write(coretemp / "name", "coretemp")
    _write(coretemp / "temp1_label", "Package id 0")
    _write(coretemp / "temp1_input", package_mc)
    for index, (core, mc) in enumerate(sorted(cores_mc.items()), start=2):
        _write(coretemp / f"temp{index}_label", f"Core {core}")
        _write(coretemp / f"temp{index}_input", mc)
    nct = root / "class/hwmon/hwmon6"
    _write(nct / "name", "nct6798")
    _write(nct / "fan1_input", 0)
    _write(nct / "fan2_input", 1644)
    _write(root / "class/hwmon/hwmon0/name", "nvme")
    for cpu, core in {0: 0, 1: 0, 2: 20, 3: 20}.items():
        base = root / f"devices/system/cpu/cpu{cpu}"
        _write(base / "topology/core_id", core)
        _write(base / "thermal_throttle/core_throttle_count", core_throttles[core])
        _write(base / "thermal_throttle/package_throttle_count", package_throttles)
        _write(base / "cpufreq/scaling_cur_freq", 3800000 if cpu == 2 else 1200000)
    _write(root / "devices/system/cpu/intel_pstate/max_perf_pct", 70)
    return root


def _run(root: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env={**os.environ, "CURIOSITY_SYSFS_ROOT": str(root), **env},
    )


# --- variable and profile ---------------------------------------------------


def _derived(profile: str) -> str:
    env = jinja2.Environment()
    env.filters["bool"] = lambda value: str(value).strip().lower() in {"true", "yes", "on", "1"}
    return env.from_string(DEFAULTS[VARIABLE]).render(
        hardware_profiles=PROFILES, hardware_profile=profile
    ).strip()


def test_only_deskmeet_turns_the_logger_on() -> None:
    assert _derived("deskmeet-b760") == "True"
    for name in set(PROFILES) - {"deskmeet-b760"} | {"no-such-profile"}:
        assert _derived(name) == "False", name


# --- script -----------------------------------------------------------------


def test_once_prints_a_full_summary(tmp_path: Path) -> None:
    result = _run(_fake_sysfs(tmp_path), "--once")
    assert result.returncode == 0, result.stderr
    line = result.stdout.strip()
    assert line == (
        "thermal pkg_max=50C pkg_avg=50C hottest=core20:61C cores_ge_90C=none "
        "throttle_core=0 throttle_pkg=+0 fan2=1644rpm freq_max=3800MHz max_perf_pct=70"
    )


def test_unconnected_fans_and_other_hwmon_are_ignored(tmp_path: Path) -> None:
    out = _run(_fake_sysfs(tmp_path), "--once").stdout
    assert "fan1" not in out
    assert "nvme" not in out


def test_hyperthread_siblings_share_one_core_counter(tmp_path: Path) -> None:
    cores, package = tl.read_throttles(_fake_sysfs(tmp_path))
    assert cores == {"0": 0, "20": 2}
    assert package == 2


def test_missing_sensors_are_reported_not_fatal(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    result = _run(tmp_path, "--once")
    assert result.returncode == 0, result.stderr
    assert "<4>curiosity-thermal-log: no coretemp sensors found" in result.stdout
    assert "pkg=unavailable" in result.stdout
    assert "max_perf_pct=n/a" in result.stdout


def test_unknown_arguments_are_an_error(tmp_path: Path) -> None:
    result = _run(_fake_sysfs(tmp_path), "--bogus")
    assert result.returncode == 2
    assert "usage" in result.stdout


def test_throttle_increase_is_an_event_and_counted_in_the_window(tmp_path: Path) -> None:
    before = tl.take_sample(_fake_sysfs(tmp_path / "a"))
    after = tl.take_sample(
        _fake_sysfs(
            tmp_path / "b",
            package_mc=99000,
            cores_mc={0: 50000, 20: 100000},
            core_throttles={0: 0, 20: 5},
            package_throttles=3,
        )
    )
    window = tl.Window()
    window.add(before)
    events = tl.throttle_events(before, after, window)
    window.add(after)
    assert events == [
        "thermal event: core 20 throttled (2 -> 5) at 100C",
        "thermal event: package throttled (2 -> 3) at 99C",
    ]
    summary = window.summary(70)
    assert "pkg_max=99C pkg_avg=74C" in summary
    assert "hottest=core20:100C" in summary
    assert "cores_ge_90C=20" in summary
    assert "throttle_core=core20+3" in summary
    assert "throttle_pkg=+1" in summary


def test_no_change_means_no_event(tmp_path: Path) -> None:
    sample = tl.take_sample(_fake_sysfs(tmp_path))
    assert tl.throttle_events(sample, sample, tl.Window()) == []


def test_daemon_logs_events_as_warnings_and_summaries(tmp_path: Path) -> None:
    """Run the loop briefly; raise a counter mid-run and expect a warning line."""
    root = _fake_sysfs(tmp_path)
    proc = subprocess.Popen(
        [sys.executable, "-I", str(SCRIPT)],
        stdout=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "CURIOSITY_SYSFS_ROOT": str(root),
            "CURIOSITY_THERMAL_SAMPLE": "0.1",
            "CURIOSITY_THERMAL_WINDOW": "0.3",
        },
    )
    try:
        assert proc.stdout is not None
        first = proc.stdout.readline()
        assert "throttles since boot: cores core20=2, package 2" in first
        for cpu in (2, 3):
            _write(root / f"devices/system/cpu/cpu{cpu}/thermal_throttle/core_throttle_count", 4)
        lines = [proc.stdout.readline() for _ in range(8)]
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    assert proc.returncode == 0  # SIGTERM is a clean exit
    assert any(l.startswith("<4>thermal event: core 20 throttled (2 -> 4)") for l in lines), lines
    assert any(l.startswith("thermal pkg_max=") for l in lines), lines


@pytest.mark.parametrize("raw", ["abc", "-1", "0"])
def test_bad_interval_falls_back_to_the_default(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURIOSITY_THERMAL_SAMPLE", raw)
    assert tl.env_seconds("CURIOSITY_THERMAL_SAMPLE", 2) == 2


# --- unit and tasks ---------------------------------------------------------


def test_unit_is_sandboxed_and_restarts() -> None:
    text = UNIT.read_text()
    for line in (
        "ExecStart=/usr/local/sbin/curiosity-thermal-log",
        "Restart=always",
        "SyslogIdentifier=curiosity-thermal-log",
        "DynamicUser=yes",
        "ProtectSystem=strict",
        "NoNewPrivileges=yes",
        "WantedBy=multi-user.target",
    ):
        assert line in text, line


def _flatten(tasks: list[dict]) -> list[dict]:
    flat = []
    for task in tasks:
        flat.append(task)
        for key in ("block", "rescue", "always"):
            flat.extend(_flatten(task.get(key) or []))
    return flat


def test_tasks_install_when_on_and_remove_when_off() -> None:
    tasks = yaml.safe_load(TASKS.read_text())
    install = next(t for t in tasks if t["name"] == "Install the CPU thermal logger")
    remove = next(t for t in tasks if t["name"] == "Remove the CPU thermal logger when it is not configured")
    assert install["when"] == f"{VARIABLE} | bool"
    assert remove["when"] == f"not ({VARIABLE} | bool)"
    started = next(t for t in _flatten(install["block"]) if "ansible.builtin.systemd" in t)
    assert started["ansible.builtin.systemd"]["state"] == "started"
    assert started["ansible.builtin.systemd"]["enabled"] is True
    stopped = next(t for t in _flatten(remove["block"]) if "ansible.builtin.systemd" in t)
    assert stopped["ansible.builtin.systemd"] | {} == {
        "name": "curiosity-thermal-log.service",
        "state": "stopped",
        "enabled": False,
    }
    removed = next(t for t in _flatten(remove["block"]) if "ansible.builtin.file" in t)
    assert "/usr/local/sbin/curiosity-thermal-log" in removed["loop"]


def test_handler_restarts_the_logger() -> None:
    handlers = yaml.safe_load((ROLE / "handlers/main.yml").read_text())
    handler = next(h for h in handlers if h["name"] == "Restart the CPU thermal logger")
    assert handler["ansible.builtin.systemd"]["name"] == "curiosity-thermal-log.service"
    assert handler["ansible.builtin.systemd"]["state"] == "restarted"
