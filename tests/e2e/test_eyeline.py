"""Eye-line coach: Camera popover, calibration wizard, reminder and Live badge, on the demo simulation."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from support import LOADED, App, Rig, poll


def open_coach(page: Page):
    page.locator('ss-button[title^="Eye-line coach"]').click()
    dialog = page.get_by_role("dialog", name="Eye-line coach")
    expect(dialog).to_be_visible()
    return dialog


def test_the_demo_rig_starts_with_the_coach_off(rig: Rig) -> None:
    eye = rig.state()["eyeline"]
    assert eye["enabled"] is False and eye["state"] == "off" and eye["available"] is True
    assert eye["calibrated"] is False


def test_the_switch_turns_the_coach_on_and_says_where_it_looks(app: App, rig: Rig) -> None:
    page = app.goto("camera").page
    dialog = open_coach(page)
    switch = dialog.get_by_role("switch", name="Eye-line coach")
    expect(switch).to_have_attribute("aria-checked", "false", timeout=LOADED)
    switch.click()
    poll(rig, lambda st: st["eyeline"]["enabled"] and st["eyeline"]["state"] == "running")
    expect(switch).to_have_attribute("aria-checked", "true")
    expect(dialog.locator(".el-status")).to_contain_text("Watching")
    rig.act("eyeline.demo.gaze", gaze="down")
    expect(dialog.locator(".el-status")).to_contain_text("down at the keyboard", timeout=LOADED)
    switch.click()
    poll(rig, lambda st: not st["eyeline"]["enabled"])
    expect(dialog.locator(".el-status")).to_have_text("Off")


def test_calibrating_at_the_lens_makes_the_angles_exact(app: App, rig: Rig) -> None:
    page = app.goto("camera").page
    dialog = open_coach(page)
    rig.act("eyeline.enable", on=True)
    poll(rig, lambda st: st["eyeline"]["state"] == "running")
    rig.act("eyeline.demo.gaze", gaze="lens")
    dialog.get_by_role("button", name="Calibrate", exact=True).click()
    poll(rig, lambda st: st["eyeline"]["calibrated"])
    expect(dialog.get_by_text("Calibrated: your lens pose is")).to_be_visible(timeout=LOADED)
    clear = dialog.get_by_role("button", name="Clear", exact=True)
    expect(clear).to_be_enabled()
    clear.click()
    poll(rig, lambda st: not st["eyeline"]["calibrated"])
    expect(clear).to_be_disabled()


def test_the_wizard_sizes_the_call_window_distance(app: App, rig: Rig) -> None:
    page = app.goto("camera").page
    dialog = open_coach(page)
    dialog.get_by_text("Camera position").click()
    field = dialog.get_by_label("Viewing distance in centimetres")
    field.fill("100")
    expect(dialog.get_by_text("within 10.5 cm of the lens")).to_be_visible(timeout=LOADED)


def test_the_reminder_switch_persists_and_the_live_badge_shows_a_bad_camera_position(app: App, rig: Rig) -> None:
    page = app.goto("camera").page
    dialog = open_coach(page)
    dialog.get_by_label("Remind me when I look away during a call").check()
    poll(rig, lambda st: st["eyeline"]["reminder"]["on"])
    rig.act("eyeline.enable", on=True)
    poll(rig, lambda st: st["eyeline"]["state"] == "running")
    rig.act("eyeline.demo.gaze", gaze="screen", offset=14)
    poll(rig, lambda st: st["eyeline"]["advice"]["kind"] in ("lower", "raise", "centre"))
    live = app.goto("live").page
    expect(live.locator(".el-badge")).to_contain_text("Camera", timeout=LOADED)
    expect(live.locator(".el-badge")).to_contain_text("off")
