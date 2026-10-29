"""Pre-flight: one button on the Live page, a score, a reason and a Fix for each finding."""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from support import LOADED, App, Rig, poll


def open_dialog(app: App, page: Page):
    app.goto("live")
    page.get_by_role("button", name="Pre-flight", exact=True).click()
    dialog = page.get_by_role("dialog", name="Pre-flight check")
    expect(dialog).to_be_visible(timeout=LOADED)
    return dialog


def run_quick(page: Page, rig: Rig, dialog) -> None:
    dialog.get_by_role("button", name="Quick check, no voice").click()
    poll(rig, lambda s: s["preflight"]["state"] == "done", timeout=30)


def test_a_healthy_rig_is_ready(app: App, page: Page, rig: Rig) -> None:
    dialog = open_dialog(app, page)
    run_quick(page, rig, dialog)
    expect(dialog.get_by_role("status").filter(has_text="/ 100")).to_have_text("100 / 100", timeout=LOADED)
    expect(dialog.get_by_text("Ready", exact=True)).to_be_visible()
    expect(dialog.get_by_role("button", name=re.compile("^Fix "))).to_have_count(0)


def test_the_voice_prompt_counts_down_then_scores(app: App, page: Page, rig: Rig) -> None:
    dialog = open_dialog(app, page)
    dialog.get_by_role("button", name="Run pre-flight").click()
    expect(dialog.get_by_text("Say your name", exact=False).first).to_be_visible(timeout=LOADED)
    poll(rig, lambda s: s["preflight"]["state"] == "done", timeout=40)
    expect(dialog.get_by_role("status").filter(has_text="/ 100")).to_be_visible(timeout=LOADED)


def test_a_light_that_is_off_offers_a_fix_that_turns_it_on(app: App, page: Page, rig: Rig) -> None:
    rig.act("lighting.power", on=False)
    poll(rig, lambda s: not s["lighting"]["master"]["on"])
    dialog = open_dialog(app, page)
    run_quick(page, rig, dialog)
    row = dialog.get_by_role("listitem").filter(has_text="Lights")
    expect(row).to_contain_text("Check", timeout=LOADED)
    row.get_by_role("button", name=re.compile("^Fix ")).click()
    poll(rig, lambda s: s["lighting"]["master"]["on"])
    run_quick(page, rig, dialog)
    expect(dialog.get_by_role("status").filter(has_text="/ 100")).to_have_text("100 / 100", timeout=LOADED)


def test_a_fault_lowers_its_row_and_clearing_it_restores_the_score(app: App, page: Page, rig: Rig) -> None:
    rig.act("preflight.demo_fault", fault="gpu_busy")
    dialog = open_dialog(app, page)
    run_quick(page, rig, dialog)
    score = int(rig.state()["preflight"]["result"]["score"])
    assert score < 100
    row = dialog.get_by_role("listitem").filter(has_text="Headroom")
    expect(row).to_be_visible(timeout=LOADED)
    expect(dialog.get_by_text("Check a few things", exact=False)).to_be_visible()

    rig.act("preflight.demo_fault", fault="gpu_busy", on=False)
    run_quick(page, rig, dialog)
    expect(dialog.get_by_role("status").filter(has_text="/ 100")).to_have_text("100 / 100", timeout=LOADED)


def test_the_palette_runs_it(app: App, page: Page, rig: Rig) -> None:
    app.goto("live")
    page.keyboard.press("Control+k")
    page.get_by_role("option", name="Run the pre-flight check").click()
    expect(page.get_by_role("dialog", name="Pre-flight check")).to_be_visible(timeout=LOADED)
    poll(rig, lambda s: s["preflight"]["state"] in ("running", "done"), timeout=10)
