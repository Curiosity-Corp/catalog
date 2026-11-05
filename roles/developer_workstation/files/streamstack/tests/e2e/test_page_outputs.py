"""The Audio page's Output section: every output with volume, mute, default, the sound test and its verdict,
hot-plug, and the output picker on the Live page. Only the demo rig's four simulated outputs are ever touched."""

from __future__ import annotations

import re
from typing import Any

import pytest
from playwright.sync_api import Locator, expect

from support import LOADED, App, Rig, poll

WAVE = "Elgato Wave XLR (demo)"
SCARLETT = "Focusrite Scarlett 2i2 (demo)"
MONITOR = "Studio Monitor (demo)"
HEADSET = "Evolve2 65 headset (demo)"
WAVE_SINK = "alsa_output.demo-wave-xlr.analog-stereo"
LABELS = (WAVE, SCARLETT, MONITOR, HEADSET)


def outputs(state: dict[str, Any]) -> dict[str, Any]:
    return state["audio"]["outputs"]


def device(state: dict[str, Any], label: str) -> dict[str, Any]:
    return next(d for d in outputs(state)["devices"] if d["label"] == label)


@pytest.fixture
def out(app: App) -> App:
    app.goto("audio")
    app.page.get_by_role("button", name=re.compile("^Output")).click()
    expect(app.page.get_by_role("list", name="Outputs")).to_be_visible()
    return app


def strip(app: App, label: str) -> Locator:
    return (
        app.page.get_by_role("listitem")
        .filter(has=app.page.get_by_role("group", name=label))
        .or_(app.page.get_by_role("listitem", name=label))
    )


def test_input_is_the_first_section_and_the_keys_switch(app: App) -> None:
    app.goto("audio")
    switch = app.region("Audio section")
    expect(switch.get_by_role("button", name="Input")).to_have_attribute("aria-pressed", "true")
    expect(app.slider("Gain")).to_be_visible()
    app.press("o")
    expect(switch.get_by_role("button", name=re.compile("^Output"))).to_have_attribute("aria-pressed", "true")
    expect(app.page.get_by_role("list", name="Outputs")).to_be_visible()
    expect(app.slider("Gain")).to_be_hidden()
    app.press("i")
    expect(app.slider("Gain")).to_be_visible()


def test_every_output_is_listed_with_its_route_apart(out: App, rig: Rig) -> None:
    page = out.page
    for label in LABELS:
        expect(page.get_by_role("listitem", name=label)).to_be_visible()
    expect(page.get_by_role("listitem")).to_have_count(4)
    expect(page.get_by_text("4 outputs", exact=True)).to_be_visible()
    # the StreamStack route is a route: shown, but with no strip and no test
    routes = out.region("Routes")
    expect(routes).to_contain_text("StreamStack Route")
    expect(routes).to_contain_text("not a device")
    expect(page.get_by_role("button", name=re.compile("^Test StreamStack Route"))).to_have_count(0)
    # the default one is marked
    expect(page.get_by_role("listitem", name=WAVE).get_by_text("Default", exact=True)).to_be_visible()
    expect(page.get_by_role("listitem", name=SCARLETT).get_by_text("Default", exact=True)).to_be_hidden()


def test_volume_and_mute_reach_the_rig(out: App, rig: Rig) -> None:
    item = out.page.get_by_role("listitem", name=SCARLETT)
    volume = item.get_by_role("slider", name="Volume")
    before = device(rig.state(), SCARLETT)["volume"]
    volume.focus()
    volume.press("ArrowUp")
    assert poll(rig, lambda s: device(s, SCARLETT)["volume"] == before + 1)
    item.get_by_role("button", name=f"Mute {SCARLETT}").click()
    assert poll(rig, lambda s: device(s, SCARLETT)["muted"])
    expect(item.get_by_role("button", name=f"Test {SCARLETT}")).to_be_disabled()
    item.get_by_role("button", name=f"Mute {SCARLETT}").click()
    assert poll(rig, lambda s: not device(s, SCARLETT)["muted"])
    expect(item.get_by_role("button", name=f"Test {SCARLETT}")).to_be_enabled()


def test_set_as_default(out: App, rig: Rig) -> None:
    item = out.page.get_by_role("listitem", name=MONITOR)
    item.get_by_role("button", name=f"Set {MONITOR} as default").click()
    assert poll(rig, lambda s: device(s, MONITOR)["default"] and not device(s, WAVE)["default"])
    expect(item.get_by_role("button", name=f"{MONITOR} is the default output")).to_be_disabled()
    expect(out.page.get_by_role("listitem", name=WAVE).get_by_text("Default", exact=True)).to_be_hidden()


