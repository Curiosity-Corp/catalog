"""One screenshot per page at 1280x720, saved to the artifacts dir (CI keeps them for 14 days)."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page

from support import ROUTES, App


@pytest.mark.parametrize("route", ROUTES)
def test_screenshot(app: App, page: Page, artifacts: Path, route: str) -> None:
    app.goto(route)
    if route == "live":
        app.wait_picture()
    page.evaluate("() => document.fonts.ready")
    target = artifacts / f"{route}-1280x720.png"
    page.screenshot(path=str(target))
    assert target.stat().st_size > 5_000


def test_screenshot_states(app: App, page: Page, artifacts: Path, rig) -> None:
    """The Live page in the states a streamer cares about: muted, privacy, light theme, phone."""
    app.goto("live")
    app.wait_picture()
    app.press("m")
    page.locator("body[data-muted]").wait_for()
    page.screenshot(path=str(artifacts / "live-muted-1280x720.png"))
    app.press("m")
    app.press("p")
    page.locator("body[data-privacy]").wait_for()
    page.screenshot(path=str(artifacts / "live-privacy-1280x720.png"))
    app.press("p")
    page.set_viewport_size({"width": 390, "height": 844})
    page.screenshot(path=str(artifacts / "live-phone-390x844.png"))
