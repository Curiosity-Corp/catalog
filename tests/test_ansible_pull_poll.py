"""The public baseline converges within one poll of a catalog merge (catalog#159).

ansible-pull.timer runs the full converge at 02:00 and after boot. The poll
(ansible-pull-poll.timer) runs `curiosity-ansible-pull --if-behind`, which starts
ansible-pull.service only when the branch has moved past the commit of the
last successful run. The behavioural tests render the real wrapper from the
task file and run it with stub git, systemctl, flock and ansible-pull on PATH
against a temporary state directory, so no checkout, network or systemd is
touched; the CI image has no git, and the stubs make every branch reachable.
"""

import os
import shlex
import subprocess
import time
from pathlib import Path

import jinja2
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
TASKS = yaml.safe_load((ROLE / "tasks/ansible-pull-timer.yml").read_text())
CI = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())

REMOTE = "b" * 40
OLD = "a" * 40


def _task(name: str) -> dict:
    return next(t for t in TASKS if t.get("name") == name)


def _content(name: str) -> str:
    return _task(name)["ansible.builtin.copy"]["content"]


# --------------------------------------------------------------------------- #
# Static contract
# --------------------------------------------------------------------------- #


def test_defaults_poll_every_fifteen_minutes() -> None:
    assert DEFAULTS["ansible_pull_poll_enabled"] is True
    assert DEFAULTS["ansible_pull_poll_schedule"] == "*:0/15"
    assert DEFAULTS["ansible_pull_state_dir"].startswith("/var/lib/")


def test_poll_starts_the_baseline_unit_instead_of_converging_itself() -> None:
    # One unit does the converge: its lock, MemoryHigh and the OnSuccess= chain
    # catalog-internal adds to ansible-pull.service apply to polled runs too.
    service = _content("Create ansible-pull poll service unit")
    assert "ExecStart=/usr/local/sbin/curiosity-ansible-pull --if-behind" in service
    assert "Environment=HOME=/root" in service
    wrapper = _content("Create ansible-pull wrapper")
    assert "systemctl start --no-block ansible-pull.service" in wrapper


def test_poll_timer_uses_the_schedule() -> None:
    timer = _content("Create ansible-pull poll timer")
    assert "OnCalendar={{ ansible_pull_poll_schedule }}" in timer
    assert "WantedBy=timers.target" in timer


def test_nightly_full_converge_is_kept() -> None:
    timer = _content("Create ansible-pull timer")
    assert "OnCalendar=*-*-* 02:00:00" in timer
    assert "OnBootSec=5min" in timer


def test_poll_units_are_gated_and_retired() -> None:
    for name in (
        "Create ansible-pull poll service unit",
        "Create ansible-pull poll timer",
        "Enable ansible-pull poll timer",
    ):
        assert _task(name)["when"] == "ansible_pull_poll_enabled | bool"
    removed = _task("Remove the retired ansible-pull poll units")
    assert removed["when"] == "not (ansible_pull_poll_enabled | bool)"
    assert set(removed["loop"]) == {"ansible-pull-poll.timer", "ansible-pull-poll.service"}
    stop = _task("Stop and disable the retired ansible-pull poll timer")["ansible.builtin.systemd"]
    assert stop["enabled"] is False and stop["state"] == "stopped"


def test_this_file_runs_in_the_policy_job() -> None:
    script = " ".join(CI["policy"]["script"])
    assert "tests/test_ansible_pull_poll.py" in script


# --------------------------------------------------------------------------- #
# Behaviour: the rendered wrapper with stub commands
# --------------------------------------------------------------------------- #

