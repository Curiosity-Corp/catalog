"""The Audio page: channel strip, tone knobs, A/B, voice presets and the mic check with both recordings."""

from __future__ import annotations

import re
from urllib.parse import urljoin

import pytest
from playwright.sync_api import Page, expect

from support import LOADED, App, Rig, poll

TONES = (("Bass", "bass_db"), ("Presence", "presence_db"), ("Treble", "treble_db"), ("Pitch", "pitch_semitones"))


@pytest.fixture
def au(app: App) -> App:
    return app.goto("audio")


def test_gain_fader_drag_keys_and_reset(au: App, rig: Rig) -> None:
    gain = au.slider("Gain")
    au.drag_fader(gain, 0.8)
    value = poll(rig, lambda s: s["audio"]["mic"]["gain_db"] > 1 and s["audio"]["mic"]["gain_db"])
    expect(gain).to_have_attribute("aria-valuenow", re.compile(r"^\d"))
    gain.focus()
    gain.press("ArrowDown")
    assert poll(rig, lambda s: s["audio"]["mic"]["gain_db"] < value)
    gain.dblclick()
    assert poll(rig, lambda s: s["audio"]["mic"]["gain_db"] == 0)


def test_server_side_gain_reaches_the_fader(au: App, rig: Rig) -> None:
    rig.act("audio.gain", db=-6)
    expect(au.slider("Gain")).to_have_attribute("aria-valuenow", "-6", timeout=LOADED)


def test_mute_pad_and_m_key(au: App, rig: Rig) -> None:
    pad = au.mute_button
    expect(pad).to_contain_text("Mute mic")
    pad.click()
    assert poll(rig, lambda s: s["audio"]["mic"]["muted"])
    expect(au.mute_button).to_contain_text("MUTED")
    au.press("m")
    assert poll(rig, lambda s: not s["audio"]["mic"]["muted"])
    expect(au.mute_button).to_contain_text("Mute mic")


@pytest.mark.parametrize(("label", "key"), TONES)
def test_tone_knob_keys_reach_the_voice_chain(au: App, rig: Rig, label: str, key: str) -> None:
    knob = au.slider(label)
    before = rig.state()["audio"]["tone"][key]
    knob.focus()
    knob.press("ArrowUp")
    assert poll(rig, lambda s: s["audio"]["tone"][key] == pytest.approx(before + 0.5))
    assert poll(rig, lambda s: s["audio"]["tone"]["custom"])
    expect(au.page.get_by_text("Custom", exact=True)).to_be_visible()


def test_tone_reset_to_preset(au: App, page: Page, rig: Rig) -> None:
    base = rig.state()["audio"]["tone"]["bass_db"]
    # set up through the API: a knob key streams its value twice, and a reset racing the repeat is not what this tests
    rig.act("audio.tone", bass_db=base + 5)
    expect(au.slider("Bass")).to_have_attribute("aria-valuenow", str(base + 5).removesuffix(".0"), timeout=LOADED)
    expect(page.get_by_text("Custom", exact=True)).to_be_visible()
    page.get_by_role("button", name="Reset to preset").click()
    assert poll(rig, lambda s: not s["audio"]["tone"]["custom"] and s["audio"]["tone"]["bass_db"] == base)
    expect(page.get_by_text("Custom", exact=True)).to_be_hidden()
    expect(au.slider("Bass")).to_have_attribute("aria-valuenow", str(base).removesuffix(".0"), timeout=LOADED)


def test_ab_compare_buttons_and_b_key(au: App, rig: Rig) -> None:
    ab = au.region("Effects A/B compare")
    raw, processed = ab.get_by_role("button", name=re.compile("^B")), ab.get_by_role("button", name=re.compile("^A"))
    expect(processed).to_have_attribute("aria-pressed", "true")
    raw.click()
    assert poll(rig, lambda s: s["audio"]["chain"]["bypass"])
    expect(raw).to_have_attribute("aria-pressed", "true")
    expect(processed).to_have_attribute("aria-pressed", "false")
    processed.click()
    assert poll(rig, lambda s: not s["audio"]["chain"]["bypass"])
    au.press("b")
    assert poll(rig, lambda s: s["audio"]["chain"]["bypass"])
    expect(raw).to_have_attribute("aria-pressed", "true")


