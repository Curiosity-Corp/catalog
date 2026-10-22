"""The stalled-microphone banner and the global Shift+R revive (``audio.demo_stall`` is the demo's trigger)."""

from __future__ import annotations

from playwright.sync_api import expect

from support import LOADED, App, Rig, poll


def test_shift_r_revives_a_stalled_mic_from_any_page(app: App, rig: Rig) -> None:
    rig.act("audio.demo_stall", on=True)
    assert poll(rig, lambda s: s["audio"]["mic"]["stalled"])
    app.goto("live")
    banner = app.page.locator(".revive[data-state='stalled']")
    expect(banner.first).to_be_visible(timeout=LOADED)
    expect(banner.first).to_contain_text("Microphone stalled")
    app.hash_to("camera")  # a page without the banner: the shortcut is global
    app.press("Shift+R")
    assert poll(rig, lambda s: s["audio"]["mic"]["revive"]["state"] == "reviving")
    assert poll(rig, lambda s: s["audio"]["mic"]["revive"]["state"] == "ok", timeout=20)
    app.hash_to("live")
    expect(app.page.locator(".revive[data-state='stalled']")).to_have_count(0)


def test_shift_r_does_nothing_while_the_mic_is_healthy(app: App, rig: Rig) -> None:
    app.goto("live")
    before = rig.state()["audio"]["mic"]["revive"]["state"]  # may still be "ok" from the previous test
    app.press("Shift+R")
    assert rig.state()["audio"]["mic"]["revive"]["state"] == before
    assert rig.state()["audio"]["mic"]["revive"]["state"] != "reviving"
