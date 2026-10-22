"""The live link: losing the event stream shows the reconnect banner and recovers on its own."""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from playwright.sync_api import Page, expect

from support import LOADED, App, FlakyProxy, Rig


@pytest.fixture
def proxy(rig: Rig) -> Iterator[FlakyProxy]:
    flaky = FlakyProxy(rig)
    try:
        yield flaky
    finally:
        flaky.close()


def open_through(app: App, proxy: FlakyProxy) -> None:
    app.page.goto(f"{proxy.url}#/live")
    app.wait_ready("live")


@pytest.mark.cross_browser
def test_stream_loss_shows_banner_then_recovers_by_itself(app: App, proxy: FlakyProxy, rig: Rig) -> None:
    page = app.page
    open_through(app, proxy)
    body, banner = page.locator("body"), page.locator("#banner")
    expect(banner).not_to_have_attribute("data-show", "")

    proxy.cut()
    expect(body).to_have_attribute("data-conn", "reconnecting", timeout=LOADED)
    expect(banner).to_have_attribute("data-kind", "reconnecting")
    expect(banner).to_contain_text("Reconnecting to StreamStack")
    expect(banner.get_by_role("button", name="Retry now")).to_be_visible()
    expect(page.locator("#conn-chip")).to_have_attribute("label", "Offline")
    expect(page.locator("#main")).to_have_attribute("data-stale", "")

    proxy.heal()
    expect(body).to_have_attribute("data-conn", "online", timeout=30_000)  # backoff tops out at 10 s
    expect(banner).not_to_have_attribute("data-show", "")
    expect(page.locator("#conn-chip")).to_have_attribute("label", "Live")
    expect(page.locator("#main")).not_to_have_attribute("data-stale", "")

    # genuinely live again: a change made on the server reaches the page over the new stream
    rig.act("audio.mute", on=True)
    expect(body).to_have_attribute("data-muted", "", timeout=LOADED)


def await_long_backoff(page: Page) -> None:
    """Wait until the next automatic retry is seconds away, so a Retry now click (not the backoff) reconnects.

    The first retries come after 0.5 s and 1 s: healing the proxy then races the click against the client's own
    reconnect, and the banner can vanish under the pointer.
    """
    expect(page.locator("#banner")).to_contain_text(re.compile(r"next try in ([4-9]|10)s"), timeout=30_000)


def test_retry_now_button_reconnects_immediately(app: App, proxy: FlakyProxy) -> None:
    page = app.page
    open_through(app, proxy)
    proxy.cut()
    expect(page.locator("body")).to_have_attribute("data-conn", "reconnecting", timeout=LOADED)
    await_long_backoff(page)
    proxy.heal()
    page.locator("#banner").get_by_role("button", name="Retry now").click()
    expect(page.locator("body")).to_have_attribute("data-conn", "online", timeout=LOADED)
    expect(page.locator("#banner")).not_to_have_attribute("data-show", "")


def test_controls_are_inert_while_offline(app: App, proxy: FlakyProxy, rig: Rig) -> None:
    page = app.page
    open_through(app, proxy)
    proxy.cut()
    expect(page.locator("#main")).to_have_attribute("data-stale", "", timeout=LOADED)
    expect(page.locator("#page")).to_have_css("pointer-events", "none")
    await_long_backoff(page)
    proxy.heal()
    page.locator("#banner").get_by_role("button", name="Retry now").click()
    expect(page.locator("#page")).not_to_have_css("pointer-events", "none")