def test_preset_cards_load_a_preset(au: App, rig: Rig) -> None:
    listing = rig.state()["audio"]["chain"]["presets"]
    current = rig.state()["audio"]["chain"]["preset"]
    other = next(p for p in listing if p["name"] != current)
    card = preset_card(au, other["label"])
    expect(card).to_have_attribute("aria-pressed", "false")
    card.click()
    assert poll(rig, lambda s: s["audio"]["chain"]["preset"] == other["name"])
    expect(card).to_have_attribute("aria-pressed", "true")
    expect(preset_card(au, next(p["label"] for p in listing if p["name"] == current))).to_have_attribute(
        "aria-pressed", "false"
    )
    assert other["blurb"]
    expect(au.region("Voice presets").get_by_role("button", name=re.compile(re.escape(other["blurb"])))).to_be_visible()


def preset_card(au: App, label: str):
    """A voice preset card: named "<label>. <blurb>" (Voice and Voice Warm share a prefix)."""
    return au.region("Voice presets").get_by_role("button", name=re.compile(rf"^{re.escape(label)}\. "))


def wait_clip_loaded(page: Page, label: str) -> str:
    audio = page.locator(f'audio[aria-label="{label}"]')
    src = audio.get_attribute("src")
    assert src and src.startswith("api/clips/"), src
    page.wait_for_function(
        "(a) => a.readyState >= 1 && a.duration > 0 && !a.error", arg=audio.element_handle(), timeout=LOADED
    )
    return src


def test_mic_check_runs_to_done_and_both_clips_load(au: App, page: Page, rig: Rig) -> None:
    rig.act("audio.mic_check", seconds=2)  # two takes of 2 s; the UI follows
    record = page.locator("button.rec")
    expect(record).to_have_attribute("aria-pressed", "true")
    expect(page.get_by_text("Step 1 of 2")).to_be_visible()
    assert poll(rig, lambda s: s["audio"]["mic_check"]["state"] == "recording-processed", timeout=15)
    expect(page.get_by_text("Step 2 of 2")).to_be_visible()
    assert poll(rig, lambda s: s["audio"]["mic_check"]["state"] == "done", timeout=20)
    expect(page.get_by_text("Done. Switch between A and B")).to_be_visible(timeout=LOADED)
    expect(record).to_contain_text("Again")
    raw_src = wait_clip_loaded(page, "Raw microphone recording")
    # the processed clip is hidden until chosen, but must be loaded and servable already
    proc_src = page.locator('audio[aria-label="Processed voice recording"]').get_attribute("src")
    assert proc_src and proc_src != raw_src
    for src in (raw_src, proc_src):
        res = page.request.get(urljoin(page.url, src))
        assert res.status == 200, src
        assert res.headers["content-type"].startswith("audio/"), res.headers
        assert res.body()[:4] == b"RIFF"
    # and an unauthenticated fetch is refused
    assert page.request.get(urljoin(page.url, re.sub(r"[?&]token=[^&]*", "", src))).status in (401, 403)


def test_mic_check_ab_switch_shows_the_processed_clip(au: App, page: Page, rig: Rig) -> None:
    rig.act("audio.mic_check", seconds=2)
    poll(rig, lambda s: s["audio"]["mic_check"]["state"] == "done", timeout=25)
    seg = au.region("Which recording to hear")
    raw = page.locator('audio[aria-label="Raw microphone recording"]')
    processed = page.locator('audio[aria-label="Processed voice recording"]')
    expect(raw).to_be_visible(timeout=LOADED)
    expect(processed).to_be_hidden()
    seg.get_by_role("button", name=re.compile("Processed")).click()
    expect(processed).to_be_visible()
    expect(raw).to_be_hidden()
    wait_clip_loaded(page, "Processed voice recording")
    au.press("a")
    expect(raw).to_be_visible()
    expect(seg.get_by_role("button", name=re.compile("Raw"))).to_have_attribute("aria-pressed", "true")


def test_mic_check_again_from_the_button_and_r_key(au: App, page: Page, rig: Rig) -> None:
    rig.act("audio.mic_check", seconds=2)
    poll(rig, lambda s: s["audio"]["mic_check"]["state"] == "done", timeout=25)
    record = page.locator("button.rec")
    expect(record).to_contain_text("Again", timeout=LOADED)
    record.click()
    assert poll(rig, lambda s: s["audio"]["mic_check"]["state"] == "recording-raw")
    expect(record).to_have_attribute("aria-pressed", "true")
    assert poll(rig, lambda s: s["audio"]["mic_check"]["state"] == "done", timeout=25)
    au.press("r")
    assert poll(rig, lambda s: s["audio"]["mic_check"]["state"] == "recording-raw")
    assert poll(rig, lambda s: s["audio"]["mic_check"]["state"] == "done", timeout=25)
