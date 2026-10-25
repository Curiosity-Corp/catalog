"""Regression checks for the opt-in /tmp hygiene task group.

The feature is OPT-IN, built on native systemd-tmpfiles, and must never delete
by name pattern, sweep outside /tmp, or put the aggressive rules where the
daily tmpfiles clean would read them. These checks pin those properties and
exercise the pressure helper against stub ``df`` and ``systemd-tmpfiles``
commands, so they run on any machine without touching a real /tmp. The tmpfs
inode cap (tmp_hygiene_nr_inodes) is checked by rendering its sizing rule and
its tmp.mount drop-in.
"""

import base64
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import jinja2
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
TASKS = ROLE / "tasks"
TASK_FILE = TASKS / "tmp-hygiene.yml"
SCRIPT = ROLE / "files/curiosity-tmp-pressure"
TEMPLATES = ROLE / "templates"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
HANDLERS = ROLE / "handlers/main.yml"

# Options= of systemd's vendor tmp.mount (systemd 256 and later, Ubuntu 25.04+).
VENDOR_TMP_MOUNT_OPTIONS = (
    "mode=1777,strictatime,nosuid,nodev,size=50%%,nr_inodes=1m,"
    "x-systemd.graceful-option=usrquota"
)
# The drop-in hand-placed on mrh and deskmeet1 during the 2026-10-25 incident.
HAND_PLACED_DROP_IN = (
    "# Managed by curiosity.workstation (tmp_hygiene_nr_inodes). Do not edit.\n"
    "[Mount]\n"
    "Options=mode=1777,strictatime,nosuid,nodev,size=50%%,nr_inodes=4m,"
    "x-systemd.graceful-option=usrquota\n"
)



def _include_file(task: dict) -> str | None:
    """The file an include_tasks loads, in its plain or `file:`/`apply:` form."""
    include = task.get("ansible.builtin.include_tasks")
    return include.get("file") if isinstance(include, dict) else include

def _render(name: str, **overrides: object) -> str:
    # Ansible's template module defaults to trim_blocks=True and keeps the
    # template's trailing newline.
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES)),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        keep_trailing_newline=True,
    )
    variables = {**DEFAULTS, **overrides}
    return env.get_template(name).render(**variables)


