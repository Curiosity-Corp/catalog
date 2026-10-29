"""The Bandwidth card on the Stream page: simulated loss steps the stream down, a clean link brings it back (#98)."""

from __future__ import annotations

import pytest
from playwright.sync_api import Locator, expect

from support import LOADED, SCALE, App, Rig, poll, tap


@pytest.fixture
def st(app: App, rig: Rig) -> App:
    rig.act("presets.platform", id="youtube")
    rig.act("presets.tier", id="balanced")
    return app.goto("stream")


def card(st: App) -> Locator:
    return st.page.locator(".st-adapt")


def adapt(rig: Rig) -> dict:
    return rig.state()["stream"]["adapt"]


def test_choosing_a_platform_and_tier_gives_the_stream_a_ladder(st: App, rig: Rig) -> None:
    state = poll(rig, lambda s: s["stream"]["adapt"]["available"] and s["stream"])
    assert state["adapt"]["ladder"] == {"platform": "youtube", "tier": "balanced"}
    assert state["adapt"]["step"] == 0 and len(state["adapt"]["steps"]) >= 4
    expect(card(st)).to_be_visible(timeout=LOADED)
    expect(card(st).locator(".st-ladder li")).to_have_count(len(state["adapt"]["steps"]))
    expect(card(st).locator(".st-ladder li[data-state='current']")).to_contain_text("your plan")
    expect(card(st).locator("[data-ref='line']")).to_contain_text("Ready")


def test_simulated_loss_steps_the_stream_down_and_a_clean_link_brings_it_back(st: App, rig: Rig) -> None:
    page = st.page
    poll(rig, lambda s: s["stream"]["adapt"]["available"] and s)
    chosen = rig.state()["stream"]["settings"]
    tap(page, page.get_by_role("button", name="Go live"))
    poll(rig, lambda s: s["stream"]["status"] == "live" and s)
    expect(card(st).locator("[data-ref='line']")).to_contain_text("The link looks fine", timeout=LOADED)

    tap(page, page.get_by_role("button", name="Simulate 12% packet loss"))
    down = poll(rig, lambda s: s["stream"]["adapt"]["step"] >= 1 and s["stream"], timeout=30 * SCALE)
    assert (
        down["adapt"]["history"][-1]["direction"] == "down" and "packet loss" in down["adapt"]["history"][-1]["reason"]
    )
    assert down["settings"] == chosen, "what the person chose is untouched"
    assert down["effective"]["bitrate_kbps"] < chosen["bitrate_kbps"]
    assert down["output"]["height"] < 1080
    expect(card(st).locator("[data-ref='line']")).to_contain_text("Sending", timeout=LOADED)
    expect(card(st).locator(".st-ladder li[data-state='current']")).not_to_contain_text("your plan")
    expect(card(st).locator(".st-adapt-hist li").first).to_be_visible()
    assert any("struggling" in e["text"] for e in down["events"])

    tap(page, page.get_by_role("button", name="Uplink is clean again"))
    up = poll(rig, lambda s: s["stream"]["adapt"]["step"] == 0 and s["stream"], timeout=90 * SCALE)
    assert up["effective"] == up["settings"] and up["output"]["height"] == 1080
    assert up["adapt"]["history"][-1]["direction"] == "up"
    expect(card(st).locator("[data-ref='line']")).to_contain_text("your plan", timeout=LOADED)
    expect(card(st).locator(".st-ladder li[data-state='current']")).to_contain_text("your plan")


def test_turning_the_automation_off_sends_the_chosen_plan_whatever_the_link_does(st: App, rig: Rig) -> None:
    page = st.page
    poll(rig, lambda s: s["stream"]["adapt"]["available"] and s)
    tap(page, page.get_by_role("button", name="Go live"))
    poll(rig, lambda s: s["stream"]["status"] == "live" and s)
    tap(page, page.get_by_role("button", name="Simulate 12% packet loss"))
    poll(rig, lambda s: s["stream"]["adapt"]["step"] >= 1 and s, timeout=30 * SCALE)
    card(st).locator("ss-toggle").click()
    state = poll(rig, lambda s: not s["stream"]["adapt"]["enabled"] and s["stream"])
    assert state["adapt"]["step"] == 0 and state["effective"] == state["settings"]
    expect(card(st).locator("[data-ref='line']")).to_contain_text("Off", timeout=LOADED)
    page.wait_for_timeout(int(3000 * SCALE))
    assert adapt(rig)["step"] == 0, "the loss is still there but nothing is judging it"
    card(st).locator("ss-toggle").click()
    poll(rig, lambda s: s["stream"]["adapt"]["enabled"] and s)
    poll(rig, lambda s: s["stream"]["adapt"]["step"] >= 1 and s, timeout=30 * SCALE)


def test_a_hand_made_picture_change_forgets_the_ladder(st: App, rig: Rig) -> None:
    poll(rig, lambda s: s["stream"]["adapt"]["available"] and s)
    rig.act("stream.set", bitrate_kbps=2222)
    poll(rig, lambda s: not s["stream"]["adapt"]["available"] and s)
    expect(card(st).locator("[data-ref='line']")).to_contain_text("Not set up", timeout=LOADED)
    expect(card(st).locator(".st-ladder li")).to_have_count(0)
