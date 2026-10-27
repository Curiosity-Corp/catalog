"""The Live cockpit: scenes, mute, privacy, faders, presets, auto director, meters. State is asserted on the server."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from support import LOADED, App, Rig, poll


@pytest.fixture
def live(app: App) -> App:
    return app.goto("live")


@pytest.fixture
def scenes(rig: Rig) -> list[str]:
    names = [s["name"] for s in rig.state()["video"]["scenes"]]
    assert len(names) >= 4, names
    return names


# ---------------------------------------------------------------------------
# Scenes
# ---------------------------------------------------------------------------


def test_scene_pad_click_switches_program(live: App, rig: Rig, scenes: list[str]) -> None:
    target = scenes[2]
    pad = live.scene_pad(target)
    expect(pad).to_have_attribute("aria-pressed", "false")
    pad.click()
    expect(pad).to_have_attribute("aria-pressed", "true")
    expect(pad).to_have_accessible_name(re.compile("on air"))
    expect(live.scene_pad(scenes[0])).to_have_attribute("aria-pressed", "false")
    assert poll(rig, lambda s: s["video"]["program_scene"] == target)
    assert [s["name"] for s in rig.state()["video"]["scenes"] if s["active"]] == [target]
    # the tally lives on the pad element itself
    assert live.page.locator(f"ss-pad[tally='program'][label='{target}']").count() == 1


@pytest.mark.parametrize("digit", ["1", "2", "3", "4"])
def test_digit_keys_switch_scenes(live: App, rig: Rig, scenes: list[str], digit: str) -> None:
    # start from a different scene than the one the key selects, so the test proves a change
    other = scenes[(int(digit)) % len(scenes)]
    rig.act("video.scene", name=other)
    expect(live.scene_pad(other)).to_have_attribute("aria-pressed", "true")
    live.press(digit)
    want = scenes[int(digit) - 1]
    expect(live.scene_pad(want)).to_have_attribute("aria-pressed", "true")
    assert poll(rig, lambda s: s["video"]["program_scene"] == want)


def test_server_side_scene_change_reaches_the_page(live: App, rig: Rig, scenes: list[str]) -> None:
    rig.act("video.scene", name=scenes[3])
    expect(live.scene_pad(scenes[3])).to_have_attribute("aria-pressed", "true", timeout=LOADED)


# ---------------------------------------------------------------------------
# Mute, privacy, bypass
# ---------------------------------------------------------------------------


def test_m_toggles_mute_with_red_edge_and_label(live: App, page: Page, rig: Rig) -> None:
    body = page.locator("body")
    expect(body).not_to_have_attribute("data-muted", "")
    live.press("m")
    expect(body).to_have_attribute("data-muted", "")
    expect(live.top_chips.get_by_text("MUTED")).to_be_visible()
    expect(live.mute_button).to_have_accessible_name(re.compile(r"^MUTED"))
    # the red edge is a real painted box-shadow, not just an attribute
    shadow = page.evaluate("getComputedStyle(document.body, '::after').boxShadow")
    assert shadow != "none"
    assert poll(rig, lambda s: s["audio"]["mic"]["muted"])
    live.press("m")
    expect(body).not_to_have_attribute("data-muted", "")
    expect(live.top_chips.get_by_text("MUTED")).to_have_count(0)
    assert poll(rig, lambda s: not s["audio"]["mic"]["muted"])


def test_mute_pad_click(live: App, rig: Rig) -> None:
    live.mute_button.click()
    expect(live.mute_button).to_have_attribute("aria-pressed", "true")
    assert poll(rig, lambda s: s["audio"]["mic"]["muted"])
    live.mute_button.click()
    assert poll(rig, lambda s: not s["audio"]["mic"]["muted"])


def test_p_toggles_privacy(live: App, page: Page, rig: Rig) -> None:
    live.press("p")
    expect(page.locator("body")).to_have_attribute("data-privacy", "")
    expect(live.top_chips.get_by_text("PRIVACY")).to_be_visible()
    expect(live.privacy_button).to_have_attribute("aria-pressed", "true")
    state = poll(rig, lambda s: s["video"]["privacy"] and s)
    assert state["video"]["program_scene"] == state["video"]["privacy_scene"]
    live.press("p")
    expect(page.locator("body")).not_to_have_attribute("data-privacy", "")
    state = poll(rig, lambda s: not s["video"]["privacy"] and s)
    assert state["video"]["program_scene"] != state["video"]["privacy_scene"]


def test_privacy_returns_to_the_scene_it_cut_from(live: App, rig: Rig, scenes: list[str]) -> None:
    live.scene_pad(scenes[1]).click()
    assert poll(rig, lambda s: s["video"]["program_scene"] == scenes[1])
    live.privacy_button.click()
    assert poll(rig, lambda s: s["video"]["privacy"])
    live.privacy_button.click()
    assert poll(rig, lambda s: not s["video"]["privacy"] and s["video"]["program_scene"] == scenes[1])


def test_b_toggles_bypass(live: App, rig: Rig) -> None:
    bypass = live.switch("Bypass")
    expect(bypass).to_have_attribute("aria-checked", "false")
    live.press("b")
    expect(bypass).to_have_attribute("aria-checked", "true")
    expect(live.page.get_by_text("Effects bypassed")).to_be_visible()
    assert poll(rig, lambda s: s["audio"]["chain"]["bypass"])
    live.press("b")
    expect(bypass).to_have_attribute("aria-checked", "false")
    assert poll(rig, lambda s: not s["audio"]["chain"]["bypass"])


def test_bypass_switch_click(live: App, rig: Rig) -> None:
    live.switch("Bypass").click()
    assert poll(rig, lambda s: s["audio"]["chain"]["bypass"])


def test_g_toggles_the_thirds_grid(live: App, page: Page) -> None:
    grid = page.get_by_role("button", name=re.compile("Grid"))
    expect(page.locator("ss-preview")).not_to_have_attribute("grid", "")
    live.press("g")
    expect(page.locator("ss-preview")).to_have_attribute("grid", "")
    expect(grid).to_have_attribute("aria-pressed", "true")


# ---------------------------------------------------------------------------
# Faders
# ---------------------------------------------------------------------------


def test_mic_fader_drag_changes_gain(live: App, rig: Rig) -> None:
    gain = live.slider("Gain").first
    assert rig.state()["audio"]["mic"]["gain_db"] == 0
    live.drag_fader(gain, 0.9)
    seen = poll(rig, lambda s: s["audio"]["mic"]["gain_db"] > 3 and s["audio"]["mic"]["gain_db"])
    # state-read: the fader's range, which no action changes
    assert seen <= rig.state()["audio"]["gain_range"][1]
    # the control and the server agree once the drag settled: `seen` may be a value from mid-drag, and the last
    # throttled post (the value the control ends on) can still be on its way, so wait for it rather than sampling once
    expect(gain).to_have_attribute("aria-valuenow", re.compile(r"^\d"))
    shown = float(gain.get_attribute("aria-valuenow"))
    rig.wait_for_state("audio.mic.gain_db", shown, tol=0.51)


def test_mic_fader_arrow_keys_change_gain(live: App, rig: Rig) -> None:
    gain = live.slider("Gain").first
    gain.focus()
    for _ in range(4):
        gain.press("ArrowUp")
    assert poll(rig, lambda s: s["audio"]["mic"]["gain_db"] == 2.0)  # 4 steps of 0.5 dB
    gain.press("ArrowDown")
    assert poll(rig, lambda s: s["audio"]["mic"]["gain_db"] == 1.5)
    gain.press("Home")
    assert poll(rig, lambda s: s["audio"]["mic"]["gain_db"] == s["audio"]["gain_range"][0])
    gain.press("Backspace")  # reset to the default
    assert poll(rig, lambda s: s["audio"]["mic"]["gain_db"] == 0)


def test_mic_fader_double_click_resets(live: App, rig: Rig) -> None:
    rig.act("audio.gain", db=6)
    gain = live.slider("Gain").first
    expect(gain).to_have_attribute("aria-valuenow", "6", timeout=LOADED)
    gain.dblclick()
    assert poll(rig, lambda s: s["audio"]["mic"]["gain_db"] == 0)


def test_gain_follows_the_server(live: App, rig: Rig) -> None:
    rig.act("audio.gain", db=-9.5)
    expect(live.slider("Gain").first).to_have_attribute("aria-valuenow", "-9.5", timeout=LOADED)


def test_look_fader_drag_changes_video_look(live: App, rig: Rig) -> None:
    brightness = live.slider("Brightness")
    assert rig.state()["video"]["look"]["brightness"] == 0
    live.drag_fader(brightness, 0.85)
    value = poll(rig, lambda s: s["video"]["look"]["brightness"] > 0.1 and s["video"]["look"]["brightness"])
    # state-read: the fader's limits, which no action changes
    lo, hi = rig.state()["video"]["limits"]["brightness"]
    assert lo <= value <= hi


@pytest.mark.parametrize("label", ["Brightness", "Warmth", "Contrast", "Saturation"])
def test_look_fader_keys_change_each_look_value(live: App, rig: Rig, label: str) -> None:
    fader = live.slider(label)
    fader.focus()
    fader.press("PageUp")
    key = label.lower()
    assert poll(rig, lambda s: s["video"]["look"][key] > 0)


def test_reset_look_returns_to_neutral(live: App, rig: Rig) -> None:
    rig.act("video.look", values={"brightness": 0.2, "warmth": -0.3})
    expect(live.slider("Warmth")).not_to_have_attribute("aria-valuenow", "0", timeout=LOADED)
    live.page.get_by_role("button", name="Reset look").click()
    assert poll(rig, lambda s: all(s["video"]["look"][k] == 0 for k in ("brightness", "warmth")))


# ---------------------------------------------------------------------------
# Presets, auto, voice
# ---------------------------------------------------------------------------


def test_preset_pad_loads_preset(live: App, rig: Rig) -> None:
    presets = rig.state()["audio"]["chain"]["presets"]
    current = rig.state()["audio"]["chain"]["preset"]
    target = next(p for p in presets if p["name"] != current)
    label = re.sub(r"^Voice (?=\S)", "", target["label"])
    pad = live.preset_pad(label)
    pad.click()
    expect(pad).to_have_attribute("aria-pressed", "true")
    assert poll(rig, lambda s: s["audio"]["chain"]["preset"] == target["name"])
    expect(live.page.locator(".preset-note")).to_contain_text(target["label"])


@pytest.mark.parametrize("width", [1280, 1920])
def test_preset_pads_show_whole_names(live: App, width: int) -> None:
    # Six presets share one row; "Minimal" and "DeepFilterNet" were cut to "Minima" and "DeepFil".
    live.page.set_viewport_size({"width": width, "height": 1000})
    pads = live.region("Voice presets").get_by_role("button")
    expect(pads.first).to_be_visible()
    clipped = live.page.eval_on_selector_all(
        "ss-pad[fit]",
        """(els) => els.filter((e) => {
            const l = e.shadowRoot.querySelector('.label');
            return l.scrollWidth > l.clientWidth + 1 || l.scrollHeight > l.clientHeight + 1;
        }).map((e) => e.getAttribute('label'))""",
    )
    assert clipped == [], f"preset labels cut off at {width}px: {clipped}"


def test_auto_director_toggle(live: App, rig: Rig) -> None:
    director = live.switch("Director")
    expect(director).to_have_attribute("aria-checked", "false")
    director.click()
    expect(director).to_have_attribute("aria-checked", "true")
    assert poll(rig, lambda s: s["auto"]["running"] and not s["auto"]["busy"])
    expect(director).to_have_attribute("aria-checked", "true")
    director.click()
    expect(director).to_have_attribute("aria-checked", "false")
    assert poll(rig, lambda s: not s["auto"]["running"])


def test_virtual_camera_button(live: App, rig: Rig) -> None:
    was = rig.state()["video"]["virtualcam"]
    live.page.get_by_role("button", name="Virtual camera").click()
    assert poll(rig, lambda s: s["video"]["virtualcam"] != was)


def test_program_picture_streams_frames(live: App, page: Page) -> None:
    live.wait_picture()
    expect(page.locator("ss-preview").first).to_have_attribute("tally", re.compile("program|off"))


def test_status_rail_reflects_the_rig(live: App, page: Page) -> None:
    status = page.get_by_role("navigation", name="Status")
    expect(status).to_contain_text("Video engine")
    expect(status).to_contain_text("Running")
    expect(status).to_contain_text("StreamStack")


# ---------------------------------------------------------------------------
# Meters
# ---------------------------------------------------------------------------


def test_meters_animate(live: App, rig: Rig) -> None:
    """The demo mic speaks in phrases with pauses: the meter canvas must change within a few seconds."""
    rig.act("audio.gain", db=0)
    seen = {live.meter_signature("mic")}
    deadline = LOADED
    page = live.page
    page.wait_for_function(
        """(first) => {
            const c = document.querySelector("ss-meter[source='mic']").shadowRoot.querySelector('canvas');
            return c.toDataURL() !== first;
        }""",
        arg=next(iter(seen)),
        timeout=deadline,
    )


def test_meter_goes_silent_when_muted(live: App, page: Page) -> None:
    live.press("m")
    meter = page.locator("ss-meter[source='mic']").first
    expect(meter).to_have_attribute("aria-valuetext", "silent", timeout=LOADED)


# ---------------------------------------------------------------------------
# Background blur pad
# ---------------------------------------------------------------------------


def test_blur_pad_switches_the_blur_on_every_camera_and_shows_its_strength(live: App, rig: Rig) -> None:
    pad = live.page.get_by_role("button", name=re.compile(r"^Blur\b"))
    expect(pad).to_be_visible()
    expect(live.slider("Blur strength")).to_be_hidden()
    pad.click()
    expect(pad).to_have_attribute("aria-pressed", "true")
    state = poll(rig, lambda s: s["video"]["blur"]["on"] and s["video"]["blur"])
    assert all(e["enabled"] for e in state["sources"].values())
    fader = live.slider("Blur strength")
    expect(fader).to_be_visible()
    fader.focus()
    fader.press("ArrowDown")
    assert poll(rig, lambda s: s["video"]["blur"]["strength"] == 7 or None)
    pad.click()
    assert poll(rig, lambda s: not s["video"]["blur"]["on"])


def test_blur_pad_is_hidden_while_the_plugin_is_missing(live: App, rig: Rig) -> None:
    pad = live.page.get_by_role("button", name=re.compile(r"^Blur\b"))
    expect(pad).to_be_visible()
    rig.act("video.blur.plugin", installed=False)
    expect(pad).to_be_hidden(timeout=LOADED)
    rig.act("video.blur.plugin", installed=True)
    expect(pad).to_be_visible(timeout=LOADED)


def test_native_blur_pad_follows_the_arc_state(live: App, rig: Rig) -> None:
    pad = live.page.get_by_role("button", name=re.compile(r"^Blur\b"))
    rig.act("video.blur.arc", mode="unavailable")
    expect(pad).to_be_hidden(timeout=LOADED)  # mid-stream is the wrong time to read why it cannot run
    rig.act("video.blur.arc", mode="running")
    expect(pad).to_be_visible(timeout=LOADED)
    pad.click()
    expect(pad).to_have_attribute("aria-pressed", "true")
    assert poll(rig, lambda s: s["video"]["blur"]["on"] and s["video"]["blur"]["native"])
