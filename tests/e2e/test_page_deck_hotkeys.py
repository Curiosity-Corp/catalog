"""The Hotkeys card on the Deck page: bind a shortcut or a MIDI control to an option, press it, see the status."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from support import App, Rig, poll, tap


@pytest.fixture
def hk(app: App) -> App:
    return app.goto("deck")


def bind_key(page: Page, combo: str, label: str = "Mute") -> None:
    page.get_by_label("Shortcut keys").press(combo.replace("ctrl+alt+", "Control+Alt+"))
    page.get_by_label("What the trigger does").select_option(label=label)
    tap(page, page.get_by_role("button", name="Add", exact=True))


def row(page: Page, text: str):
    return page.locator(".hk-row", has_text=text)


def test_the_card_says_where_keys_and_midi_stand(hk: App) -> None:
    page = hk.page
    expect(page.locator(".hk")).to_be_visible()
    expect(page.locator("[data-ref='keysLine']")).to_have_attribute("data-ok", "true")
    expect(page.locator("[data-ref='midiLine']")).to_contain_text("Demo pad")
    expect(page.locator("[data-ref='empty']")).to_be_visible()


def test_a_keyboard_shortcut_is_added_and_runs_its_option(hk: App, rig: Rig) -> None:
    page = hk.page
    bind_key(page, "ctrl+alt+m")
    expect(row(page, "Ctrl+Alt+M")).to_be_visible()
    expect(row(page, "Ctrl+Alt+M").locator(".hk-badge")).to_have_attribute("data-status", "ok")
    rig.act("hotkeys.demo.key", combo="ctrl+alt+m")
    assert poll(rig, lambda s: s["audio"]["mic"]["muted"])
    expect(row(page, "Ctrl+Alt+M").locator(".hk-last")).not_to_be_empty()
    page.wait_for_timeout(400)  # a repeat inside 0.3 s is a held key, and runs once
    rig.act("hotkeys.demo.key", combo="ctrl+alt+m")
    assert poll(rig, lambda s: not s["audio"]["mic"]["muted"])


def test_the_test_button_runs_the_binding_without_the_keys(hk: App, rig: Rig) -> None:
    page = hk.page
    bind_key(page, "ctrl+alt+m")
    tap(page, row(page, "Ctrl+Alt+M").get_by_role("button", name="Test"))
    assert poll(rig, lambda s: s["audio"]["mic"]["muted"])


def test_a_combo_another_program_holds_is_marked(hk: App, rig: Rig) -> None:
    page = hk.page
    rig.act("hotkeys.demo.conflict", combo="ctrl+alt+m", on=True)
    bind_key(page, "ctrl+alt+m")
    expect(row(page, "Ctrl+Alt+M").locator(".hk-badge")).to_have_attribute("data-status", "conflict")
    rig.act("hotkeys.demo.key", combo="ctrl+alt+m")
    assert not rig.state()["audio"]["mic"]["muted"]


def test_learning_a_midi_control_fills_the_form(hk: App, rig: Rig) -> None:
    page = hk.page
    page.get_by_label("Trigger type").select_option("note")
    tap(page, page.get_by_role("button", name="Learn"))
    assert poll(rig, lambda s: s["hotkeys"]["learn"]["active"])
    rig.act("hotkeys.demo.midi", type="note", channel=2, number=64, value=100)
    expect(page.get_by_label("MIDI number")).to_have_value("64")
    expect(page.get_by_label("MIDI channel (0 for any)")).to_have_value("2")
    page.get_by_label("What the trigger does").select_option(label="Mute")
    tap(page, page.get_by_role("button", name="Add", exact=True))
    expect(page.locator(".hk-row")).to_have_count(1)
    rig.act("hotkeys.demo.midi", type="note", channel=2, number=64, value=100)
    assert poll(rig, lambda s: s["audio"]["mic"]["muted"])


def test_switching_hotkeys_off_stops_them(hk: App, rig: Rig) -> None:
    page = hk.page
    bind_key(page, "ctrl+alt+m")
    expect(row(page, "Ctrl+Alt+M")).to_be_visible()
    tap(page, page.locator("[data-ref='enabled']"))
    assert poll(rig, lambda s: s["hotkeys"]["enabled"] is False)
    expect(row(page, "Ctrl+Alt+M").locator(".hk-badge")).to_have_attribute("data-status", "off")
    rig.act("hotkeys.demo.key", combo="ctrl+alt+m")
    assert not rig.state()["audio"]["mic"]["muted"]


def test_a_binding_is_removed(hk: App, rig: Rig) -> None:
    page = hk.page
    bind_key(page, "ctrl+alt+m")
    tap(page, row(page, "Ctrl+Alt+M").get_by_role("button", name="Remove"))
    assert poll(rig, lambda s: s["hotkeys"]["bindings"] == [])
    expect(page.locator("[data-ref='empty']")).to_be_visible()
