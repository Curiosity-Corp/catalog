"""Module payloads of the shipped plays go to disk, not the /tmp tmpfs.

catalog#176: ansible-pull on dm313red failed on 2026-11-27 and 2026-11-28 at
three different `become_user: mrh` tasks (the PQ age keypair, the npm global
update and the launcher repair) with "Module result deserialization failed:
No start of json char found", while every root task passed. AnsiballZ unpacks
each module's payload with Python's `tempfile.mkdtemp`, as the task's become
user. sudo drops TMPDIR, so that lands in /tmp, a tmpfs mounted `usrquota`
whose per-user quota (80% of the tmpfs) mrh's agent sessions had reached
(catalog#175). The write failed with EDQUOT before the module could print its
JSON, so the run reported a crash instead of a result.

The fix is the play's `environment`: Ansible puts it on the module's own
command line, inside sudo, so TMPDIR reaches every task including become_user
ones. These checks fail when a shipped play that uses become drops it or
points it back at a memory-backed or quota-limited place.
"""

from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLES = ROOT / "roles"
SHIPPED_PLAYBOOKS = sorted(
    [*(ROOT / "playbooks").glob("*.yml"), *(ROOT / "examples").glob("*.yml")]
)
# Memory-backed or per-user-quota mounts on a stock Ubuntu/Debian workstation.
TMPFS_PREFIXES = ("/tmp", "/dev/shm", "/run")


def _plays(path: Path) -> list[dict]:
    """The plays of a playbook; an empty list for a vars file."""
    document = yaml.safe_load(path.read_text())
    if not isinstance(document, list):
        return []
    return [play for play in document if isinstance(play, dict) and "hosts" in play]


def _play_environment(play: dict) -> dict:
    """A play's environment keyword merged the way Ansible merges a list."""
    environment = play.get("environment") or {}
    if isinstance(environment, list):
        merged: dict = {}
        for entry in environment:
            merged.update(entry)
        return merged
    return environment


def _becoming_plays() -> list[tuple[str, dict]]:
    plays = []
    for path in SHIPPED_PLAYBOOKS:
        for play in _plays(path):
            if play.get("become") in (True, "true", "yes"):
                plays.append((f"{path.relative_to(ROOT)}: {play.get('name')}", play))
    return plays


def _is_on_tmpfs(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in TMPFS_PREFIXES)


def _tasks(node: object):
    """Every task mapping in a parsed task file, blocks included."""
    if isinstance(node, list):
        for item in node:
            yield from _tasks(item)
    elif isinstance(node, dict):
        yield node
        for key in ("block", "rescue", "always"):
            yield from _tasks(node.get(key))


def test_the_shipped_plays_are_found() -> None:
    names = [name for name, _ in _becoming_plays()]
    assert any(name.startswith("playbooks/site.yml") for name in names), names
    assert any(name.startswith("examples/") for name in names), names


@pytest.mark.parametrize(
    "name,play", _becoming_plays(), ids=[name for name, _ in _becoming_plays()]
)
def test_play_sends_module_payloads_to_disk(name: str, play: dict) -> None:
    tmpdir = _play_environment(play).get("TMPDIR")
    assert tmpdir, (
        f"{name} uses become but sets no TMPDIR in its environment: become_user "
        "tasks then unpack their module payload in /tmp and crash once that "
        "user's /tmp quota is full (catalog#176)"
    )
    assert str(tmpdir).startswith("/"), f"{name}: TMPDIR {tmpdir!r} is not absolute"
    assert not _is_on_tmpfs(str(tmpdir)), (
        f"{name}: TMPDIR {tmpdir!r} is memory-backed or under a per-user quota"
    )


def test_role_runs_modules_as_unprivileged_users() -> None:
    """The reason the play needs TMPDIR: tasks that become a non-root user."""
    become_users = {
        str(task["become_user"])
        for path in ROLES.glob("*/tasks/*.yml")
        for task in _tasks(yaml.safe_load(path.read_text()))
        if task.get("become_user")
    }
    assert become_users - {"root"}, "no become_user task left; this check can go"


def test_no_task_points_tmpdir_back_at_tmpfs() -> None:
    offenders = []
    for path in sorted(ROLES.glob("*/tasks/*.yml")):
        for task in _tasks(yaml.safe_load(path.read_text())):
            environment = task.get("environment")
            entries = environment if isinstance(environment, list) else [environment]
            for entry in entries:
                if isinstance(entry, dict) and "TMPDIR" in entry:
                    value = str(entry["TMPDIR"])
                    if "{{" not in value and _is_on_tmpfs(value):
                        offenders.append(f"{path.relative_to(ROOT)}: {task.get('name')}")
    assert not offenders, offenders
