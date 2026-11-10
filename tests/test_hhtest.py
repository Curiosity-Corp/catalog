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

import jinja2
import pytest
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
    assert DEFAULTS["hardware_profiles"]["deskmeet-b760"]["hhtest_host"] == "dm45s2"
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
    assert DEFAULTS["hhtest_tasks_max"] == 4096
    template = (ROLE / "templates/hhtest-slice.conf.j2").read_text()
    for line in ("MemoryMax=", "MemoryHigh=", "CPUQuota=", "CPUWeight=", "TasksMax="):
        assert line in template
    service = (ROLE / "templates/hhtest-cleanup.service.j2").read_text()
    assert "User={{ hhtest_user }}" in service
    assert "work /tmp" in service


def test_tmux_is_pinned_by_checksum() -> None:
    assert DEFAULTS["hhtest_tmux_release"] == "3.7b"
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


def _age(path: Path, days: int = 10) -> None:
    past = time.time() - days * 86400
    os.utime(path, (past, past), follow_symlinks=False)


def _run_cleanup(*roots: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(CLEANUP), "3", *map(str, roots)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_cleanup_deletes_a_read_only_go_module_cache(tmp_path: Path) -> None:
    """Go makes gomodcache read-only (dirs 0555, files 0444); find used to fail on it."""
    work = tmp_path / "work"
    module = work / "gomodcache/example.com/mod@v1.0.0/internal"
    module.mkdir(parents=True)
    files = [module / "a.go", module.parent / "go.mod"]
    for file in files:
        file.write_text("x")
        file.chmod(0o444)
        _age(file)
    for directory in (module, module.parent, module.parent.parent, work / "gomodcache"):
        directory.chmod(0o555)
        _age(directory)
    try:
        result = _run_cleanup(work)
        assert result.returncode == 0, result.stderr
        assert "Permission denied" not in result.stderr
        assert not any(file.exists() for file in files)
        # Nothing newer than the age was in the cache, so only the owner's
        # write bit on directories was restored, not any file mode.
        assert os.access(module, os.W_OK)
    finally:
        for directory in work.rglob("*"):
            if directory.is_dir() and not directory.is_symlink():
                directory.chmod(0o755)


def test_cleanup_keeps_new_files_inside_a_read_only_directory(tmp_path: Path) -> None:
    work = tmp_path / "work"
    cache = work / "gomodcache/mod"
    cache.mkdir(parents=True)
    old, new = cache / "old.go", cache / "new.go"
    old.write_text("x")
    new.write_text("y")
    _age(old)
    old.chmod(0o444)
    new.chmod(0o444)
    cache.chmod(0o555)
    try:
        result = _run_cleanup(work)
        assert result.returncode == 0, result.stderr
        assert not old.exists()
        assert new.exists() and oct(new.stat().st_mode & 0o777) == "0o444"
    finally:
        cache.chmod(0o755)


def test_cleanup_never_follows_symlinks_out_of_the_roots(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    protected = outside / "protected"
    protected.mkdir(parents=True)
    victim = protected / "keep.txt"
    victim.write_text("keep")
    _age(victim)
    protected.chmod(0o555)
    work = tmp_path / "work"
    work.mkdir()
    (work / "link-to-dir").symlink_to(protected)
    _age(work / "link-to-dir")
    try:
        result = _run_cleanup(work)
        assert result.returncode == 0, result.stderr
        assert not (work / "link-to-dir").is_symlink()  # the aged link itself goes
        assert victim.read_text() == "keep"  # its target is untouched
        assert oct(protected.stat().st_mode & 0o777) == "0o555"  # and not chmodded
    finally:
        protected.chmod(0o755)


def test_cleanup_chmod_pass_is_scoped_to_real_directories_of_the_account() -> None:
    script = CLEANUP.read_text()
    chmod_lines = [line for line in script.splitlines() if "chmod" in line and not line.lstrip().startswith("#")]
    assert any("chmod u+rwx" in line for line in chmod_lines)
    pass_ = script[script.index("if ! find"):script.index("-exec chmod")]
    for guard in ('-xdev', '-mindepth 1', '-uid "$me"', "-type d", '! -uid "$me" -prune'):
        assert guard in pass_, guard
    assert "chmod -R" not in script and " -L" not in script and "-follow" not in script


def test_cleanup_never_descends_into_directories_of_other_uids() -> None:
    """/tmp holds root-owned 0700 dirs (systemd-private-*); find used to print
    "Permission denied" for each and exit 1, failing the nightly unit."""
    foreign = Path("/etc/ssl/private")
    if not foreign.is_dir() or os.access(foreign, os.R_OK | os.X_OK) or foreign.stat().st_uid == os.getuid():
        pytest.skip("needs a directory this user cannot search that another uid owns")
    result = _run_cleanup(foreign.parent)
    assert result.returncode == 0, result.stderr
    assert "Permission denied" not in result.stderr


def test_every_find_pass_prunes_foreign_directories() -> None:
    code = "\n".join(
        line for line in CLEANUP.read_text().splitlines() if not line.lstrip().startswith("#")
    )
    passes = code.split("find ")[1:]
    # chmod pass, delete pass, empty-directory pass.
    assert len(passes) == 3
    for part in passes:
        assert '\\( -type d ! -uid "$me" -prune \\)' in part
    assert "-delete" not in code


# ---- CPU confinement (hypervoid/hyperharness#885) ---------------------------------


def _render_slice(vcpus: int | None, **overrides: object) -> str:
    """Render the real defaults expressions and the real drop-in template."""
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True)
    context: dict = {
        "ansible_facts": {} if vcpus is None else {"processor_vcpus": vcpus},
        "hhtest_uid": DEFAULTS["hhtest_uid"],
    }
    for key in (
        "hhtest_memory_max",
        "hhtest_memory_high",
        "hhtest_tasks_max",
        "hhtest_cpu_count",
        "hhtest_cpu_weight",
    ):
        context[key] = DEFAULTS[key]
    context.update(overrides)
    for key in ("hhtest_vcpus", "hhtest_allowed_cpus", "hhtest_cpu_quota"):
        if key not in context:
            context[key] = env.from_string(str(DEFAULTS[key])).render(context)
    return env.from_string((ROLE / "templates/hhtest-slice.conf.j2").read_text()).render(
        context
    )


def _directives(rendered: str) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in rendered.splitlines()
        if "=" in line and not line.startswith("#")
    )