def _rules(text: str) -> list[list[str]]:
    return [
        line.split()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


# ---------------------------------------------------------------------------
# Defaults and wiring
# ---------------------------------------------------------------------------


def test_tmp_hygiene_is_off_by_default_with_documented_values() -> None:
    assert DEFAULTS["tmp_hygiene_enabled"] is False
    assert DEFAULTS["tmp_hygiene_max_age"] == "3d"
    assert DEFAULTS["tmp_hygiene_pressure_enabled"] is True
    assert DEFAULTS["tmp_hygiene_pressure_threshold"] == 85
    assert DEFAULTS["tmp_hygiene_pressure_inode_threshold"] == 85
    assert DEFAULTS["tmp_hygiene_nr_inodes"] == "auto"
    assert (
        DEFAULTS["tmp_hygiene_nr_inodes_dropin_path"]
        == "/etc/systemd/system/tmp.mount.d/50-curiosity-inodes.conf"
    )
    assert DEFAULTS["tmp_hygiene_pressure_age"] == "1d"
    assert DEFAULTS["tmp_hygiene_pressure_interval"] == "1h"
    assert DEFAULTS["tmp_hygiene_extra_exclusions"] == []


def test_default_exclusions_are_x_rules_under_tmp_and_cover_live_sessions() -> None:
    exclusions = DEFAULTS["tmp_hygiene_exclusions"]
    for rule in exclusions:
        assert re.fullmatch(r"[xX] /tmp/[^ ]+", rule), rule
        assert ".." not in rule
    paths = {rule.split()[1] for rule in exclusions}
    for required in (
        "/tmp/.X11-unix",
        "/tmp/tmux-*",
        "/tmp/ssh-*",
        "/tmp/systemd-private-%b-*",
        "/tmp/snap-private-tmp",
        "/tmp/com.google.Chrome.*",
        "/tmp/org.chromium.*",
        "/tmp/tsx-*",
        "/tmp/claude-*",
        "/tmp/.ziti",
    ):
        assert required in paths


def test_main_includes_tmp_hygiene_on_systemd_machines_only() -> None:
    main = yaml.safe_load((TASKS / "main.yml").read_text())
    entry = next(
        task
        for task in main
        if _include_file(task) == "tmp-hygiene.yml"
    )
    assert entry["tags"] == ["tmp-hygiene", "system"]
    conditions = "\n".join(entry["when"])
    assert "workstation_profile in ['desktop', 'thin-client', 'byod-kiosk']" in conditions
    assert "workspace" not in conditions
    assert "ansible_system == 'Linux'" in conditions
    assert "ansible_service_mgr" in conditions
    assert "ansible_virtualization_type" in conditions
    assert "container" in conditions
    # The include itself is not gated on the flag: the file retires its own
    # managed files when the feature is switched off.
    assert "tmp_hygiene_enabled" not in conditions


def test_task_file_gates_each_block_on_the_flags_and_retires_files() -> None:
    tasks = yaml.safe_load(TASK_FILE.read_text())
    by_name = {task["name"]: task for task in tasks}
    apply_policy = by_name["Apply /tmp hygiene policy"]
    assert apply_policy["when"] == "tmp_hygiene_enabled | bool"
    pressure = by_name["Apply /tmp pressure relief"]
    assert "tmp_hygiene_enabled | bool" in pressure["when"]
    assert "tmp_hygiene_pressure_enabled | bool" in pressure["when"]
    retire = by_name["Retire /tmp pressure relief when it is not wanted"]
    assert "tmp_hygiene_pressure_enabled" in retire["when"]
    removed = by_name["Remove the /tmp age-based cleanup drop-in when /tmp hygiene is off"]
    assert removed["ansible.builtin.file"]["state"] == "absent"
    assert removed["when"] == "not (tmp_hygiene_enabled | bool)"


def test_inputs_are_validated() -> None:
    tasks = yaml.safe_load(TASK_FILE.read_text())
    block = next(t for t in tasks if t["name"] == "Apply /tmp hygiene policy")["block"]
    check = next(t for t in block if "ansible.builtin.assert" in t)
    text = "\n".join(check["ansible.builtin.assert"]["that"])
    assert "tmp_hygiene_max_age is match" in text
    assert "tmp_hygiene_pressure_threshold | int >= 1" in text
    assert "tmp_hygiene_pressure_threshold | int <= 100" in text
    assert "tmp_hygiene_pressure_inode_threshold | int >= 1" in text
    assert "tmp_hygiene_pressure_inode_threshold | int <= 100" in text
    assert "tmp_hygiene_nr_inodes | string is match('^(auto|[1-9][0-9]*[kmg]?)?$')" in text
    assert "reject('match', '^[xX] /tmp/[^ ]+$')" in text


def test_timer_change_restarts_the_timer_and_handler_exists() -> None:
    tasks = yaml.safe_load(TASK_FILE.read_text())
    block = next(t for t in tasks if t["name"] == "Apply /tmp pressure relief")["block"]
    timer = next(
        t
        for t in block
        if "ansible.builtin.template" in t
        and t["ansible.builtin.template"]["dest"].endswith("curiosity-tmp-pressure.timer")
    )
    assert timer["notify"] == "Restart curiosity tmp-pressure timer"
    handlers = yaml.safe_load((ROLE / "handlers/main.yml").read_text())
    assert any(h["name"] == "Restart curiosity tmp-pressure timer" for h in handlers)


def test_aggressive_config_is_not_read_by_the_daily_clean() -> None:
    assert DEFAULTS["tmp_hygiene_tmpfiles_path"].startswith("/etc/tmpfiles.d/")
    # The 00- prefix wins the first-file-by-name rule over vendor and local files.
    assert Path(DEFAULTS["tmp_hygiene_tmpfiles_path"]).name.startswith("00-")
    pressure_config = DEFAULTS["tmp_hygiene_pressure_config_path"]
    assert not pressure_config.startswith(
        ("/etc/tmpfiles.d/", "/usr/lib/tmpfiles.d/", "/run/tmpfiles.d/")
    )
    assert pressure_config.startswith("/etc/curiosity/")


# ---------------------------------------------------------------------------
# Rendered templates
# ---------------------------------------------------------------------------


def test_drop_in_ages_tmp_and_excludes_live_session_paths() -> None:
    rules = _rules(_render("curiosity-tmp-hygiene.tmpfiles.conf.j2"))
    assert rules[0] == ["q", "/tmp", "1777", "root", "root", "3d"]
    # Only /tmp is ever declared, and everything after the age line is an
    # exclusion.
    for rule in rules:
        assert rule[1].startswith("/tmp")
    assert all(rule[0] in ("q", "x", "X") for rule in rules)
    assert len(rules) == 1 + len(DEFAULTS["tmp_hygiene_exclusions"])


def test_drop_in_honours_age_and_extra_exclusions() -> None:
    rules = _rules(
        _render(
            "curiosity-tmp-hygiene.tmpfiles.conf.j2",
            tmp_hygiene_max_age="7d",
            tmp_hygiene_extra_exclusions=["X /tmp/keep-me"],
        )
    )
    assert rules[0][-1] == "7d"
    assert ["X", "/tmp/keep-me"] in rules


def test_pressure_config_uses_the_shorter_age_with_the_same_exclusions() -> None:
    rules = _rules(_render("curiosity-tmp-pressure.tmpfiles.conf.j2"))
    assert rules[0] == ["q", "/tmp", "1777", "root", "root", "1d"]
    assert rules[1:] == [r.split() for r in DEFAULTS["tmp_hygiene_exclusions"]]


def test_service_unit_is_a_nice_idle_oneshot_that_sees_the_real_tmp() -> None:
    unit = _render("curiosity-tmp-pressure.service.j2")
    assert "Type=oneshot" in unit
    assert "ConditionPathIsMountPoint=/tmp" in unit
    assert "Nice=10" in unit
    assert "IOSchedulingClass=idle" in unit
    assert "Environment=TMP_PRESSURE_THRESHOLD=85" in unit
    assert "Environment=TMP_PRESSURE_INODE_THRESHOLD=85" in unit
    assert "Environment=TMP_PRESSURE_CONFIG=/etc/curiosity/tmp-pressure-tmpfiles.conf" in unit
    assert "ExecStart=/usr/local/sbin/curiosity-tmp-pressure" in unit
    assert "RuntimeDirectory=curiosity-tmp-pressure" in unit
    # Any of these would point the cleaner at a private or read-only /tmp.
    for forbidden in ("PrivateTmp", "DynamicUser", "ProtectSystem", "RemainAfterExit"):
        assert forbidden not in unit


def test_service_unit_omits_the_config_when_the_second_pass_is_disabled() -> None:
    unit = _render("curiosity-tmp-pressure.service.j2", tmp_hygiene_pressure_age="")
    assert "TMP_PRESSURE_CONFIG" not in unit
    assert "TMP_PRESSURE_THRESHOLD=85" in unit


def test_timer_is_hourly_and_not_calendar_persistent() -> None:
    timer = _render("curiosity-tmp-pressure.timer.j2")
    assert "OnBootSec=5min" in timer
    assert "OnUnitActiveSec=1h" in timer
    assert "WantedBy=timers.target" in timer
    # Persistent= only affects OnCalendar=; it would be a misleading no-op.
    assert "Persistent" not in timer


# ---------------------------------------------------------------------------
# tmpfs inode cap (tmp_hygiene_nr_inodes)
# ---------------------------------------------------------------------------


def _tasks_by_name() -> dict[str, dict]:
    return {task["name"]: task for task in yaml.safe_load(TASK_FILE.read_text())}


def _resolve_nr_inodes(requested: object, memtotal_mb: int) -> str:
    """Render the set_fact expression that turns tmp_hygiene_nr_inodes into a value."""
    expression = _tasks_by_name()["Resolve the /tmp inode cap"]["ansible.builtin.set_fact"][
        "_tmp_hygiene_nr_inodes"
    ]
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    return env.from_string(expression).render(
        tmp_hygiene_nr_inodes=requested, ansible_memtotal_mb=memtotal_mb
    )


@pytest.mark.parametrize(
    ("memtotal_mb", "expected"),
    [
        # Up to 8 GiB the tmpfs default (half of RAM pages) is at most 1m:
        # no drop-in, exactly as before.
        (7_800, ""),
        (8_192, ""),
        (16_000, "2000k"),
        # mrh: MemTotal 64155296 kB. deskmeet1: tmpfs size=32061772k (50%).
        (62_651, "7831k"),
        (62_620, "7828k"),
        # Whole mebi-counts are written as m; the cap is 16m.
        (65_536, "8m"),
        (131_072, "16m"),
        (200_000, "16m"),
    ],
)
def test_auto_inode_cap_is_tmpfs_size_in_pages_between_1m_and_16m(
    memtotal_mb: int, expected: str
) -> None:
    assert _resolve_nr_inodes("auto", memtotal_mb) == expected


@pytest.mark.parametrize(
    ("requested", "expected"), [("4m", "4m"), ("2048k", "2048k"), ("", ""), (4194304, "4194304")]
)
def test_explicit_inode_cap_overrides_auto(requested: object, expected: str) -> None:
    for memtotal_mb in (7_800, 62_651):
        assert _resolve_nr_inodes(requested, memtotal_mb) == expected


def _render_drop_in(options: str, nr_inodes: str) -> str:
    return _render(
        "curiosity-tmp-inodes.mount.conf.j2",
        _tmp_hygiene_tmp_mount_options=options,
        _tmp_hygiene_nr_inodes=nr_inodes,
    )


def test_drop_in_matches_the_hand_placed_file_byte_for_byte() -> None:
    assert _render_drop_in(VENDOR_TMP_MOUNT_OPTIONS, "4m") == HAND_PLACED_DROP_IN


def test_drop_in_only_changes_nr_inodes_in_the_vendor_options() -> None:
    rendered = _render_drop_in(VENDOR_TMP_MOUNT_OPTIONS, "7831k")
    assert rendered.splitlines()[1:] == [
        "[Mount]",
        VENDOR_TMP_MOUNT_OPTIONS.replace("nr_inodes=1m", "nr_inodes=7831k").join(
            ("Options=", "")
        ),
    ]


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ("mode=1777,nosuid,nodev", "mode=1777,nosuid,nodev,nr_inodes=2m"),
        ("nr_inodes=1m,mode=1777,nr_inodes=3m", "nr_inodes=2m,mode=1777"),
        ("", "nr_inodes=2m"),
    ],
)
def test_drop_in_appends_or_deduplicates_nr_inodes(options: str, expected: str) -> None:
    assert _render_drop_in(options, "2m").splitlines()[-1] == f"Options={expected}"


