"""The Camera page: source and picture choice, overlays, scopes, grade, sharpen/LUT, looks library, outputs."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Locator, Page, Request, expect

from support import LOADED, SCALE, App, Rig, hold, poll, tap

LOOK = "E2E Test Look"


@pytest.fixture
def cam(app: App) -> App:
    return app.goto("camera")


def toolbar(page: Page, name: str) -> Locator:
    return page.get_by_role("button", name=re.compile(rf"^{name}"))


def is_on(button: Locator) -> None:
    expect(button).to_have_attribute("aria-pressed", "true")


def is_off(button: Locator) -> None:
    expect(button).not_to_have_attribute("aria-pressed", "true")


def host_on(button: Locator) -> None:
    expect(button).to_have_attribute("pressed", "")


def host_off(button: Locator) -> None:
    expect(button).not_to_have_attribute("pressed", "")


# ---------------------------------------------------------------------------
# Picture, source, overlays
# ---------------------------------------------------------------------------


def test_source_picker_switches_the_edited_camera(cam: App, rig: Rig) -> None:
    sources = rig.state()["video"]["sources"]
    assert len(sources) >= 2
    group = cam.region("Camera to edit")
    expect(group.get_by_role("button", name=sources[0])).to_have_attribute("aria-pressed", "true")
    group.get_by_role("button", name=sources[1]).click()
    expect(group.get_by_role("button", name=sources[1])).to_have_attribute("aria-pressed", "true")
    assert poll(rig, lambda s: s["video"]["selected_source"] == sources[1])
    expect(cam.page.locator("ss-preview").first).to_have_attribute("source", sources[1])


def test_each_source_keeps_its_own_grade(cam: App, rig: Rig) -> None:
    first, second = rig.state()["video"]["sources"][:2]
    rig.act("video.look", values={"brightness": 0.2})
    expect(cam.slider("Brightness")).to_have_attribute("aria-valuenow", "0.2", timeout=LOADED)
    cam.region("Camera to edit").get_by_role("button", name=second).click()
    expect(cam.slider("Brightness")).to_have_attribute("aria-valuenow", "0", timeout=LOADED)
    cam.region("Camera to edit").get_by_role("button", name=first).click()
    expect(cam.slider("Brightness")).to_have_attribute("aria-valuenow", "0.2", timeout=LOADED)


def test_picture_toggle_between_camera_and_program(cam: App, rig: Rig) -> None:
    which = cam.region("Which picture")
    source = rig.state()["video"]["selected_source"]
    pv = cam.page.locator("ss-preview").first
    expect(pv).to_have_attribute("source", source)
    which.get_by_role("button", name="Program").click()
    expect(pv).to_have_attribute("source", "program")
    expect(which.get_by_role("button", name="Program")).to_have_attribute("aria-pressed", "true")
    which.get_by_role("button", name=source).click()
    expect(pv).to_have_attribute("source", source)


def test_overlay_keys_and_buttons(cam: App, page: Page) -> None:
    expect(page.locator("canvas.ov-zebra")).to_be_hidden()
    expect(page.locator(".ov-safe")).to_be_hidden()
    expect(page.locator(".ov-cross")).to_be_hidden()
    for key, name, overlay in (
        ("z", "Zebra", "canvas.ov-zebra"),
        ("s", "Safe", ".ov-safe"),
        ("c", "Centre", ".ov-cross"),
    ):
        cam.press(key)
        expect(page.locator(overlay)).to_be_visible()
        is_on(toolbar(page, name))
        cam.press(key)
        expect(page.locator(overlay)).to_be_hidden()
        is_off(toolbar(page, name))
    toolbar(page, "Zebra").click()
    expect(page.locator("canvas.ov-zebra")).to_be_visible()


def test_g_key_and_button_toggle_the_thirds_grid(cam: App, page: Page) -> None:
    pv = page.locator("ss-preview").first
    cam.press("g")
    expect(pv).to_have_attribute("grid", "")
    is_on(toolbar(page, "Thirds"))
    toolbar(page, "Thirds").click()
    expect(pv).not_to_have_attribute("grid", "")


def test_overlay_choices_are_remembered(cam: App, page: Page) -> None:
    cam.press("c")
    expect(page.locator(".ov-cross")).to_be_visible()
    page.reload()
    cam.wait_ready("camera")
    expect(page.locator(".ov-cross")).to_be_visible()


def test_scopes_render_from_the_live_picture(cam: App, page: Page) -> None:
    cam.wait_picture()
    hist = page.get_by_role("img", name=re.compile("Luma histogram"))
    scope = page.get_by_role("img", name="Colour scope")
    expect(hist).to_be_visible()
    expect(scope).to_be_visible()
    for canvas in (hist, scope):
        page.wait_for_function(
            """(c) => { const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
                        for (let i = 3; i < d.length; i += 4) if (d[i] > 0) return true; return false; }""",
            arg=canvas.element_handle(),
            timeout=LOADED,
        )
    # the clipping read-outs are computed from the picture, not left at the dash
    expect(page.locator("[data-ref='stHighV']")).not_to_have_text("–", timeout=LOADED)
    expect(page.locator("[data-ref='stLowV']")).not_to_have_text("–")


def test_scope_mode_and_visibility_keys(cam: App, page: Page) -> None:
    scopes = cam.region("Scope type")
    expect(scopes.get_by_role("button", name="Parade")).to_have_attribute("aria-pressed", "true")
    cam.press("v")
    expect(scopes.get_by_role("button", name="Waveform")).to_have_attribute("aria-pressed", "true")
    expect(page.get_by_text("Luma waveform")).to_be_visible()
    scopes.get_by_role("button", name="Parade").click()
    expect(page.get_by_text("RGB parade").first).to_be_visible()
    cam.press("h")
    expect(page.get_by_role("img", name=re.compile("Luma histogram"))).to_be_hidden()
    is_off(toolbar(page, "Scopes"))
    cam.press("h")
    expect(page.get_by_role("img", name=re.compile("Luma histogram"))).to_be_visible()


# ---------------------------------------------------------------------------
# Grade
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label", ["Brightness", "Contrast", "Gamma", "Saturation", "Warmth"])
def test_grade_fader_drag_changes_the_look(cam: App, rig: Rig, label: str) -> None:
    key = label.lower()
    assert rig.state()["video"]["look"][key] == 0
    cam.drag_fader(cam.slider(label), 0.9)
    value = poll(rig, lambda s: s["video"]["look"][key] > 0.05 and s["video"]["look"][key])
    # state-read: the fader limits, which no action changes
    lo, hi = rig.state()["video"]["limits"][key]
    assert lo <= value <= hi
    expect(cam.slider(label)).to_have_attribute("aria-valuenow", re.compile(r"^0\.\d+"))


def test_warmth_goes_both_ways(cam: App, rig: Rig) -> None:
    cam.drag_fader(cam.slider("Warmth"), 0.1)
    assert poll(rig, lambda s: s["video"]["look"]["warmth"] < -0.05)
    expect(cam.page.get_by_text("Cool", exact=True)).to_be_visible()
    expect(cam.page.get_by_text("Warm", exact=True)).to_be_visible()
    cam.slider("Warmth").dblclick()
    assert poll(rig, lambda s: s["video"]["look"]["warmth"] == 0)


def test_grade_fader_keys(cam: App, rig: Rig) -> None:
    fader = cam.slider("Contrast")
    fader.focus()
    fader.press("PageUp")
    assert poll(rig, lambda s: s["video"]["look"]["contrast"] == pytest.approx(0.1))
    fader.press("ArrowDown")
    assert poll(rig, lambda s: s["video"]["look"]["contrast"] == pytest.approx(0.09))
    fader.press("Backspace")  # back to the default
    assert poll(rig, lambda s: s["video"]["look"]["contrast"] == 0)


def test_reset_button_returns_the_grade_to_neutral(cam: App, rig: Rig) -> None:
    rig.act("video.look", values={"contrast": 0.4, "warmth": -0.3})
    expect(cam.slider("Contrast")).to_have_attribute("aria-valuenow", "0.4", timeout=LOADED)
    cam.page.get_by_role("button", name="Reset", exact=True).click()
    assert poll(rig, lambda s: s["video"]["look"]["contrast"] == 0 and s["video"]["look"]["warmth"] == 0)
    expect(cam.slider("Contrast")).to_have_attribute("aria-valuenow", "0", timeout=LOADED)
    expect(cam.slider("Warmth")).to_have_attribute("aria-valuenow", "0", timeout=LOADED)


def test_one_key_press_sends_one_request(cam: App, rig: Rig) -> None:
    """The fader's `change` must not stream the value its `input` already sent."""
    sent: list[str] = []

    def record(request: Request) -> None:
        if request.method == "POST" and "api/action" in request.url:
            sent.append(request.post_data or "")

    cam.page.on("request", record)
    fader = cam.slider("Gamma")
    fader.focus()
    fader.press("ArrowRight")
    assert poll(rig, lambda s: s["video"]["look"]["gamma"] == pytest.approx(0.01))
    cam.page.wait_for_timeout(int(800 * SCALE))  # past the 250 ms key commit
    assert len([b for b in sent if '"video.look"' in b]) == 1, sent


