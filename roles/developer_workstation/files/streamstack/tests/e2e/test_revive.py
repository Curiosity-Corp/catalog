"""The stalled-microphone banner and the global Shift+R revive (``audio.demo_stall`` is the demo's trigger)."""

from __future__ import annotations

import time

from playwright.sync_api import expect

from support import LOADED, SCALE, App, Rig, poll


def test_shift_r_revives_a_stalled_mic_from_any_page(app: App, rig: Rig) -> None:
    rig.act("audio.demo_stall", on=True)
    assert poll(rig, lambda s: s["audio"]["mic"]["stalled"])
    app.goto("live")
    banner = app.page.locator(".revive[data-state='stalled']")
    expect(banner.first).to_be_visible(timeout=LOADED)
    expect(banner.first).to_contain_text("Microphone stalled")
    app.hash_to("camera")  # a page without the banner: the shortcut is global
    app.press("Shift+R")
    # The demo's "reviving" lasts 1.5 s and the state is only advanced when read: a starved runner can see it already
    # "ok". The end state (revived, no longer stalled) is what proves the shortcut posted.
    assert poll(
        rig,
        lambda s: s["audio"]["mic"]["revive"]["state"] == "ok" and not s["audio"]["mic"]["stalled"],
        timeout=20 * SCALE,
    )
    app.hash_to("live")
    expect(app.page.locator(".revive[data-state='stalled']")).to_have_count(0)


def test_shift_r_does_nothing_while_the_mic_is_healthy(app: App, rig: Rig) -> None:
    app.goto("live")
    app.press("Shift+R")
    # Nothing is expected to happen, so there is no post to wait for: watch the state for longer than a demo revive
    # takes to begin. ("ok" may linger from the previous test and ends on its own clock, so only "reviving" counts.)
    watch_until = time.monotonic() + 2.0
    while time.monotonic() < watch_until:
        assert rig.state()["audio"]["mic"]["revive"]["state"] != "reviving"
        time.sleep(0.1)
