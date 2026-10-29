"""The Console is an installable web app: a valid manifest and real icons are served, with no access token."""

from __future__ import annotations

import struct
from urllib.parse import urljoin

from playwright.sync_api import Page

from support import App, Rig

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def manifest_url(page: Page) -> str:
    href = page.locator("link[rel='manifest']").get_attribute("href")
    assert href, "the page links no manifest"
    return urljoin(page.url, href)


def test_the_page_links_a_manifest_and_it_is_valid(app: App, page: Page, rig: Rig) -> None:
    app.goto("live")
    response = page.request.get(manifest_url(page))  # the browser fetches it without the token: so does this
    assert response.ok
    assert response.headers["content-type"].startswith("application/manifest+json")
    manifest = response.json()
    assert manifest["name"] == "StreamStack Console"
    assert manifest["short_name"]
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/" and manifest["scope"] == "/" and manifest["id"] == "/"
    assert manifest["background_color"].startswith("#") and manifest["theme_color"].startswith("#")
    purposes = {(icon["sizes"], icon.get("purpose")) for icon in manifest["icons"]}
    # what a browser needs to offer installing: a 192 and a 512 icon, plus a maskable one for adaptive launchers
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= purposes


def test_every_manifest_icon_is_served_and_is_what_it_says(app: App, page: Page, rig: Rig) -> None:
    app.goto("live")
    url = manifest_url(page)
    manifest = page.request.get(url).json()
    for icon in manifest["icons"]:
        response = page.request.get(urljoin(url, icon["src"]))
        assert response.ok, icon["src"]
        assert response.headers["content-type"] == icon["type"], icon["src"]
        body = response.body()
        if icon["type"] == "image/png":
            assert body.startswith(PNG_SIGNATURE), icon["src"]
            width, height = struct.unpack(">II", body[16:24])
            assert f"{width}x{height}" == icon["sizes"], icon["src"]
        else:
            assert body.lstrip().startswith(b"<svg"), icon["src"]


def test_the_manifest_does_not_break_the_csp(app: App, page: Page, problems: list[str]) -> None:
    app.goto("live")
    assert page.evaluate("window.__csp") == []
    assert problems == []