def test_reset_right_after_a_key_press_sticks(cam: App, rig: Rig) -> None:
    """A Reset pressed before the fader's key commit lands must not be overwritten by it."""
    fader = cam.slider("Contrast")
    fader.focus()
    fader.press("PageUp")
    cam.page.get_by_role("button", name="Reset", exact=True).click()
    assert poll(rig, lambda s: s["video"]["look"]["contrast"] == 0)
    cam.page.wait_for_timeout(int(800 * SCALE))
    assert rig.state()["video"]["look"]["contrast"] == 0


def test_server_side_grade_reaches_the_faders(cam: App, rig: Rig) -> None:
    rig.act("video.look", values={"gamma": -0.25})
    expect(cam.slider("Gamma")).to_have_attribute("aria-valuenow", "-0.25", timeout=LOADED)


def test_sharpen_and_lut_toggles_enable_their_amounts(cam: App, rig: Rig) -> None:
    amounts = cam.page.get_by_role("slider", name="Amount")
    sharpen, lut = cam.switch("Sharpen"), cam.switch("LUT")
    expect(sharpen).to_have_attribute("aria-checked", "false")
    expect(amounts.nth(0)).to_have_attribute("aria-disabled", "true")
    sharpen.click()
    expect(sharpen).to_have_attribute("aria-checked", "true")
    assert poll(rig, lambda s: s["video"]["look"]["sharpen_enabled"])
    expect(amounts.nth(0)).to_have_attribute("aria-disabled", "false")
    expect(amounts.nth(1)).to_have_attribute("aria-disabled", "true")

    lut.click()
    assert poll(rig, lambda s: s["video"]["look"]["lut_enabled"])
    expect(amounts.nth(1)).to_have_attribute("aria-disabled", "false")

    amounts.nth(0).focus()
    amounts.nth(0).press("PageUp")
    assert poll(rig, lambda s: s["video"]["look"]["sharpen_amount"] > 0.08)
    cam.drag_fader(amounts.nth(1), 0.9)
    assert poll(rig, lambda s: s["video"]["look"]["lut_amount"] > 0.5)

    sharpen.click()
    assert poll(rig, lambda s: not s["video"]["look"]["sharpen_enabled"])
    lut.click()
    assert poll(rig, lambda s: not s["video"]["look"]["lut_enabled"])


