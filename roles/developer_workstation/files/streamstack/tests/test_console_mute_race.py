"""The M key must never lose a mute or an unmute (#155).

The Console holds an optimistic value against server snapshots. It used to hold it for a fixed 600 ms; when the
snapshot carrying the change arrived later (a loaded machine), the page flipped back to the old value and the next M
press toggled from that stale value: it sent the same state again and the second press did nothing. The test runs the
real browser modules under Node with a fake clock, so the ordering is exact instead of timing-dependent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONSOLE = ROOT / "src" / "streamstack" / "data" / "console"
SCRIPT = Path(__file__).parent / "fixtures" / "mute_race.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def run() -> dict:
    done = subprocess.run(["node", str(SCRIPT), str(CONSOLE)], capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_second_m_press_unmutes_even_when_the_snapshot_is_late() -> None:
    result = run()
    shown = dict(result["late"])
    # the page keeps showing what was pressed until the server's snapshot agrees, however late that is
    assert shown["stale snapshot, post unanswered"] is True
    assert shown["stale snapshot 2 s after the answer"] is True
    assert shown["server snapshot"] is True
    # and so the second press is an unmute
    assert [p["on"] for p in result["posts"]] == [True, False]
    assert shown["after second M"] is False


def test_a_snapshot_that_agrees_releases_the_hold_at_once() -> None:
    shown = dict(run()["agree"])
    assert shown["agreeing snapshot"] == 5
    assert shown["next snapshot"] == 9


def test_a_server_that_never_agrees_wins_after_the_ceiling() -> None:
    shown = dict(run()["ceiling"])
    assert shown["before the ceiling"] == 7
    assert shown["after the ceiling"] == 0


def test_a_drag_is_not_pulled_back_by_older_echoes() -> None:
    shown = dict(run()["drag"])
    assert shown["dragged to 3"] == 3
    assert shown["echo of 1"] == 3
    assert shown["echo of 2"] == 3
    assert shown["older echo after the last answer"] == 3
    assert shown["echo of 3"] == 3
    assert shown["then the server moves on"] == 4


def test_a_call_setting_click_survives_an_unrelated_call_snapshot() -> None:
    shown = dict(run()["callSet"])
    assert shown["clicked"] is True
    assert shown["unrelated call snapshot"] is True
    assert shown["unrelated snapshot after the answer"] is True
    assert shown["echo"] is True


def test_every_settings_action_the_pages_send_is_optimistic() -> None:
    """A control drawn from a broad slice needs an OPTIMISTIC entry, or the next snapshot overwrites the click."""
    actions = (CONSOLE / "core" / "actions.js").read_text()
    for name in (
        "call.set",
        "network.set",
        "hotkeys.enable",
        "framing.mode",
        "framing.hold",
        "framing.handoff",
        "framing.shot",
        "framing.subjects",
        "slots.mode",
        "deck.settings",
        "lighting.auto.follow",
        "lighting.auto.schedule",
    ):
        assert f"'{name}':" in actions, name


def test_values_that_never_agree_keep_the_short_hold() -> None:
    shown = dict(run()["brief"])
    assert shown["inside the short hold"] is True
    assert shown["after the short hold"] is False
    assert shown["studio busy after start answered"] is False
