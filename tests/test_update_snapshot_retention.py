"""Update snapshot retention and the ansible-pull memory limit (#113).

Every pull copies the managed binaries into snapshots/<run_id> (3.3G each on
dm313red), and nothing removed them inside the age window. These checks pin
that pruning happens only after a successful transaction, keeps the newest
``workstation_update_snapshot_keep`` snapshots, and never removes the current
run's snapshot or one a quarantine marker names. The behavioural tests run the
real retention task file with ansible-playbook against a temporary state
directory, so they never touch /var/lib on the machine running them.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
TASKS = ROLE / "tasks"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
RETENTION_FILE = TASKS / "update-snapshot-retention.yml"
RETENTION = yaml.safe_load(RETENTION_FILE.read_text())
TRANSACTION = yaml.safe_load((TASKS / "update-transaction.yml").read_text())
FOUNDATION = yaml.safe_load((TASKS / "update-foundation.yml").read_text())
PULL_TIMER = yaml.safe_load((TASKS / "ansible-pull-timer.yml").read_text())
CI = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())

HOST_SUFFIX = "49960de5880e"


def _task(tasks: list[dict], name: str) -> dict:
    return next(t for t in tasks if t.get("name") == name)


def _include_file(task: dict) -> str | None:
    include = task.get("ansible.builtin.include_tasks")
    return include.get("file") if isinstance(include, dict) else include


# --------------------------------------------------------------------------- #
# Static contract
# --------------------------------------------------------------------------- #


def test_defaults() -> None:
    assert DEFAULTS["workstation_update_snapshot_keep"] == 3
    assert DEFAULTS["workstation_ansible_pull_memory_high"] == "8G"


def test_keep_count_is_validated_before_any_snapshot_is_taken() -> None:
    names = [t.get("name") for t in FOUNDATION]
    check = "Validate update snapshot retention count"
    assert names.index(check) < names.index("Create this pull's update snapshot directory")
    that = " ".join(_task(FOUNDATION, check)["ansible.builtin.assert"]["that"])
    assert "workstation_update_snapshot_keep | int >= 1" in that


def test_retention_runs_only_after_a_successful_transaction() -> None:
    block_task = TRANSACTION[0]
    assert "block" in block_task and "rescue" in block_task
    # Not inside block/rescue/always: a pruning error must not roll back a
    # healthy update, and the rescue path fails the pull before reaching it.
    for section in ("block", "rescue", "always"):
        assert all(
            _include_file(t) != "update-snapshot-retention.yml" for t in block_task[section]
        ), section
    prune = _task(TRANSACTION, "Prune old update snapshots after a successful transaction")
    assert TRANSACTION.index(prune) > TRANSACTION.index(block_task)
    assert _include_file(prune) == "update-snapshot-retention.yml"
    assert "workstation_update_status" in prune["when"]
    assert "'success'" in prune["when"]


def test_rescue_still_fails_the_pull_so_retention_is_unreachable_after_rollback() -> None:
    rollback = yaml.safe_load((TASKS / "update-rollback.yml").read_text())
    assert "ansible.builtin.fail" in rollback[-1]


def test_retention_protects_current_and_quarantined_runs() -> None:
    protected = _task(RETENTION, "Collect the snapshot run IDs that retention must never remove")
    expr = protected["ansible.builtin.set_fact"]["_workstation_update_protected_run_ids"]
    assert "workstation_update_run_id" in expr
    assert "run_id" in expr and "regex_findall" in expr
    select = _task(RETENTION, "Select update snapshots beyond the retention count")
    expr = select["ansible.builtin.set_fact"]["_workstation_update_snapshots_to_prune"]
    assert "_workstation_update_snapshots_kept + _workstation_update_protected_run_ids" in expr
    keep = _task(RETENTION, "Select the newest update snapshots to keep")
    expr = keep["ansible.builtin.set_fact"]["_workstation_update_snapshots_kept"]
    assert "sort(attribute='key', reverse=true)" in expr
    assert "batch(workstation_update_snapshot_keep | int)" in expr


def test_retention_only_considers_run_id_directories() -> None:
    find = _task(RETENTION, "Find local update snapshots for count-based retention")["ansible.builtin.find"]
    assert find["file_type"] == "directory"
    assert find["recurse"] is False
    assert find["use_regex"] is True
    assert find["patterns"] == ["^[0-9]+-[0-9A-Za-z]+$"]


def test_ansible_pull_service_has_memory_high_and_no_memory_max() -> None:
    unit = _task(PULL_TIMER, "Create ansible-pull service unit")["ansible.builtin.copy"]["content"]
    assert "MemoryHigh={{ workstation_ansible_pull_memory_high }}" in unit
    assert "MemoryMax" not in unit
    service = unit.split("[Service]", 1)[1]
    assert "MemoryHigh=" in service


def test_this_file_runs_in_the_policy_job() -> None:
    script = " ".join(CI["policy"]["script"])
    assert "tests/test_update_snapshot_retention.py" in script


# --------------------------------------------------------------------------- #
# Behaviour: run the real task file against a temporary state directory
# --------------------------------------------------------------------------- #

ANSIBLE_PLAYBOOK = shutil.which("ansible-playbook")
needs_ansible = pytest.mark.skipif(ANSIBLE_PLAYBOOK is None, reason="ansible-playbook not installed")


def _make_snapshot(snapshots: Path, run_id: str, mtime: int) -> Path:
    path = snapshots / run_id
    path.mkdir()
    (path / "before.json").write_text("[]\n")
    os.utime(path, (mtime, mtime))
    return path


def _run_retention(
    tmp_path: Path,
    current_run_id: str,
    keep: int = 3,
    status: str = "success",
) -> subprocess.CompletedProcess:
    playbook = tmp_path / "retention.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": {
                        "workstation_update_state_dir": str(tmp_path / "state"),
                        "workstation_update_run_id": current_run_id,
                        "workstation_update_snapshot_keep": keep,
                        "workstation_update_status": status,
                    },
                    "tasks": [
                        {
                            "name": "Run the retention include the way the transaction does",
                            "ansible.builtin.include_tasks": str(RETENTION_FILE),
                            "when": "(workstation_update_status | default('')) == 'success'",
                        }
                    ],
                }
            ]
        )
    )
    env = {
        **os.environ,
        "ANSIBLE_CONFIG": str(tmp_path / "ansible.cfg"),
        "ANSIBLE_LOCAL_TEMP": str(tmp_path / "ansible-local"),
        "ANSIBLE_REMOTE_TEMP": str(tmp_path / "ansible-remote"),
        "ANSIBLE_HOME": str(tmp_path / "ansible-home"),
        "ANSIBLE_RETRY_FILES_ENABLED": "False",
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_FORCE_COLOR": "0",
        "ANSIBLE_LOCALHOST_WARNING": "False",
        "ANSIBLE_INVENTORY_UNPARSED_WARNING": "False",
    }
    (tmp_path / "ansible.cfg").write_text("[defaults]\n")
    result = subprocess.run(
        [ANSIBLE_PLAYBOOK, "-i", "localhost,", "-c", "local", str(playbook)],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def _remaining(snapshots: Path) -> set[str]:
    return {p.name for p in snapshots.iterdir()}


@pytest.fixture
def snapshots(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "snapshots"
    path.mkdir(parents=True)
    return path


@needs_ansible
def test_keeps_the_newest_n_and_prunes_the_rest(tmp_path: Path, snapshots: Path) -> None:
    epochs = [1792715610, 1792735505, 1792783589, 1792808889, 1792811705, 1792822320, 1792882489]
    for epoch in epochs:
        _make_snapshot(snapshots, f"{epoch}-{HOST_SUFFIX}", epoch)
    current = f"{epochs[-1]}-{HOST_SUFFIX}"
    _run_retention(tmp_path, current)
    assert _remaining(snapshots) == {f"{e}-{HOST_SUFFIX}" for e in epochs[-3:]}


@needs_ansible
def test_sorts_by_epoch_value_not_by_name_or_mtime(tmp_path: Path, snapshots: Path) -> None:
    # "999-..." sorts after "1000-..." as a string; mtimes are deliberately
    # inverted so only a numeric epoch sort keeps the right ones.
    _make_snapshot(snapshots, f"999-{HOST_SUFFIX}", 5000)
    _make_snapshot(snapshots, f"1000-{HOST_SUFFIX}", 4000)
    _make_snapshot(snapshots, f"1001-{HOST_SUFFIX}", 3000)
    _make_snapshot(snapshots, f"20000-{HOST_SUFFIX}", 2000)
    _run_retention(tmp_path, f"20000-{HOST_SUFFIX}", keep=2)
    assert _remaining(snapshots) == {f"1001-{HOST_SUFFIX}", f"20000-{HOST_SUFFIX}"}


@needs_ansible
def test_mtime_breaks_ties_between_equal_epochs(tmp_path: Path, snapshots: Path) -> None:
    _make_snapshot(snapshots, "1792800000-aaaaaaaaaaaa", 1792800300)
    _make_snapshot(snapshots, "1792800000-bbbbbbbbbbbb", 1792800100)
    _make_snapshot(snapshots, "1792900000-cccccccccccc", 1792900000)
    _run_retention(tmp_path, "1792900000-cccccccccccc", keep=2)
    assert _remaining(snapshots) == {"1792800000-aaaaaaaaaaaa", "1792900000-cccccccccccc"}


@needs_ansible
def test_never_prunes_the_current_run_even_when_it_is_not_the_newest(
    tmp_path: Path, snapshots: Path
) -> None:
    # A clock that stepped backwards gives the current run the oldest epoch.
    for epoch in (1792900000, 1792900100, 1792900200):
        _make_snapshot(snapshots, f"{epoch}-{HOST_SUFFIX}", epoch)
    _make_snapshot(snapshots, f"1792000000-{HOST_SUFFIX}", 1792000000)
    _run_retention(tmp_path, f"1792000000-{HOST_SUFFIX}", keep=1)
    assert _remaining(snapshots) == {f"1792000000-{HOST_SUFFIX}", f"1792900200-{HOST_SUFFIX}"}


@needs_ansible
def test_never_prunes_a_snapshot_the_quarantine_marker_names(tmp_path: Path, snapshots: Path) -> None:
    for epoch in range(1792900000, 1792900600, 100):
        _make_snapshot(snapshots, f"{epoch}-{HOST_SUFFIX}", epoch)
    quarantined = f"1792900100-{HOST_SUFFIX}"
    (tmp_path / "state" / "quarantine.json").write_text(
        json.dumps({"schema": "curiosity.workstation.update-quarantine/v1", "run_id": quarantined}, indent=2)
    )
    _run_retention(tmp_path, f"1792900500-{HOST_SUFFIX}", keep=2)
    assert _remaining(snapshots) == {
        quarantined,
        f"1792900400-{HOST_SUFFIX}",
        f"1792900500-{HOST_SUFFIX}",
    }


@needs_ansible
def test_a_corrupt_quarantine_marker_does_not_fail_the_pull(tmp_path: Path, snapshots: Path) -> None:
    for epoch in range(1792900000, 1792900400, 100):
        _make_snapshot(snapshots, f"{epoch}-{HOST_SUFFIX}", epoch)
    (tmp_path / "state" / "quarantine.json").write_text('{"run_id": "1792900000-' + HOST_SUFFIX)
    _run_retention(tmp_path, f"1792900300-{HOST_SUFFIX}", keep=2)
    assert _remaining(snapshots) == {f"1792900200-{HOST_SUFFIX}", f"1792900300-{HOST_SUFFIX}"}


@needs_ansible
def test_leaves_directories_that_are_not_run_ids(tmp_path: Path, snapshots: Path) -> None:
    for epoch in (1792900000, 1792900100, 1792900200):
        _make_snapshot(snapshots, f"{epoch}-{HOST_SUFFIX}", epoch)
    (snapshots / "manual-copy").mkdir()
    (snapshots / "lost+found").mkdir()
    _run_retention(tmp_path, f"1792900200-{HOST_SUFFIX}", keep=1)
    assert _remaining(snapshots) == {"manual-copy", "lost+found", f"1792900200-{HOST_SUFFIX}"}


@needs_ansible
def test_nothing_is_pruned_when_the_transaction_did_not_succeed(tmp_path: Path, snapshots: Path) -> None:
    epochs = (1792900000, 1792900100, 1792900200, 1792900300)
    for epoch in epochs:
        _make_snapshot(snapshots, f"{epoch}-{HOST_SUFFIX}", epoch)
    _run_retention(tmp_path, f"1792900300-{HOST_SUFFIX}", keep=1, status="held")
    assert len(_remaining(snapshots)) == len(epochs)


@needs_ansible
def test_fewer_snapshots_than_the_keep_count_is_a_no_op(tmp_path: Path, snapshots: Path) -> None:
    _make_snapshot(snapshots, f"1792900000-{HOST_SUFFIX}", 1792900000)
    _run_retention(tmp_path, f"1792900000-{HOST_SUFFIX}", keep=3)
    assert _remaining(snapshots) == {f"1792900000-{HOST_SUFFIX}"}