# ---------------------------------------------------------------------------
# Looks library
# ---------------------------------------------------------------------------


def test_built_in_looks_are_listed_without_delete(cam: App, rig: Rig) -> None:
    looks = cam.page.get_by_role("list", name="Saved looks")
    for look in rig.state()["video"]["looks"]:
        expect(looks.get_by_role("button", name=f"Apply look {look['name']}")).to_be_visible()
    assert all(look["builtin"] for look in rig.state()["video"]["looks"])
    expect(looks.locator("ss-button.look-del:visible")).to_have_count(0)


def test_look_save_apply_delete_round_trip(cam: App, page: Page, rig: Rig) -> None:
    rig.act("video.look", values={"brightness": 0.2, "warmth": 0.3})
    expect(cam.slider("Warmth")).to_have_attribute("aria-valuenow", "0.3", timeout=LOADED)

    page.get_by_role("button", name="Save current look").click()
    page.get_by_role("textbox", name="Look name").fill(LOOK)
    page.get_by_role("button", name="Save", exact=True).click()
    mine = poll(rig, lambda s: next((x for x in s["video"]["looks"] if x["name"] == LOOK), None))
    assert mine["builtin"] is False
    assert mine["values"]["brightness"] == pytest.approx(0.2)
    assert mine["values"]["warmth"] == pytest.approx(0.3)
    card = page.get_by_role("list", name="Saved looks").get_by_role("button", name=f"Apply look {LOOK}")
    expect(card).to_be_visible()
    expect(card).to_have_attribute("aria-pressed", "true")  # it is the current look

    page.get_by_role("button", name="Reset", exact=True).click()
    assert poll(rig, lambda s: s["video"]["look"]["brightness"] == 0)
    expect(card).to_have_attribute("aria-pressed", "false")

    card.click()  # ramps over 1.5 s
    assert poll(rig, lambda s: abs(s["video"]["look"]["brightness"] - 0.2) < 0.01 and s)
    assert poll(rig, lambda s: abs(s["video"]["look"]["warmth"] - 0.3) < 0.01 and s)
    expect(card).to_have_attribute("aria-pressed", "true", timeout=LOADED)

    delete = page.locator(f'ss-button[aria-label="Hold to delete look {LOOK}"]')
    tap(page, delete)  # a tap only explains itself
    expect(page.get_by_text("Press and hold to confirm")).to_be_visible()
    assert any(x["name"] == LOOK for x in rig.state()["video"]["looks"])
    hold(page, delete, 1200)
    assert poll(rig, lambda s: all(x["name"] != LOOK for x in s["video"]["looks"]) or None) is not None
    expect(card).to_have_count(0)


