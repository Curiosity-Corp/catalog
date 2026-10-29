"""Echo check: a quiet sweep on one output, the microphone listens, the answer names the output."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from support import LOADED, App, Rig, poll

MONITOR = "alsa_output.demo-monitor.hdmi-stereo"
HEADSET = "bluez_output.demo-headset.1"


def output_section(app: App, page: Page) -> None:
    app.goto("audio")
    page.get_by_role("button", name="Output").click()


def test_a_speaker_that_bleeds_is_named_and_remembered(app: App, page: Page, rig: Rig) -> None:
    output_section(app, page)
    card = page.locator(".ae")
    expect(card).to_be_visible(timeout=LOADED)
    card.get_by_label("Output to check for bleed").select_option(MONITOR)
    card.get_by_role("button", name="Check for bleed").click()
    answer = card.get_by_role("status").filter(has_text="Studio Monitor (demo)")
    expect(answer).to_contain_text("Bleed at", timeout=LOADED)
    expect(answer).to_contain_text("use headphones or lower the volume")
    result = rig.state()["echo"]["result"]
    assert result["sink"] == MONITOR and result["level"] == "strong"
    expect(card.get_by_role("list", name="Echo check results")).to_contain_text("Studio Monitor (demo)")


def test_headphones_do_not_bleed(app: App, page: Page, rig: Rig) -> None:
    output_section(app, page)
    card = page.locator(".ae")
    card.get_by_label("Output to check for bleed").select_option(HEADSET)
    card.get_by_role("button", name="Check for bleed").click()
    expect(card).to_contain_text("No bleed from Evolve2 65 headset (demo)", timeout=LOADED)


def test_a_feedback_loop_is_called_out(app: App, page: Page, rig: Rig) -> None:
    rig.act("echo.demo", coupling_db=-12, loop_gap_ms=150)
    output_section(app, page)
    card = page.locator(".ae")
    card.get_by_role("button", name="Check for bleed").click()
    expect(card).to_contain_text("A feedback loop through", timeout=LOADED)
    assert card.get_attribute("data-tone") == "bad"


def test_it_never_plays_during_a_call(app: App, page: Page, rig: Rig) -> None:
    rig.act("call.demo", app="Teams")
    poll(rig, lambda s: s["call"]["active"], timeout=5)
    output_section(app, page)
    card = page.locator(".ae")
    button = card.get_by_role("button", name="Check for bleed")
    expect(button).to_be_disabled(timeout=LOADED)
    expect(card).to_contain_text("never runs during a call")
    status, _ = rig.request("POST", "api/action", {"action": "echo.check"}, token=rig.token)
    assert status == 409
    assert rig.state()["echo"]["state"] == "idle"


def test_a_muted_microphone_says_why(app: App, page: Page, rig: Rig) -> None:
    rig.act("audio.mute", on=True)
    poll(rig, lambda s: s["audio"]["mic"]["muted"])
    output_section(app, page)
    card = page.locator(".ae")
    expect(card.get_by_role("button", name="Check for bleed")).to_be_disabled(timeout=LOADED)
    expect(card).to_contain_text("microphone is muted")


def test_a_verdict_on_a_degraded_link_names_the_link(app: App, page: Page, rig: Rig) -> None:
    rig.act("network.demo", jitter_ms=150)
    poll(rig, lambda s: s["network"]["rating"] == "degraded", timeout=15)
    output_section(app, page)
    card = page.locator(".ae")
    card.get_by_label("Output to check for bleed").select_option(HEADSET)
    card.get_by_role("button", name="Check for bleed").click()
    expect(card).to_contain_text("No bleed from Evolve2 65 headset (demo)", timeout=LOADED)
    expect(card).to_contain_text("The link is not clean (the network is degraded")
    assert card.get_attribute("data-link") == "caveat"
    expect(card.get_by_role("list", name="Echo check results")).to_contain_text("link not clean")
