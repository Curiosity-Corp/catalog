"""The Deck page: a live picture of the Stream Deck, editing the profile, playing the keys, the simulated device."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Locator, Page, expect

from support import LOADED, App, Rig, poll, quiet, tap


@pytest.fixture
def dk(app: App) -> App:
    return app.goto("deck")


def cap(page: Page, index: int) -> Locator:
    return page.locator(f".dk-cap[data-key='{index}']")


def option(page: Page, label: str) -> Locator:
    return page.locator(".dk-item", has=page.locator(".dk-item-t", has_text=re.compile(f"^{re.escape(label)}$")))


def main_keys(rig: Rig) -> list[dict[str, Any]]:
    deck = rig.state()["deck"]
    return next(p for p in deck["profile"]["pages"] if p["id"] == "main")["keys"]


def test_a_4x4_deck_with_the_default_keys_and_a_connected_chip(dk: App) -> None:
    page = dk.page
    expect(page).to_have_title(re.compile("^Deck"))
    expect(page.locator(".dk-cap")).to_have_count(16)
    expect(page.locator(".dk ss-status-chip").first).to_have_attribute("label", "Virtual deck")
    expect(cap(page, 0)).to_have_attribute("aria-label", re.compile(r"^Key 1: \S"))
    expect(page.locator(".dk-tab")).to_have_count(5)
    if dk.rig.state()["deck"]["render"]:  # without Pillow the page says so, and draws the keys itself
        expect(page.locator(".dk-notice")).to_be_hidden()


def test_the_options_palette_lists_actions_and_filters(dk: App) -> None:
    page = dk.page
    expect(page.locator(".dk-group")).not_to_have_count(0)
    expect(option(page, "Mute")).to_be_visible()
    page.get_by_role("searchbox", name="Find an option").fill("blur")
    expect(page.locator(".dk-item")).to_have_count(4)
    page.get_by_role("searchbox", name="Find an option").fill("zzz-nothing")
    expect(page.locator(".dk-item")).to_have_count(0)


def test_play_mode_presses_do_what_the_real_key_does(dk: App, rig: Rig) -> None:
    page = dk.page
    tap(page, page.get_by_role("button", name="Play", exact=True))
    mute = next(i for i, k in enumerate(main_keys(rig)) if k["raw"]["action"] == "audio.mute")
    expect(cap(page, mute)).to_have_attribute("data-lit", "off")
    tap(page, cap(page, mute))
    assert poll(rig, lambda s: s["audio"]["mic"]["muted"])
    expect(cap(page, mute)).to_have_attribute("data-lit", "on")
    tap(page, cap(page, mute))
    assert poll(rig, lambda s: not s["audio"]["mic"]["muted"])


def test_play_mode_folders_and_back(dk: App, rig: Rig) -> None:
    page = dk.page
    tap(page, page.get_by_role("button", name="Play", exact=True))
    folder = next(i for i, k in enumerate(main_keys(rig)) if k["raw"]["action"] == "deck.page")
    tap(page, cap(page, folder))
    assert poll(rig, lambda s: s["deck"]["surface"]["page"]["id"] != "main")
    expect(page.locator(".dk-playing strong")).not_to_have_text("Live")
    tap(page, page.locator(".dk-play-row").get_by_role("button", name="Back"))
    assert poll(rig, lambda s: s["deck"]["surface"]["page"]["id"] == "main")


def test_clicking_an_option_puts_it_on_the_selected_key(dk: App, rig: Rig) -> None:
    page = dk.page
    tap(page, page.locator(".dk-tab").nth(4))  # the last page has room
    # state-read: the page layout; switching tab posts nothing
    last = rig.state()["deck"]["profile"]["pages"][4]
    free = next(i for i, k in enumerate(last["keys"]) if k["empty"])
    tap(page, cap(page, free))
    tap(page, option(page, "Mute"))
    assert poll(rig, lambda s: s["deck"]["profile"]["pages"][4]["keys"][free]["raw"]["action"] == "audio.mute")
    assert rig.state()["deck"]["profile"]["pristine"] is False
    expect(page.locator(".dk-insp")).to_be_visible()


def test_dragging_an_option_onto_a_key_assigns_it(dk: App, rig: Rig) -> None:
    page = dk.page
    tap(page, page.locator(".dk-tab").nth(4))
    # state-read: the page layout; switching tab posts nothing
    last = rig.state()["deck"]["profile"]["pages"][4]
    free = next(i for i, k in enumerate(last["keys"]) if k["empty"])
    option(page, "Privacy").first.drag_to(cap(page, free))
    assert poll(rig, lambda s: s["deck"]["profile"]["pages"][4]["keys"][free]["raw"]["action"] == "video.privacy")


def test_dragging_a_key_onto_another_swaps_them_and_onto_the_trash_clears_it(dk: App, rig: Rig) -> None:
    page = dk.page
    before = main_keys(rig)
    cap(page, 0).drag_to(cap(page, 1))
    assert poll(rig, lambda s: main_keys(rig)[0]["raw"] == before[1]["raw"])
    assert main_keys(rig)[1]["raw"] == before[0]["raw"]
    cap(page, 1).hover()  # the trash only appears once a key is being dragged
    page.mouse.down()
    cap(page, 2).hover()
    page.locator(".dk-trash").hover()
    page.mouse.up()
    assert poll(rig, lambda s: main_keys(rig)[1]["empty"])


def test_the_inspector_edits_label_icon_and_the_action(dk: App, rig: Rig) -> None:
    page = dk.page
    tap(page, cap(page, 0))
    label = page.get_by_role("textbox", name="Key label")
    label.fill("Shh")
    label.press("Enter")
    assert poll(rig, lambda s: main_keys(rig)[0]["label"] == "Shh")
    expect(cap(page, 0)).to_have_attribute("aria-label", re.compile(r"^Key 1: Shh"))
    tap(page, page.get_by_role("option", name="volume", exact=True))
    assert poll(rig, lambda s: main_keys(rig)[0]["icon"] == "volume")
    page.get_by_text("Action and parameters").click()
    page.get_by_role("textbox", name="Console action").fill("video.scene")
    page.get_by_role("textbox", name="Action parameters as JSON").fill('{"name": "Canon"}')
    tap(page, page.get_by_role("button", name="Apply", exact=True))
    assert poll(rig, lambda s: main_keys(rig)[0]["raw"]["action"] == "video.scene")
    page.get_by_role("textbox", name="Action parameters as JSON").fill("{nope")
    tap(page, page.get_by_role("button", name="Apply", exact=True))
    expect(page.locator("[data-ref='advNote']")).to_contain_text("not valid JSON")
    tap(page, page.get_by_role("button", name="Clear key"))
    assert poll(rig, lambda s: main_keys(rig)[0]["empty"])


def test_pages_can_be_added_renamed_and_deleted(dk: App, rig: Rig) -> None:
    page = dk.page
    tap(page, page.get_by_role("button", name="Page", exact=True))
    expect(page.locator(".dk-tab")).to_have_count(6)
    name = page.get_by_role("textbox", name="Page name")
    name.fill("Show")
    name.press("Enter")
    assert poll(rig, lambda s: s["deck"]["profile"]["pages"][-1]["name"] == "Show")
    tap(page, page.get_by_role("button", name="Delete page"))
    assert poll(rig, lambda s: len(s["deck"]["profile"]["pages"]) == 5)


def test_brightness_and_sleep_are_kept(dk: App, rig: Rig) -> None:
    page = dk.page
    tap(page, page.get_by_role("button", name="30 min"))
    assert poll(rig, lambda s: s["deck"]["profile"]["settings"]["sleep_minutes"] == 30)
    fader = page.get_by_role("slider", name="Brightness")
    dk.drag_fader(fader, 0.3)
    assert poll(rig, lambda s: s["deck"]["profile"]["settings"]["brightness"] <= 40)
    rig.wait_for_state("deck.profile.pristine", True)  # settings alone do not fork the default


def test_export_downloads_the_profile_and_import_restores_it(dk: App, rig: Rig, tmp_path: Path) -> None:
    page = dk.page
    tap(page, cap(page, 0))
    label = page.get_by_role("textbox", name="Key label")
    label.fill("Exported")
    label.press("Enter")
    assert poll(rig, lambda s: main_keys(rig)[0]["label"] == "Exported")
    with page.expect_download() as info:
        tap(page, page.get_by_role("button", name="Export profile"))
    saved = tmp_path / "deck.json"
    info.value.save_as(saved)
    exported = json.loads(saved.read_text())
    assert exported["pages"][0]["keys"][0]["label"] == "Exported"
    tap(page, page.get_by_role("button", name="Reset", exact=True))  # a hold control: a click only hints
    rig.act("deck.reset")
    assert poll(rig, lambda s: s["deck"]["profile"]["pristine"])
    page.locator("[data-ref='importFile']").set_input_files(saved)
    assert poll(rig, lambda s: main_keys(rig)[0].get("label") == "Exported")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    page.locator("[data-ref='importFile']").set_input_files(bad)
    expect(page.locator("[data-ref='ioNote']")).to_contain_text("not valid JSON")


def test_a_profile_for_another_deck_is_flagged_and_can_be_laid_out_again(dk: App, rig: Rig) -> None:
    page = dk.page
    tap(page, cap(page, 0))
    tap(page, page.get_by_role("button", name="Clear key"))  # saves a 4x4 profile
    assert poll(rig, lambda s: not s["deck"]["profile"]["pristine"])
    rig.act("deck.attach", model="xl")
    expect(page.locator(".dk-notice")).to_contain_text("re-flowed")
    expect(page.locator(".dk ss-status-chip").first).to_have_attribute("label", "Stream Deck XL")
    tap(page, page.get_by_role("button", name="Use 8×4 layout"))
    expect(page.locator(".dk-cap")).to_have_count(32)
    expect(page.locator(".dk-notice")).not_to_contain_text("re-flowed")


def test_with_no_deck_the_page_edits_offline_and_offers_a_model_picker(dk: App, rig: Rig) -> None:
    page = dk.page
    rig.act("deck.detach")
    expect(page.locator(".dk ss-status-chip").first).to_have_attribute("label", "No deck connected")
    expect(page.locator(".dk-notice")).to_contain_text("No Stream Deck is connected")
    models = page.get_by_role("group", name="Deck model")
    expect(models).to_be_visible()
    tap(page, models.get_by_role("button", name=re.compile("^Mini")))
    expect(page.locator(".dk-cap")).to_have_count(6)
    tap(page, models.get_by_role("button", name=re.compile("^4×4")))
    expect(page.locator(".dk-cap")).to_have_count(16)


def test_arrow_keys_move_between_keys_and_delete_clears(dk: App, rig: Rig) -> None:
    page = dk.page
    tap(page, cap(page, 5))
    cap(page, 5).press("ArrowRight")
    expect(cap(page, 6)).to_have_attribute("aria-pressed", "true")
    cap(page, 6).press("ArrowDown")
    expect(cap(page, 10)).to_have_attribute("aria-pressed", "true")
    cap(page, 10).press("Delete")
    assert poll(rig, lambda s: main_keys(rig)[10]["empty"])


def test_alt_9_opens_the_deck_and_the_page_is_quiet(app: App, page: Page, problems: list[str]) -> None:
    app.goto("live")
    quiet(page)
    page.keyboard.press("Alt+9")
    app.wait_ready("deck")
    expect(page.locator(".dk-cap")).to_have_count(16, timeout=LOADED)
    assert page.evaluate("window.__csp") == []
    assert problems == []
