"""A Ziti tunnel restart never closes live ssh sessions.

Every ssh session an agent opens to a workstation over Ziti rides the tunnel, so
a bare `systemctl restart ziti-edge-tunnel` from a handler closes all of them at
once. These checks pin the role wiring and run the real script against stub `ss`,
`systemctl`, `logger` commands, so they need neither a tunnel nor root.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import yaml

ROLE = Path(__file__).resolve().parents[1] / "roles/developer_workstation"
SCRIPT = ROLE / "files/curiosity-ziti-safe-restart"


class _Loader(yaml.SafeLoader):
    """Safe loader that reads Ansible's `!unsafe` scalars as plain strings."""


_Loader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))


def _tasks(name: str) -> list[dict]:
    return yaml.load((ROLE / "tasks" / name).read_text(), Loader=_Loader)


def test_handler_runs_the_guarded_script_not_a_bare_restart() -> None:
    handlers = {h["name"]: h for h in yaml.safe_load((ROLE / "handlers/main.yml").read_text())}
    handler = handlers["Restart ziti-edge-tunnel"]
    assert "ansible.builtin.systemd" not in handler
    assert handler["ansible.builtin.script"]["cmd"] == "curiosity-ziti-safe-restart"
    assert handler["environment"]["ZITI_SAFE_UNIT"] == "{{ ziti_service_name }}"
    assert "'restarted' in" in handler["changed_when"]


def test_no_task_restarts_the_tunnel_or_sshd_directly() -> None:
    for path in sorted((ROLE / "tasks").glob("*.yml")) + [ROLE / "handlers/main.yml"]:
        for task in yaml.load(path.read_text(), Loader=_Loader) or []:
            module = task.get("ansible.builtin.systemd") or {}
            unit = str(module.get("name", ""))
            if module.get("state") == "restarted":
                assert "ziti-edge-tunnel" not in unit and "ziti_service_name" not in unit, path
                assert unit not in {"ssh", "sshd", "ssh.service", "sshd.service"}, path


def test_deferred_restart_is_applied_by_the_ziti_tasks() -> None:
    by_name = {t["name"]: t for t in _tasks("ziti.yml")}
    task = by_name["Apply a deferred Ziti tunnel restart when no ssh connection is live"]
    assert task["ansible.builtin.script"]["cmd"] == "curiosity-ziti-safe-restart --pending"


def test_script_is_executable_and_never_kills() -> None:
    text = SCRIPT.read_text()
    assert stat.S_IMODE(SCRIPT.stat().st_mode) & 0o111
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "kill" not in code


def _stubs(tmp: Path, sessions: int) -> dict[str, str]:
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    calls = tmp / "calls"
    (bin_dir / "ss").write_text("#!/bin/sh\n" + "".join(f"echo 127.0.0.1:22 peer{i}\n" for i in range(sessions)))
    (bin_dir / "systemctl").write_text(f'#!/bin/sh\necho "$@" >> {calls}\n')
    (bin_dir / "logger").write_text("#!/bin/sh\nexit 0\n")
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "ZITI_SAFE_PENDING": str(tmp / "pending")}


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(SCRIPT), *args], env=env, capture_output=True, text=True, check=False)


def test_live_ssh_connections_defer_the_restart(tmp_path: Path) -> None:
    env = _stubs(tmp_path, sessions=3)
    result = _run(env)
    assert result.returncode == 0
    assert "deferred" in result.stdout and "restarted" not in result.stdout
    assert not (tmp_path / "calls").exists()
    assert (tmp_path / "pending").exists()


def test_idle_host_restarts_the_unit_and_clears_the_marker(tmp_path: Path) -> None:
    env = _stubs(tmp_path, sessions=0)
    (tmp_path / "pending").write_text("old\n")
    result = _run(env)
    assert result.returncode == 0 and "restarted" in result.stdout
    assert (tmp_path / "calls").read_text().strip() == "restart ziti-edge-tunnel.service"
    assert not (tmp_path / "pending").exists()


def test_pending_mode_is_a_noop_without_a_marker(tmp_path: Path) -> None:
    env = _stubs(tmp_path, sessions=0)
    result = _run(env, "--pending")
    assert result.returncode == 0 and result.stdout == ""
    assert not (tmp_path / "calls").exists()


def test_pending_mode_waits_while_busy_then_restarts_when_idle(tmp_path: Path) -> None:
    env = _stubs(tmp_path, sessions=2)
    (tmp_path / "pending").write_text("old\n")
    assert "deferred" in _run(env, "--pending").stdout
    assert not (tmp_path / "calls").exists()
    (tmp_path / "bin/ss").write_text("#!/bin/sh\nexit 0\n")
    assert "restarted" in _run(env, "--pending").stdout
    assert (tmp_path / "calls").read_text().strip() == "restart ziti-edge-tunnel.service"
    assert not (tmp_path / "pending").exists()


def test_unknown_argument_is_rejected(tmp_path: Path) -> None:
    assert _run(_stubs(tmp_path, sessions=0), "--now").returncode == 2