STUB_GIT = r"""#!/usr/bin/env bash
# Stub git: answers from files in $STUB, logs every call.
echo "git $*" >>"$STUB/calls"
[[ $1 == -C ]] && shift 2
case $1 in
  status) cat "$STUB/dirty" 2>/dev/null; exit 0 ;;
  diff) [[ -s $STUB/dirty ]] && exit 1; exit 0 ;;
  ls-remote)
    [[ -r $STUB/remote ]] || exit 2
    printf '%s\trefs/heads/main\n' "$(<"$STUB/remote")" ;;
  fetch) exit "$(cat "$STUB/fetch_rc" 2>/dev/null || echo 0)" ;;
  merge-base) exit "$(cat "$STUB/ancestor_rc" 2>/dev/null || echo 0)" ;;
  log) cat "$STUB/commit_time" ;;
  rev-parse)
    case $2 in
      HEAD) cat "$STUB/head" ;;
      *) cat "$STUB/remote" ;;
    esac ;;
  *) echo "stub git: unexpected $*" >&2; exit 99 ;;
esac
"""

STUB_SYSTEMCTL = r"""#!/usr/bin/env bash
echo "systemctl $*" >>"$STUB/calls"
case $1 in
  show) cat "$STUB/active_state" 2>/dev/null || echo inactive ;;
  start) exit "$(cat "$STUB/start_rc" 2>/dev/null || echo 0)" ;;
esac
"""

STUB_FLOCK = r"""#!/usr/bin/env bash
exit "$(cat "$STUB/flock_rc" 2>/dev/null || echo 0)"
"""

STUB_ANSIBLE_PULL = r"""#!/usr/bin/env bash
echo "ansible-pull $*" >>"$STUB/calls"
rc=$(cat "$STUB/pull_rc" 2>/dev/null || echo 0)
[[ $rc == 0 ]] && cp "$STUB/remote" "$STUB/head"
exit "$rc"
"""


def _bool(value: object) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _render(tmp_path: Path, ring: str) -> Path:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
    env.filters["bool"] = _bool
    env.filters["quote"] = lambda v: shlex.quote(str(v))
    variables = {
        "ansible_pull_repo": "https://example.invalid/catalog.git",
        "ansible_pull_branch": "main",
        "ansible_pull_playbook": "playbooks/site.yml",
        "ansible_pull_dir": str(tmp_path / "checkout"),
        "ansible_pull_extra_vars": "",
        "ansible_pull_verify_commit": False,
        "ansible_pull_state_dir": str(tmp_path / "state"),
        "dev_user": "researcher",
        "dev_user_home": "/home/researcher",
        "workstation_profile": "desktop",
        "hardware_profile": "generic",
        "workstation_update_ring": ring,
        "workstation_update_ring_min_age_seconds": DEFAULTS["workstation_update_ring_min_age_seconds"],
    }
    text = env.from_string(_content("Create ansible-pull wrapper")).render(**variables)
    text = text.replace("/usr/bin/ansible-pull", str(tmp_path / "bin/ansible-pull"))
    script = tmp_path / "curiosity-ansible-pull"
    script.write_text(text)
    script.chmod(0o755)
    return script


