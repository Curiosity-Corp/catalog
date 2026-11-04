"""Regression checks for the opt-in agent CPU policy (docs/agent-cpu-policy.md).

Nothing here touches the machine's systemd or cgroups: templates are rendered
with Jinja2, the launcher and shell functions run against stub `systemd-run`
and `systemctl` programs, and the measurement tool is exercised in `bare` mode
or with a stub launcher.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import jinja2
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
TEMPLATES = ROLE / "templates"
TASK_FILE = ROLE / "tasks/agent-cpu-policy.yml"
BENCH = ROLE / "files/curiosity-agent-cpu-bench"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
MAIN = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
HANDLERS = yaml.safe_load((ROLE / "handlers/main.yml").read_text())
TASKS = yaml.safe_load(TASK_FILE.read_text())
DOC = (ROOT / "docs/agent-cpu-policy.md").read_text()


def _render(name: str, **overrides: object) -> str:
    # Ansible's template module defaults to trim_blocks=True.
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES)),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        keep_trailing_newline=True,
    )
    variables = {"_agent_cpu_policy_quota": "600%", **DEFAULTS, **overrides}
    return env.get_template(name).render(**variables)


def _ansible_env() -> jinja2.Environment:
    """A bare Jinja2 environment with Ansible's `bool` filter."""
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["bool"] = lambda value: str(value).strip().lower() in ("1", "true", "yes", "on")
    return env


def _named(prefix: str) -> dict:
    return next(t for t in TASKS if str(t.get("name", "")).startswith(prefix))


# ---------------------------------------------------------------------------
# Opt-in and gating
# ---------------------------------------------------------------------------


def test_policy_is_off_by_default_and_public_defaults_are_generic() -> None:
    assert DEFAULTS["agent_cpu_policy_enabled"] is False
    assert DEFAULTS["agent_cpu_policy_quota"] == "auto"
    assert DEFAULTS["agent_cpu_policy_weight"] < 100  # below systemd's default weight
    assert DEFAULTS["agent_cpu_policy_slice"].endswith(".slice")
    for key, value in DEFAULTS.items():
        if key.startswith("agent_cpu_policy_"):
            assert "developerdojo" not in str(value).lower()
    # No host is opted in through a hardware profile either.
    assert not any(
        "agent_cpu_policy" in profile for profile in DEFAULTS["hardware_profiles"].values()
    )


def test_main_includes_the_policy_on_systemd_desktops_only() -> None:
    include = next(
        t for t in MAIN if str(t.get("name", "")).startswith("Include agent CPU policy")
    )
    assert include["ansible.builtin.include_tasks"]["file"] == "agent-cpu-policy.yml"
    assert include["ansible.builtin.include_tasks"]["apply"]["tags"] == ["agent-cpu-policy", "system"]
    assert include["tags"] == ["agent-cpu-policy", "system"]
    conditions = " ".join(include["when"])
    assert "workstation_profile in ['desktop', 'thin-client', 'byod-kiosk']" in conditions
    assert "ansible_service_mgr" in conditions and "systemd" in conditions
    assert "docker" in conditions and "lxc" in conditions
    # The enabled switch is handled inside the group so that false retires files.
    assert "agent_cpu_policy_enabled" not in conditions


def test_task_file_installs_only_its_own_files() -> None:
    destinations = {
        str(task[module]["dest"])
        for task in _flatten(TASKS)
        for module in ("ansible.builtin.template", "ansible.builtin.copy")
        if module in task
    }
    assert destinations == {
        "{{ agent_cpu_policy_slice_path }}",
        "{{ agent_cpu_policy_runner_path }}",
        "{{ agent_cpu_policy_bench_path }}",
        "{{ agent_cpu_policy_shell_path }}",
    }
    text = TASK_FILE.read_text()
    for forbidden in ("ziti", "user-{{", "system.slice", "user.slice", "sysctl", "grub"):
        assert forbidden not in text.replace("systemd user manager", ""), forbidden
    assert DEFAULTS["agent_cpu_policy_slice_path"].startswith("/etc/systemd/user/")


def _flatten(tasks: list[dict]) -> list[dict]:
    flat = []
    for task in tasks:
        flat.append(task)
        flat.extend(_flatten(task.get("block", [])))
    return flat