def test_save_form_validates_names(cam: App, page: Page) -> None:
    page.get_by_role("button", name="Save current look").click()
    name = page.get_by_role("textbox", name="Look name")
    save = page.get_by_role("button", name="Save", exact=True)
    expect(name).to_be_focused()
    expect(save).to_be_disabled()
    name.fill("Natural")
    expect(page.get_by_text("built-in look")).to_be_visible()
    expect(name).to_have_attribute("aria-invalid", "true")
    expect(save).to_be_disabled()
    name.fill("Fine name")
    expect(name).to_have_attribute("aria-invalid", "false")
    expect(save).to_be_enabled()
    name.press("Escape")
    expect(name).to_be_hidden()
    expect(page.get_by_role("button", name="Save current look")).to_be_visible()


def test_saving_with_enter_replaces_an_existing_look(cam: App, page: Page, rig: Rig) -> None:
    rig.act("video.looks.save", name=LOOK)
    page.get_by_role("button", name="Save current look").click()
    name = page.get_by_role("textbox", name="Look name")
    name.fill(LOOK)
    expect(page.get_by_text(f"Replaces your look “{LOOK}”")).to_be_visible()
    rig.act("video.look", values={"contrast": 0.4})
    name.press("Enter")
    assert poll(
        rig,
        lambda s: next((x["values"]["contrast"] for x in s["video"]["looks"] if x["name"] == LOOK), 0) > 0.3,
    )


# ---------------------------------------------------------------------------
# Outputs: virtual camera, record, stream
# ---------------------------------------------------------------------------


