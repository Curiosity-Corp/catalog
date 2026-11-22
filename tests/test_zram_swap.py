"""Checks for opt-in zram swap and the swap sysctls (tasks/system-tuning.yml).

zram is off by default and must leave a host that does not ask for it exactly
as before: the sysctl file renders byte-for-byte what the role wrote before
zram existed. With zram on, swappiness becomes 100 and swap readahead
(vm.page-cluster) 0. The size converge helper runs against fake /proc and
/sys/block trees with stub swapon and systemctl commands.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import stat
import subprocess
import sys
import types
from pathlib import Path

import jinja2
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
TEMPLATES = ROLE / "templates"
TASKS = yaml.safe_load((ROLE / "tasks/system-tuning.yml").read_text())
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
HELPER = ROLE / "files/curiosity-zram-converge"
MIB = 1024 * 1024

# What tasks/system-tuning.yml wrote (copy: content) before zram existed.
PRE_ZRAM_SYSCTL = "# Managed by Ansible — developer-workstation role\nvm.swappiness=10\n"


def _ansible_bool(value: object) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y", "t")


def _env() -> jinja2.Environment:
    # Ansible's template module: trim_blocks, keeps the trailing newline.
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES)),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["bool"] = _ansible_bool
    return env


def _resolve(name: str, **variables: object) -> str:
    """A default's expression evaluated the way Ansible templates a string var."""
    return _env().from_string(str(DEFAULTS[name])).render(**variables)


def _render(template: str, **overrides: object) -> str:
    zram = overrides.get("zram_swap_enabled", DEFAULTS["zram_swap_enabled"])
    variables = {
        **DEFAULTS,
        "vm_swappiness": _resolve("vm_swappiness", zram_swap_enabled=zram),
        "vm_page_cluster": _resolve("vm_page_cluster", zram_swap_enabled=zram),
        **overrides,
    }
    return _env().get_template(template).render(**variables)


def _zram_block() -> dict:
    return next(t for t in TASKS if t.get("name") == "Configure zram swap")


def load_helper() -> types.ModuleType:
    loader = importlib.machinery.SourceFileLoader("curiosity_zram_converge", str(HELPER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


helper = load_helper()


# ---------------------------------------------------------------------------
# Defaults, wiring and rendered files
# ---------------------------------------------------------------------------


def test_zram_is_off_by_default_with_documented_values() -> None:
    assert DEFAULTS["zram_swap_enabled"] is False
    assert DEFAULTS["zram_swap_size_expression"] == "min(ram / 2, 32768)"
    assert DEFAULTS["zram_swap_algorithm"] == "zstd"
    assert DEFAULTS["zram_swap_priority"] == 100


@pytest.mark.parametrize(("zram", "swappiness", "page_cluster"), [(False, "10", ""), (True, "100", "0")])
def test_swap_sysctl_defaults_follow_zram(zram: bool, swappiness: str, page_cluster: str) -> None:
    assert _resolve("vm_swappiness", zram_swap_enabled=zram) == swappiness
    assert _resolve("vm_page_cluster", zram_swap_enabled=zram) == page_cluster


def test_sysctl_file_is_unchanged_for_hosts_without_zram() -> None:
    assert _render("workstation-tuning-sysctl.conf.j2") == PRE_ZRAM_SYSCTL


def test_sysctl_file_with_zram() -> None:
    text = _render("workstation-tuning-sysctl.conf.j2", zram_swap_enabled=True)
    assert text.splitlines()[1:] == ["vm.swappiness=100", "vm.page-cluster=0"]


def test_explicit_values_win_over_the_zram_defaults() -> None:
    text = _render("workstation-tuning-sysctl.conf.j2", zram_swap_enabled=True, vm_swappiness=60, vm_page_cluster=1)
    assert text.splitlines()[1:] == ["vm.swappiness=60", "vm.page-cluster=1"]


def test_zram_generator_config() -> None:
    text = _render("zram-generator.conf.j2", zram_swap_enabled=True)
    lines = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert lines == [
        "[zram0]",
        "zram-size = min(ram / 2, 32768)",
        "compression-algorithm = zstd",
        "swap-priority = 100",
    ]


def test_every_zram_task_is_gated_on_the_switch() -> None:
    block = _zram_block()
    assert block["when"] == "zram_swap_enabled | bool"
    names = [t["name"] for t in block["block"]]
    assert names == [
        "Install the zram generator",
        "Write the zram generator config",
        "Read the active swap devices",
        "Start zram swap now (adds a swap device; nothing is moved or stopped)",
        "Converge the zram device size",
    ]
    # Nothing outside the block touches zram (disabled leaves a host alone).
    outside = yaml.safe_dump([t for t in TASKS if t is not block])
    assert "zram" not in outside


def test_converge_task_runs_the_helper_with_the_quoted_expression() -> None:
    task = _zram_block()["block"][-1]
    script = task["ansible.builtin.script"]
    assert script["cmd"] == "curiosity-zram-converge {{ zram_swap_size_expression | quote }}"
    assert script["executable"] == "/usr/bin/python3"
    assert task["changed_when"] == "_zram_converge.stdout is search('^resized')"
    assert os.access(HELPER, os.X_OK)


def test_sysctl_task_uses_the_template() -> None:
    task = next(t for t in TASKS if t.get("name") == "Set vm.swappiness and vm.page-cluster")
    assert task["ansible.builtin.template"]["src"] == "workstation-tuning-sysctl.conf.j2"
    assert task["ansible.builtin.template"]["dest"] == "/etc/sysctl.d/99-workstation-tuning.conf"


# ---------------------------------------------------------------------------
# The size expression
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "ram", "want"),
    [
        ("min(ram / 2, 32768)", 62651, 31325),
        ("min(ram / 2, 32768)", 128000, 32768),
        ("min(ram / 2, 32768)", 15872, 7936),
        ("ram", 8000, 8000),
        ("max(ram // 4, 2048)", 4000, 2048),
        ("4096", 62651, 4096),
    ],
)
def test_size_expression(expression: str, ram: int, want: int) -> None:
    assert helper.evaluate(expression, ram) == want


