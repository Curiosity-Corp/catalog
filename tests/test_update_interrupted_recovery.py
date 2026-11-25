"""An update killed half way, then run again, recovers (catalog#132).

The test runs the role's own task text in a generated playbook against a
temporary directory, the pattern of
tests/test_update_summary.py::test_skipped_integration_task_runs_as_written:

* the snapshot tasks from update-foundation.yml,
* the transaction block from update-transaction.yml, with its managed-updates
  include replaced by the real "Install latest CLI archives" task from
  cli-tools.yml (it unpacks an already-downloaded archive, so no network),
* the manifest task from update-manifest.yml in the block's ``always``.

The first run is interrupted the way a power cut or ``kill -9`` interrupts a
pull: after the snapshot and the install, the generated playbook (never the
role) waits on a marker and the test SIGKILLs the whole ansible process group.
Nothing in ``rescue`` or ``always`` runs. The test then truncates one tool, which
stands in for tar being killed while it wrote that file, and runs the same
playbook again without the wait.

What it does not cover is listed in docs/architecture.md ("Interrupted
update").
"""

from __future__ import annotations

import copy
import io
import json
import os
import signal
import subprocess
import tarfile
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "roles/developer_workstation/tasks"

FOUNDATION_TASKS = [
    "Set workstation update run facts",
    "Set workstation update snapshot path",
    "Create this pull's update snapshot directory",
    "Check managed paths before the update transaction",
    "Check which managed paths are symlinks before the update transaction",
    "Build the pre-update binary inventory",
    "Snapshot existing managed binaries for rollback",
    "Save the pre-update inventory",
]
MANIFEST_TASKS = [
    "List optional integrations that are switched off on this host",
    "Write the local workstation update manifest",
]
TOOLS = {"lazygit": "v0.50.0", "zoxide": "0.9.8"}


def _load(name: str) -> list[dict]:
    return yaml.safe_load((TASKS / name).read_text())


def _task(tasks: list[dict], name: str) -> dict:
    found = [t for t in tasks if t.get("name") == name]
    assert len(found) == 1, f"expected one task named {name!r}, found {len(found)}"
    return copy.deepcopy(found[0])


def _unprivileged(task: dict) -> dict:
    """Drop root ownership so the task runs as the CI user in a temp dir."""
    for module in task.values():
        if isinstance(module, dict):
            module.pop("owner", None)
            module.pop("group", None)
    return task


def _script(name: str, version: str) -> bytes:
    return f"#!/bin/sh\necho '{name} {version}'\n".encode()


def _archive(path: Path, name: str, version: str) -> None:
    body = _script(name, version)
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(body)
        info.mode = 0o755
        info.mtime = 1_750_000_000
        tar.addfile(info, io.BytesIO(body))


def _playbook(tmp: Path, interrupt: bool) -> Path:
    state = tmp / "state"
    bindir = tmp / "bin"
    cache = tmp / "cache"
    marker = tmp / "interrupted-here"
    digests = {name: f"sha256:{index:064x}" for index, name in enumerate(TOOLS, start=1)}

    foundation = _load("update-foundation.yml")
    snapshot = [_unprivileged(_task(foundation, name)) for name in FOUNDATION_TASKS]

    install = _task(_load("cli-tools.yml"), "Install latest CLI archives")
    assert install["ansible.builtin.unarchive"]["dest"] == "/usr/local/bin"
    install["ansible.builtin.unarchive"]["dest"] = str(bindir)

    manifest_tasks = _load("update-manifest.yml")
    manifest = [_unprivileged(_task(manifest_tasks, name)) for name in MANIFEST_TASKS]

    rollback = [_unprivileged(t) for t in copy.deepcopy(_load("update-rollback.yml"))]

    transaction = _task(_load("update-transaction.yml"), "Execute workstation software update transaction with controls")
    assert [t["name"] for t in transaction["block"]] == [
        "Apply managed software updates",
        "Verify managed software after update",
    ]
    assert [t["name"] for t in transaction["always"]] == ["Write local workstation update manifest"]
    hook = [
        {"name": "Test only: mark the interruption point", "ansible.builtin.copy": {"dest": str(marker), "content": "x"}},
        {
            "name": "Test only: wait here until the test kills the run",
            "ansible.builtin.wait_for": {"path": str(tmp / "never"), "timeout": 600},
        },
    ]
    health = {
        "name": "Test stand-in for the health gate: every tool runs",
        "ansible.builtin.command": {"argv": [str(bindir / "{{ item }}")]},
        "loop": list(TOOLS),
        "changed_when": False,
    }
    transaction["block"] = [install, *(hook if interrupt else []), health]
    transaction["rescue"] = rollback
    transaction["always"] = manifest

    play = [
        {
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": True,
            "gather_subset": ["!all"],
            "vars": {
                "workstation_update_state_dir": str(state),
                "workstation_update_manifest_path": str(state / "last-run.json"),
                "workstation_update_sbom_path": str(state / "sbom.cdx.json"),
                "workstation_update_artifact_cache_dir": str(cache),
                "workstation_update_managed_paths": [str(bindir / name) for name in TOOLS],
                "workstation_update_quarantine_seconds": 3600,
                "workstation_profile": "desktop",
                "workstation_update_ring": "general",
                "workstation_catalog_commit": "unresolved",
                "catalog_integrity_mode": "off",
                "latest_release_contracts": {},
                "latest_cli_archives": [
                    {"name": name, "asset": {"digest": digests[name]}, "include": [name], "extra_opts": []}
                    for name in TOOLS
                ],
            },
            "tasks": [
                {"name": "Test setup: state directory", "ansible.builtin.file": {"path": str(state / "snapshots"), "state": "directory"}},
                *snapshot,
                transaction,
            ],
        }
    ]
    cache.mkdir(exist_ok=True)
    bindir.mkdir(exist_ok=True)
    for name, version in TOOLS.items():
        _archive(cache / f"{name}-{digests[name].removeprefix('sha256:')}.archive", name, version)
    path = tmp / ("interrupted.yml" if interrupt else "recovery.yml")
    path.write_text(yaml.safe_dump(play, sort_keys=False))
    return path


