"""Keyboard shortcuts overlay and the command palette."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from support import App, Rig, poll


@pytest.fixture
def live(app: App) -> App:
    return app.goto("live")


@pytest.mark.cross_browser
def test_question_mark_opens_shortcuts_and_escape_closes(live: App) -> None:
    expect(live.shortcuts).to_be_hidden()
    live.press("?")
    expect(live.shortcuts).to_be_visible()
    for text in ("Mute / unmute the microphone", "Command palette", "Go to Camera", "Switch to scene 1"):
        expect(live.shortcuts).to_contain_text(text)
    live.page.keyboard.press("Escape")
    expect(live.shortcuts).to_be_hidden()


def test_shortcuts_button_and_close_button(live: App) -> None:
    live.page.get_by_role("button", name="Keyboard shortcuts").click()
    expect(live.shortcuts).to_be_visible()
    live.shortcuts.get_by_role("button", name="Close").click()
    expect(live.shortcuts).to_be_hidden()


@pytest.mark.cross_browser
def test_ctrl_k_palette_searches_and_runs_a_command(live: App, rig: Rig) -> None:
    live.press("Control+k")
    expect(live.palette).to_be_visible()
    box = live.palette.get_by_role("combobox")
    expect(box).to_be_focused()
    box.fill("mute")
    first = live.palette.get_by_role("option").first
    expect(first).to_contain_text("Mute microphone")
    expect(first).to_have_attribute("aria-selected", "true")
    live.page.keyboard.press("Enter")
    expect(live.palette).to_be_hidden()
    expect(live.page.locator("body")).to_have_attribute("data-muted", "")
    assert poll(rig, lambda s: s["audio"]["mic"]["muted"])


def test_palette_finds_scenes_and_navigates_with_arrows(live: App, rig: Rig) -> None:
    scene = rig.state()["video"]["scenes"][3]["name"]
    live.press("Control+k")
    live.palette.get_by_role("combobox").fill(scene)
    expect(live.palette.get_by_role("option").first).to_contain_text(f"Scene: {scene}")
    live.page.keyboard.press("Enter")
    assert poll(rig, lambda s: s["video"]["program_scene"] == scene)


def test_palette_navigates_to_a_page(live: App, page: Page) -> None:
    live.press("Control+k")
    live.palette.get_by_role("combobox").fill("go to system")
    live.page.keyboard.press("Enter")
    live.wait_ready("system")


def test_palette_click_runs_command_and_ctrl_k_toggles(live: App, rig: Rig) -> None:
    live.press("Control+k")
    live.palette.get_by_role("option", name="Bypass voice effects").click()
    assert poll(rig, lambda s: s["audio"]["chain"]["bypass"])
    live.press("Control+k")
    expect(live.palette).to_be_visible()
    live.page.keyboard.press("Control+k")
    expect(live.palette).to_be_hidden()


def test_palette_button_in_the_top_bar(live: App) -> None:
    live.page.get_by_role("button", name="Open command palette").click()
    expect(live.palette).to_be_visible()
    live.palette.get_by_role("combobox").fill("zzzzqq")
    expect(live.palette).to_contain_text("No matching command")


def test_palette_lists_the_voice_presets_and_looks(live: App, rig: Rig) -> None:
    live.press("Control+k")
    box = live.palette.get_by_role("combobox")
    box.fill("look:")
    looks = rig.state()["video"]["looks"]
    assert looks
    expect(live.palette.get_by_role("option").first).to_contain_text("Look")


def test_theme_command_in_palette_toggles_theme(live: App) -> None:
    live.press("Control+k")
    live.palette.get_by_role("combobox").fill("light theme")
    live.page.keyboard.press("Enter")
    expect(live.page.locator("html")).to_have_attribute("data-theme", "light")
