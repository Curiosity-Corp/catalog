"""Regression checks for the opt-in /tmp hygiene task group.

The feature is OPT-IN, built on native systemd-tmpfiles, and must never delete
by name pattern, sweep outside /tmp, or put the aggressive rules where the
daily tmpfiles clean would read them. These checks pin those properties and
exercise the pressure helper against stub ``df`` and ``systemd-tmpfiles``
commands, so they run on any machine without touching a real /tmp.
"""

import os
import re
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


def _render(name: str, **overrides: object) -> str:
    # Ansible's template module defaults to trim_blocks=True.
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES)),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
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
        if task.get("ansible.builtin.include_tasks") == "tmp-hygiene.yml"
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

STUB_DF = """#!/bin/bash
seq="$STUB_DIR/df-sequence"
pct=$(head -n 1 "$seq")
if [ "$(wc -l < "$seq")" -gt 1 ]; then
    tail -n +2 "$seq" > "$seq.next" && mv "$seq.next" "$seq"
fi
echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
echo "tmpfs 1000 500 500 ${pct}% /tmp"
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
    config: Path | None = None,
    threshold: str = "85",
    tmpfiles_rc: int = 0,
) -> subprocess.CompletedProcess:
    (stub_dir / "df-sequence").write_text("\n".join(str(p) for p in sequence) + "\n")
    env = {
        "PATH": f"{stub_dir}:{os.environ['PATH']}",
        "STUB_DIR": str(stub_dir),
        "STUB_TMPFILES_RC": str(tmpfiles_rc),
        "TMP_PRESSURE_THRESHOLD": threshold,
        "RUNTIME_DIRECTORY": str(stub_dir.parent / "run"),
    }
    if config is not None:
        env["TMP_PRESSURE_CONFIG"] = str(config)
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
    assert "at 90% (threshold 85%)" in result.stdout
    assert "cleanup done: 90% -> 60%" in result.stdout
    assert "still at" not in result.stdout


def test_helper_triggers_exactly_at_the_threshold(stubs: Path) -> None:
    result = _run_helper(stubs, [85, 85], threshold="85")
    assert result.returncode == 0
    assert len(_calls(stubs)) == 1
    assert "cleanup done: 85% -> 85%" in result.stdout


def test_helper_warns_when_no_second_pass_is_configured(stubs: Path) -> None:
    result = _run_helper(stubs, [92, 91])
    assert result.returncode == 0
    assert len(_calls(stubs)) == 1
    assert "no aggressive pass configured" in result.stdout
    assert "cleanup done: 92% -> 91%" in result.stdout


def test_second_pass_merges_static_and_package_exclusions(stubs: Path, tmp_path: Path) -> None:
    config = _aggressive_config(tmp_path)
    result = _run_helper(stubs, [95, 90, 70], config=config)
    assert result.returncode == 0, result.stderr
    calls = _calls(stubs)
    assert calls[0] == "--clean --prefix=/tmp"
    assert calls[1] == "--cat-config"
    assert calls[2].startswith("--clean --prefix=/tmp ")
    assert "cleanup done: 95% -> 70%" in result.stdout

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
    assert "(threshold 85%): nothing more can be removed safely" in result.stdout
    assert "cleanup done: 97% -> 95%" in result.stdout


def test_tmpfiles_failure_does_not_abort_the_remaining_passes(stubs: Path, tmp_path: Path) -> None:
    result = _run_helper(stubs, [95, 90, 60], config=_aggressive_config(tmp_path), tmpfiles_rc=65)
    assert result.returncode == 0
    assert result.stdout.count("systemd-tmpfiles --clean exited with status 65") == 2
    assert "cleanup done: 95% -> 60%" in result.stdout


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