def output(page: Page, name: str) -> Locator:
    """The ss-button host (its state text lives in the light DOM; pressing it presses the inner button)."""
    return page.get_by_role("group", name="Outputs").locator("ss-button").filter(has_text=re.compile(rf"^\s*{name}"))


def test_virtual_cam_toggles_both_ways(cam: App, page: Page, rig: Rig) -> None:
    was = rig.state()["video"]["virtualcam"]
    button = output(page, "Virtual cam")
    (host_on if was else host_off)(button)
    tap(page, button)
    assert poll(rig, lambda s: s["video"]["virtualcam"] != was or None) is not None
    (host_off if was else host_on)(button)
    tap(page, button)
    assert poll(rig, lambda s: s["video"]["virtualcam"] == was)


def test_record_starts_with_a_click_and_stops_only_with_a_hold(cam: App, page: Page, rig: Rig) -> None:
    button = output(page, "Record")
    tap(page, button)
    assert poll(rig, lambda s: s["video"]["recording"]["active"])
    expect(button).to_contain_text("hold to stop")

    tap(page, button)  # a tap must not stop it
    expect(page.get_by_text("Press and hold to confirm")).to_be_visible()
    assert rig.state()["video"]["recording"]["active"]

    hold(page, button, 1200)
    assert poll(rig, lambda s: not s["video"]["recording"]["active"])
    expect(button).not_to_contain_text("hold to stop")


def test_stream_starts_with_a_click_and_stops_only_with_a_hold(cam: App, page: Page, rig: Rig) -> None:
    button = output(page, "Stream")
    tap(page, button)
    assert poll(rig, lambda s: s["video"]["streaming"]["active"])
    expect(button).to_contain_text("hold to stop")
    tap(page, button)
    expect(page.get_by_text("Press and hold to confirm")).to_be_visible()
    assert rig.state()["video"]["streaming"]["active"]
    hold(page, button, 1400)
    assert poll(rig, lambda s: not s["video"]["streaming"]["active"])


def test_scene_pads_on_the_camera_page(cam: App, rig: Rig) -> None:
    scenes = [s["name"] for s in rig.state()["video"]["scenes"]]
    cam.scene_pad(scenes[2]).click()
    assert poll(rig, lambda s: s["video"]["program_scene"] == scenes[2])
    cam.press("1")
    assert poll(rig, lambda s: s["video"]["program_scene"] == scenes[0])


# ---------------------------------------------------------------------------
# Background blur (the OpenVINO mask): toggle, strength, smoothness, cost guard, missing plugin
# ---------------------------------------------------------------------------


def blur_of(rig: Rig) -> dict:
    state = rig.state()["video"]
    return state["blur"]["sources"][state["selected_source"]]


def test_blur_toggle_enables_the_filter_on_the_edited_camera_only(cam: App, rig: Rig) -> None:
    first, second = rig.state()["video"]["sources"][:2]
    toggle = cam.switch("Blur background")
    expect(toggle).to_have_attribute("aria-checked", "false")
    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "true")
    state = poll(rig, lambda s: s["video"]["blur"]["sources"][first]["enabled"] and s["video"]["blur"])
    assert state["sources"][second]["enabled"] is False
    expect(cam.page.get_by_text("Within budget")).to_be_visible(timeout=LOADED)
    toggle.click()
    assert poll(rig, lambda s: not s["video"]["blur"]["on"])
    expect(cam.page.get_by_text("Within budget")).to_have_count(0)


def test_blur_strength_fader_sets_the_plugin_pass_count(cam: App, rig: Rig) -> None:
    cam.switch("Blur background").click()
    fader = cam.slider("Blur strength")
    expect(fader).to_have_attribute("aria-valuenow", "8")
    fader.focus()
    fader.press("ArrowUp")
    assert poll(rig, lambda s: blur_of(rig)["strength"] == 9 or None)
    fader.press("End")
    assert poll(rig, lambda s: blur_of(rig)["strength"] == 20 or None)
    expect(fader).to_have_attribute("aria-valuenow", "20")


