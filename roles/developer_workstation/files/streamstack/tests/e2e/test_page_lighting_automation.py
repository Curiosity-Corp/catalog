"""Lighting automation on the Lighting page: camera follow, the daylight schedule, light settings and the status line.

The demo rig runs the automation engine inside the Console over simulated lights and a scripted camera, so every
control and every status sentence can be exercised without a camera, a light or a service.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from support import LOADED, App, Rig, poll, tap

ONE, TWO = "c901-usb-demo-1", "c901-usb-demo-2"


def light(state: dict, light_id: str) -> dict:
    return next(entry for entry in state["lighting"]["lights"] if entry["id"] == light_id)


def auto(state: dict) -> dict:
    return state["lighting"]["automation"]


@pytest.fixture
def li(app: App) -> App:
    return app.goto("lighting")


def banner(page: Page):
    return page.get_by_role("region", name="Lighting automation")


def test_the_page_shows_the_status_and_the_three_panels(li: App) -> None:
    expect(banner(li.page)).to_contain_text("Camera idle", timeout=LOADED)
    expect(banner(li.page)).to_contain_text("Running in the Console (demo)")
    for name in ("Camera follow", "Daylight schedule", "Light settings"):
        expect(li.region(name)).to_be_visible()
    expect(li.page.get_by_role("img", name=re.compile("24 hours"))).to_be_visible()


def test_a_call_switches_the_lights_on_and_off_again(li: App, rig: Rig) -> None:
    rig.act("lighting.power", on=False)
    rig.act("lighting.auto.follow", grace_seconds=5, start_delay_seconds=0)
    expect(li.region("Camera follow")).to_be_visible()
    tap(li.page, li.page.get_by_role("button", name="Teams opens the camera"))
    assert poll(rig, lambda s: light(s, ONE)["power"] and light(s, TWO)["power"])
    expect(banner(li.page)).to_contain_text("Teams opened the camera", timeout=LOADED)
    expect(banner(li.page)).to_contain_text("Camera in use")
    expect(li.page.get_by_role("list", name="Automation log")).to_contain_text("switched on", timeout=LOADED)
    tap(li.page, li.page.get_by_role("button", name="Camera released"))
    assert poll(rig, lambda s: not light(s, ONE)["power"] and not light(s, TWO)["power"], timeout=30)
    expect(li.page.get_by_role("list", name="Automation log")).to_contain_text("switched off", timeout=LOADED)


def test_a_light_that_was_already_on_stays_on_after_the_call(li: App, rig: Rig) -> None:
    rig.act("lighting.power", id=TWO, on=False)
    rig.act("lighting.auto.follow", grace_seconds=5, start_delay_seconds=0)
    rig.act("lighting.demo.camera", on=True, app="Zoom")
    assert poll(rig, lambda s: light(s, TWO)["power"])
    rig.act("lighting.demo.camera", on=False)
    assert poll(rig, lambda s: not light(s, TWO)["power"], timeout=30)
    assert light(rig.state(), ONE)["power"]


def test_camera_follow_switch_and_f_key(li: App, rig: Rig) -> None:
    follow = li.switch("Follow the camera")
    expect(follow).to_have_attribute("aria-checked", "true", timeout=LOADED)
    tap(li.page, follow)
    assert poll(rig, lambda s: auto(s)["settings"]["follow"]["enabled"] is False)
    expect(follow).to_have_attribute("aria-checked", "false", timeout=LOADED)
    li.press("f")
    assert poll(rig, lambda s: auto(s)["settings"]["follow"]["enabled"] is True)


def test_grace_and_sources_are_saved(li: App, rig: Rig) -> None:
    grace = li.page.get_by_role("spinbutton", name="Grace period in seconds")
    grace.fill("45")
    grace.press("Tab")
    assert poll(rig, lambda s: auto(s)["settings"]["follow"]["grace_seconds"] == 45)
    tap(li.page, li.switch(re.compile("OBS is recording")))
    assert poll(rig, lambda s: auto(s)["settings"]["follow"]["record"] is False)
    ignore = li.page.get_by_role("textbox", name="Apps to ignore")
    ignore.fill("Slack, obs-studio")
    ignore.press("Tab")
    assert poll(rig, lambda s: auto(s)["settings"]["follow"]["ignore"] == ["Slack", "obs-studio"])


def test_a_refused_value_is_reverted_with_a_message(li: App, rig: Rig) -> None:
    rig.act("lighting.auto.follow", grace_seconds=60)
    ignore = li.page.get_by_role("textbox", name="Extra video devices")
    ignore.fill("video0")
    ignore.press("Tab")
    expect(li.page.locator("ss-toast")).to_contain_text("/dev/", timeout=LOADED)
    assert auto(rig.state())["settings"]["follow"]["devices"] == []


def test_the_schedule_sets_the_lights_and_a_manual_change_wins(li: App, rig: Rig) -> None:
    rig.act("lighting.auto.schedule", fade_seconds=0, hold_minutes=30)
    schedule = li.switch("Follow the schedule")
    expect(schedule).to_have_attribute("aria-checked", "false", timeout=LOADED)
    tap(li.page, schedule)
    now = poll(rig, lambda s: auto(s)["settings"]["schedule"]["enabled"] and auto(s)["now"])
    assert poll(rig, lambda s: abs(light(s, ONE)["brightness"] - round(now["brightness"])) <= 2)
    expect(banner(li.page)).to_contain_text("daylight schedule", timeout=LOADED)
    rig.act("lighting.brightness", id=ONE, percent=7)
    expect(banner(li.page)).to_contain_text("changed a light by hand", timeout=LOADED)
    resume = li.page.get_by_role("button", name=re.compile("Resume the schedule"))
    expect(resume).to_be_visible()
    assert light(rig.state(), ONE)["brightness"] == 7
    tap(li.page, resume)
    assert poll(rig, lambda s: light(s, ONE)["brightness"] > 7)
    expect(resume).to_be_hidden(timeout=LOADED)


def test_editing_a_slot_level(li: App, rig: Rig) -> None:
    brightness = li.region("Schedule levels").get_by_role("spinbutton", name="Evening brightness in percent")
    brightness.fill("33")
    brightness.press("Tab")
    assert poll(rig, lambda s: auto(s)["settings"]["schedule"]["slots"]["evening"]["brightness"] == 33)
    kelvin = li.region("Schedule levels").get_by_role("spinbutton", name="Evening colour temperature in Kelvin")
    kelvin.fill("3000")
    kelvin.press("Tab")
    assert poll(rig, lambda s: auto(s)["settings"]["schedule"]["slots"]["evening"]["kelvin"] == 3000)


def test_the_clock_table_edits_one_hour(li: App, rig: Rig) -> None:
    tap(li.page, li.region("Schedule type").get_by_role("button", name="24 hour table"))
    assert poll(rig, lambda s: auto(s)["settings"]["schedule"]["mode"] == "clock")
    levels = li.region("Schedule levels")
    levels.get_by_role("combobox", name="Hour to edit").select_option("9")
    brightness = levels.get_by_role("spinbutton", name="09:00 brightness in percent")
    brightness.fill("88")
    brightness.press("Tab")
    assert poll(rig, lambda s: auto(s)["settings"]["schedule"]["hours"][9]["brightness"] == 88)
    tap(li.page, li.region("Schedule type").get_by_role("button", name="Follow the sun"))
    assert poll(rig, lambda s: auto(s)["settings"]["schedule"]["mode"] == "sun")


def test_the_s_key_and_the_palette_toggle_the_schedule(li: App, rig: Rig) -> None:
    li.press("s")
    assert poll(rig, lambda s: auto(s)["settings"]["schedule"]["enabled"] is True)
    li.press("Control+k")
    li.palette.get_by_role("combobox").fill("daylight schedule off")
    li.page.keyboard.press("Enter")
    assert poll(rig, lambda s: auto(s)["settings"]["schedule"]["enabled"] is False)


def test_the_shortcut_help_lists_the_lighting_keys(li: App) -> None:
    li.press("?")
    expect(li.shortcuts).to_contain_text("Camera follow on / off", timeout=LOADED)
    expect(li.shortcuts).to_contain_text("Daylight schedule on / off")


def test_a_light_can_be_renamed_and_given_a_role_and_limits(li: App, rig: Rig) -> None:
    row = li.region("Litra Beam 1 settings")
    name = row.get_by_role("textbox", name="Name")
    name.fill("Desk key")
    name.press("Tab")
    assert poll(rig, lambda s: light(s, ONE)["name"] == "Desk key")
    expect(li.page.get_by_role("region", name="Desk key")).to_be_visible(timeout=LOADED)
    row.get_by_role("combobox", name="Role").select_option("fill")
    assert poll(rig, lambda s: auto(s)["settings"]["lights"][ONE]["role"] == "fill")
    top = row.get_by_role("spinbutton", name="Brightest allowed in percent")
    top.fill("60")
    top.press("Tab")
    assert poll(rig, lambda s: light(s, ONE)["limits"]["max_brightness"] == 60)


def test_a_manual_role_light_is_left_alone_by_a_call(li: App, rig: Rig) -> None:
    rig.act("lighting.light.settings", id=TWO, role="manual")
    rig.act("lighting.power", id=TWO, on=False)
    rig.act("lighting.auto.follow", grace_seconds=5, start_delay_seconds=0)
    rig.act("lighting.demo.camera", on=True, app="Teams")
    expect(banner(li.page)).to_contain_text("Teams opened the camera", timeout=LOADED)
    assert not light(rig.state(), TWO)["power"]
    expect(li.region("Litra Beam 2 settings")).to_contain_text("never touches", timeout=LOADED)


def test_the_director_takes_brightness_and_colour_from_the_schedule(li: App, rig: Rig) -> None:
    rig.act("lighting.auto.schedule", enabled=True)
    expect(banner(li.page)).to_contain_text("The daylight schedule", timeout=LOADED)
    tap(li.page, li.switch("Pretend the AI Director drives the lights"))
    expect(banner(li.page)).to_contain_text("The AI Director", timeout=LOADED)
    expect(banner(li.page)).to_contain_text("Camera follow still switches power")
    assert auto(rig.state())["status"]["owner"] == "director"


def test_reset_the_schedule_restores_the_default_levels(li: App, rig: Rig) -> None:
    rig.act("lighting.auto.slot", slot="evening", brightness=11)
    tap(li.page, li.page.get_by_role("button", name="Reset the schedule"))
    assert poll(rig, lambda s: auto(s)["settings"]["schedule"]["slots"]["evening"]["brightness"] != 11)