@pytest.mark.parametrize(
    "expression",
    ["__import__('os')", "ram.bit_length()", "open('/etc/passwd')", "'4096'", "ram -", "0", "ram - ram", "x * 2", "ram / 0"],
)
def test_size_expression_refuses_anything_but_arithmetic(expression: str) -> None:
    with pytest.raises(ValueError):
        helper.evaluate(expression, 62651)


# ---------------------------------------------------------------------------
# The converge helper against fake trees
# ---------------------------------------------------------------------------


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _host(tmp_path: Path, *, mem_kb: int, disksize: int | None, zram_used: int, reset_clears: bool = True) -> dict:
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "meminfo").write_text(f"MemTotal:       {mem_kb} kB\nMemFree:  1 kB\n")
    sys_block = tmp_path / "sys"
    if disksize is not None:
        (sys_block / "zram0").mkdir(parents=True)
        (sys_block / "zram0/disksize").write_text(f"{disksize}\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    _stub(bin_dir, "swapon", f'printf "/swap.img 0\\n/dev/zram0 {zram_used}\\n"\n')
    # systemctl stop resets the device (or fails to, as seen on Ubuntu 26.04);
    # start brings it up at the size the generator config would give.
    want = helper.evaluate("min(ram / 2, 32768)", mem_kb // 1024) * MIB
    stop_action = f'echo 0 > {sys_block}/zram0/disksize' if reset_clears else ":"
    _stub(bin_dir, "systemctl", f"""echo "systemctl $*" >> {log}
case "$1" in
  stop) {stop_action} ;;
  start) echo {want} > {sys_block}/zram0/disksize ;;
esac
""")
    env = dict(os.environ, CURIOSITY_ZRAM_PROC=str(proc), CURIOSITY_ZRAM_SYS_BLOCK=str(sys_block),
               PATH=f"{bin_dir}:{os.environ['PATH']}")
    return {"env": env, "log": log, "sys": sys_block}


def _converge(host: dict, expression: str = "min(ram / 2, 32768)") -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-I", str(HELPER), expression], env=host["env"],
                          capture_output=True, text=True, check=False, timeout=30)


def _calls(host: dict) -> list[str]:
    return host["log"].read_text().splitlines() if host["log"].exists() else []


def test_absent_device_is_reported_and_left_alone(tmp_path: Path) -> None:
    host = _host(tmp_path, mem_kb=64155296, disksize=None, zram_used=0)
    result = _converge(host)
    assert (result.returncode, result.stdout) == (0, "absent\n")
    assert _calls(host) == []


def test_right_size_is_unchanged(tmp_path: Path) -> None:
    host = _host(tmp_path, mem_kb=64155296, disksize=31325 * MIB, zram_used=5 * MIB)
    result = _converge(host)
    assert (result.returncode, result.stdout) == (0, "unchanged 31325MiB\n")
    assert _calls(host) == []


def test_busy_device_keeps_its_size_until_boot(tmp_path: Path) -> None:
    host = _host(tmp_path, mem_kb=64155296, disksize=8192 * MIB, zram_used=4096)
    result = _converge(host)
    assert result.returncode == 0
    assert result.stdout.startswith("busy: 8192MiB holds 4096 bytes; 31325MiB")
    assert _calls(host) == []


@pytest.mark.parametrize("reset_clears", [True, False])
def test_idle_device_is_recreated_at_the_configured_size(tmp_path: Path, reset_clears: bool) -> None:
    host = _host(tmp_path, mem_kb=64155296, disksize=8192 * MIB, zram_used=0, reset_clears=reset_clears)
    result = _converge(host)
    assert (result.returncode, result.stdout) == (0, "resized 8192MiB -> 31325MiB\n"), result.stderr
    assert _calls(host) == [
        "systemctl daemon-reload",
        "systemctl stop dev-zram0.swap systemd-zram-setup@zram0.service",
        "systemctl reset-failed systemd-zram-setup@zram0.service",
        "systemctl start dev-zram0.swap",
    ]
    # The reset file is written only when the unit's own reset left a size.
    assert (host["sys"] / "zram0/reset").exists() is (not reset_clears)


def test_bad_expression_fails_before_touching_the_device(tmp_path: Path) -> None:
    host = _host(tmp_path, mem_kb=64155296, disksize=8192 * MIB, zram_used=0)
    result = _converge(host, "__import__('os').system('true')")
    assert result.returncode == 2
    assert "unsupported zram-size expression" in result.stderr or "only min() and max()" in result.stderr
    assert _calls(host) == []


def test_failing_systemctl_is_reported(tmp_path: Path) -> None:
    host = _host(tmp_path, mem_kb=64155296, disksize=8192 * MIB, zram_used=0)
    _stub(Path(host["env"]["PATH"].split(":")[0]), "systemctl", 'echo "unit not found" >&2; exit 5\n')
    result = _converge(host)
    assert result.returncode == 1
    assert "systemctl daemon-reload exited 5: unit not found" in result.stderr
