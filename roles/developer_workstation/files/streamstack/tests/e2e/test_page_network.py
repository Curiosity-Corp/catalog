"""Network quality: the System page card, the top-bar chip, one warning, the bandwidth test and its call rule."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from support import LOADED, App, Rig, poll


def card(page: Page):
    return page.locator("section.net")


def test_a_healthy_link_reads_good(app: App, page: Page, rig: Rig) -> None:
    app.goto("system")
    net = card(page)
    expect(net.get_by_role("status").first).to_have_text("Good", timeout=LOADED)
    expect(net).to_contain_text("The connection is good for a call")
    expect(net.get_by_role("list", name="Call services")).to_contain_text("Microsoft Teams")
    expect(net.get_by_role("list", name="Call services")).to_contain_text("Zoom")
    expect(app.top_chips).not_to_contain_text("NETWORK")


def test_150ms_of_jitter_degrades_it_with_a_chip_one_toast_and_a_cause(app: App, page: Page, rig: Rig) -> None:
    app.goto("system")
    expect(card(page).get_by_role("status").first).to_have_text("Good", timeout=LOADED)
    rig.act("network.demo", jitter_ms=150)
    net = card(page)
    expect(net.get_by_role("status").first).to_have_text("Degraded", timeout=LOADED)
    expect(net).to_contain_text("150 ms jitter")
    expect(net).to_contain_text("Likely cause")
    expect(app.top_chips).to_contain_text("NETWORK DEGRADED")
    toast = page.locator("ss-toast .t").filter(has_text="Likely cause")
    expect(toast).to_have_count(1, timeout=LOADED)
    page.wait_for_timeout(1500)
    expect(toast).to_have_count(1)  # not a storm


def test_five_percent_loss_degrades_it(app: App, page: Page, rig: Rig) -> None:
    rig.act("network.demo", loss_pct=5)
    app.goto("system")
    expect(card(page).get_by_role("status").first).to_have_text("Degraded", timeout=LOADED)
    expect(card(page)).to_contain_text("5% loss")


def test_no_route_says_offline(app: App, page: Page, rig: Rig) -> None:
    rig.act("network.demo", route=False)
    app.goto("system")
    net = card(page)
    expect(net.get_by_role("status").first).to_have_text("Offline", timeout=LOADED)
    expect(net).to_contain_text("No route")
    expect(app.top_chips).to_contain_text("NETWORK OFFLINE")


def test_the_bandwidth_test_reports_headroom(app: App, page: Page, rig: Rig) -> None:
    rig.act("network.demo", up_mbps=2.0, down_mbps=80.0)
    app.goto("system")
    net = card(page)
    net.get_by_role("button", name="Test bandwidth").click()
    expect(net).to_contain_text("Not enough for a 1080p call", timeout=LOADED)
    expect(net).to_contain_text("Upload 2.0 Mbit/s")
    assert rig.state()["network"]["bandwidth"]["verdict"] == "short"


def test_the_bandwidth_test_asks_first_during_a_call(app: App, page: Page, rig: Rig) -> None:
    rig.act("call.demo", app="Teams")
    poll(rig, lambda s: s["call"]["active"], timeout=5)
    app.goto("system")
    expect(app.top_chips.get_by_text("IN CALL TEAMS")).to_be_visible(timeout=LOADED)  # the page knows about the call
    card(page).get_by_role("button", name="Test bandwidth").click()
    ask = page.locator("ss-toast .t").filter(has_text="A call is on")
    expect(ask).to_be_visible(timeout=LOADED)
    ask.locator("button.act").click()
    poll(rig, lambda s: s["network"]["bandwidth"]["state"] == "done")


def test_targets_can_be_added_and_removed(app: App, page: Page, rig: Rig) -> None:
    app.goto("system")
    net = card(page)
    net.get_by_text("Targets", exact=True).click()
    net.get_by_label("Target name").fill("Office VPN")
    net.get_by_label("Target host").fill("10.1.2.3")
    net.get_by_label("Target port").fill("443")
    net.get_by_label("Probe kind").select_option("tcp")
    net.get_by_role("button", name="Add", exact=True).click()
    expect(net.get_by_role("list", name="Probe targets")).to_contain_text("Office VPN", timeout=LOADED)
    assert any(t["name"] == "Office VPN" for t in rig.state()["network"]["settings"]["targets"])
    net.get_by_role("button", name="Remove Office VPN").click()
    expect(net.get_by_role("list", name="Probe targets")).not_to_contain_text("Office VPN", timeout=LOADED)
    net.get_by_label("Target host").fill("not a host!")
    net.get_by_label("Target name").fill("Bad")
    net.get_by_label("Target port").fill("1")
    net.get_by_role("button", name="Add", exact=True).click()
    expect(page.locator("ss-toast")).to_contain_text("not a host name", timeout=LOADED)
