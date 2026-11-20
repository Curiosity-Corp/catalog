"""Regression checks for a science venv stranded on a replaced Python minor."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
TASKS = yaml.safe_load(
    (ROOT / "roles/developer_workstation/tasks/science-venv.yml").read_text()
)
TOOLS = yaml.safe_load(
    (ROOT / "roles/developer_workstation/tasks/science-tools.yml").read_text()
)
DEFAULTS = yaml.safe_load(
    (ROOT / "roles/developer_workstation/defaults/main.yml").read_text()
)


def _index(name: str) -> int:
    return next(i for i, t in enumerate(TASKS) if t["name"] == name)


def test_probe_checks_pip_without_failing_the_play() -> None:
    probe = TASKS[_index("Probe the science venv for a usable interpreter and pip")]
    argv = probe["ansible.builtin.command"]["argv"]
    assert argv == ["{{ science_python_venv }}/bin/python", "-c", "import pip"]
    assert probe["changed_when"] is False
    assert probe["failed_when"] is False
    assert probe["register"] == "_science_venv_probe"


def test_rebuild_clears_stale_venv_instead_of_trusting_creates_guard() -> None:
    rebuild = TASKS[_index("Create or rebuild the science venv")]
    command = rebuild["ansible.builtin.command"]
    assert "creates" not in command
    assert command["argv"] == ["python3", "-m", "venv", "--clear", "{{ science_python_venv }}"]
    assert rebuild["when"] == "_science_venv_probe.rc != 0"


def test_venv_is_repaired_before_any_pip_install() -> None:
    rebuild = _index("Create or rebuild the science venv")
    probe = _index("Probe the science venv for a usable interpreter and pip")
    first_pip = next(i for i, t in enumerate(TASKS) if "ansible.builtin.pip" in t)
    assert probe < rebuild < first_pip


def test_default_venv_path_is_unchanged() -> None:
    assert DEFAULTS["science_python_venv"] == "/opt/science-venv"


def test_system_wiring_runs_after_the_venv_is_built() -> None:
    include = TOOLS[0]
    assert include["ansible.builtin.include_tasks"]["file"] == "science-venv.yml"
    for task in TOOLS[1:]:
        assert "ansible.builtin.pip" not in task
