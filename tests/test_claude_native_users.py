"""Native Claude Code for accounts that are not desktop users (claude_native_users).

A person who works on a host over ssh only (Curiosity-Corp/catalog-internal#34:
suzanne on dm313orange) needs the same managed native Claude Code as a dev_users
account, but none of the desktop: a dev_users entry also gets the screen lock,
which refuses an account with no password. claude_native_users feeds those
accounts through the one per-user task file, claude-code-user.yml.
"""
from __future__ import annotations

import re
from pathlib import Path

import jinja2
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"


def _tasks() -> list[dict]:
    return yaml.safe_load((ROLE / "tasks/ai-assistants.yml").read_text())


def _task(name: str) -> dict:
    return next(t for t in _tasks() if t.get("name") == name)


def _env() -> jinja2.Environment:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    # The two Ansible tests the assert uses, with Ansible's semantics.
    env.tests["match"] = lambda value, pattern: re.match(pattern, value) is not None
    env.tests["search"] = lambda value, pattern: re.search(pattern, value) is not None
    env.filters["to_json"] = repr
    return env


def _expr(text: str) -> str:
    inner = text.strip()
    assert inner.startswith("{{") and inner.endswith("}}")
    return inner[2:-2]


def _loop(dev_users: list[dict], claude_native_users: list[dict]) -> list[dict]:
    loop = _task("Keep a single native Claude Code install for every managed user")["loop"]
    return _env().compile_expression(_expr(loop))(dev_users=dev_users, claude_native_users=claude_native_users)


def _valid(claude_native_users: object) -> bool:
    that = _task("Validate the Claude Code only accounts")["ansible.builtin.assert"]["that"]
    env = _env()
    return all(env.compile_expression(cond)(claude_native_users=claude_native_users) for cond in that)


MRH = {"name": "mrh", "home": "/home/mrh"}
SUZANNE = {"name": "suzanne", "home": "/home/suzanne"}


def test_default_adds_no_account() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults["claude_native_users"] == []
    assert _loop([MRH], []) == [MRH]


def test_claude_only_accounts_go_through_the_same_per_user_task_file() -> None:
    include = _task("Keep a single native Claude Code install for every managed user")
    assert include["ansible.builtin.include_tasks"]["file"] == "claude-code-user.yml"
    assert include["loop_control"]["loop_var"] == "dw_user"
    assert _loop([MRH], [SUZANNE]) == [MRH, SUZANNE]


def test_an_account_in_both_lists_is_converged_once() -> None:
    assert _loop([MRH], [MRH, SUZANNE]) == [MRH, SUZANNE]


def test_validation_runs_before_the_install_and_under_the_same_switch() -> None:
    names = [t.get("name") for t in _tasks()]
    assert names.index("Validate the Claude Code only accounts") < names.index(
        "Keep a single native Claude Code install for every managed user")
    assert _task("Validate the Claude Code only accounts")["when"] == "claude_native_enabled | default(true)"


@pytest.mark.parametrize("users", [[], [SUZANNE], [MRH, SUZANNE]])
def test_well_formed_accounts_pass(users: list[dict]) -> None:
    assert _valid(users)


@pytest.mark.parametrize("users", [
    "suzanne",
    [{"name": "suzanne"}],
    [{"name": "Suzanne", "home": "/home/suzanne"}],
    [{"name": "suzanne", "home": "home/suzanne"}],
    [{"name": "suzanne", "home": "/home/../root"}],
    [SUZANNE, SUZANNE],
])
def test_malformed_accounts_fail(users: object) -> None:
    assert not _valid(users)


def test_argument_spec_declares_the_list() -> None:
    spec = yaml.safe_load((ROLE / "meta/argument_specs.yml").read_text())
    options = spec["argument_specs"]["main"]["options"]
    assert options["claude_native_users"] == {"type": "list", "elements": "dict"}
