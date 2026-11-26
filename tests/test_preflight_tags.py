"""The role's pre-flight checks run whatever --tags are given (Curiosity-Corp/catalog#165).

The quickstarts run the role with --tags. Untagged tasks do not run then, so
the root check, the managed-user checks and the profile check were skipped: a
missing --become or a missing account showed up later as unrelated errors, and
the troubleshooting row `This role must run as root` could not appear.

Every task in main.yml's "Pre-flight Validation" section, and every task in
user-preflight.yml, carries `always` (runs under any --tags) and `preflight`
(a preview that cannot become root skips them by name). The host-local vars
file the checks read is loaded under `always` too.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "roles/developer_workstation/tasks"
MAIN = TASKS / "main.yml"
PREFLIGHT_TAGS = {"always", "preflight"}


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_multi_constructor("!", lambda loader, suffix, node: loader.construct_scalar(node))


def _load(path: Path) -> list[dict]:
    return yaml.load(path.read_text(), _Loader)


def _tags(task: dict) -> set[str]:
    tags = task.get("tags") or []
    return {tags} if isinstance(tags, str) else set(tags)


def _section(header: str) -> list[dict]:
    """The top-level main.yml tasks under a `# <header>` banner, up to the next task with other tags."""
    text = MAIN.read_text()
    match = re.search(rf"^# {re.escape(header)}\n# =+\n", text, flags=re.MULTILINE)
    assert match, f"main.yml has no '{header}' section"
    names = re.findall(r"^- name: (.+)$", text[match.end():], flags=re.MULTILINE)
    by_name = {task["name"]: task for task in _load(MAIN)}
    section = []
    for name in names:
        task = by_name[name]
        if not section or _tags(task) & PREFLIGHT_TAGS:
            section.append(task)
        else:
            break
    return section


PREFLIGHT = _section("Pre-flight Validation")
EXPECTED_CHECKS = {
    "Verify running as root",
    "Validate the managed user list",
    "Verify every managed user exists and has a home directory",
    "Verify workstation profile is valid",
}


def test_preflight_section_holds_every_documented_check() -> None:
    assert EXPECTED_CHECKS <= {task["name"] for task in PREFLIGHT}


@pytest.mark.parametrize("task", PREFLIGHT, ids=lambda task: task["name"])
def test_preflight_check_runs_under_any_tags(task: dict) -> None:
    assert PREFLIGHT_TAGS <= _tags(task), (
        f"pre-flight task '{task['name']}' must be tagged {sorted(PREFLIGHT_TAGS)}, "
        "or a run with --tags skips it"
    )


def test_every_preflight_assertion_is_in_the_checked_section() -> None:
    """A new top-level assert placed before the first tagged task must join the section."""
    tasks = _load(MAIN)
    first_other = next(
        index for index, task in enumerate(tasks)
        if _tags(task) and not _tags(task) & {"always"}
    )
    untagged_asserts = [
        task["name"] for task in tasks[:first_other]
        if "ansible.builtin.assert" in task and not PREFLIGHT_TAGS <= _tags(task)
    ]
    assert not untagged_asserts, f"pre-flight assertions without {sorted(PREFLIGHT_TAGS)}: {untagged_asserts}"


def test_per_user_checks_inherit_the_tags() -> None:
    include = next(task for task in PREFLIGHT if "ansible.builtin.include_tasks" in task)
    spec = include["ansible.builtin.include_tasks"]
    assert isinstance(spec, dict) and spec["file"] == "user-preflight.yml"
    # A dynamic include passes its tags on only through apply (catalog#104).
    assert PREFLIGHT_TAGS <= set(spec.get("apply", {}).get("tags", []))


def test_local_vars_the_checks_read_load_under_any_tags() -> None:
    tasks = {task["name"]: task for task in _load(MAIN)}
    for name in (
        "Check for local workstation vars file",
        "Load local workstation vars",
        "Derive dev_user_home when only dev_user was locally overridden",
    ):
        assert "always" in _tags(tasks[name]), f"'{name}' must be tagged always"


def test_always_is_reserved_for_the_preflight_and_its_inputs() -> None:
    """`always` widens every tagged run; keep it to the tasks this file names."""
    allowed = EXPECTED_CHECKS | {
        "Check for local workstation vars file",
        "Load local workstation vars",
        "Derive dev_user_home when only dev_user was locally overridden",
    }
    tagged = {task["name"] for task in _load(MAIN) if "always" in _tags(task)}
    assert tagged == allowed
