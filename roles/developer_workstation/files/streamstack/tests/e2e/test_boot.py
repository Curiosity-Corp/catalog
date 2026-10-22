"""The app boots clean, and the API is token-guarded."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from support import App, Rig


@pytest.mark.cross_browser
def test_loads_without_console_errors_or_csp_violations(app: App, page: Page, problems: list[str]) -> None:
    app.goto("live")
    expect(page).to_have_title("Live · StreamStack")
    expect(page.locator("#conn-chip")).to_have_attribute("label", "Live")
    expect(page.locator("#demo-chip")).to_be_visible()
    # Let the preview stream, meters and lazy page CSS finish: nothing may have complained by then.
    expect(page.locator("ss-preview").first).to_be_visible()
    assert page.evaluate("window.__csp") == [], "CSP violations"
    assert problems == []


@pytest.mark.cross_browser
def test_every_page_loads_without_csp_violations(app: App, page: Page, problems: list[str]) -> None:
    app.goto("live")
    for route in ("camera", "audio", "auto", "system", "mini", "live"):
        app.hash_to(route)
    assert page.evaluate("window.__csp") == []
    assert problems == []


def test_response_headers_lock_the_page_down(rig: Rig, page: Page) -> None:
    response = page.request.get(rig.base)
    assert response.ok
    headers = response.headers
    assert "script-src 'self'" in headers["content-security-policy"]
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["cache-control"] == "no-store"


class TestToken:
    def test_api_requires_the_token(self, rig: Rig) -> None:
        assert rig.request("GET", "api/state")[0] == 401
        assert rig.request("GET", "api/state", token="wrong")[0] == 401
        assert rig.request("POST", "api/action", {"action": "audio.mute", "on": True})[0] == 401
        assert rig.request("GET", "api/state", token=rig.token)[0] == 200

    def test_query_token_works_for_event_stream_urls(self, rig: Rig, page: Page) -> None:
        assert page.request.get(f"{rig.base}api/preview.jpg?source=program&width=160&token={rig.token}").ok
        assert page.request.get(f"{rig.base}api/preview.jpg?source=program&width=160").status == 401

    def test_static_files_need_no_token(self, rig: Rig, page: Page) -> None:
        assert page.request.get(f"{rig.base}static/app.js").ok
        assert page.request.get(rig.base).ok

    def test_page_without_token_shows_no_access(self, rig: Rig, page: Page) -> None:
        page.goto(rig.base)
        expect(page.locator("body")).to_have_attribute("data-conn", "unauthorized")
        expect(page.locator("#banner")).to_contain_text("token")
        expect(page.locator("#conn-chip")).to_have_attribute("label", "No access")

    def test_page_with_wrong_token_shows_no_access(self, rig: Rig, page: Page) -> None:
        page.goto(f"{rig.base}?token=nope")
        expect(page.locator("body")).to_have_attribute("data-conn", "unauthorized")
        expect(page.locator("#banner")).to_be_visible()

    def test_unknown_action_is_rejected(self, rig: Rig) -> None:
        status, body = rig.request("POST", "api/action", {"action": "nope.nothing"}, token=rig.token)
        assert status == 400
        assert body["ok"] is False