def test_test_plays_on_one_output_then_asks_for_the_verdict(out: App, rig: Rig) -> None:
    item = out.page.get_by_role("listitem", name=SCARLETT)
    expect(item).to_contain_text("Not tested yet")
    item.get_by_role("button", name=f"Test {SCARLETT}").click()
    state = poll(rig, lambda s: device(s, SCARLETT)["playing"] and s)
    assert device(state, WAVE)["playing"] is False
    assert outputs(state)["default"] == WAVE_SINK, "a test must never change the default output"
    expect(item.locator("ss-button.ao-test")).to_contain_text("Playing")
    # the other outputs cannot start a second test meanwhile
    expect(out.page.get_by_role("button", name=f"Test {MONITOR}")).to_be_disabled()
    expect(item).to_have_attribute("data-playing", "true")
    # it ends by itself and asks
    expect(item.get_by_text("Did you hear it?")).to_be_visible(timeout=LOADED)
    assert device(rig.state(), SCARLETT)["result"]["result"] == "pending"
    item.get_by_role("button", name=f"Heard the test on {SCARLETT}").click()
    assert poll(rig, lambda s: device(s, SCARLETT)["result"]["result"] == "heard")
    expect(item).to_contain_text("Heard OK")
    expect(out.page.get_by_text("1 of 4 confirmed by ear", exact=False)).to_be_visible()


def test_not_heard_is_remembered_as_such(out: App, rig: Rig) -> None:
    item = out.page.get_by_role("listitem", name=MONITOR)
    item.get_by_role("button", name=f"Test {MONITOR}").click()
    expect(item.get_by_text("Did you hear it?")).to_be_visible(timeout=LOADED)
    item.get_by_role("button", name=f"Did not hear the test on {MONITOR}").click()
    assert poll(rig, lambda s: device(s, MONITOR)["result"]["result"] == "not-heard")
    expect(item).to_contain_text("Not heard")
    out.page.reload()
    out.wait_ready("audio")
    out.page.get_by_role("button", name=re.compile("^Output")).click()
    expect(out.page.get_by_role("listitem", name=MONITOR)).to_contain_text("Not heard")


def test_test_all_runs_each_output_and_stop_ends_it(out: App, rig: Rig) -> None:
    out.page.get_by_role("button", name="Test all outputs").click()
    state = poll(rig, lambda s: outputs(s)["test"]["state"] == "playing" and s)
    assert outputs(state)["test"]["all"] is True
    assert outputs(state)["test"]["total"] == 4
    stop = out.page.get_by_role("button", name="Stop test")
    expect(stop).to_be_visible()
    expect(out.page.get_by_text("Testing", exact=False).first).to_be_visible()
    stop.click()
    assert poll(rig, lambda s: outputs(s)["test"]["state"] == "idle")
    expect(out.page.get_by_role("button", name="Test all outputs")).to_be_enabled()


def test_unplugging_the_default_warns_and_the_list_follows(out: App, rig: Rig) -> None:
    rig.act("audio.demo_output_plug", name=WAVE_SINK, plugged=False)
    alert = out.page.get_by_role("alert").filter(has_text="was the default output")
    expect(alert).to_be_visible(timeout=LOADED)
    expect(out.page.get_by_role("listitem", name=WAVE)).to_have_count(0)
    expect(out.page.get_by_role("listitem")).to_have_count(3)
    expect(out.region("Audio section").get_by_role("button", name=re.compile("Default output lost"))).to_be_visible()
    alert.get_by_role("button", name="Dismiss").click()
    expect(alert).to_be_hidden()
    assert poll(rig, lambda s: outputs(s)["warning"] is None)
    rig.act("audio.demo_output_plug", name=WAVE_SINK, plugged=True)
    expect(out.page.get_by_role("listitem", name=WAVE)).to_be_visible(timeout=LOADED)


def test_live_page_has_the_output_picker_and_a_test_button(app: App, rig: Rig) -> None:
    app.goto("live")
    picker = app.page.get_by_role("combobox", name="Output device")
    expect(picker).to_have_value(WAVE_SINK)
    expect(picker.locator("option")).to_have_count(5)  # four outputs and the route
    picker.select_option("alsa_output.demo-scarlett.analog-stereo")
    assert poll(rig, lambda s: device(s, SCARLETT)["default"])
    app.page.get_by_role("button", name="Test the selected output").click()
    assert poll(rig, lambda s: device(s, SCARLETT)["playing"])
    assert poll(rig, lambda s: device(s, SCARLETT)["result"] is not None)
    expect(app.page.get_by_role("img", name="Did you hear it?")).to_be_visible(timeout=LOADED)


# ---- the output menu in the top bar: on every page, so the switch is never hard to find ----

SCARLETT_SINK = "alsa_output.demo-scarlett.analog-stereo"


def menu_button(app: App) -> Locator:
    return app.page.get_by_role("button", name=re.compile("^Sound output"))


def menu(app: App) -> Locator:
    return app.page.get_by_role("menu", name="Sound output")


@pytest.mark.parametrize("route", ["live", "camera", "audio", "system"])
def test_top_bar_shows_where_the_sound_plays_on_every_page(app: App, route: str) -> None:
    app.goto(route)
    expect(menu_button(app)).to_contain_text(WAVE)
    expect(menu_button(app)).to_have_attribute("aria-expanded", "false")
    expect(menu(app)).to_be_hidden()