def test_switching_off_retires_every_installed_file() -> None:
    retire = _named("Retire the agent CPU policy")
    assert retire["ansible.builtin.file"]["state"] == "absent"
    assert set(retire["loop"]) == {
        "{{ agent_cpu_policy_shell_path }}",
        "{{ agent_cpu_policy_runner_path }}",
        "{{ agent_cpu_policy_bench_path }}",
        "{{ agent_cpu_policy_slice_path }}",
    }
    # Evaluate the retire condition for the four enabled/supported states.
    env = _ansible_env()
    expression = "{{ " + retire["when"].strip() + " }}"
    for enabled, supported, retired in (
        (False, False, True),
        (False, True, True),
        (True, False, True),
        (True, True, False),
    ):
        got = env.from_string(expression).render(
            agent_cpu_policy_enabled=enabled, _agent_cpu_policy_supported=supported
        )
        assert got == str(retired), (enabled, supported)
    # The retire task must not read the support fact when the group was skipped.
    assert "default(false)" in retire["when"]


def test_a_reload_handler_exists_and_never_fails_the_run() -> None:
    handler = next(h for h in HANDLERS if h["name"] == "Reload agent CPU policy in user managers")
    assert handler["failed_when"] is False
    assert "dev_users" in handler["loop"]
    script = "\n".join(handler["ansible.builtin.command"]["argv"])
    assert "daemon-reload" in script and "--user" in script
    # Only reloads; never restarts or stops a user's units.
    assert "restart" not in script and "stop" not in script and "kill" not in script
    notified = [
        n
        for task in _flatten(TASKS)
        for n in ([task["notify"]] if isinstance(task.get("notify"), str) else task.get("notify", []))
    ]
    assert set(notified) == {"Reload agent CPU policy in user managers"}


def _validation_passes(**overrides: object) -> bool:
    """Evaluate every clause of the role's assert against the given variables."""
    assertion = _named("Validate agent CPU policy inputs")["ansible.builtin.assert"]
    env = _ansible_env()
    env.tests["match"] = lambda value, pattern: re.match(pattern, str(value)) is not None
    env.tests["boolean"] = lambda value: isinstance(value, bool)
    variables = {k: v for k, v in DEFAULTS.items() if k.startswith("agent_cpu_policy_")}
    variables.update(overrides)
    return all(
        env.from_string("{{ (" + clause + ") }}").render(**variables) == "True"
        for clause in assertion["that"]
    )


def test_defaults_pass_validation_and_hostile_inputs_fail() -> None:
    assert _validation_passes()
    assert _validation_passes(agent_cpu_policy_quota="800%", agent_cpu_policy_wrapped_commands=[])
    assert _validation_passes(agent_cpu_policy_wrapped_commands=["claude", "codex", "my_agent2"])
    bad = (
        {"agent_cpu_policy_quota": "0%"},
        {"agent_cpu_policy_quota": "800"},
        {"agent_cpu_policy_quota": "auto; rm -rf /"},
        {"agent_cpu_policy_share_percent": 0},
        {"agent_cpu_policy_share_percent": 101},
        {"agent_cpu_policy_weight": 0},
        {"agent_cpu_policy_weight": 10001},
        {"agent_cpu_policy_min_quota_percent": 0},
        {"agent_cpu_policy_split_across_users": "yes please"},
        {"agent_cpu_policy_wrapped_commands": "claude"},
        # A hostile command or slice name must not reach a shell function or unit.
        {"agent_cpu_policy_wrapped_commands": ["rm -rf"]},
        {"agent_cpu_policy_wrapped_commands": ["a;b"]},
        {"agent_cpu_policy_wrapped_commands": ["$(x)"]},
        {"agent_cpu_policy_wrapped_commands": ["a-b"]},
        {"agent_cpu_policy_wrapped_commands": [""]},
        {"agent_cpu_policy_slice": "../../evil.slice"},
        {"agent_cpu_policy_slice": "x.service"},
        {"agent_cpu_policy_slice": "a b.slice"},
    )
    for overrides in bad:
        assert not _validation_passes(**overrides), overrides


# ---------------------------------------------------------------------------
# Quota sizing
# ---------------------------------------------------------------------------