def test_cpu_defaults_are_a_small_low_weight_range() -> None:
    assert DEFAULTS["hhtest_cpu_count"] == 8
    assert DEFAULTS["hhtest_cpu_weight"] == 10


def test_28_vcpu_host_gets_the_last_eight_cores_and_a_matching_quota() -> None:
    directives = _directives(_render_slice(28))
    assert directives["AllowedCPUs"] == "20-27"
    assert directives["CPUWeight"] == "10"
    assert directives["CPUQuota"] == "800%"
    # The pre-existing memory and task caps are kept.
    assert directives["MemoryMax"] == "16G"
    assert directives["MemoryHigh"] == "14G"
    assert directives["TasksMax"] == "4096"


@pytest.mark.parametrize("vcpus", [None, 0, 4, 8, 12])
def test_small_or_unknown_host_gets_no_cpuset(vcpus: int | None) -> None:
    rendered = _render_slice(vcpus)
    assert "AllowedCPUs" not in rendered
    assert _directives(rendered)["CPUWeight"] == "10"


def test_cpuset_starts_just_above_the_small_host_threshold() -> None:
    assert "AllowedCPUs" not in _render_slice(12)
    assert _directives(_render_slice(13))["AllowedCPUs"] == "5-12"


def test_explicit_cpu_overrides_win() -> None:
    directives = _directives(
        _render_slice(
            28,
            hhtest_allowed_cpus="26-27",
            hhtest_cpu_weight=1,
            hhtest_cpu_quota="200%",
        )
    )
    assert directives["AllowedCPUs"] == "26-27"
    assert directives["CPUWeight"] == "1"
    assert directives["CPUQuota"] == "200%"


def test_cpu_inputs_are_validated() -> None:
    validate = _named(TASKS, "Validate hhtest inputs")["ansible.builtin.assert"]
    conditions = "\n".join(str(c) for c in validate["that"])
    for needle in ("hhtest_cpu_count", "hhtest_cpu_weight", "hhtest_allowed_cpus"):
        assert needle in conditions