def test_top_bar_menu_lists_every_output_and_switches_back_and_forth(app: App, rig: Rig) -> None:
    app.goto("system")
    button, pop = menu_button(app), menu(app)
    button.click()
    expect(pop.get_by_role("menuitemradio")).to_have_count(5)  # four outputs and the route
    for label in LABELS:
        expect(pop.get_by_role("menuitemradio", name=label)).to_be_visible()
    expect(pop.get_by_role("menuitemradio", name=WAVE)).to_have_attribute("aria-checked", "true")
    expect(pop.get_by_role("menuitemradio", name=SCARLETT)).to_have_attribute("aria-checked", "false")

    pop.get_by_role("menuitemradio", name=SCARLETT).click()
    assert poll(rig, lambda s: device(s, SCARLETT)["default"] and not device(s, WAVE)["default"])
    expect(pop).to_be_hidden()
    expect(button).to_contain_text(SCARLETT)

    button.click()
    pop.get_by_role("menuitemradio", name=WAVE).click()
    assert poll(rig, lambda s: device(s, WAVE)["default"] and not device(s, SCARLETT)["default"])
    expect(button).to_contain_text(WAVE)


def test_top_bar_menu_follows_a_switch_made_elsewhere(app: App, rig: Rig) -> None:
    app.goto("live")
    rig.act("audio.output_default", name=SCARLETT_SINK)
    expect(menu_button(app)).to_contain_text(SCARLETT, timeout=LOADED)
    menu_button(app).click()
    expect(menu(app).get_by_role("menuitemradio", name=SCARLETT)).to_have_attribute("aria-checked", "true")


def test_top_bar_menu_is_operable_from_the_keyboard(app: App, rig: Rig) -> None:
    app.goto("live")
    button, pop = menu_button(app), menu(app)
    button.focus()
    button.press("ArrowDown")
    expect(pop.get_by_role("menuitemradio", name=WAVE)).to_be_focused()  # opens on the current output
    app.page.keyboard.press("ArrowDown")
    expect(pop.get_by_role("menuitemradio", name=SCARLETT)).to_be_focused()
    app.page.keyboard.press("Enter")
    assert poll(rig, lambda s: device(s, SCARLETT)["default"])
    expect(pop).to_be_hidden()
    expect(button).to_be_focused()
    button.press("ArrowDown")
    expect(pop).to_be_visible()
    app.page.keyboard.press("Escape")
    expect(pop).to_be_hidden()
    expect(button).to_be_focused()
    assert device(rig.state(), SCARLETT)["default"], "Escape must not change the output"


def test_top_bar_menu_closes_on_an_outside_click(app: App) -> None:
    app.goto("live")
    menu_button(app).click()
    expect(menu(app)).to_be_visible()
    app.page.locator("a.rail-item[data-id=camera]").click()
    expect(menu(app)).to_be_hidden()


def test_top_bar_menu_follows_hot_plug_while_open(app: App, rig: Rig) -> None:
    app.goto("live")
    menu_button(app).click()
    pop = menu(app)
    rig.act("audio.demo_output_plug", name=WAVE_SINK, plugged=False)
    expect(pop.get_by_role("menuitemradio", name=WAVE)).to_have_count(0, timeout=LOADED)
    expect(pop.get_by_role("menuitemradio")).to_have_count(4)
    expect(pop).to_contain_text("was the default output")  # and says so
    expect(menu_button(app)).not_to_contain_text(WAVE)
    rig.act("audio.demo_output_plug", name=WAVE_SINK, plugged=True)
    expect(pop.get_by_role("menuitemradio", name=WAVE)).to_be_visible(timeout=LOADED)
    expect(pop.get_by_role("menuitemradio")).to_have_count(5)


def test_top_bar_menu_opens_the_audio_pages_output_section(app: App) -> None:
    app.goto("live")
    menu_button(app).click()
    menu(app).get_by_role("menuitem", name="Volume, mute and sound test").click()
    expect(app.page.get_by_role("list", name="Outputs")).to_be_visible(timeout=LOADED)
    expect(app.region("Audio section").get_by_role("button", name=re.compile("^Output"))).to_have_attribute(
        "aria-pressed", "true"
    )
    expect(menu(app)).to_be_hidden()


def test_the_live_pickers_label_says_what_it_is(app: App) -> None:
    app.goto("live")
    expect(app.page.get_by_text("Sound plays on")).to_be_visible()


def test_the_palette_can_switch_the_output_from_any_page(app: App, rig: Rig) -> None:
    app.goto("system")
    app.press("Control+k")
    expect(app.palette).to_be_visible()
    app.palette.get_by_role("combobox").fill("Play sound on Focusrite")
    app.palette.get_by_role("option", name=re.compile("Play sound on Focusrite Scarlett")).click()
    assert poll(rig, lambda s: device(s, SCARLETT)["default"])
    expect(menu_button(app)).to_contain_text(SCARLETT, timeout=LOADED)