@pytest.fixture
def host(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {
        "git": STUB_GIT,
        "systemctl": STUB_SYSTEMCTL,
        "flock": STUB_FLOCK,
        "ansible-pull": STUB_ANSIBLE_PULL,
    }.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    stub = tmp_path / "stub"
    stub.mkdir()
    (tmp_path / "checkout/.git").mkdir(parents=True)
    (stub / "remote").write_text(REMOTE + "\n")
    (stub / "head").write_text(OLD + "\n")
    (stub / "commit_time").write_text(f"{int(time.time()) - 60}\n")
    (stub / "calls").write_text("")
    return tmp_path


def _run(host: Path, *args: str, ring: str = "canary") -> subprocess.CompletedProcess:
    script = _render(host, ring)
    env = dict(os.environ, STUB=str(host / "stub"), PATH=f"{host / 'bin'}:{os.environ['PATH']}")
    return subprocess.run([str(script), *args], env=env, capture_output=True, text=True, check=False)


def _calls(host: Path) -> list[str]:
    return (host / "stub/calls").read_text().splitlines()


def _started(host: Path) -> bool:
    return "systemctl start --no-block ansible-pull.service" in _calls(host)


def _state(host: Path, name: str) -> str | None:
    path = host / "state" / name
    return path.read_text().strip() if path.exists() else None


def test_moved_branch_starts_one_run_and_records_it(host: Path) -> None:
    (host / "state").mkdir()
    (host / "state/applied-commit").write_text(OLD + "\n")
    result = _run(host, "--if-behind")
    assert result.returncode == 0, result.stderr
    assert _started(host)
    assert _state(host, "poll-started-commit") == REMOTE

    # The next poll sees the same commit: no second start, whatever the run did.
    (host / "stub/calls").write_text("")
    result = _run(host, "--if-behind")
    assert result.returncode == 0, result.stderr
    assert not _started(host)
    assert "02:00 run retries" in result.stdout


def test_applied_branch_does_nothing(host: Path) -> None:
    (host / "state").mkdir()
    (host / "state/applied-commit").write_text(REMOTE + "\n")
    result = _run(host, "--if-behind")
    assert result.returncode == 0, result.stderr
    assert "already applied" in result.stdout
    assert not _started(host)
    assert not any(c.startswith("git -C") and " fetch " in c for c in _calls(host))


def test_first_poll_with_no_record_converges(host: Path) -> None:
    result = _run(host, "--if-behind")
    assert result.returncode == 0, result.stderr
    assert _started(host)


def test_running_converge_is_left_alone(host: Path) -> None:
    (host / "stub/active_state").write_text("activating\n")
    result = _run(host, "--if-behind")
    assert result.returncode == 0, result.stderr
    assert "in progress" in result.stdout
    assert not _started(host)


def test_held_lock_is_left_alone(host: Path) -> None:
    (host / "stub/flock_rc").write_text("1\n")
    result = _run(host, "--if-behind")
    assert result.returncode == 0, result.stderr
    assert not _started(host)


def test_unreadable_remote_fails_loudly(host: Path) -> None:
    (host / "stub/remote").unlink()
    result = _run(host, "--if-behind")
    assert result.returncode == 1
    assert "cannot read main" in result.stderr
    assert not _started(host)


def test_failed_fetch_fails_loudly(host: Path) -> None:
    (host / "stub/fetch_rc").write_text("128\n")
    result = _run(host, "--if-behind")
    assert result.returncode == 1
    assert "cannot fetch main" in result.stderr
    assert not _started(host)


@pytest.mark.parametrize("setup", ["dirty", "ahead"])
def test_reviewed_local_checkout_is_left_to_the_full_run(host: Path, setup: str) -> None:
    if setup == "dirty":
        (host / "stub/dirty").write_text(" M roles/x.yml\n")
    else:
        (host / "stub/ancestor_rc").write_text("1\n")
    result = _run(host, "--if-behind")
    assert result.returncode == 0, result.stderr
    assert "the poll does not" in result.stdout
    assert not _started(host)


def test_later_ring_waits_for_the_observation_window(host: Path) -> None:
    result = _run(host, "--if-behind", ring="early")
    assert result.returncode == 0, result.stderr
    assert "ring early waits" in result.stdout
    assert not _started(host)

    window = DEFAULTS["workstation_update_ring_min_age_seconds"]["early"]
    (host / "stub/commit_time").write_text(f"{int(time.time()) - window - 60}\n")
    result = _run(host, "--if-behind", ring="early")
    assert result.returncode == 0, result.stderr
    assert _started(host)


def test_failed_start_is_not_recorded(host: Path) -> None:
    (host / "stub/start_rc").write_text("1\n")
    result = _run(host, "--if-behind")
    assert result.returncode == 1
    assert _state(host, "poll-started-commit") is None


def test_successful_run_records_the_applied_commit(host: Path) -> None:
    result = _run(host)
    assert result.returncode == 0, result.stderr
    assert any(c.startswith("ansible-pull ") for c in _calls(host))
    assert _state(host, "applied-commit") == REMOTE


def test_failed_run_records_nothing(host: Path) -> None:
    (host / "state").mkdir()
    (host / "state/applied-commit").write_text(OLD + "\n")
    (host / "stub/pull_rc").write_text("2\n")
    result = _run(host)
    assert result.returncode == 2
    assert _state(host, "applied-commit") == OLD


def test_unknown_argument_is_refused(host: Path) -> None:
    result = _run(host, "--now")
    assert result.returncode == 2
    assert "usage" in result.stderr
