"""npm 12 blocks install scripts by default; Claude Code needs its postinstall."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
TASKS = yaml.safe_load((ROLE / "tasks/ai-assistants.yml").read_text())
NAMES = [task["name"] for task in TASKS]


def _task(name: str) -> dict:
    return TASKS[NAMES.index(name)]


def test_the_allowlist_is_set_before_the_refresh_and_merged() -> None:
    allow = NAMES.index("Allow the AI assistants' install scripts for global npm installs")
    assert allow < NAMES.index("Update user-scoped AI coding assistants to latest")
    task = _task("Allow the AI assistants' install scripts for global npm installs")
    argv = task["ansible.builtin.command"]["argv"]
    assert argv[1:3] == ["config", "set"] and argv[-1] == "--location=user"
    # The user's own entries are kept, and npm < 12 (no such setting) is left alone.
    assert "_ai_allow_scripts_current +" in task["vars"]["_ai_allow_scripts_wanted"]
    assert any("version('12', '>=')" in cond for cond in task["when"])
    assert task["become_user"] == "{{ dev_user }}"
    assert "@anthropic-ai/claude-code" in DEFAULTS["ai_assistant_install_scripts"]
    assert set(DEFAULTS["ai_assistant_install_scripts"]) <= set(DEFAULTS["ai_assistant_packages"])


def test_a_stub_launcher_is_repaired_after_the_refresh() -> None:
    check = NAMES.index("Check that the Claude Code launcher runs")
    repair = NAMES.index("Finish a Claude Code install whose postinstall npm blocked")
    assert NAMES.index("Update user-scoped AI coding assistants to latest") < check < repair
    task = _task("Finish a Claude Code install whose postinstall npm blocked")
    assert task["ansible.builtin.command"]["argv"] == ["node", "install.cjs"]
    assert task["ansible.builtin.command"]["chdir"].endswith("/lib/node_modules/@anthropic-ai/claude-code")
    assert "_ai_claude_check.rc != 0" in task["when"]
    assert _task("Check that the Claude Code launcher runs")["failed_when"] is False
