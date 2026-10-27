"""The System page's hardware card, against the demo rig's recorded hosts (ADR 0003).

The demo rig replays a recording of each real studio (dm313red with the Wave XLR, deskmeet1 with the Stream Deck and
two GPUs), so the same card is checked against both layouts without either machine.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from support import LOADED, App, Rig, poll


@pytest.fixture
def hw(app: App, page: Page) -> Page:
    app.goto("system")
    expect(page.locator("[data-testid=hw-rig]")).not_to_have_text("Probing…", timeout=LOADED)
    return page


def device(page: Page, ident: str):
    return page.locator(f"[data-device='{ident}']")


def gpu(page: Page, pci: str):
    return page.locator(f".hw-gpu[data-pci='{pci}']")


def show(page: Page, rig: Rig, host: str) -> None:
    page.get_by_role("button", name=host, exact=True).click()
    poll(rig, lambda s: s["hardware"]["host"] == host)


def test_dm313red_resolves_the_wave_xlr_rig(hw: Page, rig: Rig) -> None:
    expect(hw.locator("[data-testid=hw-rig]")).to_have_text("Studio with Elgato Wave XLR")
    expect(hw.locator("[data-testid=hw-why]")).to_contain_text("devices found")
    expect(device(hw, "wave-xlr")).to_have_attribute("data-state", "present")
    expect(device(hw, "cam-link-4k")).to_have_attribute("data-state", "present")
    expect(device(hw, "stream-deck-v2")).to_have_attribute("data-state", "absent")
    expect(device(hw, "uhd-770")).to_have_attribute("data-state", "absent")
    expect(hw.locator(".hw-gpu")).to_have_count(1)
    arc = gpu(hw, "8086:56a5")
    expect(arc).to_have_attribute("data-roles", "grade encode decode blur")
    expect(arc.locator(".hw-node")).to_have_text("/dev/dri/renderD128")
    audio = hw.locator("[data-testid=hw-audio]")
    expect(audio).to_have_attribute("data-interface", "wave-xlr")
    expect(audio).to_have_attribute("data-rate", "48000")
    expect(audio).to_have_attribute("data-format", "s24le")
    assert rig.state()["hardware"]["rig"]["id"] == "studio-wave-xlr"


def test_deskmeet1_resolves_the_deck_rig_with_split_gpu_roles(hw: Page, rig: Rig) -> None:
    show(hw, rig, "deskmeet1")
    expect(hw.locator("[data-testid=hw-rig]")).to_have_text("Studio with Stream Deck")
    expect(device(hw, "stream-deck-v2")).to_have_attribute("data-state", "present")
    expect(device(hw, "wave-xlr")).to_have_attribute("data-state", "absent")
    expect(hw.locator(".hw-gpu")).to_have_count(2)
    arc = gpu(hw, "8086:56a5")
    igpu = gpu(hw, "8086:a780")
    expect(arc).to_have_attribute("data-roles", "grade encode blur")
    expect(arc.locator(".hw-node")).to_have_text("/dev/dri/renderD129")
    expect(igpu).to_have_attribute("data-roles", "decode")
    expect(igpu.locator(".hw-node")).to_have_text("/dev/dri/renderD128")
    expect(hw.locator("[data-testid=hw-audio]")).to_have_text(re.compile(r"^No audio interface"))
    expect(hw.locator(".hw-notice[data-id='audio-fallback']")).to_be_visible()
    expect(hw.locator(".hw-notice[data-id='audio-dummy-sink']")).to_contain_text("Dummy Output")


def test_switching_back_restores_the_first_layout(hw: Page, rig: Rig) -> None:
    show(hw, rig, "deskmeet1")
    show(hw, rig, "dm313red")
    expect(hw.locator("[data-testid=hw-rig]")).to_have_text("Studio with Elgato Wave XLR")
    expect(hw.locator(".hw-gpu")).to_have_count(1)


def test_the_page_never_shows_a_serial_number(hw: Page, rig: Rig) -> None:
    text = hw.locator("[data-testid=hardware]").inner_text()
    assert "<serial>" in text or "usb-" not in text
    assert not re.search(r"_[A-Z0-9]{8,}-\d\d", text)
    assert not re.search(r"_[A-Z0-9]{8,}-\d\d", str(rig.state()["hardware"]))


def test_probe_again_reports_back(hw: Page) -> None:
    with hw.expect_response(
        lambda r: r.url.endswith("api/action") and "hardware.refresh" in (r.request.post_data or "")
    ) as hit:
        hw.get_by_role("button", name="Probe the hardware again").click()
    assert hit.value.status == 200


# -- the doctor's hardware group: the live snapshot checked against the rig profile (#122) --------------


def doctor_card(page: Page):
    return page.locator("section.cgrp[aria-label='Hardware']")


def test_the_doctor_card_names_the_rig_and_how_it_was_chosen(hw: Page, rig: Rig) -> None:
    card = doctor_card(hw)
    expect(card).to_be_visible(timeout=LOADED)
    expect(card.locator(".ck").first).to_contain_text("rig: studio-wave-xlr (selected by overlap)")
    checks = {c["id"]: c for c in rig.state()["health"]["checks"] if c["group"] == "hardware"}
    assert checks["hw-rig"]["title"] == "rig: studio-wave-xlr (selected by overlap)"
    assert all(c["hint"] for c in checks.values() if c["state"] != "ok")


def test_the_doctor_card_shows_the_wave_xlr_at_its_native_rate(hw: Page) -> None:
    row = doctor_card(hw).locator(".ck").filter(has_text="48000 Hz s24le")
    expect(row).to_have_count(1)
    expect(row).to_have_attribute("data-state", "ok")
    expect(row).to_contain_text("no resampling")


def test_the_doctor_card_warns_that_the_scarlett_is_not_enumerating_on_deskmeet1(hw: Page, rig: Rig) -> None:
    show(hw, rig, "deskmeet1")
    row = doctor_card(hw).locator(".ck").filter(has_text="not enumerating on USB")
    expect(row).to_have_count(1, timeout=LOADED)
    expect(row).to_have_attribute("data-state", "warn")  # not green, and not a failure: nothing is broken
    expect(row.locator(".ck-hint")).to_contain_text("direct rear USB port")
    expect(doctor_card(hw).locator(".ck").first).to_contain_text("rig: studio-deck (selected by ")
    checks = [c for c in rig.state()["health"]["checks"] if c["group"] == "hardware"]
    assert not any(c["state"] == "fail" for c in checks)


def test_the_doctor_card_follows_the_host_back_to_dm313red(hw: Page, rig: Rig) -> None:
    show(hw, rig, "deskmeet1")
    show(hw, rig, "dm313red")
    expect(doctor_card(hw).locator(".ck").first).to_contain_text("rig: studio-wave-xlr", timeout=LOADED)
    expect(doctor_card(hw)).not_to_contain_text("not enumerating on USB")


def test_the_doctor_card_leaks_no_serial(hw: Page) -> None:
    text = doctor_card(hw).inner_text()
    assert not re.search(r"_[A-Z0-9]{8,}-\d\d", text)
