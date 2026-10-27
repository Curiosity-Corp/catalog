"""Call awareness: the top-bar chip, the System page card and the one "you are muted" warning."""

from __future__ import annotations

import time

from playwright.sync_api import Page, expect

from support import LOADED, App, Rig, poll


def card(page: Page):
    return page.get_by_role("region", name="Call awareness", exact=False)


def join(rig: Rig, **params: object) -> None:
    rig.act("call.demo", app="Teams", **params)
    poll(rig, lambda s: s["call"]["state"] == "in_call")


def test_the_chip_and_the_card_follow_the_call(app: App, page: Page, rig: Rig) -> None:
    app.goto("system")
    section = card(page)
    expect(section).to_be_visible(timeout=LOADED)
    expect(section.get_by_role("status")).to_have_text("No call")
    expect(app.top_chips.get_by_text("IN CALL")).to_have_count(0)

    join(rig)
    expect(app.top_chips.get_by_text("IN CALL TEAMS")).to_be_visible(timeout=LOADED)
    expect(section.get_by_role("status")).to_have_text("In a call (Teams)")

    rig.act("call.demo", app=None)
    poll(rig, lambda s: s["call"]["state"] == "idle")
    expect(app.top_chips.get_by_text("IN CALL TEAMS")).to_have_count(0, timeout=LOADED)
    expect(section.get_by_role("status")).to_have_text("No call", timeout=LOADED)


def test_talking_into_a_muted_call_warns_once(app: App, page: Page, rig: Rig) -> None:
    app.goto("system")
    expect(card(page)).to_be_visible(timeout=LOADED)
    join(rig, corked=True, talk=False)
    time.sleep(2.5)  # the quiet room is the noise floor
    assert rig.state()["call"]["muted_warning"]["count"] == 0

    rig.act("call.demo", app="Teams", corked=True, talk=True)
    poll(rig, lambda s: s["call"]["muted_warning"]["count"] == 1)
    expect(page.get_by_text("You are muted. Teams cannot hear you.")).to_be_visible(timeout=LOADED)
    expect(card(page).get_by_text("Warned 1 time this session.")).to_be_visible(timeout=LOADED)
    time.sleep(2.0)
    assert rig.state()["call"]["muted_warning"]["count"] == 1  # one warning per episode, not one per second


def test_an_open_mic_never_warns_and_the_warning_can_be_switched_off(app: App, page: Page, rig: Rig) -> None:
    app.goto("system")
    section = card(page)
    expect(section).to_be_visible(timeout=LOADED)
    join(rig, talk=False)
    time.sleep(2.5)
    rig.act("call.demo", app="Teams", talk=True)
    time.sleep(2.0)
    assert rig.state()["call"]["muted_warning"]["count"] == 0  # not muted in the app: nobody to warn

    switch = section.get_by_role("switch", name="Warn when I talk while muted")
    expect(switch).to_have_attribute("aria-checked", "true")
    switch.click()
    expect(switch).to_have_attribute("aria-checked", "false", timeout=LOADED)
    assert rig.state()["call"]["muted_warning"]["enabled"] is False
    expect(section.get_by_label("Warning sensitivity")).to_be_hidden()


def test_sensitivity_and_wait_are_saved(app: App, page: Page, rig: Rig) -> None:
    app.goto("system")
    section = card(page)
    expect(section).to_be_visible(timeout=LOADED)
    section.get_by_label("Warning sensitivity").select_option("high")
    poll(rig, lambda s: s["call"]["muted_warning"]["sensitivity"] == "high")
    section.get_by_label("Wait between warnings").select_option("45")
    poll(rig, lambda s: s["call"]["muted_warning"]["cooldown_s"] == 45)
    section.get_by_label("Play a short beep with the warning").check()
    poll(rig, lambda s: s["call"]["muted_warning"]["sound"] is True)
