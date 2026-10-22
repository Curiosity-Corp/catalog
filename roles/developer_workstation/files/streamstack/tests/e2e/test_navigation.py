"""The shell: every route renders, the rail and Alt+n move between pages."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from support import ROUTES, App

RAIL = {"live": "Live", "camera": "Camera", "audio": "Audio", "auto": "Auto", "system": "System", "mini": "Mini"}


@pytest.mark.parametrize("route", ROUTES)
def test_route_renders(app: App, page: Page, problems: list[str], route: str) -> None:
    app.goto(route)
    expect(page.locator("body")).to_have_attribute("data-page", route)
    expect(page.locator("#page")).not_to_contain_text("Could not open")
    assert problems == []


def test_unknown_route_falls_back_to_live(app: App, page: Page) -> None:
    app.goto("live")
    page.evaluate("() => { location.hash = '#/does-not-exist'; }")
    expect(page.locator("body")).to_have_attribute("data-page", "live")


def test_rail_lists_the_pages_and_marks_the_current_one(app: App, page: Page) -> None:
    app.goto("live")
    rail = page.get_by_role("navigation", name="Pages")
    for title in ("Live", "Camera", "Audio", "Auto", "System", "Mini"):
        expect(rail.get_by_role("link", name=title)).to_be_visible()
    expect(rail.get_by_role("link", name="Live")).to_have_attribute("aria-current", "page")


@pytest.mark.parametrize("route", ["camera", "audio", "auto", "system"])
def test_rail_click_navigates(app: App, page: Page, route: str) -> None:
    app.goto("live")
    rail = page.get_by_role("navigation", name="Pages")
    rail.get_by_role("link", name=RAIL[route]).click()
    app.wait_ready(route)
    expect(rail.get_by_role("link", name=RAIL[route])).to_have_attribute("aria-current", "page")
    expect(rail.get_by_role("link", name="Live")).not_to_have_attribute("aria-current", "page")


@pytest.mark.parametrize(
    ("digit", "route"), [("1", "live"), ("2", "camera"), ("3", "audio"), ("4", "auto"), ("5", "system")]
)
def test_alt_digit_goes_to_page(app: App, page: Page, digit: str, route: str) -> None:
    app.goto("system" if route == "live" else "live")
    app.press(f"Alt+{digit}")
    app.wait_ready(route)
    expect(page.locator("body")).to_have_attribute("data-page", route)


def test_mini_has_no_app_chrome_and_links_back(app: App, page: Page) -> None:
    app.goto("mini")
    expect(page.locator("#rail")).to_be_hidden()
    expect(page.locator("#topbar")).to_be_hidden()
    page.get_by_role("link", name="Open the full console").click()
    app.wait_ready("live")
    expect(page.locator("#rail")).to_be_visible()


def test_live_layout_survives_a_tour_of_every_page(app: App, page: Page) -> None:
    # Page stylesheets stay loaded; a class name shared with another page (audio's .presets) once
    # restyled the Live voice presets after a visit and squashed the microphone strip.
    app.goto("live")
    presets = page.get_by_role("group", name="Voice presets")
    mute = app.mute_button

    def geometry() -> tuple[int, int, float]:
        tops = presets.get_by_role("button").evaluate_all(
            "(els) => new Set(els.map((e) => Math.round(e.getBoundingClientRect().top))).size"
        )
        box = mute.bounding_box()
        assert box is not None
        return tops, len(presets.get_by_role("button").all()), round(box["height"])

    before = geometry()
    assert before[0] == 1, "voice presets should sit in one row"
    for route in ("camera", "audio", "auto", "system", "live"):
        app.hash_to(route)
    after = geometry()
    assert after[0] == 1, "voice presets wrapped after visiting the other pages"
    assert abs(after[2] - before[2]) <= 2, f"mute pad changed height: {before[2]} -> {after[2]}"
