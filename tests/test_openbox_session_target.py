"""Openbox sessions reach graphical-session.target (Curiosity-Corp/catalog#106).

Openbox has no systemd integration, so user units WantedBy=graphical-session.target
(StreamStack lights, auto mode) never started on the minimal desktop.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROLE = Path(__file__).resolve().parents[1] / "roles/developer_workstation"
TARGET = ROLE / "files/minimal-desktop/openbox-session.target"
AUTOSTART = ROLE / "files/minimal-desktop/autostart"


def test_the_session_target_binds_graphical_session() -> None:
    unit = TARGET.read_text()
    assert "BindsTo=graphical-session.target" in unit
    assert "Before=graphical-session.target" in unit


def test_the_target_is_installed_for_every_user() -> None:
    tasks = yaml.safe_load((ROLE / "tasks/minimal-desktop.yml").read_text())
    task = next(t for t in tasks if t["name"] == "Install the Openbox session target for systemd --user")
    copy = task["ansible.builtin.copy"]
    assert copy["src"] == "minimal-desktop/openbox-session.target"
    assert copy["dest"] == "/etc/systemd/user/openbox-session.target"


def test_autostart_starts_it_after_publishing_the_display_without_blocking() -> None:
    text = AUTOSTART.read_text()
    start = text.index("systemctl --user start --no-block openbox-session.target")
    assert text.index("dbus-update-activation-environment --systemd") < start
    assert "timeout 10" in text[text.index("session_target_timeout"):start]
