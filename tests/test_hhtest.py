"""Regression checks for the hhtest unprivileged test-bed account.

The account must be gated to one host, must never touch another account or an
existing unit, must hold no privileges, and the cleanup helper must only ever
delete old regular files that the invoking user owns.
"""

import os
import re
import subprocess
import time
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
TASKS_TEXT = (ROLE / "tasks/hhtest.yml").read_text()
TASKS = yaml.safe_load(TASKS_TEXT)
MAIN = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
CLEANUP = ROLE / "files/hhtest-cleanup"


def _named(tasks: list, prefix: str) -> dict:
    return next(t for t in tasks if str(t.get("name", "")).startswith(prefix))


def test_gate_is_hardware_profile_key_plus_live_hostname() -> None:
    assert "hhtest_host" not in DEFAULTS["hardware_profiles"]["generic"]
    gated = [
        name
        for name, profile in DEFAULTS["hardware_profiles"].items()
        if "hhtest_host" in profile
    ]
    assert gated == ["deskmeet-b760"]
    assert DEFAULTS["hardware_profiles"]["deskmeet-b760"]["hhtest_host"] == "deskmeet1"
    include = _named(MAIN, "Include the hhtest")
    assert "hhtest_enabled | bool" in include["when"]
    assert "ansible_facts['hostname'] == hhtest_required_hostname" in include["when"]


def test_authorized_keys_are_public_keys_only() -> None:
    keys = DEFAULTS["hardware_profiles"]["deskmeet-b760"]["hhtest_authorized_keys"]
    assert keys
    for key in keys:
        assert re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+( .*)?", key)
        assert "PRIVATE" not in key
    assert "PRIVATE KEY" not in (ROLE / "defaults/main.yml").read_text()


def test_account_has_no_privileges() -> None:
    user = _named(TASKS, "Create the hhtest account")["ansible.builtin.user"]
    assert user["groups"] == []
    assert user["append"] is False
    assert user["password"] == "*"
    assert "sudo" not in TASKS_TEXT.replace("no sudo", "")
    proof = _named(TASKS, "Prove the account has no supplementary groups")
    assert "hhtest_user" in proof["failed_when"]


def test_only_own_objects_are_written() -> None:
    assert "dev_user" in TASKS_TEXT  # only to refuse a collision
    assert "hhtest_user != dev_user" in TASKS_TEXT
    dests = re.findall(r"^\s+(?:dest|path): (.+)$", TASKS_TEXT, re.M)
    for dest in dests:
        if dest == '"{{ item }}"':  # loop over the hhtest directories below
            continue
        assert any(
            marker in dest
            for marker in ("hhtest", "/etc/systemd/system/user-{{ hhtest_uid }}")
        ) or dest.endswith('"/var/lib/systemd/linger/{{ hhtest_user }}"'), dest
    assert "loginctl" in TASKS_TEXT
    assert 'enable-linger, "{{ hhtest_user }}"' in TASKS_TEXT


def test_slice_caps_match_the_issue() -> None:
    assert DEFAULTS["hhtest_memory_max"] == "16G"
    assert DEFAULTS["hhtest_memory_high"] == "14G"
    assert DEFAULTS["hhtest_cpu_quota"] == "1200%"
    assert DEFAULTS["hhtest_tasks_max"] == 4096
    template = (ROLE / "templates/hhtest-slice.conf.j2").read_text()
    for line in ("MemoryMax=", "MemoryHigh=", "CPUQuota=", "TasksMax="):
        assert line in template
    service = (ROLE / "templates/hhtest-cleanup.service.j2").read_text()
    assert "User={{ hhtest_user }}" in service
    assert "work /tmp" in service


def test_tmux_is_pinned_by_checksum() -> None:
    assert DEFAULTS["hhtest_tmux_version"] == "3.7b"
    assert re.fullmatch(r"[0-9a-f]{64}", DEFAULTS["hhtest_tmux_sha256"])


def test_cleanup_only_removes_old_files_and_keeps_new_ones(tmp_path: Path) -> None:
    work = tmp_path / "work"
    (work / "old/sub").mkdir(parents=True)
    old = work / "old/sub/file"
    old.write_text("x")
    new = work / "new"
    new.write_text("y")
    past = time.time() - 10 * 86400
    os.utime(old, (past, past))
    for directory in (work / "old/sub", work / "old"):
        os.utime(directory, (past, past))
    result = subprocess.run(
        ["bash", str(CLEANUP), "3", str(work)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not old.exists()
    # Directories are pruned only once empty and untouched for an hour; the
    # deletion above just refreshed them, so the next nightly run removes them.
    assert (work / "old/sub").is_dir()
    assert new.exists()


def test_cleanup_rejects_bad_age(tmp_path: Path) -> None:
    for bad in ("0", "-1", "x", ""):
        result = subprocess.run(
            ["bash", str(CLEANUP), bad, str(tmp_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
