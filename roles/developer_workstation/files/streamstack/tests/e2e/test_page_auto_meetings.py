"""The Meetings card on the Auto page: sources, the next meetings, and the preparation before one starts."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from support import App, Rig, poll, tap


@pytest.fixture
def mt(app: App) -> App:
    return app.goto("auto")


def test_the_card_starts_empty_and_offers_both_sources(mt: App) -> None:
    page = mt.page
    expect(page.locator(".mt")).to_be_visible()
    expect(page.locator(".mt [data-ref='empty']")).to_be_visible()
    expect(page.get_by_role("button", name="Sign in to Microsoft 365")).to_be_visible()
    expect(page.get_by_label("Calendar link")).to_be_visible()


def test_a_calendar_link_lists_its_meetings(mt: App, rig: Rig) -> None:
    page = mt.page
    rig.act("meetings.demo.feed", link="https://cal.example.org/team.ics", minutes=30, title="Standup")
    page.get_by_label("Calendar link").fill("https://cal.example.org/team.ics")
    page.get_by_label("Calendar name").fill("Team")
    tap(page, page.get_by_role("button", name="Add link"))
    row = page.locator(".mt-row", has_text="Standup")
    expect(row).to_be_visible(timeout=15000)
    expect(row.locator(".mt-badge")).to_have_text("Waiting")
    expect(page.locator(".mt-src", has_text="Team")).to_be_visible()
    tap(page, page.locator(".mt-src", has_text="Team").get_by_role("button", name="Remove"))
    expect(page.locator(".mt-row", has_text="Standup")).to_have_count(0, timeout=15000)


def test_microsoft_sign_in_shows_the_code_and_connects(mt: App, rig: Rig) -> None:
    page = mt.page
    tap(page, page.get_by_role("button", name="Sign in to Microsoft 365"))
    expect(page.locator(".mt [data-ref='codeBox']")).to_be_visible(timeout=15000)
    expect(page.locator(".mt [data-ref='codeText']")).not_to_be_empty()
    rig.act("meetings.demo.cloud", minutes=45, title="Board call")
    rig.act("meetings.demo.approve")
    badge = page.locator(".mt-src", has_text="Microsoft 365").locator(".mt-badge")
    expect(badge).to_have_text("Connected", timeout=20000)
    expect(page.locator(".mt-row", has_text="Board call")).to_be_visible(timeout=20000)


def test_the_rig_is_prepared_before_the_meeting_and_the_card_says_so(mt: App, rig: Rig) -> None:
    page = mt.page
    rig.act("meetings.demo.feed", link="https://cal.example.org/a.ics", minutes=1, title="Client sync")
    rig.act("meetings.ics.add", link="https://cal.example.org/a.ics", name="A")
    rig.act("meetings.demo.tick")
    assert poll(rig, lambda s: s["meetings"]["prep"]["state"] != "idle")
    expect(page.locator(".mt [data-ref='prep']")).to_be_visible(timeout=15000)
    expect(page.locator(".mt [data-ref='prepTitle']")).to_contain_text("Client sync")
    expect(page.locator(".mt-row", has_text="Client sync").locator(".mt-badge")).to_have_text("Ready")


def test_nothing_changes_while_a_call_is_on(mt: App, rig: Rig) -> None:
    page = mt.page
    rig.act("call.demo", app="Teams")
    assert poll(rig, lambda s: s["call"]["active"])
    rig.act("meetings.demo.feed", link="https://cal.example.org/b.ics", minutes=1, title="Back to back")
    rig.act("meetings.ics.add", link="https://cal.example.org/b.ics", name="B")
    rig.act("meetings.demo.tick")
    expect(page.locator(".mt [data-ref='next']")).to_contain_text("A call is on now", timeout=15000)
    assert rig.state()["meetings"]["prep"]["state"] in ("idle", "skipped")


def test_the_settings_are_kept(mt: App, rig: Rig) -> None:
    page = mt.page
    lead = page.get_by_label("Minutes before the meeting")
    lead.fill("5")
    lead.press("Tab")
    assert poll(rig, lambda s: s["meetings"]["lead_minutes"] == 5)
    page.locator("[data-step]").first.click()
    assert poll(rig, lambda s: sum(x["on"] for x in s["meetings"]["steps"]) < len(s["meetings"]["steps"]))
