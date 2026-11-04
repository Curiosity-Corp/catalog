"""Failure-injection rollback drill for the software update transaction (catalog #16).

The drill runs the role's real ``update-foundation.yml``, ``update-health.yml``,
``update-rollback.yml``, ``update-manifest.yml`` and ``update-transaction.yml``
with ansible-playbook against a temporary directory tree. Only ``managed-updates.yml``
(the part that downloads and installs) is replaced by a stub that installs a
chosen artifact, so a failed health check can be injected after a
representative update. Owner/group/become are stripped from the copied task
files so the drill runs as an ordinary user and never touches /usr, /var or
the machine running it; PATH gets stub ``dpkg``/``dpkg-query`` so the result does
not depend on the host's package state.

Sequence (one shared run, several assertions):

1. a healthy update succeeds and records its digest;
2. an update whose health check fails is rolled back byte for byte, the files it
   created are removed, the manifest records status, failure reason, before and
   failed-artifact digests and the catalog commit, and a quarantine marker names
   the run;
3. a retry inside the quarantine window installs nothing and is recorded as held;
4. after the window the same update is allowed, succeeds and clears the marker.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
TASKS = ROLE / "tasks"
ANSIBLE_PLAYBOOK = shutil.which("ansible-playbook")
needs_ansible = pytest.mark.skipif(ANSIBLE_PLAYBOOK is None, reason="ansible-playbook not installed")

COPIED = (
    "update-foundation.yml",
    "update-health.yml",
    "update-rollback.yml",
    "update-manifest.yml",
    "update-snapshot-retention.yml",
    "update-transaction.yml",
)
STRIP = {"become", "become_user", "become_method", "owner", "group"}

GOOD_V1 = b"#!/bin/sh\necho tool 1.0.0\n"
GOOD_V2 = b"#!/bin/sh\necho tool 2.0.0\n"
BAD = b"#!/bin/sh\necho 'tool: broken release' >&2\nexit 1\n"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def strip_privilege(node):
    if isinstance(node, list):
        return [strip_privilege(item) for item in node]
    if isinstance(node, dict):
        return {key: strip_privilege(value) for key, value in node.items() if key not in STRIP}
    return node


STUB_UPDATES = [
    {
        "name": "Drill: install the chosen artifact over the managed tool",
        "ansible.builtin.copy": {"src": "{{ drill_artifact }}", "dest": "{{ drill_tool }}", "mode": "0755"},
        "when": "workstation_update_install_allowed | default(true)",
    },
    {
        "name": "Drill: the update also creates a new managed binary",
        "ansible.builtin.copy": {"content": "#!/bin/sh\nexit 0\n", "dest": "{{ drill_created }}", "mode": "0755"},
        "when": "workstation_update_install_allowed | default(true)",
    },
    {
        "name": "Drill: the update touches a package-owned path that rollback must not remove",
        "ansible.builtin.copy": {"content": "owned by a package\n", "dest": "{{ drill_package_owned }}", "mode": "0755"},
        "when": "workstation_update_install_allowed | default(true)",
    },
]


class Drill:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.bin = base / "bin"
        self.state = base / "state"
        self.tool = self.bin / "tool"
        self.created = self.bin / "created-by-update"
        self.package_owned = self.bin / "package-owned"
        self.manifest = self.state / "last-run.json"
        self.quarantine = self.state / "quarantine.json"
        self.tasks = base / "tasks"
        self.commit = ""
        self._prepare()

    def _prepare(self) -> None:
        self.bin.mkdir(parents=True)
        self.tasks.mkdir()
        for name in COPIED:
            document = yaml.safe_load((TASKS / name).read_text())
            (self.tasks / name).write_text(yaml.safe_dump(strip_privilege(document), sort_keys=False, width=10_000))
        (self.tasks / "managed-updates.yml").write_text(yaml.safe_dump(STUB_UPDATES, sort_keys=False))
        stubs = self.base / "stubs"
        stubs.mkdir()
        for name in ("dpkg", "dpkg-query"):
            (stubs / name).write_text("#!/bin/sh\nexit 0\n")
            (stubs / name).chmod(0o755)
        # The CI image has no git, so the catalog commit comes from a stub git too.
        self.commit = "0123456789abcdef0123456789abcdef01234567"
        (stubs / "git").write_text(f'#!/bin/sh\ncase " $* " in *" rev-parse "*) echo {self.commit}; exit 0;; esac\nexit 1\n')
        (stubs / "git").chmod(0o755)
        (self.base / "catalog").mkdir()
        self.tool.write_bytes(GOOD_V1)
        self.tool.chmod(0o755)

        self.extra = {
            "workstation_update_state_dir": str(self.state),
            "workstation_update_artifact_cache_dir": str(self.base / "cache"),
            "workstation_update_manifest_path": str(self.manifest),
            "workstation_update_sbom_path": str(self.state / "sbom.cdx.json"),
            "workstation_update_managed_paths": [str(self.tool), str(self.created), str(self.package_owned)],
            "workstation_update_non_removable_paths": [str(self.package_owned)],
            "workstation_update_rollback_excluded_paths": [],
            "latest_github_repositories": {"tool": "example/tool"},
            "latest_release_contracts": {
                "tool": {"installer": "github-binary", "verification": "github-asset-digest", "healthcheck": [str(self.tool), "--version"]}
            },
            "workstation_application_contracts": {},
            "workstation_update_user_healthchecks": [],
            "ansible_pull_dir": str(self.base / "catalog"),
            "catalog_integrity_mode": "off",
            "dev_user": "drill",
            "dev_user_home": str(self.base / "home"),
            "drill_tool": str(self.tool),
            "drill_created": str(self.created),
            "drill_package_owned": str(self.package_owned),
        }
        self.playbook = self.base / "drill.yml"
        self.playbook.write_text(
            yaml.safe_dump(
                [
                    {
                        "hosts": "localhost",
                        "gather_facts": True,
                        "gather_subset": ["date_time"],
                        "vars_files": [str(ROLE / "defaults/main.yml")],
                        "tasks": [
                            {"name": "foundation", "ansible.builtin.include_tasks": str(self.tasks / "update-foundation.yml")},
                            {"name": "transaction", "ansible.builtin.include_tasks": str(self.tasks / "update-transaction.yml")},
                        ],
                    }
                ],
                sort_keys=False,
            )
        )
        (self.base / "ansible.cfg").write_text("[defaults]\n")

    def run(self, artifact: bytes) -> subprocess.CompletedProcess:
        source = self.base / "artifact"
        source.write_bytes(artifact)
        extra = {**self.extra, "drill_artifact": str(source)}
        env = {
            **os.environ,
            "PATH": f"{self.base / 'stubs'}:{os.environ['PATH']}",
            "ANSIBLE_CONFIG": str(self.base / "ansible.cfg"),
            "ANSIBLE_HOME": str(self.base / "ansible-home"),
            "ANSIBLE_LOCAL_TEMP": str(self.base / "ansible-local"),
            "ANSIBLE_REMOTE_TEMP": str(self.base / "ansible-remote"),
            "ANSIBLE_RETRY_FILES_ENABLED": "False",
            "ANSIBLE_NOCOLOR": "1",
            "ANSIBLE_FORCE_COLOR": "0",
            "ANSIBLE_LOCALHOST_WARNING": "False",
            "ANSIBLE_INVENTORY_UNPARSED_WARNING": "False",
        }
        return subprocess.run(
            [ANSIBLE_PLAYBOOK, "-i", "localhost,", "-c", "local", "-e", json.dumps(extra), str(self.playbook)],
            capture_output=True,
            text=True,
            env=env,
            timeout=600,
            check=False,
        )

    def manifest_json(self) -> dict:
        return json.loads(self.manifest.read_text())


@pytest.fixture(scope="module")
def drill(tmp_path_factory) -> dict:
    harness = Drill(tmp_path_factory.mktemp("rollback-drill"))
    steps: dict[str, dict] = {"harness": harness}

    healthy = harness.run(GOOD_V2)
    steps["healthy"] = {"result": healthy, "manifest": harness.manifest_json() if harness.manifest.exists() else None, "tool": harness.tool.read_bytes() if harness.tool.exists() else None}
    # Reset to the pre-update state for the failure run, as if a prior release were installed.
    harness.tool.write_bytes(GOOD_V1)
    harness.created.unlink(missing_ok=True)
    harness.package_owned.unlink(missing_ok=True)

    time.sleep(1.1)  # run ids are epoch based
    failed = harness.run(BAD)
    steps["failed"] = {
        "result": failed,
        "manifest": harness.manifest_json() if harness.manifest.exists() else None,
        "tool": harness.tool.read_bytes(),
        "created_exists": harness.created.exists(),
        "package_owned_exists": harness.package_owned.exists(),
        "quarantine": json.loads(harness.quarantine.read_text()) if harness.quarantine.exists() else None,
    }

    time.sleep(1.1)
    retry = harness.run(GOOD_V2)
    steps["retry"] = {
        "result": retry,
        "manifest": harness.manifest_json(),
        "tool": harness.tool.read_bytes(),
        "quarantine_exists": harness.quarantine.exists(),
    }

    old = time.time() - 2 * 3600
    os.utime(harness.quarantine, (old, old))
    time.sleep(1.1)
    later = harness.run(GOOD_V2)
    steps["after_window"] = {
        "result": later,
        "manifest": harness.manifest_json(),
        "tool": harness.tool.read_bytes(),
        "quarantine_exists": harness.quarantine.exists(),
    }
    return steps


def output(step: dict) -> str:
    return step["result"].stdout + step["result"].stderr


# --- static: the stub stands in for something that is itself gated -----------------

def test_every_installing_task_is_gated_by_the_quarantine_decision() -> None:
    tasks = yaml.safe_load((TASKS / "managed-updates.yml").read_text())
    ungated = [task.get("name") for task in tasks if "workstation_update_install_allowed" not in str(task.get("when"))]
    assert ungated == [], f"these managed-update tasks would run during a quarantine: {ungated}"


def test_transaction_wires_rescue_to_rollback_and_always_to_manifest() -> None:
    transaction = yaml.safe_load((TASKS / "update-transaction.yml").read_text())[0]
    assert [task["name"] for task in transaction["rescue"]] == ["Restore managed software after a failed update transaction"]
    assert [task["name"] for task in transaction["always"]] == ["Write local workstation update manifest"]
    rollback = yaml.safe_load((TASKS / "update-rollback.yml").read_text())
    assert "ansible.builtin.fail" in rollback[-1], "a rolled-back pull must still fail so the service reports it"


# --- behaviour ---------------------------------------------------------------------

@needs_ansible
def test_healthy_update_succeeds_and_records_the_installed_digest(drill) -> None:
    step = drill["healthy"]
    assert step["result"].returncode == 0, output(step)
    assert step["tool"] == GOOD_V2
    manifest = step["manifest"]
    assert manifest["status"] == "success"
    after = {item["path"]: item["checksum"] for item in manifest["after"]}
    assert after[str(drill["harness"].tool)] == sha256(GOOD_V2)


@needs_ansible
def test_failed_health_check_fails_the_pull_and_restores_the_previous_binary(drill) -> None:
    step = drill["failed"]
    assert step["result"].returncode != 0, "a rolled-back update must fail the pull"
    assert step["tool"] == GOOD_V1, "the previous artifact must be restored byte for byte"
    assert "Post-update healthchecks failed for: tool" in output(step)


@needs_ansible
def test_rollback_removes_files_the_failed_update_created_but_keeps_package_owned_paths(drill) -> None:
    step = drill["failed"]
    assert step["created_exists"] is False
    assert step["package_owned_exists"] is True, "workstation_update_non_removable_paths must survive rollback (documented limitation)"


@needs_ansible
def test_manifest_records_status_reason_digests_and_catalog_commit(drill) -> None:
    harness: Drill = drill["harness"]
    manifest = drill["failed"]["manifest"]
    assert manifest["status"] == "rolled_back"
    assert manifest["catalog_commit"] == harness.commit
    assert manifest["healthcheck_failures"] == ["tool"]
    failure = manifest["failure"]
    assert "Post-update healthchecks failed for: tool" in failure["message"]
    assert failure["task"] and failure["recorded_at"]
    before = {item["path"]: item for item in manifest["before"]}
    after = {item["path"]: item for item in manifest["after"]}
    tool = str(harness.tool)
    assert before[tool]["checksum"] == sha256(GOOD_V1)
    assert after[tool]["checksum"] == sha256(BAD), "the failed artifact's digest must be recorded for diagnosis"
    assert before[str(harness.created)]["exists"] is False and after[str(harness.created)]["exists"] is True


@needs_ansible
def test_quarantine_marker_names_the_failed_run(drill) -> None:
    marker = drill["failed"]["quarantine"]
    manifest = drill["failed"]["manifest"]
    assert marker["schema"] == "curiosity.workstation.update-quarantine/v1"
    assert marker["run_id"] == manifest["run_id"]
    assert marker["status"] == "rolled_back"
    assert marker["retry_after_seconds"] == 3600
    assert marker["failure"]["message"] == manifest["failure"]["message"]


@needs_ansible
def test_snapshot_of_the_failed_run_survives_for_recovery(drill) -> None:
    harness: Drill = drill["harness"]
    run_id = drill["failed"]["manifest"]["run_id"]
    snapshot = harness.state / "snapshots" / run_id
    assert (snapshot / "tool").read_bytes() == GOOD_V1
    before = json.loads((snapshot / "before.json").read_text())
    assert {item["path"]: item["checksum"] for item in before}[str(harness.tool)] == sha256(GOOD_V1)


@needs_ansible
def test_retry_inside_the_quarantine_window_installs_nothing(drill) -> None:
    step = drill["retry"]
    assert step["result"].returncode == 0, output(step)
    assert step["tool"] == GOOD_V1, "nothing may be installed while the quarantine is active"
    manifest = step["manifest"]
    assert manifest["status"] == "held"
    assert manifest["quarantine_active"] is True and manifest["install_allowed"] is False
    assert step["quarantine_exists"] is True


@needs_ansible
def test_update_is_allowed_again_after_the_window_and_clears_the_marker(drill) -> None:
    step = drill["after_window"]
    assert step["result"].returncode == 0, output(step)
    assert step["tool"] == GOOD_V2
    assert step["manifest"]["status"] == "success"
    assert step["manifest"]["quarantine_active"] is False
    assert step["quarantine_exists"] is False
