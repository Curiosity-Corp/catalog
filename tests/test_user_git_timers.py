"""User timers that run a tool from a pinned git checkout, and their drift check.

workstation_user_git_timers renders user .service/.timer units whose ExecStart
lies inside a root-owned checkout at a pinned commit, removes hand-installed
copies, and fails the converge through curiosity-user-timer-drift when a
declared service runs code from anywhere else. These tests pin the public
contract (empty default, opt-in include), run the real drift check against
unit fixtures, and feed it units rendered from the role's own templates.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import jinja2
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
DRIFT = ROLE / "files/curiosity-user-timer-drift"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())


def run_drift(unit_dir: Path, root: Path, *units: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DRIFT), "--unit-dir", str(unit_dir), "--root", str(root), *units],
        capture_output=True,
        text=True,
        check=False,
    )


def write_service(unit_dir: Path, name: str, exec_start: str, *drop_ins: str) -> None:
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / name).write_text(
        "[Unit]\nDescription=t\n\n[Service]\nType=oneshot\n" f"ExecStart={exec_start}\n"
    )
    if drop_ins:
        drop_dir = unit_dir / f"{name}.d"
        drop_dir.mkdir(exist_ok=True)
        for index, body in enumerate(drop_ins):
            (drop_dir / f"{index:02d}-x.conf").write_text(body)


@pytest.fixture()
def layout(tmp_path: Path) -> tuple[Path, Path]:
    managed = tmp_path / "opt/user-git-tools/tool"
    (managed / "scripts").mkdir(parents=True)
    script = managed / "scripts/sync"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    return tmp_path / "home/.config/systemd/user", managed


def test_public_default_is_empty_and_include_is_opt_in() -> None:
    assert DEFAULTS["workstation_user_git_timers"] == []
    assert DEFAULTS["workstation_user_git_tools_root"].startswith("/")
    assert DEFAULTS["workstation_user_timer_drift_check"].startswith("/usr/local/")
    main = (ROLE / "tasks/main.yml").read_text()
    assert "file: user-git-timers.yml" in main
    assert "when: workstation_user_git_timers | default([]) | length > 0" in main


def test_tasks_pin_commits_keep_secrets_out_and_gate_on_drift() -> None:
    outer = (ROLE / "tasks/user-git-timers.yml").read_text()
    entry = (ROLE / "tasks/user-git-timer-entry.yml").read_text()
    # A pinned ref is a full commit SHA; an https URL may not carry credentials.
    assert "'^[0-9a-f]{40}$'" in outer
    assert "'^https://[^@\\s]+$'" in outer
    # The credential reaches git only as a file path through GIT_CONFIG_*.
    assert "GIT_CONFIG_VALUE_0" in entry and "store --file=" in entry
    assert "GIT_TERMINAL_PROMPT" in entry
    assert "_ugt_cred.stat.pw_name == 'root'" in entry
    # The checkout is root-owned; the drift check runs and is never ignored.
    assert "workstation_user_timer_drift_check" in entry
    drift_task = entry.split("Fail on timer drift outside the checkout", 1)[1].split("- name:", 1)[0]
    assert "failed_when" not in drift_task and "ignore_errors" not in drift_task
    for task_file in (outer, entry):
        assert "ignore_errors" not in task_file


def test_managed_exec_start_passes(layout: tuple[Path, Path]) -> None:
    unit_dir, managed = layout
    write_service(unit_dir, "tool-sync.service", f"{managed}/scripts/sync teams")
    result = run_drift(unit_dir, managed, "tool-sync.service")
    assert result.returncode == 0, result.stderr


def test_hand_installed_copy_fails_and_is_named(layout: tuple[Path, Path], tmp_path: Path) -> None:
    unit_dir, managed = layout
    write_service(unit_dir, "tool-sync.service", f"{tmp_path}/home/.local/bin/tool-sync")
    result = run_drift(unit_dir, managed, "tool-sync.service")
    assert result.returncode == 1
    assert "tool-sync.service" in result.stderr
    assert ".local/bin/tool-sync" in result.stderr


def test_drop_in_override_outside_fails(layout: tuple[Path, Path]) -> None:
    unit_dir, managed = layout
    write_service(
        unit_dir,
        "tool-sync.service",
        f"{managed}/scripts/sync",
        "[Service]\nExecStart=\nExecStart=/usr/local/bin/tool-sync\n",
    )
    assert run_drift(unit_dir, managed, "tool-sync.service").returncode == 1


def test_drop_in_reset_to_managed_passes_and_env_drop_in_is_ignored(layout: tuple[Path, Path]) -> None:
    unit_dir, managed = layout
    write_service(
        unit_dir,
        "tool-sync.service",
        "/home/x/.local/bin/old",
        f"[Service]\nExecStart=\nExecStart=-{managed}/scripts/sync\n",
        "[Service]\nEnvironment=PATH=/usr/bin:/bin\n",
    )
    result = run_drift(unit_dir, managed, "tool-sync.service")
    assert result.returncode == 0, result.stderr


def test_appended_exec_start_outside_fails(layout: tuple[Path, Path]) -> None:
    unit_dir, managed = layout
    write_service(
        unit_dir,
        "tool-sync.service",
        f"{managed}/scripts/sync",
        "[Service]\nExecStart=/tmp/evil\n",
    )
    assert run_drift(unit_dir, managed, "tool-sync.service").returncode == 1


@pytest.mark.parametrize(
    "exec_start",
    ["scripts/sync", "{managed}/../escape/sync", "", "{managed}-other/scripts/sync"],
)
def test_relative_escaping_empty_or_sibling_paths_fail(layout: tuple[Path, Path], exec_start: str) -> None:
    unit_dir, managed = layout
    write_service(unit_dir, "tool-sync.service", exec_start.format(managed=managed))
    assert run_drift(unit_dir, managed, "tool-sync.service").returncode == 1


def test_symlink_out_of_checkout_fails(layout: tuple[Path, Path], tmp_path: Path) -> None:
    unit_dir, managed = layout
    outside = tmp_path / "outside-sync"
    outside.write_text("#!/bin/sh\n")
    os.symlink(outside, managed / "scripts/link")
    write_service(unit_dir, "tool-sync.service", f"{managed}/scripts/link")
    assert run_drift(unit_dir, managed, "tool-sync.service").returncode == 1


def test_missing_unit_fails(layout: tuple[Path, Path]) -> None:
    unit_dir, managed = layout
    unit_dir.mkdir(parents=True)
    result = run_drift(unit_dir, managed, "absent.service")
    assert result.returncode == 1
    assert "absent.service" in result.stderr


def test_relative_root_is_a_usage_error(layout: tuple[Path, Path]) -> None:
    unit_dir, _ = layout
    assert run_drift(unit_dir, Path("relative"), "x.service").returncode == 2


def render(template: str, **context: object) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(ROLE / "templates")),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        keep_trailing_newline=True,
    )
    return env.get_template(template).render(**context)


def test_rendered_units_pass_the_drift_check(layout: tuple[Path, Path]) -> None:
    unit_dir, managed = layout
    unit = {
        "name": "tool-sync",
        "description": "Tool sync",
        "command": "scripts/sync teams",
        "on_boot_sec": "10min",
        "on_unit_active_sec": "30min",
        "environment": {"PATH": "/home/u/.npm-global/bin:/usr/bin:/bin"},
    }
    service = render("user-git-timer.service.j2", ugt_unit=unit, _ugt_checkout=str(managed))
    timer = render("user-git-timer.timer.j2", ugt_unit=unit)
    unit_dir.mkdir(parents=True)
    (unit_dir / "tool-sync.service").write_text(service)
    assert f"ExecStart={managed}/scripts/sync teams" in service
    assert 'Environment="PATH=/home/u/.npm-global/bin:/usr/bin:/bin"' in service
    assert "OnBootSec=10min" in timer and "OnUnitActiveSec=30min" in timer
    assert "Persistent=true" in timer and "WantedBy=timers.target" in timer
    assert "OnCalendar" not in timer
    result = run_drift(unit_dir, managed, "tool-sync.service")
    assert result.returncode == 0, result.stderr


def test_configuration_documents_the_contract() -> None:
    docs = (ROOT / "docs/configuration.md").read_text()
    for fragment in (
        "workstation_user_git_timers",
        "curiosity-user-timer-drift",
        "credentials_file",
        "remove_paths",
        "protected inventory",
    ):
        assert fragment in docs, f"docs/configuration.md is missing {fragment!r}"