def _env() -> dict[str, str]:
    return {
        **os.environ,
        "ANSIBLE_LOCALHOST_WARNING": "False",
        "ANSIBLE_INVENTORY_UNPARSED_WARNING": "False",
        "ANSIBLE_RETRY_FILES_ENABLED": "False",
    }


def _snapshots(state: Path) -> list[Path]:
    return sorted(p for p in (state / "snapshots").iterdir() if p.is_dir())


def test_interrupted_update_is_recovered_by_the_next_run(tmp_path: Path) -> None:
    state = tmp_path / "state"
    bindir = tmp_path / "bin"
    marker = tmp_path / "interrupted-here"
    manifest = state / "last-run.json"

    # An older lazygit is installed; zoxide is new in this update.
    bindir.mkdir()
    (bindir / "lazygit").write_bytes(_script("lazygit", "v0.49.0"))
    (bindir / "lazygit").chmod(0o755)

    # Run 1: killed after the snapshot and the install, before the manifest.
    first = subprocess.Popen(
        ["ansible-playbook", "-i", "localhost,", str(_playbook(tmp_path, interrupt=True))],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=_env(),
        cwd=ROOT,
        start_new_session=True,
    )
    deadline = time.monotonic() + 300
    while not marker.exists():
        if first.poll() is not None:
            raise AssertionError("first run ended before the interruption point:\n" + first.communicate()[0])
        if time.monotonic() > deadline:
            os.killpg(first.pid, signal.SIGKILL)
            raise AssertionError("first run never reached the interruption point:\n" + first.communicate()[0])
        time.sleep(0.2)
    os.killpg(first.pid, signal.SIGKILL)
    first.communicate()
    assert first.returncode == -signal.SIGKILL

    # What an interrupted run leaves behind: a snapshot of the old tool and the
    # pre-update inventory, tools in whatever state the kill caught them, and
    # no manifest, because rescue and always never ran.
    interrupted = _snapshots(state)
    assert len(interrupted) == 1
    assert (interrupted[0] / "lazygit").read_bytes() == _script("lazygit", "v0.49.0")
    before = {item["path"]: item for item in json.loads((interrupted[0] / "before.json").read_text())}
    assert before[str(bindir / "lazygit")]["exists"] is True
    assert before[str(bindir / "zoxide")]["exists"] is False
    assert not manifest.exists()
    assert not (state / "quarantine.json").exists()
    # tar killed while writing zoxide would leave a short, broken file.
    half = _script("zoxide", TOOLS["zoxide"])[:9]
    (bindir / "zoxide").write_bytes(half)

    # Run 2: the same pull again, not interrupted.
    time.sleep(1.1)  # a new epoch second gives the second run its own run id
    second = subprocess.run(
        ["ansible-playbook", "-i", "localhost,", str(_playbook(tmp_path, interrupt=False))],
        capture_output=True,
        text=True,
        env=_env(),
        cwd=ROOT,
        timeout=600,
    )
    assert second.returncode == 0, second.stdout + second.stderr

    written = json.loads(manifest.read_text())
    assert written["schema"] == "curiosity.workstation.update/v1"
    assert written["status"] == "success"
    assert written["failure"] == {}

    # No half-installed tool: every tool is the full new build and runs.
    for name, version in TOOLS.items():
        tool = bindir / name
        assert tool.read_bytes() == _script(name, version), name
        assert os.access(tool, os.X_OK), name
        ran = subprocess.run([str(tool)], capture_output=True, text=True, check=True)
        assert ran.stdout.strip() == f"{name} {version}"
    assert sorted(p.name for p in bindir.iterdir()) == sorted(TOOLS)

    # The second run took its own snapshot, and that snapshot is the state the
    # interruption left (the broken zoxide), not the state before run 1. The
    # interrupted run's snapshot is kept, so the old lazygit is still on disk.
    snapshots = _snapshots(state)
    assert len(snapshots) == 2 and interrupted[0] in snapshots
    recovery = next(p for p in snapshots if p != interrupted[0])
    assert written["run_id"] == recovery.name
    assert (recovery / "zoxide").read_bytes() == half
    assert (interrupted[0] / "lazygit").read_bytes() == _script("lazygit", "v0.49.0")
    recorded = {item["path"]: item for item in written["before"]}
    assert recorded[str(bindir / "zoxide")]["exists"] is True
