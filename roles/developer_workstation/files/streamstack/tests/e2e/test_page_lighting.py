"""The Lighting page: Litra lights as faders and pads, the All-lights master, presets and quick controls."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Locator, Page, expect

from support import LOADED, App, Rig, poll, quiet, tap

ONE, TWO = "c901-usb-demo-1", "c901-usb-demo-2"


def light(state: dict, light_id: str) -> dict:
    return next(entry for entry in state["lighting"]["lights"] if entry["id"] == light_id)


@pytest.fixture
def li(app: App) -> App:
    return app.goto("lighting")


def card(app: App, name: str) -> Locator:
    return app.page.get_by_role("region", name=name)


def power_pad(region: Locator) -> Locator:
    return region.get_by_role("button", name=re.compile(r"^(On|Off|All lights)\b"))


def test_one_card_per_light_and_a_master(li: App) -> None:
    for name in ("Litra Beam 1", "Litra Beam 2", "All lights"):
        region = card(li, name)
        expect(region).to_be_visible()
        expect(region.get_by_role("slider", name="Brightness")).to_be_visible()
        expect(region.get_by_role("slider", name="Temperature")).to_be_visible()
    expect(li.page).to_have_title(re.compile("^Lighting"))


def test_brightness_fader_drag_reaches_the_light(li: App, rig: Rig) -> None:
    fader = card(li, "Litra Beam 1").get_by_role("slider", name="Brightness")
    li.drag_fader(fader, 0.9)
    assert poll(rig, lambda s: light(s, ONE)["brightness"] >= 80)
    assert light(rig.state(), TWO)["brightness"] == 55  # the other light is untouched


def test_temperature_fader_drag_reaches_the_light(li: App, rig: Rig) -> None:
    fader = card(li, "Litra Beam 2").get_by_role("slider", name="Temperature")
    li.drag_fader(fader, 0.05)
    assert poll(rig, lambda s: light(s, TWO)["kelvin"] <= 3000)
    assert light(rig.state(), ONE)["kelvin"] == 5000


def test_fader_keys_step_brightness(li: App, rig: Rig) -> None:
    fader = card(li, "Litra Beam 1").get_by_role("slider", name="Brightness")
    fader.focus()
    fader.press("ArrowUp")
    assert poll(rig, lambda s: light(s, ONE)["brightness"] > 70)


def test_power_pad_toggles_one_light(li: App, rig: Rig) -> None:
    pad = power_pad(card(li, "Litra Beam 1"))
    expect(pad).to_contain_text("On")
    tap(li.page, pad)
    assert poll(rig, lambda s: not light(s, ONE)["power"])
    assert light(rig.state(), TWO)["power"]
    expect(pad).to_contain_text("Off", timeout=LOADED)
    tap(li.page, pad)
    assert poll(rig, lambda s: light(s, ONE)["power"])


def test_dragging_a_fader_on_an_off_light_turns_it_on(li: App, rig: Rig) -> None:
    rig.act("lighting.power", id=ONE, on=False)
    expect(power_pad(card(li, "Litra Beam 1"))).to_contain_text("Off", timeout=LOADED)
    li.drag_fader(card(li, "Litra Beam 1").get_by_role("slider", name="Brightness"), 0.4)
    assert poll(rig, lambda s: light(s, ONE)["power"])


def test_server_side_change_reaches_the_fader(li: App, rig: Rig) -> None:
    rig.act("lighting.brightness", id=TWO, percent=20)
    expect(card(li, "Litra Beam 2").get_by_role("slider", name="Brightness")).to_have_attribute(
        "aria-valuenow", "20", timeout=LOADED
    )


def test_all_lights_master_moves_every_light(li: App, rig: Rig) -> None:
    master = card(li, "All lights")
    li.drag_fader(master.get_by_role("slider", name="Brightness"), 0.2)
    assert poll(rig, lambda s: light(s, ONE)["brightness"] <= 30 and light(s, TWO)["brightness"] <= 30)
    li.drag_fader(master.get_by_role("slider", name="Temperature"), 0.95)
    assert poll(rig, lambda s: light(s, ONE)["kelvin"] >= 6000 and light(s, TWO)["kelvin"] >= 6000)


def test_the_controls_do_not_move_when_a_manual_change_is_held(li: App, rig: Rig) -> None:
    """The first hand on a light makes the engine hold it, and the page must not jump when "Resume" appears."""
    master = card(li, "All lights")
    rail = master.get_by_role("slider", name="Temperature").locator(".track .rail")
    before = rail.bounding_box()
    li.drag_fader(master.get_by_role("slider", name="Brightness"), 0.2)
    assert poll(rig, lambda s: light(s, ONE)["brightness"] <= 30)
    expect(li.page.get_by_role("button", name=re.compile("Resume the schedule"))).to_be_visible(timeout=LOADED)
    after = rail.bounding_box()
    assert before is not None and after is not None
    assert abs(after["y"] - before["y"]) < 1, f"the Temperature fader moved from {before['y']} to {after['y']}"


def test_all_lights_power_pad(li: App, rig: Rig) -> None:
    pad = power_pad(card(li, "All lights"))
    tap(li.page, pad)
    assert poll(rig, lambda s: not light(s, ONE)["power"] and not light(s, TWO)["power"])
    expect(pad).to_contain_text("off", timeout=LOADED)
    tap(li.page, pad)
    assert poll(rig, lambda s: light(s, ONE)["power"] and light(s, TWO)["power"])


def test_l_key_toggles_every_light(li: App, rig: Rig) -> None:
    li.press("l")
    assert poll(rig, lambda s: not light(s, ONE)["power"] and not light(s, TWO)["power"])
    li.press("l")
    assert poll(rig, lambda s: light(s, ONE)["power"] and light(s, TWO)["power"])


def test_preset_pads_apply_to_both_lights(li: App, rig: Rig) -> None:
    presets = li.region("Lighting presets")
    warm = presets.get_by_role("button", name=re.compile(r"^Warm evening\b"))
    tap(li.page, warm)
    assert poll(rig, lambda s: light(s, ONE)["kelvin"] == 2700 and light(s, TWO)["kelvin"] == 2700)
    expect(warm).to_have_attribute("aria-pressed", "true", timeout=LOADED)
    tap(li.page, presets.get_by_role("button", name=re.compile(r"^Off\b")))
    assert poll(rig, lambda s: not light(s, ONE)["power"] and not light(s, TWO)["power"])


def test_save_and_delete_your_own_preset(li: App, rig: Rig) -> None:
    page: Page = li.page
    rig.act("lighting.brightness", id="all", percent=33)
    rig.act("lighting.temperature", id="all", kelvin=3300)
    page.get_by_role("textbox", name="Preset name").fill("Interview")
    page.get_by_role("button", name="Save current").click()
    assert poll(rig, lambda s: any(p["name"] == "Interview" and not p["builtin"] for p in s["lighting"]["presets"]))
    pad = li.region("Lighting presets").get_by_role("button", name=re.compile(r"^Interview\b"))
    expect(pad).to_be_visible(timeout=LOADED)
    rig.act("lighting.preset", name="Daylight")
    assert poll(rig, lambda s: light(s, ONE)["kelvin"] == 5600)
    tap(page, pad)
    assert poll(rig, lambda s: light(s, ONE)["kelvin"] == 3300 and light(s, ONE)["brightness"] == 33)
    delete = page.get_by_role("button", name=re.compile("^Delete preset"))
    expect(delete).to_be_visible(timeout=LOADED)
    tap(page, delete)
    assert poll(rig, lambda s: all(p["name"] != "Interview" for p in s["lighting"]["presets"]))
    expect(pad).to_have_count(0, timeout=LOADED)


def test_identify_pulses_and_restores(li: App, rig: Rig) -> None:
    before = light(rig.state(), ONE)["brightness"]
    tap(li.page, card(li, "Litra Beam 1").get_by_role("button", name="Identify"))
    assert poll(rig, lambda s: not light(s, ONE)["identifying"] and light(s, ONE)["brightness"] == before)


def test_live_page_quick_control(app: App, rig: Rig) -> None:
    app.goto("live")
    group = app.region("Lights")
    expect(group).to_be_visible()
    quiet(app.page)
    group.get_by_role("button", name="Lights").click()
    assert poll(rig, lambda s: not light(s, ONE)["power"] and not light(s, TWO)["power"])
    app.drag_fader(group.get_by_role("slider", name="Light level"), 0.9)
    assert poll(rig, lambda s: light(s, ONE)["power"] and light(s, ONE)["brightness"] >= 80)


def test_command_palette_lists_lighting(app: App, page: Page, rig: Rig) -> None:
    app.goto("live")
    app.press("Control+k")
    expect(app.palette).to_be_visible()
    app.palette.get_by_role("combobox").fill("warm evening")
    app.palette.get_by_role("option", name=re.compile("Lights: Warm evening")).click()
    assert poll(rig, lambda s: light(s, ONE)["kelvin"] == 2700)


def test_shortcut_help_lists_the_lights_key(app: App) -> None:
    app.goto("lighting")
    app.press("?")
    expect(app.shortcuts).to_contain_text("Lights on / off")
