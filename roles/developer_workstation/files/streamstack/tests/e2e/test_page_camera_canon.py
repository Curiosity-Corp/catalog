"""The Canon body tab on the Camera page, against the demo rig's simulated Canon."""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from playwright.sync_api import expect

from support import LOADED, App, Rig, poll


@pytest.fixture
def body(app: App, rig: Rig) -> Iterator[App]:
    app.goto("camera")
    app.page.get_by_role("button", name="Canon body", exact=True).click()
    yield app
    # the demo camera is shared by the whole session: put it back
    rig.act("video.canon_action", name="demo_dial", mode="Manual")
    rig.act("video.canon_set", id="iso", value="400")
    rig.act("video.canon_set", id="whitebalance", value="Color Temperature")
    rig.act("video.canon_set", id="kelvin", value=5200)


def canon(rig: Rig) -> dict:
    return rig.state()["video"]["canon"]


def row(app: App, label: str):
    return app.page.locator(".cb-row").filter(has=app.page.get_by_text(label, exact=True))


def test_tab_shows_the_camera_and_its_choices(body: App) -> None:
    page = body.page
    expect(page.locator(".c-body")).to_be_visible(timeout=LOADED)
    expect(page.locator(".c-looks")).to_be_hidden()
    expect(page.locator(".cb-info")).to_contain_text("Canon EOS 50D")
    select = row(body, "ISO").locator("select")
    expect(select).to_have_value("400")
    expect(select.locator("option")).to_have_count(14)
    expect(page.locator(".side-tabs")).to_be_visible()


def test_changing_iso_reaches_the_camera_and_reads_back(body: App, rig: Rig) -> None:
    select = row(body, "ISO").locator("select")
    select.select_option("1600")
    poll(rig, lambda s: s["video"]["canon"]["controls"]["iso"]["value"] == "1600")
    expect(select).to_have_value("1600")


def test_step_buttons_walk_the_choice_list(body: App, rig: Rig) -> None:
    ctl = row(body, "ISO")
    ctl.get_by_role("button", name="Next value").click()
    poll(rig, lambda s: s["video"]["canon"]["controls"]["iso"]["value"] == "500")
    ctl.get_by_role("button", name="Previous value").click()
    poll(rig, lambda s: s["video"]["canon"]["controls"]["iso"]["value"] == "400")


def test_a_setting_the_mode_dial_blocks_says_why(body: App, rig: Rig) -> None:
    rig.act("video.canon_action", name="demo_dial", mode="Shutter priority")
    ap = row(body, "Aperture")
    expect(ap.locator("select")).to_be_disabled(timeout=LOADED)
    expect(ap).to_contain_text("mode dial")
    rig.act("video.canon_action", name="demo_dial", mode="Aperture priority")
    expect(ap.locator("select")).to_be_enabled(timeout=LOADED)
    expect(ap.locator(".cb-why")).to_have_count(0)


def test_kelvin_slider_sets_the_color_temperature(body: App, rig: Rig) -> None:
    rig.act("video.canon_set", id="whitebalance", value="Daylight")
    kelvin = body.slider("Kelvin")
    expect(kelvin).to_be_visible(timeout=LOADED)
    kelvin.focus()
    body.page.keyboard.press("ArrowRight")
    body.page.keyboard.press("Tab")  # leaving the control commits it
    controls = poll(
        rig,
        lambda s: (
            s["video"]["canon"]["controls"]
            if s["video"]["canon"]["controls"]["whitebalance"]["value"] == "Color Temperature"
            else None
        ),
    )
    assert int(controls["kelvin"]["value"]) != 5200


def test_autofocus_and_focus_nudges_do_not_error(body: App) -> None:
    body.page.get_by_role("button", name=re.compile(r"^Autofocus")).click()
    body.page.get_by_role("button", name="Focus farther 2").click()
    body.page.get_by_role("button", name="Focus nearer 3").click()
    expect(body.page.locator("ss-toast")).not_to_contain_text("could not", ignore_case=True)


def test_system_page_reports_the_bridge(app: App) -> None:
    app.goto("system")
    panel = app.page.locator(".cbr")
    expect(panel).to_be_visible(timeout=LOADED)
    expect(panel).to_contain_text("GPU (Intel Quick Sync)")
    expect(panel).to_contain_text("1056×704")
    expect(panel).to_contain_text("29.9 → 29.9")