def _quota(vcpus: int, users: int = 1, **overrides: object) -> str:
    expression = _named("Resolve the per-user agent CPU quota")["ansible.builtin.set_fact"][
        "_agent_cpu_policy_quota"
    ]
    variables = {
        **{k: v for k, v in DEFAULTS.items() if k.startswith("agent_cpu_policy_")},
        "ansible_facts": {"processor_vcpus": vcpus},
        "dev_users": [{"name": f"u{i}"} for i in range(users)],
        **overrides,
    }
    return _ansible_env().from_string(expression).render(**variables)


@pytest.mark.parametrize(
    ("vcpus", "users", "expected"),
    [
        (1, 1, "50%"),  # floor: never zero, never a whole CPU on a 1-CPU box
        (2, 1, "100%"),  # reserve 1 CPU: min(150, 100)
        (4, 1, "300%"),  # min(300, 300)
        (8, 1, "600%"),  # 75% of 8
        (16, 1, "1200%"),
        (32, 1, "2400%"),
        (8, 2, "300%"),  # budget split across two users
        (16, 4, "300%"),
        (4, 8, "50%"),  # many users on few CPUs: floor, documented
    ],
)
def test_auto_quota_scales_with_cpu_count_and_users(vcpus: int, users: int, expected: str) -> None:
    assert _quota(vcpus, users) == expected


def test_quota_knobs() -> None:
    assert _quota(16, 4, agent_cpu_policy_split_across_users=False) == "1200%"
    assert _quota(16, agent_cpu_policy_share_percent=50) == "800%"
    assert _quota(16, agent_cpu_policy_reserve_cpus=12) == "400%"
    assert _quota(8, agent_cpu_policy_min_quota_percent=200, agent_cpu_policy_share_percent=10) == "200%"
    # An explicit percentage is used as written, whatever the CPU count or users.
    assert _quota(64, 3, agent_cpu_policy_quota="250%") == "250%"


def test_auto_quota_never_takes_every_cpu() -> None:
    for vcpus in range(2, 129):
        quota = int(_quota(vcpus).rstrip("%"))
        assert quota <= (vcpus - 1) * 100, vcpus


# ---------------------------------------------------------------------------
# Slice unit
# ---------------------------------------------------------------------------


