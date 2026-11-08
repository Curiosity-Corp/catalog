"""Declarative retirement of system services and files.

workstation_system_masked_units and workstation_removed_paths let a host say
which system units must never run again and which paths must not exist. The
public defaults are empty. These tests pin the policy that makes a retired
service stay retired (stop, unlink, native /dev/null mask, reload, tripwire)
and render the real validation expressions with Ansible's own filters.
"""

from pathlib import Path

import jinja2
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
TASKS = yaml.safe_load((ROLE / "tasks/system-retirement.yml").read_text())

VALIDATE_UNITS = "Validate system unit mask declarations"
VALIDATE_PATHS = "Validate removed path declarations"
STOP = "Stop and disable the declared system units"
LINKS = "Remove stale enablement links for the declared system units"
MASK = "Enforce native systemd masks for the declared system units"
RELOAD = "Reload systemd after mask convergence"
REMOVE = "Remove the declared paths"
READ_STATE = "Read the load and active state of the declared system units"
ASSERT_UNITS = "Assert the declared system units are masked and not running"
ASSERT_PATHS = "Assert the declared paths do not exist"


def _task(name: str) -> dict:
    return next(t for t in TASKS if t.get("name") == name)


def _environment() -> jinja2.Environment:
    from ansible.plugins.filter.core import FilterModule
    from ansible.plugins.test.core import TestModule

    env = jinja2.Environment(keep_trailing_newline=True)
    env.filters.update(FilterModule().filters())
    env.tests.update(TestModule().tests())
    env.filters["default"] = jinja2.filters.do_default
    return env


def _holds(task_name: str, item: object) -> bool:
    """True when every `that` expression of the task holds for the item."""
    env = _environment()
    conditions = _task(task_name)["ansible.builtin.assert"]["that"]
    if isinstance(conditions, str):
        conditions = [conditions]
    return all(bool(env.compile_expression(expr)(item=item)) for expr in conditions)


def test_nothing_is_retired_by_default() -> None:
    assert DEFAULTS["workstation_system_masked_units"] == []
    assert DEFAULTS["workstation_removed_paths"] == []
    assert DEFAULTS["workstation_system_unit_dir"] == "/etc/systemd/system"


def test_public_defaults_carry_no_site_names() -> None:
    policy = (ROLE / "tasks/system-retirement.yml").read_text()
    for private in ("decoder", "srt-", "ziti-controller", "gen3", "viperstream"):
        assert private not in policy, f"site-specific name {private!r} in the public role"


def test_tasks_run_in_the_order_that_keeps_a_unit_retired() -> None:
    names = [t.get("name") for t in TASKS]
    order = [names.index(n) for n in (STOP, LINKS, MASK, RELOAD, REMOVE, READ_STATE, ASSERT_UNITS, ASSERT_PATHS)]
    assert order == sorted(order), "stop, unlink, mask, reload, remove, then assert"
    assert names.index(VALIDATE_UNITS) < names.index(STOP)
    assert names.index(VALIDATE_PATHS) < names.index(REMOVE)


def test_unit_is_stopped_not_merely_disabled() -> None:
    stop = _task(STOP)["ansible.builtin.systemd_service"]
    assert stop["state"] == "stopped"
    assert stop["enabled"] is False


def test_mask_is_the_native_dev_null_link() -> None:
    mask = _task(MASK)["ansible.builtin.file"]
    assert mask["src"] == "/dev/null"
    assert mask["state"] == "link"
    assert mask["force"] is True
    policy = (ROLE / "tasks/system-retirement.yml").read_text()
    assert "systemctl mask" not in policy


def test_stop_tolerates_units_absent_on_host() -> None:
    failed_when = _task(STOP)["failed_when"]
    for phrase in ("'not found'", "'could not find'", "'not loaded'", "'masked'"):
        assert phrase in failed_when, f"stop task must tolerate {phrase}"


def test_tripwire_fails_the_run_on_a_live_or_unmasked_unit() -> None:
    state = _task(READ_STATE)
    assert state["changed_when"] is False
    assert "failed_when" not in state
    task = _task(ASSERT_UNITS)
    assert "ansible.builtin.assert" in task
    masked = "LoadState=masked\nActiveState=inactive\n"
    for stdout, ok in (
        (masked, True),
        ("LoadState=masked\nActiveState=failed\n", True),
        ("LoadState=loaded\nActiveState=inactive\n", False),
        ("LoadState=masked\nActiveState=active\n", False),
        ("LoadState=masked\nActiveState=activating\n", False),
        ("LoadState=not-found\nActiveState=inactive\n", False),
    ):
        assert _holds(ASSERT_UNITS, {"stdout": stdout}) is ok, stdout


def test_path_tripwire_fails_when_a_path_exists() -> None:
    assert _holds(ASSERT_PATHS, {"stat": {"exists": False}}) is True
    assert _holds(ASSERT_PATHS, {"stat": {"exists": True}}) is False


@pytest.mark.parametrize(
    "unit, ok",
    [
        ("obsolete-helper.service", True),
        ("obsolete-helper.timer", True),
        ("getty@tty1.service", True),
        ("obsolete-helper", False),
        ("../evil.service", False),
        ("/etc/systemd/system/x.service", False),
        ("a b.service", False),
        ("", False),
    ],
)
def test_unit_names_are_validated(unit: str, ok: bool) -> None:
    assert _holds(VALIDATE_UNITS, unit) is ok


@pytest.mark.parametrize(
    "path, ok",
    [
        ("/opt/old-service", True),
        ("/opt/old-service/identities/a.json", True),
        ("/etc/ziti/identities/old.json.bak", True),
        ("/home/someone/.ziti/identities/old.json", True),
        ("/", False),
        ("/opt", False),
        ("/etc", False),
        ("/usr/local/bin/tool", False),
        ("/proc/1/environ", False),
        ("/etc/ansible/local-vars.yml", False),
        ("/etc/systemd/system/x.service", False),
        ("/etc/ssh/sshd_config", False),
        ("/home/someone", False),
        ("/opt/old-service/../../etc/passwd", False),
        ("/opt/old-service/..", False),
        ("/opt/old-*", False),
        ("opt/old-service", False),
        ("/opt/old service", False),
    ],
)
def test_removed_paths_are_validated(path: str, ok: bool) -> None:
    assert _holds(VALIDATE_PATHS, path) is ok


def test_main_reaches_system_retirement_when_either_list_is_set() -> None:
    tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
    include = next(t for t in tasks if t.get("name") == "Retire declared system services and remove declared paths")
    assert include["ansible.builtin.include_tasks"]["file"] == "system-retirement.yml"
    condition = include["when"]
    assert "workstation_system_masked_units" in condition
    assert "workstation_removed_paths" in condition


def test_configuration_documents_the_retirement_policy() -> None:
    docs = (ROOT / "docs/configuration.md").read_text()
    for term in ("workstation_system_masked_units", "workstation_removed_paths", "tripwire"):
        assert term in docs
