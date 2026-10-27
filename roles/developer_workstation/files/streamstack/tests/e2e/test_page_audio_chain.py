"""The Audio page's Chain section: stage faders, switches, presets, A/B, loudness and the meters, on the demo rig."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from support import LOADED, App, Rig, poll

STAGES = ("High-pass", "Noise gate", "Neural denoise", "De-esser", "EQ", "Compressor", "Makeup gain", "Limiter")


@pytest.fixture
def ch(app: App) -> App:
    app.goto("audio")
    app.page.get_by_role("button", name="Chain").click()
    expect(app.page.get_by_role("group", name="Chain stages")).to_be_visible(timeout=LOADED)
    return app


def stage(ch: App, name: str):
    return ch.page.get_by_role("group", name="Chain stages").get_by_role("group", name=name, exact=True)


def chain_param(rig: Rig, key: str):
    for st in rig.state()["audio"]["chain"]["stages"]:
        for p in st["params"]:
            if p["key"] == key:
                return p["value"]
    raise AssertionError(key)


def test_every_stage_is_shown_in_signal_order(ch: App) -> None:
    names = ch.page.get_by_role("group", name="Chain stages").locator(".st-name").all_inner_texts()
    assert names == list(STAGES)


def test_a_stage_fader_reaches_the_chain(ch: App, rig: Rig) -> None:
    ratio = stage(ch, "Compressor").get_by_role("slider", name="Ratio")
    before = chain_param(rig, "comp_ratio")
    ratio.focus()
    ratio.press("ArrowUp")
    assert poll(rig, lambda s: chain_param(rig, "comp_ratio") == pytest.approx(before + 0.5))
    assert poll(rig, lambda s: s["audio"]["chain"]["custom"])
    expect(ch.page.get_by_role("region", name="Chain").get_by_text("Custom", exact=True)).to_be_visible()


def test_a_server_side_change_reaches_the_fader(ch: App, rig: Rig) -> None:
    rig.act("audio.chain", params={"limiter_ceiling_db": -3})
    expect(stage(ch, "Limiter").get_by_role("slider", name="Ceiling")).to_have_attribute(
        "aria-valuenow", "-3", timeout=LOADED
    )


def test_a_stage_switch_turns_the_stage_off_and_on(ch: App, rig: Rig) -> None:
    gate = stage(ch, "Noise gate")
    switch = gate.get_by_role("switch")
    expect(switch).to_have_attribute("aria-checked", "true")
    switch.click()
    assert poll(rig, lambda s: next(x for x in s["audio"]["chain"]["stages"] if x["id"] == "gate")["on"] is False)
    expect(gate).to_have_attribute("data-on", "false")
    expect(gate.get_by_role("slider", name="Threshold")).to_be_disabled()
    switch.click()
    assert poll(rig, lambda s: next(x for x in s["audio"]["chain"]["stages"] if x["id"] == "gate")["on"] is True)


def test_the_denoise_stage_chooses_the_engine(ch: App, rig: Rig) -> None:
    pad = stage(ch, "Neural denoise").get_by_role("group", name="Neural denoise engine")
    rnnoise = pad.get_by_role("button", name=re.compile("^RNNoise"))
    rnnoise.click()
    assert poll(rig, lambda s: s["audio"]["denoise"]["engine"] == "rnnoise")
    expect(rnnoise).to_have_attribute("aria-pressed", "true")


def test_preset_recall_and_reset(ch: App, rig: Rig) -> None:
    listing = rig.state()["audio"]["chain"]["presets"]
    current = rig.state()["audio"]["chain"]["preset"]
    other = next(p for p in listing if p["name"] != current and not p["unavailable"])
    pads = ch.page.get_by_role("group", name="Chain presets")
    pads.get_by_role("button", name=other["label"].removeprefix("Voice ").strip() or "Voice", exact=True).click()
    assert poll(rig, lambda s: s["audio"]["chain"]["preset"] == other["name"])
    rig.act("audio.chain", params={"comp_ratio": 8})
    expect(ch.page.get_by_role("button", name="Reset to preset")).to_be_enabled(timeout=LOADED)
    ch.page.get_by_role("button", name="Reset to preset").click()
    assert poll(rig, lambda s: not s["audio"]["chain"]["custom"])


def test_save_the_current_settings_as_a_preset(ch: App, page: Page, rig: Rig) -> None:
    rig.act("audio.chain", params={"comp_ratio": 7})
    page.get_by_role("textbox", name="Preset name").fill("E2E take")
    page.get_by_role("button", name="Save", exact=True).click()
    assert poll(rig, lambda s: any(p["name"] == "E2E take" for p in s["audio"]["chain"]["presets"]))
    expect(page.get_by_role("group", name="Chain presets").get_by_role("button", name="E2E take")).to_be_visible(
        timeout=LOADED
    )


def test_ab_bypass_follows_the_server(ch: App, rig: Rig) -> None:
    ab = ch.page.get_by_role("group", name="Chain A/B compare")
    raw = ab.get_by_role("button", name=re.compile("^B"))
    raw.click()
    assert poll(rig, lambda s: s["audio"]["chain"]["bypass"])
    expect(raw).to_have_attribute("aria-pressed", "true")
    rig.act("audio.bypass", on=False)
    expect(raw).to_have_attribute("aria-pressed", "false", timeout=LOADED)


def test_loudness_leveller_switch_and_targets(ch: App, rig: Rig) -> None:
    ch.switch("Auto level").click()
    assert poll(rig, lambda s: s["audio"]["chain"]["loudness"]["on"])
    targets = ch.page.get_by_role("group", name="Loudness target")
    targets.get_by_role("button", name="-14 LUFS").click()
    assert poll(rig, lambda s: s["audio"]["chain"]["loudness"]["target_lufs"] == -14.0)
    expect(targets.get_by_role("button", name="-14 LUFS")).to_have_attribute("aria-pressed", "true")
    expect(targets.get_by_role("button", name="-16 LUFS")).to_have_attribute("aria-pressed", "false")


def test_loudness_state_reaches_the_card(ch: App, rig: Rig) -> None:
    rig.act("audio.loudness", on=True)
    states = {"riding", "holding", "silence", "muted", "no-signal", "at-limit", "capped"}
    assert poll(rig, lambda s: s["audio"]["chain"]["loudness"]["state"] in states)
    expect(ch.page.locator(".ch-loud")).not_to_have_attribute("data-state", "off", timeout=LOADED)


def test_the_gain_reduction_estimate_follows_the_voice_meter(ch: App, page: Page, rig: Rig) -> None:
    gr = page.get_by_role("meter", name="Gain reduction, estimated")
    expect(gr).to_be_attached()
    rig.act("audio.chain", params={"comp_threshold_db": -50, "comp_ratio": 10})
    # the demo voice speaks in phrases: within a phrase a low threshold makes the estimate leave zero
    expect(gr).not_to_have_attribute("aria-valuenow", "0.0", timeout=20_000)