def test_drop_in_is_installed_only_for_a_derivable_tmpfs_and_otherwise_removed() -> None:
    tasks = _tasks_by_name()
    install = tasks["Install the /tmp inode cap drop-in"]
    assert install["ansible.builtin.template"]["dest"] == "{{ tmp_hygiene_nr_inodes_dropin_path }}"
    assert install["when"] == [
        "tmp_hygiene_enabled | bool",
        "_tmp_hygiene_nr_inodes | string | length > 0",
        "_tmp_hygiene_tmp_mount_derivable | bool",
    ]
    assert install["notify"] == ["Reload systemd", "Remount /tmp with the managed inode cap"]

    remove = tasks["Remove the /tmp inode cap drop-in when it is not wanted"]
    assert remove["ansible.builtin.file"] == {
        "path": "{{ tmp_hygiene_nr_inodes_dropin_path }}",
        "state": "absent",
    }
    # The exact negation of the install condition, safe when the facts were
    # never set (switch off).
    condition = " ".join(remove["when"].split())
    assert condition == (
        "not ((tmp_hygiene_enabled | bool) "
        "and (_tmp_hygiene_nr_inodes | default('') | string | length > 0) "
        "and (_tmp_hygiene_tmp_mount_derivable | default(false) | bool))"
    )
    assert remove["notify"] == "Reload systemd"

    derive = tasks["Derive the tmp.mount options for the inode cap"]
    assert derive["when"] == [
        "tmp_hygiene_enabled | bool",
        "_tmp_hygiene_nr_inodes | string | length > 0",
    ]
    show = derive["block"][0]["ansible.builtin.command"]["argv"]
    assert show == ["systemctl", "show", "--property=FragmentPath", "--value", "tmp.mount"]


