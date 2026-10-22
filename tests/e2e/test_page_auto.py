"""The Auto page: AI director switch, ask-now, hold-to-undo and the timeline."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from support import LOADED, App, Rig, hold, poll, quiet


@pytest.fixture
def aut(app: App) -> App:
    return app.goto("auto")


def director(page: Page):
    return page.get_by_role("switch", name=re.compile("^AI director"))


def settled(rig: Rig, running: bool) -> bool:
    return bool(poll(rig, lambda s: s["auto"]["running"] is running and not s["auto"]["busy"]))


def test_director_switch_and_d_key(aut: App, page: Page, rig: Rig) -> None:
    switch = director(page)
    expect(switch).to_have_attribute("aria-checked", "false")
    switch.click()
    assert settled(rig, True)
    expect(switch).to_have_attribute("aria-checked", "true", timeout=LOADED)
    expect(switch).to_contain_text("On")
    aut.press("d")
    assert settled(rig, False)
    expect(switch).to_have_attribute("aria-checked", "false", timeout=LOADED)


def test_server_side_director_change_reaches_the_switch(aut: App, page: Page, rig: Rig) -> None:
    rig.act("auto.on")
    expect(director(page)).to_have_attribute("aria-checked", "true", timeout=LOADED)


def test_ask_now_adds_a_note_to_the_timeline(aut: App, page: Page, rig: Rig) -> None:
    before = len(rig.state()["auto"]["journal"])
    page.get_by_role("button", name="Ask Claude now").click()
    assert poll(rig, lambda s: s["auto"]["busy"] or len(s["auto"]["journal"]) > before)
    assert poll(rig, lambda s: len(s["auto"]["journal"]) > before and not s["auto"]["busy"])
    timeline = page.get_by_role("region", name="Timeline of changes, newest first")
    expect(timeline).to_contain_text("Looks well exposed", timeout=LOADED)
    expect(page.get_by_text("changes", exact=False).first).to_be_visible()


def test_n_key_asks_now(aut: App, rig: Rig) -> None:
    before = len(rig.state()["auto"]["journal"])
    aut.press("n")
    assert poll(rig, lambda s: len(s["auto"]["journal"]) > before and not s["auto"]["busy"])


def test_latest_note_card_shows_the_newest_review(aut: App, page: Page, rig: Rig) -> None:
    rig.act("auto.once")
    poll(rig, lambda s: not s["auto"]["busy"] and "Looks well exposed" in (s["auto"]["last_note"] or ""))
    expect(page.locator("blockquote")).to_contain_text("Looks well exposed", timeout=LOADED)


def test_undo_needs_a_hold(aut: App, page: Page, rig: Rig) -> None:
    rig.act("auto.once")  # the demo can only undo after the director changed something
    poll(rig, lambda s: not s["auto"]["busy"] and s["auto"]["can_undo"])
    undo = page.locator("ss-button", has_text="Undo all changes")
    expect(undo).not_to_have_attribute("disabled", "", timeout=LOADED)
    before = len(rig.state()["auto"]["journal"])
    quiet(page)
    undo.click()  # a tap only explains itself
    expect(page.get_by_text("Press and hold to confirm")).to_be_visible()
    assert rig.state()["auto"]["can_undo"]
    hold(page, undo, 1400)
    assert poll(rig, lambda s: not s["auto"]["can_undo"] and not s["auto"]["busy"])
    assert len(rig.state()["auto"]["journal"]) > before
    expect(page.get_by_role("region", name="Timeline of changes, newest first")).to_contain_text(
        "restored the look", timeout=LOADED
    )
    expect(undo).to_have_attribute("disabled", "")


def test_auto_page_has_no_overflow_on_a_laptop(aut: App, page: Page) -> None:
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
