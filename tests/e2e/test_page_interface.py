"""The Interface page: a Scarlett 2i2 (Gen 4 and Gen 3) and a Wave XLR driven from the demo rig's simulated hardware."""

from __future__ import annotations

import pytest
from playwright.sync_api import Locator, Page, expect

from support import LOADED, App, Rig, hold, poll, quiet


@pytest.fixture
def ifc(app: App) -> App:
    return app.goto("interface")


def card(page: Page, heading: str) -> Locator:
    return page.locator(f"ss-card[heading='{heading}']")


def control(rig: Rig, wid: str) -> dict:
    return next(w for w in rig.state()["interface"]["controls"] if w["id"] == wid)


def pick(page: Page, profile: str) -> None:
    page.locator(f"[data-demo='{profile}']").click()
    expect(page.locator(f"[data-demo='{profile}']")).to_have_attribute("aria-pressed", "true", timeout=LOADED)


def test_gen4_scarlett_is_detected_with_its_controls(ifc: App, page: Page) -> None:
    expect(page.get_by_role("heading", name="Focusrite Scarlett 2i2 (4th Gen)")).to_be_visible()
    expect(page.locator(".ifc-sub")).to_contain_text("USB 1235:8219")
    for heading in ("Input 1", "Input 2", "Power", "Monitoring"):
        expect(card(page, heading)).to_be_visible()
    expect(card(page, "Input 1").get_by_role("slider", name="Preamp gain")).to_be_visible()
    expect(card(page, "Input 1").get_by_role("button", name="Autogain")).to_be_visible()


def test_gain_key_reaches_the_interface(ifc: App, page: Page, rig: Rig) -> None:
    before = control(rig, "gain.1")["value"]
    slider = card(page, "Input 1").get_by_role("slider", name="Preamp gain")
    slider.focus()
    slider.press("ArrowUp")
    assert poll(rig, lambda s: next(w for w in s["interface"]["controls"] if w["id"] == "gain.1")["value"] > before)


def test_autogain_runs_and_reports(ifc: App, page: Page, rig: Rig) -> None:
    quiet(page)
    card(page, "Input 1").get_by_role("button", name="Autogain").click()
    assert poll(rig, lambda s: next(w for w in s["interface"]["controls"] if w["id"] == "autogain.1")["value"])
    expect(card(page, "Input 1")).to_contain_text("Measuring", timeout=LOADED)
    expect(card(page, "Input 1")).to_contain_text("Last result: Done", timeout=LOADED)


def test_phantom_power_needs_a_hold_and_a_confirm(ifc: App, page: Page, rig: Rig) -> None:
    status, body = rig.request(
        "POST", "api/action", {"action": "interface.set", "id": "phantom.1", "value": True}, rig.token
    )
    assert status == 400 and "confirmation" in body["error"], body
    assert control(rig, "phantom.1")["value"] is False
    arm = page.get_by_role("button", name="Hold to switch on")
    quiet(page)
    arm.click()  # a plain click is not enough
    expect(page.get_by_role("switch", name="48V phantom power")).to_be_hidden()
    hold(page, arm, 1400)
    assert poll(rig, lambda s: next(w for w in s["interface"]["controls"] if w["id"] == "phantom.1")["value"])
    switch = page.get_by_role("switch", name="48V phantom power")
    expect(switch).to_have_attribute("aria-checked", "true", timeout=LOADED)
    switch.click()
    assert poll(rig, lambda s: not next(w for w in s["interface"]["controls"] if w["id"] == "phantom.1")["value"])


def test_air_choice_is_driven_by_a_click(ifc: App, page: Page, rig: Rig) -> None:
    quiet(page)
    card(page, "Input 1").get_by_role("button", name="Presence", exact=True).click()
    assert poll(rig, lambda s: next(w for w in s["interface"]["controls"] if w["id"] == "air.1")["value"] == 1)


def test_gen3_has_no_software_gain_or_autogain(ifc: App, page: Page, rig: Rig) -> None:
    pick(page, "scarlett-2i2-gen3")
    expect(page.get_by_role("heading", name="Focusrite Scarlett 2i2 (3rd Gen)")).to_be_visible()
    expect(page.get_by_role("slider", name="Preamp gain")).to_have_count(0)
    expect(page.get_by_role("button", name="Autogain")).to_have_count(0)
    expect(page.get_by_role("switch", name="Air")).to_have_count(2)


def test_wave_xlr_hardware_controls_and_honest_gaps(ifc: App, page: Page, rig: Rig) -> None:
    pick(page, "wave-xlr")
    expect(page.get_by_role("heading", name="Elgato Wave XLR")).to_be_visible()
    expect(page.locator(".ifc-sub")).to_contain_text("USB 0fd9:007d")
    expect(page.get_by_role("slider", name="Mic gain")).to_be_visible()
    unsupported = page.locator("ss-card[heading='Not available on this interface']")
    expect(unsupported).to_contain_text("Clipguard")
    expect(unsupported).to_contain_text("Low cut")
    quiet(page)
    page.get_by_role("switch", name="Mute").click()
    assert poll(rig, lambda s: next(w for w in s["interface"]["controls"] if w["id"] == "mute")["value"])


def test_no_interface_says_what_to_plug_in(ifc: App, page: Page, rig: Rig) -> None:
    pick(page, "none")
    expect(page.get_by_role("heading", name="No supported audio interface found")).to_be_visible()
    expect(page.locator(".ifc-sub")).to_contain_text("Scarlett 2i2")
    assert poll(rig, lambda s: not s["interface"]["present"] and s["interface"]["controls"] == [])
