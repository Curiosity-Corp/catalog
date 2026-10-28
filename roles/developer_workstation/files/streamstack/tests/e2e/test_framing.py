"""Auto-framing: the Camera panel, the compact Live control and the actuator actions, on the demo simulation."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from support import LOADED, App, Rig, poll


def mode_button(page: Page, label: str):
    return page.get_by_role("group", name="Auto-framing mode").get_by_role("button", name=label, exact=True)


def open_panel(page: Page) -> None:
    """The Camera page keeps the panel in a popover over the picture."""
    page.locator('ss-button[title^="Auto-framing"]').click()
    expect(page.get_by_role("dialog", name="Auto-framing")).to_be_visible()


def test_the_demo_rig_starts_with_framing_off_and_available(rig: Rig) -> None:
    framing = rig.state()["framing"]
    assert framing["mode"] == "off" and framing["state"] == "off" and framing["available"] is True
    assert [m["name"] for m in framing["modes"]] == ["off", "gentle", "tight", "follow"]


def test_camera_panel_switches_mode_and_shows_device_and_time(app: App, rig: Rig) -> None:
    cam = app.goto("camera")
    page = cam.page
    open_panel(page)
    expect(mode_button(page, "Off")).to_have_attribute("aria-pressed", "true", timeout=LOADED)
    mode_button(page, "Tight").click()
    poll(rig, lambda st: st["framing"]["mode"] == "tight" and st["framing"]["state"] == "tracking")
    expect(mode_button(page, "Tight")).to_have_attribute("aria-pressed", "true")
    expect(mode_button(page, "Off")).to_have_attribute("aria-pressed", "false")
    status = page.locator(".fr-status")
    expect(status).to_contain_text("Tracking")
    expect(status).to_contain_text("Intel Arc A380 (GPU)")
    expect(status).to_contain_text("3.4 ms")
    mode_button(page, "Off").click()
    poll(rig, lambda st: st["framing"]["state"] == "off")
    expect(status).to_have_text("Off")


def test_hold_freezes_and_is_disabled_while_off(app: App, rig: Rig) -> None:
    page = app.goto("camera").page
    open_panel(page)
    hold = page.get_by_role("button", name="Hold", exact=True)
    expect(hold).to_be_disabled(timeout=LOADED)
    mode_button(page, "Follow").click()
    expect(hold).to_be_enabled()
    hold.click()
    poll(rig, lambda st: st["framing"]["held"] and st["framing"]["state"] == "held")
    expect(hold).to_have_attribute("aria-pressed", "true")
    hold.click()
    poll(rig, lambda st: not st["framing"]["held"] and st["framing"]["state"] == "tracking")


def test_live_page_has_the_compact_control(app: App, rig: Rig) -> None:
    page = app.goto("live").page
    compact = page.locator(".fr-compact")
    expect(compact).to_be_visible(timeout=LOADED)
    mode_button(page, "Gentle").click()
    poll(rig, lambda st: st["framing"]["mode"] == "gentle")
    expect(compact.locator(".fr-status")).to_contain_text("Tracking")


def test_a_change_made_elsewhere_reaches_the_page(app: App, rig: Rig) -> None:
    page = app.goto("camera").page
    open_panel(page)
    rig.act("framing.mode", mode="follow")
    expect(mode_button(page, "Follow")).to_have_attribute("aria-pressed", "true", timeout=LOADED)


def test_the_popover_closes_with_escape_and_marks_the_button_when_on(app: App, rig: Rig) -> None:
    page = app.goto("camera").page
    button = page.locator('ss-button[title^="Auto-framing"]')
    open_panel(page)
    mode_button(page, "Gentle").click()
    poll(rig, lambda st: st["framing"]["mode"] == "gentle")
    expect(button).to_have_attribute("pressed", "", timeout=LOADED)
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog", name="Auto-framing")).to_be_hidden()


def test_actions_validate_input(rig: Rig) -> None:
    status, body = rig.request("POST", "api/action", {"action": "framing.mode", "mode": "cinematic"}, token=rig.token)
    assert status == 400, body
    status, body = rig.request("POST", "api/action", {"action": "framing.hold", "on": True}, token=rig.token)
    assert status in (400, 409), body  # framing is off


def test_system_health_reports_the_detector(app: App, rig: Rig) -> None:
    rig.act("framing.mode", mode="gentle")
    page = app.goto("system").page
    expect(page.get_by_text("3.4 ms per detection")).to_be_visible(timeout=LOADED)


@pytest.mark.parametrize("route", ["camera", "live"])
def test_no_console_errors_with_framing_on(app: App, rig: Rig, route: str, problems: list[str]) -> None:
    rig.act("framing.mode", mode="tight")
    page = app.goto(route).page
    if route == "camera":
        open_panel(page)
    poll(rig, lambda st: st["framing"]["state"] == "tracking")
    assert problems == []


def shot_button(page: Page, label: str):
    return page.get_by_role("group", name="Shot").get_by_role("button", name=label, exact=True)


def test_the_demo_offers_the_shots_and_the_subjects(rig: Rig) -> None:
    framing = rig.state()["framing"]
    assert [s["name"] for s in framing["shots"]] == ["auto", "close", "medium", "wide", "presenter", "desk"]
    assert framing["subjects"] == "group" and framing["handoff"] is False


def test_choosing_a_shot_starts_framing_and_marks_the_button(app: App, rig: Rig) -> None:
    page = app.goto("camera").page
    open_panel(page)
    expect(shot_button(page, "Auto")).to_have_attribute("aria-pressed", "true", timeout=LOADED)
    shot_button(page, "Wide").click()
    poll(rig, lambda st: st["framing"]["shot"] == "wide" and st["framing"]["mode"] == "gentle")
    expect(shot_button(page, "Wide")).to_have_attribute("aria-pressed", "true")
    expect(shot_button(page, "Auto")).to_have_attribute("aria-pressed", "false")
    expect(mode_button(page, "Gentle")).to_have_attribute("aria-pressed", "true")


def test_who_to_frame_and_follow_speaker_reach_the_rig(app: App, rig: Rig) -> None:
    page = app.goto("camera").page
    open_panel(page)
    follow = page.get_by_role("button", name="Follow speaker", exact=True)
    expect(follow).to_have_attribute("aria-pressed", "false", timeout=LOADED)
    follow.click()
    poll(rig, lambda st: st["framing"]["handoff"] is True)
    expect(follow).to_have_attribute("aria-pressed", "true")
    page.get_by_label("Who to frame").select_option("one")
    poll(rig, lambda st: st["framing"]["subjects"] == "one")
    expect(follow).to_be_disabled()  # hand-off is for a group
    page.get_by_label("Who to frame").select_option("group")
    poll(rig, lambda st: st["framing"]["subjects"] == "group")
    expect(follow).to_be_enabled()


def test_a_shot_chosen_elsewhere_reaches_the_page(app: App, rig: Rig) -> None:
    page = app.goto("camera").page
    open_panel(page)
    rig.act("framing.shot", shot="presenter")
    expect(shot_button(page, "Presenter")).to_have_attribute("aria-pressed", "true", timeout=LOADED)
