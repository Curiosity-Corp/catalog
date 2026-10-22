"""The Cam Link's HDMI mode: the chip on the Camera page and the System health checks."""

from __future__ import annotations

from playwright.sync_api import expect

from support import LOADED, App, Rig

MODE = "1080p59.94 YUYV"


def test_the_demo_rig_reports_the_best_hdmi_mode(app: App, rig: Rig) -> None:
    link = rig.state()["studio"]["camlink"]
    assert link["present"] and link["signal"] and link["mode"] == MODE
    assert link["offered"][0] == MODE
    assert link["audio"]


def test_camera_page_shows_the_hdmi_mode_on_the_cam_link_picture(app: App) -> None:
    cam = app.goto("camera")
    sources = cam.region("Camera to edit")
    sources.get_by_role("button", name="Cam Link 4K").click()
    chips = cam.page.locator(".badges")
    expect(chips.get_by_text("HDMI")).to_be_visible(timeout=LOADED)
    expect(chips.get_by_text(MODE)).to_be_visible()


def test_the_hdmi_chip_belongs_to_the_card_not_the_canon(app: App, rig: Rig) -> None:
    cam = app.goto("camera")
    sources = cam.region("Camera to edit")
    sources.get_by_role("button", name="Cam Link 4K").click()
    expect(cam.page.locator(".badges").get_by_text("HDMI")).to_be_visible(timeout=LOADED)
    sources.get_by_role("button", name="Canon EOS 50D").click()
    expect(cam.page.locator(".badges").get_by_text("HDMI")).to_have_count(0)


def test_system_health_names_the_mode_and_the_hdmi_audio(app: App) -> None:
    sysp = app.goto("system")
    expect(sysp.page.get_by_text(f"Elgato Cam Link on /dev/video0: {MODE}")).to_be_visible(timeout=LOADED)
    expect(sysp.page.get_by_text("Cam Link HDMI audio is the camera's sound, not a call microphone")).to_be_visible()
