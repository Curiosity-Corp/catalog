"""A damaged file never stops the Console: it is set aside, the System page says so, and the notice can be dismissed."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from support import LOADED, App, Rig, poll


@pytest.fixture
def damaged_looks(rig: Rig):
    """Write half a JSON file where the demo rig keeps the saved looks, and clear the notices afterwards."""
    assert rig.home is not None
    path = rig.home / ".config/streamstack/looks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    for stale in path.parent.glob("looks.json*"):  # a .bak from an earlier test would be restored, not defaults
        stale.unlink()
    rig.act("storage.dismiss")
    path.write_text('{"Mine": {"brightness": ')
    yield path
    rig.act("storage.dismiss")
    for leftover in path.parent.glob("looks.json*"):
        leftover.unlink()


def test_a_damaged_looks_file_is_set_aside_and_shown_on_the_system_page(
    app: App, page: Page, rig: Rig, damaged_looks
) -> None:
    app.goto("system")
    warning = poll(rig, lambda s: s["storage"]["warnings"] and s["storage"]["warnings"][0])
    assert warning["path"] == "~/.config/streamstack/looks.json"
    assert warning["action"] == "defaults"
    assert not damaged_looks.exists()
    assert (damaged_looks.parent / warning["quarantined_to"]).read_text() == '{"Mine": {"brightness": '
    section = page.get_by_role("region", name="Repaired files")
    expect(section).to_be_visible(timeout=LOADED)
    expect(section).to_contain_text("~/.config/streamstack/looks.json")
    expect(section).to_contain_text("set aside")
    expect(section).to_contain_text(warning["quarantined_to"])
    assert rig.state()["video"]["looks"]  # the built-in looks still load


def test_the_notice_can_be_dismissed(app: App, page: Page, rig: Rig, damaged_looks) -> None:
    app.goto("system")
    section = page.get_by_role("region", name="Repaired files")
    expect(section).to_be_visible(timeout=LOADED)
    with page.expect_response(
        lambda r: r.url.endswith("api/action") and "storage.dismiss" in (r.request.post_data or "")
    ):
        section.get_by_role("button", name="Dismiss the notice about ~/.config/streamstack/looks.json").click()
    expect(section).to_be_hidden(timeout=LOADED)
    assert rig.state()["storage"]["warnings"] == []


def test_the_section_is_hidden_when_nothing_was_repaired(app: App, page: Page, rig: Rig) -> None:
    rig.act("storage.dismiss")
    app.goto("system")
    expect(page.locator(".stor")).to_be_hidden()