def test_slice_unit_carries_the_bound() -> None:
    unit = _render("curiosity-agents.slice.j2", _agent_cpu_policy_quota="450%")
    assert "[Slice]" in unit
    assert "CPUQuota=450%" in unit
    assert "CPUWeight=50" in unit
    assert "CPUAccounting=yes" in unit
    # A bound only: no memory, task or IO limits, nothing that kills work.
    for other in ("MemoryMax", "MemoryHigh", "TasksMax", "IOWeight", "Delegate", "[Service]"):
        assert other not in unit


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze not installed")
def test_slice_unit_parses_with_systemd_analyze(tmp_path: Path) -> None:
    unit = tmp_path / "curiosity-agents.slice"
    unit.write_text(_render("curiosity-agents.slice.j2", _agent_cpu_policy_quota="450%"))
    # `verify` only reads the file; it does not talk to the running manager.
    result = subprocess.run(
        ["systemd-analyze", "--user", "verify", str(unit)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert "CPUQuota" not in result.stderr and "Unknown key" not in result.stderr, result.stderr
    assert "Failed to parse" not in result.stderr, result.stderr


# ---------------------------------------------------------------------------
# Launcher (curiosity-agent-run) against stub systemd programs
# ---------------------------------------------------------------------------

SYSTEMCTL_STUB = """#!/bin/sh
# Stub: `systemctl --user show <slice> -p LoadState --value` and `--status` output.
echo "systemctl $*" >> "$STUB_LOG"
case "$*" in
  *"-p LoadState --value"*) echo "${STUB_LOAD_STATE:-loaded}"; exit "${STUB_SYSTEMCTL_RC:-0}" ;;
  *) echo "LoadState=loaded"; echo "CPUQuotaPerSecUSec=6s"; echo "CPUWeight=50" ;;
esac
"""
SYSTEMD_RUN_STUB = """#!/bin/sh
echo "systemd-run $*" >> "$STUB_LOG"
# Skip the options up to `--` and run the command, as a scope would.
while [ "$1" != -- ]; do shift; done
shift
exec "$@"
"""
TARGET_STUB = """#!/bin/sh
echo "target-ran $*" >> "$STUB_LOG"
"""


def _executable(path: Path, content: str) -> Path:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def sandbox(tmp_path: Path) -> dict:
    """A PATH holding only stubs and the few real tools the launcher needs."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("sh", "grep", "id", "cat", "echo", "true"):
        found = shutil.which(tool)
        assert found
        (bindir / tool).symlink_to(found)
    _executable(bindir / "systemctl", SYSTEMCTL_STUB)
    _executable(bindir / "systemd-run", SYSTEMD_RUN_STUB)
    _executable(bindir / "agentcli", TARGET_STUB)
    runner = tmp_path / "curiosity-agent-run"
    _executable(runner, _render("curiosity-agent-run.j2"))
    log = tmp_path / "log"
    log.write_text("")
    cgroup = tmp_path / "cgroup"
    cgroup.write_text("0::/user.slice/user-1000.slice/session-3.scope\n")
    env = {
        "PATH": str(bindir),
        "STUB_LOG": str(log),
        "CURIOSITY_AGENT_CGROUP_FILE": str(cgroup),
    }
    return {"bin": bindir, "runner": runner, "log": log, "cgroup": cgroup, "env": env}


def _run(sandbox: dict, *args: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/sh", str(sandbox["runner"]), *args],
        env={**sandbox["env"], **env},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _log(sandbox: dict) -> list[str]:
    return sandbox["log"].read_text().splitlines()


def test_launcher_script_is_valid_posix_sh(tmp_path: Path) -> None:
    script = tmp_path / "curiosity-agent-run"
    script.write_text(_render("curiosity-agent-run.j2"))
    for shell in ("sh", "dash", "bash"):
        found = shutil.which(shell)
        if found:
            subprocess.run([found, "-n", str(script)], check=True)
    if shutil.which("shellcheck"):
        subprocess.run(["shellcheck", "--shell=sh", str(script)], check=True)


def test_launcher_starts_the_command_in_the_user_slice(sandbox: dict) -> None:
    result = _run(sandbox, "agentcli", "--flag", "two words")
    assert result.returncode == 0, result.stderr
    log = _log(sandbox)
    run = next(line for line in log if line.startswith("systemd-run "))
    assert "--user" in run and "--scope" in run
    assert "--slice=curiosity-agents.slice" in run
    assert run.endswith("-- agentcli --flag two words")
    assert "target-ran --flag two words" in log
    assert result.stderr == ""


def test_off_switch_runs_the_command_directly(sandbox: dict) -> None:
    result = _run(sandbox, "agentcli", "x", CURIOSITY_AGENT_CPU_POLICY="off")
    assert result.returncode == 0
    assert _log(sandbox) == ["target-ran x"]


def test_missing_slice_fails_open_with_a_warning(sandbox: dict) -> None:
    result = _run(sandbox, "agentcli", "x", STUB_LOAD_STATE="not-found")
    assert result.returncode == 0
    assert "running without the CPU bound" in result.stderr
    assert "systemd-run" not in "\n".join(_log(sandbox))
    assert "target-ran x" in _log(sandbox)


def test_unreachable_user_manager_fails_open(sandbox: dict) -> None:
    result = _run(sandbox, "agentcli", "x", STUB_SYSTEMCTL_RC="1", STUB_LOAD_STATE="")
    assert result.returncode == 0
    assert "running without the CPU bound" in result.stderr
    assert "target-ran x" in _log(sandbox)


def test_strict_mode_refuses_instead_of_running_unbounded(sandbox: dict) -> None:
    result = _run(sandbox, "agentcli", "x", STUB_LOAD_STATE="not-found", CURIOSITY_AGENT_CPU_STRICT="1")
    assert result.returncode == 70
    assert "refusing to run unbounded" in result.stderr
    assert "target-ran" not in "\n".join(_log(sandbox))


def test_missing_systemd_run_fails_open(sandbox: dict) -> None:
    (sandbox["bin"] / "systemd-run").unlink()
    result = _run(sandbox, "agentcli", "x")
    assert result.returncode == 0
    assert "systemd-run is not installed" in result.stderr
    assert "target-ran x" in _log(sandbox)


def test_command_already_in_the_slice_is_not_wrapped_again(sandbox: dict) -> None:
    sandbox["cgroup"].write_text(
        "0::/user.slice/user-1000.slice/user@1000.service/curiosity-agents.slice/run-r1.scope\n"
    )
    result = _run(sandbox, "agentcli", "x")
    assert result.returncode == 0
    assert _log(sandbox) == ["target-ran x"]


def test_usage_errors_and_unknown_commands(sandbox: dict) -> None:
    assert _run(sandbox).returncode == 64
    assert _run(sandbox, "--help").returncode == 0
    missing = _run(sandbox, "no-such-agent-command")
    assert missing.returncode == 127
    assert "command not found" in missing.stderr
    assert "systemd-run" not in "\n".join(_log(sandbox))


def test_status_reports_the_slice(sandbox: dict) -> None:
    result = _run(sandbox, "--status")
    assert result.returncode == 0
    assert "CPUQuotaPerSecUSec=6s" in result.stdout
    assert any(line.startswith("systemctl --user show curiosity-agents.slice") for line in _log(sandbox))


def test_exit_status_of_the_command_is_preserved(sandbox: dict) -> None:
    _executable(sandbox["bin"] / "failcli", "#!/bin/sh\nexit 7\n")
    assert _run(sandbox, "failcli").returncode == 7
    assert _run(sandbox, "failcli", CURIOSITY_AGENT_CPU_POLICY="off").returncode == 7


# ---------------------------------------------------------------------------
# Shell functions (/etc/profile.d/curiosity-agent-cpu.sh)
# ---------------------------------------------------------------------------


def _shell_snippet(tmp_path: Path, **overrides: object) -> Path:
    path = tmp_path / "curiosity-agent-cpu.sh"
    path.write_text(_render("curiosity-agent-cpu-shell.sh.j2", **overrides))
    return path


@pytest.mark.parametrize("shell", ["sh", "dash", "bash"])
def test_shell_function_routes_through_the_launcher_with_escape_hatches(
    shell: str, tmp_path: Path
) -> None:
    shell_path = shutil.which(shell)
    if shell_path is None:
        pytest.skip(f"{shell} not installed")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _executable(bindir / "claude", '#!/bin/sh\necho "real-claude $*"\n')
    _executable(bindir / "curiosity-agent-run", '#!/bin/sh\necho "via-launcher $*"\n')
    snippet = _shell_snippet(tmp_path)
    base = {"PATH": f"{bindir}:/usr/bin:/bin"}

    def call(extra: dict | None = None, setup: str = "") -> str:
        script = f". {snippet}; {setup} claude a b"
        return subprocess.run(
            [shell_path, "-c", script],
            env={**base, **(extra or {})},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    assert call() == "via-launcher claude a b"
    # Escape hatch 1: the environment switch.
    assert call({"CURIOSITY_AGENT_CPU_POLICY": "off"}) == "real-claude a b"
    # Escape hatch 2: `command` bypasses the function for one call.
    out = subprocess.run(
        [shell_path, "-c", f". {snippet}; command claude a b"],
        env=base,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out == "real-claude a b"
    # Safe disable: when the launcher is gone, the function falls back to a plain run.
    (bindir / "curiosity-agent-run").unlink()
    assert call() == "real-claude a b"


def test_shell_snippet_defines_exactly_the_configured_commands(tmp_path: Path) -> None:
    text = _shell_snippet(tmp_path, agent_cpu_policy_wrapped_commands=["claude", "codex"]).read_text()
    assert text.count("() {") == 2
    assert "claude()" in text and "codex()" in text
    assert "gemini" not in text
    empty = _shell_snippet(tmp_path, agent_cpu_policy_wrapped_commands=[]).read_text()
    assert "() {" not in empty


# ---------------------------------------------------------------------------
# Measurement tool
# ---------------------------------------------------------------------------


def _load_bench():
    loader = importlib.machinery.SourceFileLoader("curiosity_agent_cpu_bench", str(BENCH))
    spec = importlib.util.spec_from_loader("curiosity_agent_cpu_bench", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["curiosity_agent_cpu_bench"] = module
    loader.exec_module(module)
    return module


bench = _load_bench()


def test_percentiles_use_nearest_rank() -> None:
    values = [float(v) for v in range(1, 101)]
    assert bench.percentile(values, 50) == 50.0
    assert bench.percentile(values, 95) == 95.0
    assert bench.percentile(values, 99) == 99.0
    assert bench.percentile([3.0], 99) == 3.0
    assert bench.percentile([], 95) == 0.0
    assert bench.summarize([1.0, 2.0, 9.0])["max"] == 9.0


def test_kernel_text_parsers() -> None:
    stat_text = "usage_usec 1000\nuser_usec 600\nnr_periods 40\nnr_throttled 12\nthrottled_usec 345\n"
    assert bench.parse_keyed_counters(stat_text)["nr_throttled"] == 12
    psi = "some avg10=1.50 avg60=2.00 avg300=3.25 total=987654\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
    parsed = bench.parse_cpu_pressure(psi)
    assert parsed["some_avg10"] == 1.5 and parsed["some_total"] == 987654.0
    cgroup = "0::/user.slice/user-1000.slice/user@1000.service/curiosity-agents.slice/run-r1.scope\n"
    assert (
        bench.slice_cgroup_path(cgroup, "curiosity-agents.slice")
        == "/user.slice/user-1000.slice/user@1000.service/curiosity-agents.slice"
    )
    assert bench.slice_cgroup_path("0::/user.slice/session-1.scope\n", "curiosity-agents.slice") is None


def test_default_workers_oversubscribe_the_cpus() -> None:
    assert bench.default_workers(1) == 2
    assert bench.default_workers(8) == 16


def _result(label: str, mode: str, cpu: float, wake_p99: float, fork_p99: float, iters: int) -> dict:
    probe = lambda p99: {"count": 10, "p50": p99 / 4, "p95": p99 / 2, "p99": p99, "max": p99}  # noqa: E731
    return {
        "label": label,
        "mode": mode,
        "results": {
            "agent_cpu_percent": cpu,
            "iterations": iters,
            "probes": {
                "wakeup_ms": probe(wake_p99),
                "forkexec_ms": probe(fork_p99),
                "resolver_ms": probe(fork_p99),
            },
            "pressure": {"some_avg10_end": 40.0 if mode == "bare" else 10.0},
        },
    }


def test_compare_passes_when_cpu_is_bounded_and_latency_improves() -> None:
    before = _result("before", "bare", 780.0, 90.0, 120.0, 40)
    after = _result("after", "policy", 590.0, 12.0, 20.0, 30)
    comparison = bench.compare_runs(before, after, 600.0, 0.10)
    assert comparison["ok"] is True
    assert all(check["ok"] for check in comparison["checks"])
    table = bench.render_markdown(comparison, before, after)
    assert "agent CPU (all workers)" in table and "PASS" in table and "FAIL" not in table


def test_compare_fails_when_the_quota_is_not_enforced() -> None:
    before = _result("before", "bare", 780.0, 90.0, 120.0, 40)
    after = _result("after", "policy", 770.0, 85.0, 118.0, 39)
    comparison = bench.compare_runs(before, after, 600.0, 0.10)
    assert comparison["ok"] is False
    assert [c for c in comparison["checks"] if not c["ok"]][0]["check"].startswith("after: agent CPU")


def test_compare_fails_when_responsiveness_gets_worse() -> None:
    before = _result("before", "bare", 780.0, 10.0, 12.0, 40)
    after = _result("after", "policy", 500.0, 90.0, 12.0, 30)
    assert bench.compare_runs(before, after, 600.0, 0.10)["ok"] is False


def _bench_args(mode: str, **overrides: object):
    parser = bench._build_parser()
    argv = [
        "run", "--label", "t", "--mode", mode, "--workload", "sleep 0.05",
        "--workers", "2", "--duration", "1.5", "--probe-interval-ms", "20",
    ]
    return parser.parse_args(argv), overrides


def test_worker_command_is_plain_in_bare_mode_and_wrapped_in_policy_mode(tmp_path: Path) -> None:
    bare, _ = _bench_args("bare")
    policy, _ = _bench_args("policy")
    policy.runner = "/usr/local/bin/curiosity-agent-run"
    bare_cmd = bench.build_worker_command(bare, str(tmp_path))
    policy_cmd = bench.build_worker_command(policy, str(tmp_path))
    assert bare_cmd[:2] == ["sh", "-c"]
    assert policy_cmd[0] == "/usr/local/bin/curiosity-agent-run"
    assert policy_cmd[1:] == bare_cmd  # identical workload, only the launcher differs


def test_bare_run_end_to_end_with_a_real_command(tmp_path: Path) -> None:
    out = tmp_path / "bare.json"
    result = subprocess.run(
        [
            sys.executable, str(BENCH), "run", "--label", "before", "--mode", "bare",
            "--workload", "sleep 0.05", "--workers", "2", "--duration", "1.5",
            "--probe-interval-ms", "20", "--workdir", str(tmp_path), "--json", str(out),
        ],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text())
    assert data["mode"] == "bare" and data["meta"]["workers"] == 2
    assert data["meta"]["slice"] is None
    assert data["results"]["iterations"] >= 1
    assert data["results"]["workload_failures"] == 0
    assert data["results"]["probes"]["wakeup_ms"]["count"] >= 1
    assert "hostname" not in json.dumps(data).lower()
    assert not list(Path(os.environ.get("TMPDIR", "/tmp")).glob("agent-cpu-bench-*"))


def test_policy_run_goes_through_the_launcher(tmp_path: Path) -> None:
    runner = _executable(
        tmp_path / "stub-runner",
        f'#!/bin/sh\necho "$$" >> {tmp_path}/runner.calls\nexec "$@"\n',
    )
    out = tmp_path / "policy.json"
    result = subprocess.run(
        [
            sys.executable, str(BENCH), "run", "--label", "after", "--mode", "policy",
            "--runner", str(runner), "--workload", "sleep 0.05", "--workers", "3",
            "--duration", "1.5", "--probe-interval-ms", "20", "--workdir", str(tmp_path),
            "--json", str(out),
        ],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert len((tmp_path / "runner.calls").read_text().split()) == 3
    assert json.loads(out.read_text())["mode"] == "policy"


def test_compare_cli_exit_status(tmp_path: Path) -> None:
    good_b, good_a = tmp_path / "b.json", tmp_path / "a.json"
    good_b.write_text(json.dumps(_result("before", "bare", 780.0, 90.0, 120.0, 40)))
    good_a.write_text(json.dumps(_result("after", "policy", 590.0, 12.0, 20.0, 30)))
    ok = subprocess.run(
        [sys.executable, str(BENCH), "compare", str(good_b), str(good_a), "--quota-percent", "600%"],
        capture_output=True, text=True, check=False,
    )
    assert ok.returncode == 0 and "PASS" in ok.stdout
    good_a.write_text(json.dumps(_result("after", "policy", 790.0, 12.0, 20.0, 30)))
    bad = subprocess.run(
        [sys.executable, str(BENCH), "compare", str(good_b), str(good_a), "--quota-percent", "600%"],
        capture_output=True, text=True, check=False,
    )
    assert bad.returncode == 1 and "FAIL" in bad.stdout


# ---------------------------------------------------------------------------
# Documentation and CI wiring
# ---------------------------------------------------------------------------


def test_docs_cover_the_required_topics() -> None:
    lowered = DOC.lower()
    for heading in (
        "## escape hatches",
        "## failure modes",
        "## safe disable",
        "## measuring before and after",
    ):
        assert heading in lowered, heading
    for phrase in (
        "agent_cpu_policy_enabled",
        "CURIOSITY_AGENT_CPU_POLICY=off",
        "CURIOSITY_AGENT_CPU_STRICT",
        "curiosity-agent-cpu-bench",
        "default off",
    ):
        assert phrase.lower() in lowered, phrase
    configuration = (ROOT / "docs/configuration.md").read_text()
    assert "agent_cpu_policy_enabled" in configuration
    assert "docs/agent-cpu-policy.md" in configuration or "agent-cpu-policy.md" in configuration
    assert "agent_cpu_policy" in (ROOT / "CHANGELOG.md").read_text()


def test_every_documented_variable_exists_in_defaults() -> None:
    documented = set(re.findall(r"agent_cpu_policy_[a-z_]+", DOC))
    assert documented
    assert documented <= set(DEFAULTS), sorted(documented - set(DEFAULTS))


def test_the_test_runs_in_ci_and_make() -> None:
    assert "tests/test_agent_cpu_policy.py" in (ROOT / ".gitlab-ci.yml").read_text()
    assert "tests/test_agent_cpu_policy.py" in (ROOT / "Makefile").read_text()
