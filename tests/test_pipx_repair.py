"""Regression checks for pipx virtualenvs stranded on a removed interpreter."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "roles/developer_workstation/tasks"


def _tasks(name: str) -> list[dict]:
    return yaml.safe_load((TASKS / name).read_text())


def test_repair_rebuilds_only_on_invalid_interpreter() -> None:
    tasks = _tasks("pipx-interpreter-repair.yml")
    probe, rebuild = tasks
    assert probe["ansible.builtin.command"]["argv"] == ["pipx", "list", "--short"]
    assert probe["changed_when"] is False
    assert probe["failed_when"] is False
    argv = rebuild["ansible.builtin.command"]["argv"]
    assert argv[:2] == ["pipx", "reinstall-all"]
    assert "--python" in argv
    assert "invalid interpreter" in rebuild["when"]
    for task in tasks:
        assert task["become_user"] == "{{ pipx_repair_user }}"
        assert task["environment"]["HOME"] == "{{ pipx_repair_home }}"


def test_repair_runs_before_every_pipx_install() -> None:
    for name in ("python-tools.yml", "hardware-pipx.yml"):
        tasks = _tasks(name)
        repair = next(
            i
            for i, t in enumerate(tasks)
            if (t.get("ansible.builtin.include_tasks") or {}).get("file")
            == "pipx-interpreter-repair.yml"
        )
        first_pipx = next(
            i for i, t in enumerate(tasks) if "community.general.pipx" in t
        )
        assert repair < first_pipx, f"{name} must repair pipx before installing"


def test_repair_interpreter_default_is_system_python() -> None:
    defaults = yaml.safe_load(
        (ROOT / "roles/developer_workstation/defaults/main.yml").read_text()
    )
    assert defaults["pipx_repair_python"] == "/usr/bin/python3"