def test_remount_handler_runs_after_daemon_reload_and_only_on_tmpfs() -> None:
    handlers = yaml.safe_load(HANDLERS.read_text())
    names = [h["name"] for h in handlers]
    remount_group = [h for h in handlers if h.get("listen") == "Remount /tmp with the managed inode cap"]
    assert [h["name"] for h in remount_group] == [
        "Check that /tmp is a tmpfs mount point before remounting",
        "Remount /tmp with the managed inode cap",
        "Warn when the live /tmp inode cap could not be changed",
    ]
    # Handlers run in definition order, so the drop-in is loaded first.
    assert names.index("Reload systemd") < names.index(remount_group[0]["name"])
    check, remount, _warn = remount_group
    assert check["ansible.builtin.command"]["argv"] == [
        "findmnt", "--noheadings", "--output", "FSTYPE", "--mountpoint", "/tmp",
    ]
    assert remount["ansible.builtin.command"]["argv"] == [
        "mount", "-o", "remount,nr_inodes={{ _tmp_hygiene_nr_inodes }}", "/tmp",
    ]
    assert remount["when"] == "_tmp_hygiene_tmp_fstype.stdout | trim == 'tmpfs'"


UNIT_FILES = {
    "vendor": (
        "[Unit]\nDescription=Temporary Directory /tmp\n\n[Mount]\nWhat=tmpfs\n"
        f"Where=/tmp\nType=tmpfs\nOptions={VENDOR_TMP_MOUNT_OPTIONS}\n",
        VENDOR_TMP_MOUNT_OPTIONS,
        True,
    ),
    "fstab-ext4": ("[Mount]\nWhat=/dev/sda2\nWhere=/tmp\nType=ext4\nOptions=defaults\n", "defaults", False),
    "continued": ("[Mount]\nType=tmpfs\nOptions=mode=1777,\\\n  nosuid\n", "mode=1777,\\", False),
    "spaces": ("[Mount]\nType=tmpfs\nOptions=mode=1777 nosuid\n", "mode=1777 nosuid", False),
    "no-options": ("[Mount]\nType=tmpfs\n", "", True),
}


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-core")
def test_tmp_mount_options_are_derived_only_from_a_plain_tmpfs_unit(tmp_path: Path) -> None:
    """Run the real set_fact (Ansible filters) against sample unit files."""
    derive = _tasks_by_name()["Derive the tmp.mount options for the inode cap"]["block"]
    record = next(t for t in derive if t["name"] == "Record the tmp.mount type and options")
    tasks: list[dict] = []
    for name, (unit, _options, _derivable) in UNIT_FILES.items():
        task = {**record, "name": name}
        task["vars"] = {
            **record["vars"],
            "_tmp_hygiene_tmp_mount_unit": {"content": base64.b64encode(unit.encode()).decode()},
        }
        tasks.append(task)
        tasks.append(
            {
                "ansible.builtin.copy": {
                    "dest": str(tmp_path / f"{name}.out"),
                    "content": "{{ _tmp_hygiene_tmp_mount_options }}\n"
                    "{{ _tmp_hygiene_tmp_mount_derivable | bool }}\n",
                    "mode": "0644",
                }
            }
        )
    playbook = tmp_path / "derive.yml"
    playbook.write_text(yaml.safe_dump([{"hosts": "localhost", "gather_facts": False, "tasks": tasks}]))
    env = {**os.environ, "ANSIBLE_LOCALHOST_WARNING": "false", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false"}
    result = subprocess.run(
        ["ansible-playbook", "-c", "local", "-i", "localhost,", str(playbook)],
        capture_output=True, text=True, check=False, env=env, cwd=tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for name, (_unit, options, derivable) in UNIT_FILES.items():
        assert (tmp_path / f"{name}.out").read_text().splitlines() == [options, str(derivable)], name


# ---------------------------------------------------------------------------
# Static safety of the helper
# ---------------------------------------------------------------------------


def test_helper_is_executable_and_parses() -> None:
    assert SCRIPT.stat().st_mode & stat.S_IXUSR
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_helper_never_deletes_by_pattern_or_outside_tmp() -> None:
    code = "\n".join(
        line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#")
    )
    recursive_rm = "rm -" + "rf"
    for forbidden in (
        recursive_rm,
        "rm -r ",
        "find ",
        "/var/tmp",
        "systemd-private",
        "*.png",
        "*.log",
        "pytest-of",
    ):
        assert forbidden not in code, forbidden
    # The only deletion is of the helper's own scratch file and directory.
    assert re.findall(r"\brm\b.*", code) == ['rm -f -- "$merged"']
    assert "--prefix=" in code
    assert "set -euo pipefail" in code


def test_role_files_do_not_reference_the_hand_installed_cleaner() -> None:
    for path in (TASK_FILE, SCRIPT, *TEMPLATES.glob("curiosity-tmp-*")):
        text = path.read_text()
        for legacy in ("tmp-cleaner", "tmp-pressure.sh", "node-compile-cache"):
            assert legacy not in text, f"{path.name} mentions {legacy}"


# ---------------------------------------------------------------------------
# Helper behaviour against stub df / systemd-tmpfiles
# ---------------------------------------------------------------------------

# `df -P` reads block percentages from df-sequence and `df -iP` inode
# percentages from df-inode-sequence; the last value repeats. An inode value of
# `-` mimics a filesystem that does not report inodes.
STUB_DF = """#!/bin/bash
seq="$STUB_DIR/df-sequence"
case "$1" in -i*) seq="$STUB_DIR/df-inode-sequence" ;; esac
pct=$(head -n 1 "$seq")
if [ "$(wc -l < "$seq")" -gt 1 ]; then
    tail -n +2 "$seq" > "$seq.next" && mv "$seq.next" "$seq"
fi
[ "$pct" = - ] || pct="${pct}%"
case "$1" in
-i*)
    echo "Filesystem Inodes IUsed IFree IUse% Mounted on"
    echo "tmpfs 1000 500 500 ${pct} /tmp"
    ;;
*)
    echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
    echo "tmpfs 1000 500 500 ${pct} /tmp"
    ;;
esac
"""

STUB_TMPFILES = """#!/bin/bash
echo "$*" >> "$STUB_DIR/tmpfiles-calls"
for arg in "$@"; do
    case "$arg" in
    --cat-config)
        cat "$STUB_DIR/cat-config"
        exit 0
        ;;
    --*) ;;
    *)
        n=$(wc -l < "$STUB_DIR/tmpfiles-calls")
        cp "$arg" "$STUB_DIR/seen-config-$n"
        ;;
    esac
done
exit "${STUB_TMPFILES_RC:-0}"
"""

CAT_CONFIG = """# /usr/lib/tmpfiles.d/tmp.conf
q /tmp 1777 root root 10d
q /var/tmp 1777 root root 30d

# /usr/lib/tmpfiles.d/podman.conf
x /tmp/podman-run-*
x /var/tmp/ignored-outside-tmp
X /tmp/ssh-*
x! /tmp/boot-only-excluded
r! /tmp/.X[0-9]*-lock
"""


@pytest.fixture
def stubs(tmp_path: Path) -> Path:
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    for name, body in (("df", STUB_DF), ("systemd-tmpfiles", STUB_TMPFILES)):
        path = stub_dir / name
        path.write_text(body)
        path.chmod(0o755)
    (stub_dir / "cat-config").write_text(CAT_CONFIG)
    (stub_dir / "tmpfiles-calls").write_text("")
    (tmp_path / "run").mkdir()
    return stub_dir


def _run_helper(
    stub_dir: Path,
    sequence: list[int],
    *,
    inodes: list[int | str] | None = None,
    config: Path | None = None,
    threshold: str = "85",
    inode_threshold: str | None = None,
    tmpfiles_rc: int = 0,
) -> subprocess.CompletedProcess:
    """Run the helper; inode usage defaults to a constant 10%."""
    (stub_dir / "df-sequence").write_text("\n".join(str(p) for p in sequence) + "\n")
    inode_sequence = inodes if inodes is not None else [10]
    (stub_dir / "df-inode-sequence").write_text("\n".join(str(p) for p in inode_sequence) + "\n")
    env = {
        "PATH": f"{stub_dir}:{os.environ['PATH']}",
        "STUB_DIR": str(stub_dir),
        "STUB_TMPFILES_RC": str(tmpfiles_rc),
        "TMP_PRESSURE_THRESHOLD": threshold,
        "RUNTIME_DIRECTORY": str(stub_dir.parent / "run"),
    }
    if config is not None:
        env["TMP_PRESSURE_CONFIG"] = str(config)
    if inode_threshold is not None:
        env["TMP_PRESSURE_INODE_THRESHOLD"] = inode_threshold
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )


def _calls(stub_dir: Path) -> list[str]:
    return (stub_dir / "tmpfiles-calls").read_text().splitlines()


def _aggressive_config(tmp_path: Path) -> Path:
    path = tmp_path / "pressure.conf"
    path.write_text(_render("curiosity-tmp-pressure.tmpfiles.conf.j2"))
    return path


def test_helper_is_silent_and_does_nothing_below_the_threshold(stubs: Path) -> None:
    result = _run_helper(stubs, [84])
    assert result.returncode == 0
    assert result.stdout == ""
    assert _calls(stubs) == []


def test_helper_runs_the_standard_pass_scoped_to_tmp_and_logs_before_after(stubs: Path) -> None:
    result = _run_helper(stubs, [90, 60])
    assert result.returncode == 0, result.stderr
    assert _calls(stubs) == ["--clean --prefix=/tmp"]
    assert "at 90% blocks, 10% inodes (thresholds 85% blocks, 85% inodes)" in result.stdout
    assert "cleanup done: blocks 90% -> 60%, inodes 10% -> 10%" in result.stdout
    assert "still at" not in result.stdout


def test_helper_triggers_exactly_at_the_threshold(stubs: Path) -> None:
    result = _run_helper(stubs, [85, 85], threshold="85")
    assert result.returncode == 0
    assert len(_calls(stubs)) == 1
    assert "cleanup done: blocks 85% -> 85%" in result.stdout


def test_helper_warns_when_no_second_pass_is_configured(stubs: Path) -> None:
    result = _run_helper(stubs, [92, 91])
    assert result.returncode == 0
    assert len(_calls(stubs)) == 1
    assert "no aggressive pass configured" in result.stdout
    assert "cleanup done: blocks 92% -> 91%" in result.stdout


def test_second_pass_merges_static_and_package_exclusions(stubs: Path, tmp_path: Path) -> None:
    config = _aggressive_config(tmp_path)
    result = _run_helper(stubs, [95, 90, 70], config=config)
    assert result.returncode == 0, result.stderr
    calls = _calls(stubs)
    assert calls[0] == "--clean --prefix=/tmp"
    assert calls[1] == "--cat-config"
    assert calls[2].startswith("--clean --prefix=/tmp ")
    assert "cleanup done: blocks 95% -> 70%" in result.stdout

    merged = (stubs / "seen-config-3").read_text().splitlines()
    rules = [line.split() for line in merged]
    # The shorter age comes from the static file; other files' q lines do not.
    assert rules[0] == ["q", "/tmp", "1777", "root", "root", "1d"]
    assert [r for r in rules if r[0] == "q"] == [rules[0]]
    # Static exclusions are kept, package x/X rules under /tmp are added once,
    # and rules outside /tmp, r! rules and duplicates are not.
    assert ["X", "/tmp/.X11-unix"] in rules
    assert ["x", "/tmp/podman-run-*"] in rules
    assert ["x!", "/tmp/boot-only-excluded"] in rules
    assert not any(r[1].startswith("/var/tmp") for r in rules)
    assert not any(r[0] == "r!" for r in rules)
    assert len(merged) == len(set(merged))
    # The scratch file is gone afterwards and nothing leaked into the runtime dir.
    assert list((stubs.parent / "run").iterdir()) == []


def test_second_pass_is_skipped_once_the_first_pass_is_enough(stubs: Path, tmp_path: Path) -> None:
    result = _run_helper(stubs, [95, 50], config=_aggressive_config(tmp_path))
    assert result.returncode == 0
    assert _calls(stubs) == ["--clean --prefix=/tmp"]


def test_still_over_threshold_warns_but_exits_zero(stubs: Path, tmp_path: Path) -> None:
    result = _run_helper(stubs, [97, 96, 95], config=_aggressive_config(tmp_path))
    assert result.returncode == 0
    assert "(thresholds 85% blocks, 85% inodes): nothing more can be removed safely" in result.stdout
    assert "cleanup done: blocks 97% -> 95%" in result.stdout


def test_tmpfiles_failure_does_not_abort_the_remaining_passes(stubs: Path, tmp_path: Path) -> None:
    result = _run_helper(stubs, [95, 90, 60], config=_aggressive_config(tmp_path), tmpfiles_rc=65)
    assert result.returncode == 0
    assert result.stdout.count("systemd-tmpfiles --clean exited with status 65") == 2
    assert "cleanup done: blocks 95% -> 60%" in result.stdout


def test_unreadable_second_pass_config_is_a_warning(stubs: Path, tmp_path: Path) -> None:
    result = _run_helper(stubs, [95, 94], config=tmp_path / "missing.conf")
    assert result.returncode == 0
    assert "is not readable" in result.stdout
    assert len(_calls(stubs)) == 1


@pytest.mark.parametrize("value", ["abc", "0", "101", "8 5"])
def test_invalid_threshold_is_rejected_before_touching_anything(stubs: Path, value: str) -> None:
    result = _run_helper(stubs, [99], threshold=value)
    assert result.returncode == 2
    assert "TMP_PRESSURE_THRESHOLD" in result.stderr
    assert _calls(stubs) == []


def test_unparseable_df_output_fails_loudly(stubs: Path) -> None:
    (stubs / "df").write_text("#!/bin/bash\necho garbage\n")
    result = _run_helper(stubs, [90])
    assert result.returncode == 1
    assert "cannot determine usage" in result.stderr
    assert _calls(stubs) == []


# ---------------------------------------------------------------------------
# Inode pressure (2026-10-25: /tmp out of inodes at 23% blocks)
# ---------------------------------------------------------------------------


def test_inode_pressure_alone_triggers_the_cleanup(stubs: Path) -> None:
    result = _run_helper(stubs, [23, 22], inodes=[100, 40])
    assert result.returncode == 0, result.stderr
    assert _calls(stubs) == ["--clean --prefix=/tmp"]
    assert "at 23% blocks, 100% inodes (thresholds 85% blocks, 85% inodes)" in result.stdout
    assert "cleanup done: blocks 23% -> 22%, inodes 100% -> 40%" in result.stdout
    assert "still at" not in result.stdout


def test_inode_pressure_triggers_exactly_at_the_inode_threshold(stubs: Path) -> None:
    result = _run_helper(stubs, [10], inodes=[70, 50], inode_threshold="70")
    assert result.returncode == 0
    assert len(_calls(stubs)) == 1
    assert "thresholds 85% blocks, 70% inodes" in result.stdout


def test_below_both_thresholds_is_silent(stubs: Path) -> None:
    result = _run_helper(stubs, [84], inodes=[84])
    assert result.returncode == 0
    assert result.stdout == ""
    assert _calls(stubs) == []


def test_inode_threshold_defaults_to_the_block_threshold(stubs: Path) -> None:
    quiet = _run_helper(stubs, [10], inodes=[59], threshold="60")
    assert quiet.stdout == ""
    (stubs / "tmpfiles-calls").write_text("")
    busy = _run_helper(stubs, [10], inodes=[60, 30], threshold="60")
    assert "thresholds 60% blocks, 60% inodes" in busy.stdout
    assert len(_calls(stubs)) == 1


def test_inode_pressure_runs_the_second_pass(stubs: Path, tmp_path: Path) -> None:
    result = _run_helper(stubs, [20], inodes=[99, 95, 30], config=_aggressive_config(tmp_path))
    assert result.returncode == 0, result.stderr
    calls = _calls(stubs)
    assert len(calls) == 3
    assert calls[2].startswith("--clean --prefix=/tmp ")
    assert "still at 20% blocks, 95% inodes: running shorter-age pass" in result.stdout
    assert "cleanup done: blocks 20% -> 20%, inodes 99% -> 30%" in result.stdout


def test_inodes_still_full_warns_but_exits_zero(stubs: Path) -> None:
    result = _run_helper(stubs, [20], inodes=[99, 98])
    assert result.returncode == 0
    assert "still at 20% blocks, 98% inodes: no aggressive pass configured" in result.stdout
    assert "nothing more can be removed safely" in result.stdout


def test_filesystem_without_inode_counts_is_judged_on_blocks_alone(stubs: Path) -> None:
    quiet = _run_helper(stubs, [50], inodes=["-"])
    assert quiet.returncode == 0, quiet.stderr
    assert quiet.stdout == ""
    assert _calls(stubs) == []

    busy = _run_helper(stubs, [90, 60], inodes=["-"])
    assert busy.returncode == 0, busy.stderr
    assert _calls(stubs) == ["--clean --prefix=/tmp"]
    assert "at 90% blocks, inodes not reported" in busy.stdout
    assert "cleanup done: blocks 90% -> 60%, inodes not reported" in busy.stdout


@pytest.mark.parametrize("value", ["abc", "0", "101"])
def test_invalid_inode_threshold_is_rejected(stubs: Path, value: str) -> None:
    result = _run_helper(stubs, [99], inode_threshold=value)
    assert result.returncode == 2
    assert "TMP_PRESSURE_INODE_THRESHOLD" in result.stderr
    assert _calls(stubs) == []


def test_unparseable_inode_output_fails_loudly(stubs: Path) -> None:
    result = _run_helper(stubs, [10], inodes=["garbage"])
    assert result.returncode == 1
    assert "cannot determine inode usage" in result.stderr
    assert _calls(stubs) == []
