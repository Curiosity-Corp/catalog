"""Phone layout, light theme and accessibility smoke."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from support import ROUTES, App

PHONE = {"width": 390, "height": 844}


@pytest.mark.parametrize("route", ["live", "camera", "audio", "auto", "system", "mini"])
def test_phone_layout_has_no_horizontal_scroll(app: App, page: Page, route: str) -> None:
    page.set_viewport_size(PHONE)
    app.goto(route)
    overflow = page.evaluate(
        "() => ({sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth,"
        " bw: document.body.scrollWidth})"
    )
    assert overflow["sw"] <= overflow["cw"], overflow
    assert overflow["bw"] <= overflow["cw"], overflow


def test_phone_shows_a_bottom_tab_bar(app: App, page: Page) -> None:
    page.set_viewport_size(PHONE)
    app.goto("live")
    rail = page.get_by_role("navigation", name="Pages")
    expect(rail).to_be_visible()
    box = rail.bounding_box()
    assert box is not None
    assert box["y"] + box["height"] == pytest.approx(PHONE["height"], abs=1), box
    assert box["width"] == pytest.approx(PHONE["width"], abs=1), box
    for title in ("Live", "Camera", "Audio", "Auto", "System"):
        expect(rail.get_by_role("link", name=title)).to_be_visible()
    rail.get_by_role("link", name="Audio").click()
    app.wait_ready("audio")


def test_phone_controls_still_work(app: App, page: Page) -> None:
    page.set_viewport_size(PHONE)
    app.goto("live")
    app.mute_button.scroll_into_view_if_needed()
    app.mute_button.click()
    expect(page.locator("body")).to_have_attribute("data-muted", "")


def test_desktop_has_a_side_rail_and_no_scroll_on_live(app: App, page: Page) -> None:
    app.goto("live")
    rail = page.get_by_role("navigation", name="Pages").bounding_box()
    assert rail is not None
    assert rail["x"] == 0
    assert rail["height"] > rail["width"], rail
    sizes = page.evaluate("() => [document.documentElement.scrollHeight, document.documentElement.clientHeight]")
    assert sizes[0] <= sizes[1], f"Live scrolls at 1280x720: {sizes}"


@pytest.mark.cross_browser
def test_light_theme_toggle(app: App, page: Page) -> None:
    app.goto("live")
    html = page.locator("html")
    expect(html).to_have_attribute("data-theme", "dark")
    dark_bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    page.get_by_role("button", name="Switch to light theme").click()
    expect(html).to_have_attribute("data-theme", "light")
    expect(page.get_by_role("button", name="Switch to dark theme")).to_be_visible()
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") != dark_bg
    assert page.evaluate("localStorage.getItem('streamstack.console.theme')") == "light"
    page.reload()
    app.wait_ready("live")
    expect(html).to_have_attribute("data-theme", "light")
    page.get_by_role("button", name="Switch to dark theme").click()
    expect(html).to_have_attribute("data-theme", "dark")


# ---------------------------------------------------------------------------
# Accessibility smoke
# ---------------------------------------------------------------------------


def test_live_controls_have_roles_and_names(app: App, page: Page) -> None:
    app.goto("live")
    for name in ("Gain", "Brightness", "Warmth", "Contrast", "Saturation"):
        slider = page.get_by_role("slider", name=name)
        expect(slider.first).to_be_visible()
        for attr in ("aria-valuenow", "aria-valuemin", "aria-valuemax"):
            expect(slider.first).to_have_attribute(attr, re.compile(r"-?\d"))
    expect(app.mute_button).to_be_visible()
    expect(app.privacy_button).to_be_visible()
    expect(page.get_by_role("switch", name="Bypass")).to_be_visible()
    expect(page.get_by_role("switch", name="Director")).to_be_visible()
    expect(page.get_by_role("button", name="Open command palette")).to_be_visible()
    expect(page.get_by_role("button", name="Keyboard shortcuts")).to_be_visible()
    expect(page.get_by_role("meter", name="Mic level")).to_be_visible()
    expect(page.get_by_role("group", name="Scenes")).to_be_visible()
    expect(page.get_by_role("group", name="Voice presets")).to_be_visible()
    expect(page.get_by_role("navigation", name="Pages")).to_be_visible()
    expect(page.get_by_role("navigation", name="Status")).to_be_visible()


UNNAMED = re.compile(r"^\s*- (?:button|slider|switch|link|combobox|textbox|checkbox)(?:\s*\[[^\]]*\])*:?\s*$")


def test_every_interactive_control_has_an_accessible_name(app: App, page: Page) -> None:
    """The browser's own accessibility tree (shadow DOM included) must name every control on every page."""
    app.goto("live")
    for route in ROUTES:
        app.hash_to(route)
        tree = page.locator("body").aria_snapshot()
        unnamed = [line.strip() for line in tree.splitlines() if UNNAMED.match(line)]
        assert unnamed == [], f"{route}: controls without an accessible name: {unnamed}"


FOCUSED_NAME = """() => {
  const el = document.activeElement;
  return el ? (el.getAttribute('aria-label') || el.getAttribute('label') || el.id || el.tagName) : '';
}"""


def test_keyboard_reaches_the_controls(app: App, page: Page) -> None:
    """Tab walks through the page and lands on the faders and switches, not just on links."""
    app.goto("live")
    seen: set[str] = set()
    for _ in range(40):
        page.keyboard.press("Tab")
        seen.add(page.evaluate(FOCUSED_NAME))
    assert {"Gain", "Brightness", "Warmth", "Contrast", "Saturation"} <= seen, seen