def test_blur_smoothness_changes_the_mask_rate_and_the_smoothest_is_over_budget(cam: App, rig: Rig) -> None:
    cam.switch("Blur background").click()
    group = cam.region("Mask smoothness")
    expect(group.get_by_role("button", name="Balanced")).to_have_attribute("aria-pressed", "true")
    group.get_by_role("button", name="Light").click()
    assert poll(rig, lambda s: blur_of(rig)["rate"] == 3 or None)
    group.get_by_role("button", name="Smooth").click()
    assert poll(rig, lambda s: blur_of(rig)["rate"] == 1 or None)
    expect(cam.page.get_by_text("Over budget")).to_be_visible(timeout=LOADED)
    check = poll(rig, lambda s: next((c for c in s["health"]["checks"] if c["id"] == "background-blur"), None))
    assert check["state"] == "warn" and "budget" in check["title"]
    group.get_by_role("button", name="Light").click()
    expect(cam.page.get_by_text("Within budget")).to_be_visible(timeout=LOADED)


def test_blur_without_the_mask_says_what_is_missing(cam: App, rig: Rig) -> None:
    rig.act("video.blur.plugin", installed=False)
    expect(cam.page.get_by_text("segmentation model", exact=False).first).to_be_visible(timeout=LOADED)
    expect(cam.switch("Blur background")).to_be_disabled()
    check = poll(rig, lambda s: next((c for c in s["health"]["checks"] if c["id"] == "background-blur"), None))
    assert check["state"] == "warn" and check["hint"]


# ---------------------------------------------------------------------------
# Background blur on the Arc through OpenVINO (the native engine): the demo rig has no GPU, `video.blur.arc` puts the
# page in each state the real controller can report
# ---------------------------------------------------------------------------


def blur_info(cam: App) -> Locator:
    return cam.page.locator("[data-ref=blurInfo]")


def test_native_blur_running_shows_the_arc_and_its_measured_numbers(cam: App, rig: Rig) -> None:
    rig.act("video.blur.arc", mode="running")
    toggle = cam.switch("Blur background")
    expect(toggle).to_be_enabled(timeout=LOADED)
    toggle.click()
    expect(blur_info(cam)).to_contain_text("Running on Intel Arc A380 via OpenVINO", timeout=LOADED)
    expect(blur_info(cam)).to_contain_text("15 masks/s")  # the default smoothness (every second frame) at 30 fps
    expect(blur_info(cam)).to_contain_text("2.4 ms")
    expect(cam.page.get_by_text("Within budget")).to_be_visible(timeout=LOADED)
    assert poll(rig, lambda s: s["video"]["blur"]["on"] and s["video"]["blur"]["native"])


def test_native_blur_strength_and_smoothness_stay_live(cam: App, rig: Rig) -> None:
    rig.act("video.blur.arc", mode="running")
    cam.switch("Blur background").click()
    fader = cam.slider("Blur strength")
    expect(fader).to_have_attribute("aria-valuenow", "8")
    fader.focus()
    fader.press("ArrowUp")
    assert poll(rig, lambda s: s["video"]["blur"]["strength"] == 9 or None)
    cam.region("Mask smoothness").get_by_role("button", name="Smooth").click()
    assert poll(rig, lambda s: s["video"]["blur"]["rate"] == 1 or None)
    expect(blur_info(cam)).to_contain_text("30 masks/s", timeout=LOADED)


def test_native_blur_without_openvino_says_unavailable_and_why(cam: App, rig: Rig) -> None:
    rig.act("video.blur.arc", mode="unavailable")
    expect(blur_info(cam)).to_contain_text("Unavailable: the openvino package is not installed", timeout=LOADED)
    expect(cam.switch("Blur background")).to_be_disabled()
    expect(cam.slider("Blur strength")).to_be_disabled()
    expect(cam.region("Mask smoothness").get_by_role("button", name="Light")).to_be_disabled()
    status, body = rig.request("POST", "api/action", {"action": "video.blur", "on": True}, token=rig.token)
    assert status == 409 and "unavailable: the openvino package" in str(body)
    rig.act("video.blur", on=False)  # switching off is always allowed


