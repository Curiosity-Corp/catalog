"""The System page: health summary, service tiles, tool launchers and call setup copy buttons."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from support import LOADED, App, Rig, hold, poll, quiet

TOOLS = ("OBS Studio", "Easy Effects", "qpwgraph", "pavucontrol", "qv4l2")
TOOL_NAMES = ("obs", "easyeffects", "qpwgraph", "pavucontrol", "qv4l2")


@pytest.fixture
def sysp(app: App) -> App:
    return app.goto("system")


def posted(action: str):
    return lambda r: r.url.endswith("api/action") and f'"{action}"' in (r.request.post_data or "")


def tile(page: Page, name: str):
    return page.get_by_role("group", name=re.compile(rf"^{re.escape(name)}: "))


def test_hero_summarises_health(sysp: App, page: Page, rig: Rig) -> None:
    summary = rig.state()["health"]["summary"]
    assert summary
    expect(page.get_by_role("heading", level=1)).not_to_have_text("Checking…", timeout=LOADED)
    expect(page.get_by_role("heading", level=1)).to_have_text(re.compile(r"\w"))
    expect(page.get_by_text(rig.state()["version"], exact=True).first).to_be_visible()


def test_recheck_button_and_k_key_ask_for_a_new_check(sysp: App, page: Page) -> None:
    with page.expect_response(posted("health.refresh")) as hit:
        page.get_by_role("button", name="Re-check").click()
    assert hit.value.status == 200
    with page.expect_response(posted("health.refresh")) as key:
        sysp.press("k")
    assert key.value.status == 200


def test_video_checks_show_the_active_obs_encoder(sysp: App, page: Page, rig: Rig) -> None:
    encoder = next(c for c in rig.state()["health"]["checks"] if c["id"] == "encoder")
    assert encoder["group"] == "video"
    card = page.get_by_role("region", name="Video", exact=True).locator(".ck").filter(has_text="OBS encodes with")
    expect(card).to_have_count(1, timeout=LOADED)
    expect(card).to_contain_text(encoder["title"])
    expect(card).to_contain_text("av1_ffmpeg_vaapi_tex")


def test_service_tiles_reflect_the_rig(sysp: App, page: Page, rig: Rig) -> None:
    expect(tile(page, "Studio")).to_have_attribute("aria-label", "Studio: Running")
    expect(tile(page, "Voice chain")).to_contain_text("Open Audio")
    expect(tile(page, "AI director")).to_have_attribute("aria-label", "AI director: Off")
    rig.act("auto.on")
    expect(tile(page, "AI director")).to_have_attribute("aria-label", "AI director: On", timeout=LOADED)


@pytest.mark.parametrize(
    ("tile_name", "label", "route"),
    [
        ("Voice chain", "Open Audio", "audio"),
        ("AI director", "Open Auto", "auto"),
    ],
)
def test_tile_buttons_navigate(sysp: App, page: Page, tile_name: str, label: str, route: str) -> None:
    tile(page, tile_name).locator("ss-button").click()
    sysp.wait_ready(route)
    assert page.evaluate("() => location.hash") == f"#/{route}"


def test_camera_bridge_tile_opens_the_camera_page(sysp: App, page: Page) -> None:
    page.locator(".tile[data-id='bridge'] ss-button").click()
    sysp.wait_ready("camera")


def test_studio_stops_only_on_a_hold_and_can_start_again(sysp: App, page: Page, rig: Rig) -> None:
    stop = tile(page, "Studio").locator("ss-button")
    expect(stop).to_contain_text("Stop studio")
    quiet(page)
    stop.click()
    expect(page.get_by_text("Press and hold to confirm")).to_be_visible()
    assert rig.state()["studio"]["running"]
    hold(page, stop, 1400)
    assert poll(rig, lambda s: not s["studio"]["running"] and not s["studio"]["busy"])
    expect(tile(page, "Studio")).to_have_attribute("aria-label", "Studio: Stopped", timeout=LOADED)
    quiet(page)
    start = tile(page, "Studio").locator("ss-button")
    expect(start).to_contain_text("Start studio")
    start.click()
    assert poll(rig, lambda s: s["studio"]["running"] and not s["studio"]["busy"])
    expect(tile(page, "Studio")).to_have_attribute("aria-label", "Studio: Running", timeout=LOADED)


@pytest.mark.parametrize(("title", "name"), list(zip(TOOLS, TOOL_NAMES, strict=True)))
def test_tool_buttons_call_open_app(sysp: App, page: Page, title: str, name: str) -> None:
    button = page.get_by_role("button", name=re.compile(rf"^Open {re.escape(title)}\."))
    with page.expect_response(posted("open.app")) as hit:
        button.click()
    response = hit.value
    assert response.status == 200
    assert response.request.post_data_json["name"] == name
    body = response.json()
    assert body["ok"] is True
    assert "demo: nothing is launched" in body["message"]


def test_call_setup_switches_between_teams_and_zoom(sysp: App, page: Page) -> None:
    seg = sysp.region("Which call app")
    expect(seg.get_by_role("button", name="Teams")).to_have_attribute("aria-pressed", "true")
    teams = page.locator(".steps").inner_text()
    seg.get_by_role("button", name="Zoom").click()
    expect(seg.get_by_role("button", name="Zoom")).to_have_attribute("aria-pressed", "true")
    assert page.locator(".steps").inner_text() != teams


@pytest.mark.parametrize("target", ["StreamStack Camera", "Easy Effects Source"])
def test_call_setup_copy_buttons_copy_the_device_name(sysp: App, page: Page, target: str) -> None:
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    button = page.get_by_role("button", name=f"Copy {target}")
    button.click()
    expect(button).to_contain_text("Copied")
    assert page.evaluate("() => navigator.clipboard.readText()") == target
    expect(button).to_contain_text("Copy", timeout=LOADED)
    expect(button).not_to_contain_text("Copied", timeout=LOADED)
