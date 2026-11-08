"""Slack desktop snap: gating, store identity check, and healthcheck contract.

The role installs the official `slack` snap only when slack_desktop_enabled is
true, refuses a snap whose snap-id is not Slack's, and installs a healthcheck
helper that exists only where Slack was installed. The identity assert and the
helper are exercised against realistic `snap info` output.
"""

import os
import re
import subprocess
from pathlib import Path

import jinja2
import pytest
import yaml


ROLE = Path(__file__).resolve().parents[1] / "roles/developer_workstation"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
INSTALL = yaml.safe_load((ROLE / "tasks/slack-desktop.yml").read_text())
MANAGED = yaml.safe_load((ROLE / "tasks/managed-updates.yml").read_text())
UPDATES = (ROLE / "tasks/system-updates.yml").read_text()

SNAP_ID = DEFAULTS["slack_desktop_snap_id"]

INSTALLED_INFO = f"""name:      slack
summary:   Team communication for the 21st century.
publisher: Slack**
store-url: https://snapcraft.io/slack
license:   Proprietary
description: |
  Slack brings team communication and collaboration into one place.
commands:
  - slack
snap-id:      {SNAP_ID}
tracking:     latest/stable
refresh-date: today at 10:00 EST
channels:
  latest/stable:    4.53.280 2026-11-04 (268) 233MB -
installed:          4.53.280            (268) 233MB -
"""


def _task(tasks: list[dict], name: str) -> dict:
    return next(t for t in tasks if t.get("name") == name)


def _environment() -> jinja2.Environment:
    from ansible.plugins.filter.core import FilterModule
    from ansible.plugins.test.core import TestModule

    env = jinja2.Environment()
    env.filters.update(FilterModule().filters())
    env.tests.update(TestModule().tests())
    env.filters["default"] = jinja2.filters.do_default
    return env


def _assert_passes(stdout: str, rc: int = 0) -> bool:
    task = _task(INSTALL, "Require the installed Slack snap to be the official one")
    env = _environment()
    variables = {
        "_slack_desktop_info": {"rc": rc, "stdout": stdout},
        "slack_desktop_snap_id": SNAP_ID,
    }
    return all(
        env.from_string("{{ " + cond + " }}").render(**variables) == "True"
        for cond in task["ansible.builtin.assert"]["that"]
    )


def test_defaults_are_off_and_pin_the_official_snap() -> None:
    assert DEFAULTS["slack_desktop_enabled"] is False
    assert DEFAULTS["slack_desktop_snap_channel"] == "latest/stable"
    assert re.fullmatch(r"[A-Za-z0-9]{32}", SNAP_ID)


def test_application_contract_names_its_flag_and_helper() -> None:
    contract = DEFAULTS["workstation_application_contracts"]["slack_desktop"]
    assert contract == {
        "installer": "snap-store",
        "verification": "snap-assertions",
        "enabled_when": "slack_desktop_enabled",
        "healthcheck": ["/usr/local/sbin/curiosity-slack-desktop-check"],
    }


def test_include_is_gated_like_the_other_desktop_apps() -> None:
    include = _task(MANAGED, "Include Slack desktop app")
    assert include["ansible.builtin.include_tasks"]["file"] == "slack-desktop.yml"
    assert include["when"] == [
        "workstation_profile == 'desktop'",
        "slack_desktop_enabled | default(false) | bool",
        "ansible_architecture == 'x86_64'",
        "workstation_update_install_allowed | default(true)",
    ]


def test_snap_is_installed_from_the_configured_channel() -> None:
    install = _task(INSTALL, "Install the Slack desktop snap")["community.general.snap"]
    assert install == {"name": "slack", "channel": "{{ slack_desktop_snap_channel }}", "state": "present"}


def test_installed_snaps_are_refreshed_by_system_updates() -> None:
    assert "snap refresh" in UPDATES


def test_identity_assert_accepts_the_official_installed_snap() -> None:
    assert _assert_passes(INSTALLED_INFO)


@pytest.mark.parametrize(
    "stdout, rc",
    [
        (INSTALLED_INFO.replace(SNAP_ID, "X" * 32), 0),
        (INSTALLED_INFO.replace(SNAP_ID, SNAP_ID + "extra"), 0),
        ("\n".join(l for l in INSTALLED_INFO.splitlines() if not l.startswith("installed:")), 0),
        ("", 1),
    ],
    ids=["foreign-snap-id", "longer-snap-id", "not-installed", "snap-info-failed"],
)
def test_identity_assert_refuses_anything_else(stdout: str, rc: int) -> None:
    assert not _assert_passes(stdout, rc)


def _helper_script() -> str:
    content = _task(INSTALL, "Install the Slack desktop healthcheck helper")["ansible.builtin.copy"]["content"]
    return _environment().from_string(content).render(slack_desktop_snap_id=SNAP_ID)


def _run_helper(tmp_path: Path, *, listed: bool, info: str, files: bool) -> subprocess.CompletedProcess:
    """Run the rendered helper with a fake `snap` and its paths rewritten into tmp_path."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (tmp_path / "info.txt").write_text(info)
    fake = bindir / "snap"
    fake.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = list ]; then exit {0 if listed else 1}; fi\n'
        f'if [ "$1" = info ]; then cat "{tmp_path}/info.txt"; exit 0; fi\n'
        "exit 2\n"
    )
    fake.chmod(0o755)
    launcher = tmp_path / "snap-bin-slack"
    desktop = tmp_path / "slack_slack.desktop"
    if files:
        launcher.write_text("")
        desktop.write_text("")
    script = (
        _helper_script()
        .replace("/snap/bin/slack", str(launcher))
        .replace("/var/lib/snapd/desktop/applications/slack_slack.desktop", str(desktop))
    )
    helper = tmp_path / "check.sh"
    helper.write_text(script)
    helper.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:/usr/bin:/bin"}
    return subprocess.run([str(helper)], capture_output=True, text=True, env=env, check=False)


def test_helper_reports_the_installed_revision(tmp_path: Path) -> None:
    result = _run_helper(tmp_path, listed=True, info=INSTALLED_INFO, files=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().startswith("slack snap 4.53.280")


@pytest.mark.parametrize(
    "listed, info, files, message",
    [
        (False, INSTALLED_INFO, True, "not installed"),
        (True, INSTALLED_INFO.replace(SNAP_ID, "X" * 32), True, "unexpected snap-id"),
        (True, INSTALLED_INFO, False, "missing"),
    ],
    ids=["not-installed", "foreign-snap-id", "launcher-missing"],
)
def test_helper_fails_with_one_clear_line(tmp_path: Path, listed: bool, info: str, files: bool, message: str) -> None:
    result = _run_helper(tmp_path, listed=listed, info=info, files=files)
    assert result.returncode == 1
    assert message in result.stderr
    assert len(result.stderr.strip().splitlines()) == 1
