"""The Auto page: AI director switch, ask-now, hold-to-undo and the timeline."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from support import LOADED, App, Rig, hold, poll, quiet


@pytest.fixture
def aut(app: App) -> App:
    return app.goto("auto")


def director(page: Page):
    return page.get_by_role("switch", name=re.compile("^AI director"))


def settled(rig: Rig, running: bool) -> bool:
    return bool(poll(rig, lambda s: s["auto"]["running"] is running and not s["auto"]["busy"]))


def test_director_switch_and_d_key(aut: App, page: Page, rig: Rig) -> None:
    switch = director(page)
    expect(switch).to_have_attribute("aria-checked", "false")
    switch.click()
    assert settled(rig, True)
    expect(switch).to_have_attribute("aria-checked", "true", timeout=LOADED)
    expect(switch).to_contain_text("On")
    aut.press("d")
    assert settled(rig, False)
    expect(switch).to_have_attribute("aria-checked", "false", timeout=LOADED)


def test_server_side_director_change_reaches_the_switch(aut: App, page: Page, rig: Rig) -> None:
    rig.act("auto.on")
    expect(director(page)).to_have_attribute("aria-checked", "true", timeout=LOADED)


def test_ask_now_adds_a_note_to_the_timeline(aut: App, page: Page, rig: Rig) -> None:
    before = rig.state()["auto"]["journal_total"]
    page.get_by_role("button", name="Ask Claude now").click()
    assert poll(rig, lambda s: s["auto"]["busy"] or s["auto"]["journal_total"] > before)
    assert poll(rig, lambda s: s["auto"]["journal_total"] > before and not s["auto"]["busy"])
    timeline = page.get_by_role("region", name="Timeline of changes, newest first")
    expect(timeline).to_contain_text("Looks well exposed", timeout=LOADED)
    expect(page.get_by_text("changes", exact=False).first).to_be_visible()


def test_n_key_asks_now(aut: App, rig: Rig) -> None:
    before = rig.state()["auto"]["journal_total"]
    aut.press("n")
    assert poll(rig, lambda s: s["auto"]["journal_total"] > before and not s["auto"]["busy"])


def test_latest_note_card_shows_the_newest_review(aut: App, page: Page, rig: Rig) -> None:
    rig.act("auto.once")
    poll(rig, lambda s: not s["auto"]["busy"] and "Looks well exposed" in (s["auto"]["last_note"] or ""))
    expect(page.locator("blockquote")).to_contain_text("Looks well exposed", timeout=LOADED)


def test_undo_needs_a_hold(aut: App, page: Page, rig: Rig) -> None:
    rig.act("auto.once")  # the demo can only undo after the director changed something
    poll(rig, lambda s: not s["auto"]["busy"] and s["auto"]["can_undo"])
    undo = page.locator("ss-button", has_text="Undo all changes")
    expect(undo).not_to_have_attribute("disabled", "", timeout=LOADED)
    before = rig.state()["auto"]["journal_total"]
    quiet(page)
    undo.click()  # a tap only explains itself
    expect(page.get_by_text("Press and hold to confirm")).to_be_visible()
    assert rig.state()["auto"]["can_undo"]
    hold(page, undo, 1400)
    assert poll(rig, lambda s: not s["auto"]["can_undo"] and not s["auto"]["busy"])
    assert rig.state()["auto"]["journal_total"] > before
    expect(page.get_by_role("region", name="Timeline of changes, newest first")).to_contain_text(
        "restored the look", timeout=LOADED
    )
    expect(undo).to_have_attribute("disabled", "")


def test_auto_page_has_no_overflow_on_a_laptop(aut: App, page: Page) -> None:
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")


# -- AI Director 2.0: personas, what Claude saw and heard, time of day, reasons -------------------------------------


def persona_card(page: Page, name: str):
    return page.get_by_role("radio", name=re.compile(f"^{name}"))


def test_persona_cards_show_the_shipped_personas_and_choosing_one_sticks(aut: App, page: Page, rig: Rig) -> None:
    group = page.get_by_role("radiogroup", name="Persona")
    for name in ("Executive", "Bright podcast", "Late-night calm", "Daylight natural"):
        expect(persona_card(page, name)).to_be_visible(timeout=LOADED)
    expect(group.get_by_role("radio", checked=True)).to_have_count(1)
    persona_card(page, "Executive").click()
    assert poll(rig, lambda s: s["auto"]["director"]["persona"] == "executive")
    expect(persona_card(page, "Executive")).to_have_attribute("aria-checked", "true", timeout=LOADED)
    expect(persona_card(page, "Daylight natural")).to_have_attribute("aria-checked", "false")


def test_a_persona_can_be_edited_and_a_bad_value_is_explained(aut: App, page: Page, rig: Rig) -> None:
    persona_card(page, "Bright podcast").click()
    page.get_by_role("button", name="Edit this persona").click()
    editor = page.get_by_role("form", name="Edit persona")
    expect(editor).to_be_visible(timeout=LOADED)
    editor.get_by_label("Tagline").fill("Loud and proud")
    editor.get_by_label("Face brightness, from").fill("250")
    editor.get_by_label("Face brightness, to").fill("10")
    editor.get_by_role("button", name="Save changes").click()
    expect(editor.get_by_role("alert")).to_contain_text("face_luma", timeout=LOADED)
    editor.get_by_label("Face brightness, from").fill("120")
    editor.get_by_label("Face brightness, to").fill("170")
    editor.get_by_role("button", name="Save changes").click()
    expect(editor).to_be_hidden(timeout=LOADED)
    expect(persona_card(page, "Bright podcast")).to_contain_text("Loud and proud", timeout=LOADED)
    expect(persona_card(page, "Bright podcast")).to_contain_text("Edited")


def test_time_of_day_strip_says_where_the_day_is(aut: App, page: Page) -> None:
    now = page.get_by_role("status").filter(has_text="Now:")
    expect(now).to_have_text(re.compile(r"^Now: (Night|Dawn|Morning|Afternoon|Evening)"), timeout=LOADED)
    expect(page.get_by_text(re.compile(r"Sunrise \d\d:\d\d, sunset \d\d:\d\d"))).to_be_visible()
    expect(page.get_by_role("img", name=re.compile("^The day in slots"))).to_be_visible()


def test_the_property_location_can_be_set_and_cleared(aut: App, page: Page, rig: Rig) -> None:
    form = page.get_by_role("form", name="Property location")
    form.get_by_label("Property").fill("Lake house")
    form.get_by_label("Latitude").fill("44.1")
    form.get_by_label("Longitude").fill("-79.4")
    form.get_by_role("button", name="Set location").click()
    assert poll(rig, lambda s: s["auto"]["director"]["time"]["location"]["name"] == "Lake house")
    expect(page.get_by_text("for Lake house")).to_be_visible(timeout=LOADED)
    form.get_by_role("button", name="Use time zone").click()
    assert poll(rig, lambda s: s["auto"]["director"]["time"]["location"]["source"] != "settings")


def test_the_contact_sheet_loads_and_the_picture_numbers_are_shown(aut: App, page: Page) -> None:
    sheet = page.get_by_role("img", name=re.compile("^Contact sheet"))
    expect(sheet).to_be_visible(timeout=LOADED)
    assert page.evaluate("(el) => el.complete && el.naturalWidth > 0", sheet.element_handle())
    expect(page.get_by_text("sent to Claude as one picture")).to_be_visible()
    expect(page.get_by_role("img", name="Exposure histogram of the picture")).to_be_visible()
    expect(page.get_by_text("Face brightness", exact=True)).to_be_visible()


def test_the_voice_read_out_shows_bars_against_the_persona(aut: App, page: Page) -> None:
    for name in ("Pitch", "Depth", "Clarity", "Sibilance", "Clean"):
        bar = page.get_by_role("meter", name=name)
        expect(bar).to_be_visible(timeout=LOADED)
        assert 0.0 <= float(bar.get_attribute("aria-valuenow") or "-1") <= 1.0
    expect(page.get_by_text("Only these numbers are shared with Claude", exact=False)).to_be_visible()


def test_privacy_text_is_plain_about_stills_and_audio(aut: App, page: Page) -> None:
    text = page.locator(".privacy")
    expect(text).to_contain_text("The stills go to Claude")
    expect(text).to_contain_text("Your audio never does")


def test_the_timeline_gives_a_reason_for_each_change(aut: App, page: Page) -> None:
    timeline = page.get_by_role("region", name="Timeline of changes, newest first")
    expect(timeline).to_contain_text("Why:", timeout=LOADED)
    expect(timeline).to_contain_text("small step")


def test_findings_and_actuators_are_listed_and_the_hardware_ones_are_ready_on_the_demo_rig(
    aut: App, page: Page
) -> None:
    expect(page.get_by_role("list", name="Findings").get_by_role("listitem").first).to_be_visible(timeout=LOADED)
    acts = page.get_by_role("list", name="Actuators")
    expect(acts).to_contain_text("OBS picture look")
    for name in ("Canon exposure and white balance", "Litra key and fill lights", "Audio interface hardware gain"):
        row = acts.get_by_role("listitem").filter(has_text=name)
        expect(row).to_contain_text("Ready")
        expect(row).not_to_contain_text("Planned")
    expect(acts.get_by_role("listitem").filter(has_text="Face finder")).to_contain_text("Ready")


def test_hardware_changes_appear_in_the_timeline_with_their_reasons(aut: App, page: Page) -> None:
    timeline = page.get_by_role("region", name="Timeline of changes, newest first")
    expect(timeline).to_contain_text("Easing towards Daylight natural's light", timeout=LOADED)
    expect(timeline).to_contain_text("exposure compensation moves first")
    expect(timeline).to_contain_text("Speech was quiet against the room")
    expect(timeline).to_contain_text("Litra Beam 1")
