"""Time-of-day profiles on the Camera page: the popover, the strip, editing a slot, applying and the hold."""

from __future__ import annotations

from playwright.sync_api import Locator, Page, expect

from support import LOADED, App, Rig, poll


def open_panel(page: Page) -> Locator:
    page.locator('ss-button[title^="Time-of-day"]').click()
    dialog = page.get_by_role("dialog", name="Time-of-day profiles")
    expect(dialog).to_be_visible()
    return dialog


def row(dialog: Locator, slot: str) -> Locator:
    return dialog.locator(f'.sl-row[data-slot="{slot}"]')


def profile_of(rig: Rig, slot: str) -> dict:
    return next(s for s in rig.state()["slots"]["slots"] if s["id"] == slot)["profile"]


def test_the_popover_shows_the_day_the_mode_and_a_row_per_slot(app: App, rig: Rig) -> None:
    page = app.goto("camera").page
    dialog = open_panel(page)
    expect(dialog.locator(".sl-row")).to_have_count(6, timeout=LOADED)
    expect(dialog.locator(".sl-seg-part").first).to_be_visible()
    expect(dialog.locator(".sl-now")).to_be_attached()
    expect(dialog.get_by_role("group", name="When profiles apply").get_by_role("button", name="Off")).to_have_attribute(
        "aria-pressed", "true"
    )
    expect(dialog.get_by_role("status")).to_contain_text("Profiles are off")
    current = rig.state()["slots"]["current"]["slot"]
    expect(dialog.locator(".sl-row[data-current]")).to_have_attribute("data-slot", current)


def test_the_mode_switch_and_a_change_made_elsewhere_reach_the_server_and_the_page(app: App, rig: Rig) -> None:
    page = app.goto("camera").page
    dialog = open_panel(page)
    modes = dialog.get_by_role("group", name="When profiles apply")
    modes.get_by_role("button", name="Automatic").click()
    poll(rig, lambda st: st["slots"]["mode"] == "auto")
    expect(modes.get_by_role("button", name="Automatic")).to_have_attribute("aria-pressed", "true")
    rig.act("slots.mode", mode="suggest")
    expect(modes.get_by_role("button", name="Suggest")).to_have_attribute("aria-pressed", "true", timeout=LOADED)


def test_choosing_a_look_and_lights_stores_the_slot_profile(app: App, rig: Rig) -> None:
    page = app.goto("camera").page
    dialog = open_panel(page)
    dawn = row(dialog, "dawn")
    expect(dawn).to_have_attribute("data-empty", "", timeout=LOADED)
    dawn.get_by_label("Look").select_option("Golden Hour")
    poll(
        rig,
        lambda st: next(s for s in st["slots"]["slots"] if s["id"] == "dawn")["profile"].get("look") == "Golden Hour",
    )
    dawn.get_by_label("Lights").select_option("Warm evening")
    poll(
        rig,
        lambda st: (
            next(s for s in st["slots"]["slots"] if s["id"] == "dawn")["profile"].get("lighting") == "Warm evening"
        ),
    )
    assert profile_of(rig, "dawn") == {"look": "Golden Hour", "lighting": "Warm evening"}
    expect(dawn).not_to_have_attribute("data-empty", "")
    dawn.get_by_role("button", name="Clear").click()
    poll(rig, lambda st: not next(s for s in st["slots"]["slots"] if s["id"] == "dawn")["profile"])


def test_apply_now_sets_the_rig_and_undo_puts_it_back(app: App, rig: Rig) -> None:
    rig.act("slots.profile", slot="dawn", profile={"lighting": "Warm evening", "look": "Noir"})
    before = rig.state()["video"]["look"]
    page = app.goto("camera").page
    dialog = open_panel(page)
    row(dialog, "dawn").get_by_role("button", name="Apply", exact=True).click()
    poll(rig, lambda st: st["lighting"]["preset"] == "Warm evening")
    status = dialog.get_by_role("status")
    expect(status).to_contain_text("dawn profile is in place", timeout=LOADED)
    expect(dialog.locator(".sl-events")).to_contain_text("Dawn profile applied")
    dialog.get_by_role("button", name="Undo", exact=True).click()
    poll(rig, lambda st: st["video"]["look"] == before)
    expect(dialog.get_by_role("button", name="Undo", exact=True)).to_be_disabled()


def test_crossing_into_a_slot_applies_its_profile_and_a_manual_change_holds_it(app: App, rig: Rig) -> None:
    rig.act("slots.profile", slot="dawn", profile={"lighting": "Warm evening"})
    rig.act("slots.mode", mode="auto")
    rig.act("slots.demo.clock", time="12:00")
    page = app.goto("camera").page
    dialog = open_panel(page)
    rig.act("slots.demo.clock", time="05:30")
    poll(rig, lambda st: st["lighting"]["preset"] == "Warm evening")
    expect(dialog.get_by_role("status")).to_contain_text("dawn profile is in place", timeout=LOADED)
    rig.act("lighting.preset", name="Daylight")  # somebody picks other lights
    poll(rig, lambda st: st["slots"]["status"]["state"] == "held", timeout=30)
    expect(dialog.get_by_role("status")).to_contain_text("You changed the lights", timeout=LOADED)
    dialog.get_by_role("button", name="Hand back to profiles").click()
    poll(rig, lambda st: st["slots"]["status"]["held"] is None)


def test_the_popover_closes_with_escape_and_the_framing_popover_takes_its_place(app: App) -> None:
    page = app.goto("camera").page
    dialog = open_panel(page)
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    open_panel(page)
    page.locator('ss-button[title^="Auto-framing"]').click()
    expect(page.get_by_role("dialog", name="Auto-framing")).to_be_visible()
    expect(page.get_by_role("dialog", name="Time-of-day profiles")).to_be_hidden()


def test_a_slot_profile_can_hold_a_camera_shot_that_apply_now_selects(app: App, rig: Rig) -> None:
    page = app.goto("camera").page
    dialog = open_panel(page)
    dawn = row(dialog, "dawn")
    expect(dawn).to_have_attribute("data-empty", "", timeout=LOADED)
    dawn.get_by_label("Camera shot").select_option("wide")
    poll(rig, lambda st: profile_of(rig, "dawn").get("framing") == {"shot": "wide"})
    dawn.get_by_role("button", name="Apply", exact=True).click()
    poll(rig, lambda st: st["framing"]["shot"] == "wide" and st["framing"]["mode"] != "off")
    expect(dawn.get_by_label("Camera shot")).to_have_value("wide")