def test_native_blur_download_failure_is_unavailable_not_a_crash(cam: App, rig: Rig) -> None:
    rig.act("video.blur.arc", mode="failed")
    expect(blur_info(cam)).to_contain_text(
        "Unavailable: the segmentation model could not be downloaded", timeout=LOADED
    )
    expect(cam.switch("Blur background")).to_be_disabled()


def test_native_blur_downloads_the_model_the_first_time(cam: App, rig: Rig) -> None:
    rig.act("video.blur.arc", mode="downloading")
    cam.switch("Blur background").click()
    expect(blur_info(cam)).to_contain_text("Downloading the person-segmentation model", timeout=LOADED)
    expect(cam.switch("Blur background")).to_have_attribute("aria-checked", "true")


def test_native_blur_stalled_says_the_picture_is_unblurred(cam: App, rig: Rig) -> None:
    rig.act("video.blur.arc", mode="stalled")
    cam.switch("Blur background").click()
    expect(blur_info(cam)).to_contain_text("the mask is late", timeout=LOADED)
    expect(cam.page.get_by_text("Over budget")).to_be_visible(timeout=LOADED)


# ---------------------------------------------------------------------------
# The native video engine
# ---------------------------------------------------------------------------


def test_engine_chip_shows_the_gpu_path_and_frame_rate(cam: App, rig: Rig) -> None:
    engine = rig.state()["video"]["engine"]
    assert engine["native"] is True
    chip = cam.page.get_by_text("ENGINE", exact=True)
    expect(chip).to_be_visible(timeout=LOADED)
    expect(cam.page.get_by_text(re.compile(r"GPU · \d+ fps"))).to_be_visible()


def test_an_idle_engine_is_shown_and_keep_awake_wakes_it(cam: App, page: Page, rig: Rig) -> None:
    """#128: no app reads StreamStack Camera, so the engine rests; the page says so and Keep awake overrides it."""
    rig.act("video.idle.demo", state="idle")
    engine = page.locator("ss-status-chip[label='ENGINE']")
    expect(engine).to_have_attribute("value", "idle", timeout=LOADED)
    expect(engine).to_have_attribute("state", "info")
    expect(engine).to_have_attribute("title", re.compile("no app is reading StreamStack Camera"))
    assert rig.state()["video"]["engine"]["idle"]["state"] == "idle"
    assert rig.state()["video"]["virtualcam"] is True  # still listed while idle

    keep = output(page, "Keep awake")
    host_off(keep)
    tap(page, keep)
    assert poll(rig, lambda s: s["video"]["engine"]["idle"]["keep_alive"])
    host_on(keep)
    expect(keep).to_contain_text("On")
    expect(engine).to_have_attribute("value", re.compile(r"GPU"), timeout=LOADED)  # the demo woke on keep alive
    tap(page, keep)
    assert poll(rig, lambda s: not s["video"]["engine"]["idle"]["keep_alive"])
    host_off(keep)


def test_the_canon_card_says_live_view_is_off_while_idle(cam: App, page: Page, rig: Rig) -> None:
    rig.act("video.idle.demo", state="idle")
    assert poll(rig, lambda s: s["video"]["canon"]["state"] == "idle")
    expect(page.get_by_text("Live view is off while no app uses the camera").first).to_be_attached(timeout=LOADED)
    rig.act("video.idle.demo", state="running")
    assert poll(rig, lambda s: s["video"]["canon"]["state"] == "running")


def test_a_slider_move_reaches_the_engine_without_a_restart(cam: App, rig: Rig) -> None:
    before = rig.state()["video"]["engine"]
    rig.act("video.look", values={"contrast": 0.3})
    assert poll(rig, lambda s: s["video"]["look"]["contrast"] == 0.3)
    after = rig.state()["video"]["engine"]
    assert after["state"] == before["state"] == "running"
    expect(cam.slider("Contrast")).to_have_attribute("aria-valuenow", "0.3", timeout=LOADED)
