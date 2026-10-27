"""The Stream page: go live, record, destinations, encoder, live controls, meters (demo rig: nothing is encoded)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import Locator, Page, expect

from support import LOADED, App, Rig, hold, poll, quiet, tap

PHONE = {"width": 390, "height": 844}
SECRET = "live_9f3KEYsecretVALUE42"


@pytest.fixture
def st(app: App) -> App:
    return app.goto("stream")


def word(page: Page) -> Locator:
    return page.locator("[data-ref='streamWord']")


def go_live(st: App, rig: Rig) -> None:
    tap(st.page, st.page.get_by_role("button", name="Go live"))
    poll(rig, lambda s: s["stream"]["status"] == "live" and s)
    expect(word(st.page)).to_have_text("On air")


def dest(page: Page, name: str) -> Locator:
    return page.locator(".st-dest[data-key]").filter(has=page.locator(".st-dest-name", has_text=name))


def add_destination(page: Page, *, kind: str, name: str, url: str, key: str = "") -> None:
    tap(page, page.get_by_role("button", name="Add destination"))
    page.get_by_label("Type").select_option(kind)
    page.get_by_label("Name").fill(name)
    page.get_by_label("URL").fill(url)
    if key:
        page.locator("[data-ref='fKey']").fill(key)
    page.get_by_role("button", name="Save destination").click()
    expect(page.locator("[data-ref='form']")).to_be_hidden()


def test_page_shows_offline_and_ready_destinations(st: App) -> None:
    page = st.page
    expect(page).to_have_title(re.compile("^Stream"))
    expect(word(page)).to_have_text("Offline")
    expect(page.get_by_role("button", name="Go live")).to_be_enabled()
    expect(dest(page, "Twitch")).to_be_visible()
    expect(dest(page, "Backup SRT")).to_be_visible()


def test_go_live_and_stop(st: App, rig: Rig) -> None:
    page = st.page
    go_live(st, rig)
    expect(dest(page, "Twitch").locator(".st-dest-pill")).to_have_text("Live")
    expect(dest(page, "Backup SRT").locator(".st-dest-pill")).to_have_text("Live")
    expect(page.locator("[data-ref='streamClock']")).to_be_visible()
    # a live destination cannot be edited or removed from under the stream
    expect(dest(page, "Twitch").get_by_role("button", name="Edit Twitch")).to_be_disabled()
    expect(page.get_by_role("button", name="Go live")).to_be_hidden()
    hold(page, page.get_by_role("button", name="Stop stream"), 1300)
    poll(rig, lambda s: s["stream"]["status"] == "idle" and s)
    expect(word(page)).to_have_text("Offline")
    expect(page.get_by_role("button", name="Go live")).to_be_visible()


def test_one_destination_starts_and_stops_alone(st: App, rig: Rig) -> None:
    page = st.page
    tap(page, page.get_by_role("button", name="Start Backup SRT"))
    state = poll(rig, lambda s: s["stream"]["status"] == "live" and s)
    assert [d["id"] for d in state["stream"]["destinations"] if d["active"]] == ["backup"]
    hold(page, page.get_by_role("button", name="Hold to stop Backup SRT"), 1200)
    poll(rig, lambda s: not any(d["active"] for d in s["stream"]["destinations"]) and s)


def test_record_and_stop(st: App, rig: Rig) -> None:
    page = st.page
    page.get_by_role("group", name="Recording format").get_by_role("button", name="MP4").click()
    tap(page, page.get_by_role("button", name="Record", exact=True))
    state = poll(rig, lambda s: s["stream"]["recording"]["active"] and s)
    assert state["stream"]["recording"]["format"] == "mp4"
    expect(page.locator("[data-ref='recordWord']")).to_have_text("Recording")
    expect(page.locator("[data-ref='recordSub']")).to_contain_text("demo.mp4")
    tap(page, page.get_by_role("button", name="Stop recording"))
    poll(rig, lambda s: not s["stream"]["recording"]["active"] and s)
    expect(page.locator("[data-ref='recordWord']")).to_have_text("Not recording")


def test_top_bar_shows_on_air_and_rec(st: App, rig: Rig) -> None:
    go_live(st, rig)
    rig.act("record.start", format="mkv")
    chips = st.page.get_by_role("banner")
    expect(chips).to_contain_text("ON AIR")
    expect(chips).to_contain_text("REC")


def test_destination_key_is_write_only(st: App, rig: Rig) -> None:
    page = st.page
    add_destination(page, kind="rtmp", name="Kick", url="rtmp://live.kick.test/app", key=SECRET)
    state = poll(rig, lambda s: any(d["id"] == "kick" for d in s["stream"]["destinations"]) and s)
    kick = next(d for d in state["stream"]["destinations"] if d["id"] == "kick")
    assert kick["key"] == "set" and kick["kind"] == "rtmp"
    expect(dest(page, "Kick")).to_contain_text("Key set")
    # never shown, never in the state, never in the page, never in the rig's log
    assert SECRET not in json.dumps(state)
    assert SECRET not in page.content()
    assert SECRET not in page.locator("body").inner_text()
    assert not any(SECRET in line for line in rig.log)
    # editing keeps the stored key, and the key field starts empty
    tap(page, page.get_by_role("button", name="Edit Kick"))
    expect(page.locator("[data-ref='fKey']")).to_have_value("")
    expect(page.locator("[data-ref='fKey']")).to_have_attribute("type", "password")
    page.get_by_label("Name").fill("Kick Main")
    page.get_by_role("button", name="Save destination").click()
    state = poll(rig, lambda s: any(d["name"] == "Kick Main" for d in s["stream"]["destinations"]) and s)
    renamed = next(d for d in state["stream"]["destinations"] if d["id"] == "kick")
    assert renamed["key"] == "set"
    # removing the stored key is explicit
    tap(page, page.get_by_role("button", name="Edit Kick Main"))
    page.get_by_label("Remove the stored key").check()
    page.get_by_role("button", name="Save destination").click()
    poll(rig, lambda s: next(d for d in s["stream"]["destinations"] if d["id"] == "kick")["key"] == "none" and s)
    # and removal
    hold(page, page.get_by_role("button", name="Hold to remove Kick Main"), 1300)
    poll(rig, lambda s: all(d["id"] != "kick" for d in s["stream"]["destinations"]) and s)
    expect(dest(page, "Kick Main")).to_have_count(0)
    assert SECRET not in json.dumps(rig.state())


def test_add_srt_destination_with_options(st: App, rig: Rig) -> None:
    page = st.page
    tap(page, page.get_by_role("button", name="Add destination"))
    page.get_by_label("Type").select_option("srt")
    page.get_by_label("Name").fill("Studio SRT")
    page.get_by_label("URL").fill("srt://studio.test:9000")
    page.get_by_label("Latency (ms)").fill("250")
    page.get_by_label("Connection").select_option("listener")
    page.get_by_role("button", name="Save destination").click()
    state = poll(rig, lambda s: any(d["name"] == "Studio SRT" for d in s["stream"]["destinations"]) and s)
    srt = next(d for d in state["stream"]["destinations"] if d["name"] == "Studio SRT")
    assert srt["kind"] == "srt" and srt["options"]["latency_ms"] == 250 and srt["options"]["mode"] == "listener"


def test_destination_needs_name_and_url(st: App) -> None:
    page = st.page
    tap(page, page.get_by_role("button", name="Add destination"))
    page.get_by_role("button", name="Save destination").click()
    expect(page.get_by_role("alert").filter(has_text="name")).to_be_visible()
    page.get_by_label("Name").fill("Nowhere")
    page.get_by_role("button", name="Save destination").click()
    expect(page.get_by_role("alert").filter(has_text="address")).to_be_visible()


def test_enabled_toggle_decides_what_go_live_starts(st: App, rig: Rig) -> None:
    page = st.page
    dest(page, "Backup SRT").get_by_role("switch").click()
    poll(rig, lambda s: not next(d for d in s["stream"]["destinations"] if d["id"] == "backup")["enabled"] and s)
    tap(page, page.get_by_role("button", name="Go live"))
    state = poll(rig, lambda s: s["stream"]["status"] == "live" and s)
    assert [d["id"] for d in state["stream"]["destinations"] if d["active"]] == ["twitch"]


def test_no_enabled_destination_explains_why_go_live_is_off(st: App, rig: Rig) -> None:
    rig.act("stream.set", destinations=[])
    expect(st.page.get_by_role("button", name="Go live")).to_be_disabled()
    expect(st.page.locator("[data-ref='goWhy']")).to_contain_text("Add a destination")


def test_encoder_picker_follows_the_probe(st: App, rig: Rig) -> None:
    page = st.page
    probe = rig.state()["stream"]["probe"]
    for enc in probe["encoders"]:
        button = page.locator(f"[data-encoder='{enc['name']}']")
        expect(button).to_be_visible()
        if enc["ok"]:
            expect(button).to_be_enabled()
        else:
            expect(button).to_be_disabled()
            expect(button).to_contain_text(enc["reason"])
    # WHIP is probed like the rest: available here, so it carries no reason
    expect(page.locator(".st-transports [data-transport='whip']")).to_contain_text("WHIP (WebRTC)")
    expect(page.locator(".st-transports [data-transport='whip']")).not_to_contain_text("unavailable")
    expect(page.locator(".st-reasons [data-transport='whip']")).to_have_count(0)
    # an encoder is picked live
    tap(page, page.locator("[data-encoder='libx264']"))
    poll(rig, lambda s: s["stream"]["settings"]["encoder"] == "libx264" and s)
    expect(page.locator("[data-encoder='libx264']")).to_have_attribute("aria-checked", "true")


def test_whip_destination_form_offers_webrtc_codecs_and_a_token(st: App) -> None:
    page = st.page
    tap(page, page.get_by_role("button", name="Add destination"))
    expect(page.get_by_label("Type").locator("option[value='whip']")).to_be_enabled()
    page.get_by_label("Type").select_option("whip")
    expect(page.locator("[data-ref='fTypeHint']")).to_contain_text("H.264 and Opus over WebRTC")
    expect(page.locator("[data-ref='fKeyLabel']")).to_have_text("Bearer token (optional)")
    expect(page.get_by_label("URL")).to_have_attribute("placeholder", "https://whip.example.com/endpoint")
    codecs = page.get_by_label("Codec").locator("option")
    assert [o.get_attribute("value") for o in codecs.all()] == ["auto", "h264", "hevc", "av1"]


def test_whip_destination_goes_live_with_a_write_only_token(st: App, rig: Rig) -> None:
    page = st.page
    add_destination(page, kind="whip", name="Studio ingest", url="https://whip.demo.test/live", key=SECRET)
    state = poll(rig, lambda s: any(d["id"] == "studio-ingest" for d in s["stream"]["destinations"]) and s)
    row = next(d for d in state["stream"]["destinations"] if d["id"] == "studio-ingest")
    assert row["kind"] == "whip" and row["key"] == "set"
    expect(dest(page, "Studio ingest")).to_contain_text("WHIP")
    expect(dest(page, "Studio ingest")).to_contain_text("Key set")
    tap(page, page.get_by_role("button", name="Start Studio ingest"))
    live = dest(page, "Studio ingest")
    expect(live.locator(".st-dest-pill")).to_have_text("Live")
    expect(live.locator(".st-dest-stats")).to_contain_text(re.compile(r"RTT \d+ ms"))
    expect(live.locator(".st-dest-stats")).to_contain_text("% loss")
    expect(live.locator(".st-dest-meta")).to_contain_text("H.264")
    state = poll(
        rig,
        lambda s: next(d for d in s["stream"]["destinations"] if d["id"] == "studio-ingest")["state"] == "live" and s,
    )
    whip = next(d for d in state["stream"]["destinations"] if d["id"] == "studio-ingest")["whip"]
    assert whip["ice_state"] == "completed" and whip["dtls_state"] == "connected"
    assert SECRET not in json.dumps(state)
    assert SECRET not in page.content()
    assert SECRET not in page.locator("body").inner_text()
    assert not any(SECRET in line for line in rig.log)
    hold(page, page.get_by_role("button", name="Hold to stop Studio ingest"), 1200)
    poll(rig, lambda s: not any(d["active"] for d in s["stream"]["destinations"]) and s)


def test_whip_destination_that_cannot_connect_shows_reconnecting(st: App, rig: Rig) -> None:
    page = st.page
    add_destination(page, kind="whip", name="Shaky WHIP", url="https://flaky.demo.test/whip")
    poll(rig, lambda s: any(d["id"] == "shaky-whip" for d in s["stream"]["destinations"]) and s)
    tap(page, page.get_by_role("button", name="Start Shaky WHIP"))
    row = dest(page, "Shaky WHIP")
    expect(row.locator(".st-dest-pill")).to_have_text("Reconnecting")
    expect(row.locator(".st-dest-error")).to_contain_text("flaky.demo.test")


def fader(page: Page, name: str) -> Locator:
    return page.get_by_role("slider", name=name)


def drag(st: App, slider: Locator, to: float) -> None:
    """The page scrolls, and a mouse move outside the viewport does nothing: bring the fader into view first."""
    slider.scroll_into_view_if_needed()
    st.drag_fader(slider, to)


def test_faders_apply_live_without_reconnecting(st: App, rig: Rig) -> None:
    page = st.page
    go_live(st, rig)
    before = {d["id"]: d["restarts"] for d in rig.state()["stream"]["destinations"] if d["active"]}
    drag(st, fader(page, "Video bitrate"), 0.5)
    state = poll(rig, lambda s: s["stream"]["settings"]["bitrate_kbps"] >= 8000 and s)
    assert 8000 <= state["stream"]["settings"]["bitrate_kbps"] <= 17000
    # the page shows what the encoder is doing about it, then settles
    expect(page.locator("[data-ref='livePill']")).to_contain_text(re.compile("Live|Applying"))
    expect(page.locator("[data-ref='livePill']")).to_contain_text("Live")
    fader(page, "Keyframe interval").focus()
    fader(page, "Keyframe interval").press("ArrowRight")
    poll(rig, lambda s: s["stream"]["settings"]["keyint_s"] > 2.0 and s)
    drag(st, fader(page, "Audio bitrate"), 0.9)
    state = poll(rig, lambda s: s["stream"]["settings"]["audio_bitrate_kbps"] >= 240 and s)
    after = {d["id"]: d["restarts"] for d in state["stream"]["destinations"] if d["active"]}
    assert after == before, "a live control reconnected a destination"
    assert all(d["state"] == "live" for d in state["stream"]["destinations"] if d["active"])


def test_a_fast_series_of_changes_is_never_refused(st: App, rig: Rig, problems: list[str]) -> None:
    page = st.page
    go_live(st, rig)
    for target in (0.2, 0.8, 0.4, 0.9):
        drag(st, fader(page, "Video bitrate"), target)
    state = poll(rig, lambda s: s["stream"]["settings"]["bitrate_kbps"] >= 18000 and s)
    assert state["stream"]["settings"]["bitrate_kbps"] >= 18000
    expect(page.locator("ss-toast .t").filter(has_text="still being applied")).to_have_count(0)
    assert problems == []


def test_rate_control_switch_enables_quality(st: App, rig: Rig) -> None:
    page = st.page
    expect(fader(page, "Quality (QP, lower is sharper)")).to_have_attribute("aria-disabled", "true")
    page.get_by_role("radio", name="Quality (CQP)").click()
    poll(rig, lambda s: s["stream"]["settings"]["rate_control"] == "cqp" and s)
    expect(fader(page, "Video bitrate")).to_have_attribute("aria-disabled", "true")
    drag(st, fader(page, "Quality (QP, lower is sharper)"), 0.2)
    poll(rig, lambda s: 0 < s["stream"]["settings"]["quality"] < 28 and s)


def test_idle_changes_say_they_apply_at_start(st: App) -> None:
    expect(st.page.locator("[data-ref='liveNote']")).to_contain_text("apply when the stream starts")


def test_picture_change_says_it_reconnects_once(st: App, rig: Rig) -> None:
    page = st.page
    go_live(st, rig)
    expect(page.locator("[data-ref='reconnectText']")).to_contain_text("reconnects")
    page.get_by_label("Resolution").select_option("720p")
    expect(page.locator("[data-ref='reconnectText']")).to_contain_text("every destination reconnects once")
    # nothing is sent until Apply
    assert rig.state()["stream"]["settings"]["tier"] == "source"
    tap(page, page.get_by_role("button", name="Apply and reconnect once"))
    poll(rig, lambda s: s["stream"]["settings"]["tier"] == "720p" and s)
    expect(page.locator("[data-ref='liveNote']")).to_contain_text("reconnect once")


def test_codec_buttons_follow_the_probe_and_stage(st: App, rig: Rig) -> None:
    page = st.page
    codec = page.get_by_role("radiogroup", name="Codec")
    for name in ("Auto", "AV1", "HEVC", "H.264"):
        expect(codec.get_by_role("radio", name=name)).to_be_enabled()
    codec.get_by_role("radio", name="HEVC").click()
    # state-read: a staged change posts nothing until Save
    assert rig.state()["stream"]["settings"]["codec"] == "auto"
    expect(page.get_by_role("button", name="Discard")).to_be_visible()
    tap(page, page.get_by_role("button", name="Save"))
    poll(rig, lambda s: s["stream"]["settings"]["codec"] == "hevc" and s)


def test_flaky_destination_shows_reconnect_state(st: App, rig: Rig) -> None:
    page = st.page
    add_destination(page, kind="rtmp", name="Shaky", url="rtmp://flaky.demo.test/app")
    poll(rig, lambda s: any(d["id"] == "shaky" for d in s["stream"]["destinations"]) and s)
    tap(page, page.get_by_role("button", name="Start Shaky"))
    row = dest(page, "Shaky")
    expect(row.locator(".st-dest-pill")).to_have_text("Reconnecting")
    expect(row.locator(".st-dest-error")).to_contain_text("flaky.demo.test")
    expect(row.locator(".st-dest-stats")).to_contain_text("reconnect")
    expect(word(page)).to_have_text("Degraded")


def test_meters_read_the_state(st: App, rig: Rig) -> None:
    page = st.page
    expect(page.locator("[data-meter='fps'] .st-meter-value")).to_have_text("–")
    go_live(st, rig)
    expect(page.locator("[data-meter='fps'] .st-meter-value")).to_have_text("30.0")
    expect(page.locator("[data-meter='bitrate'] .st-meter-value")).to_contain_text(re.compile(r"Mbps|kbps"))
    expect(page.locator("[data-meter='dropped'] .st-meter-value")).to_have_text("0")
    expect(page.locator("[data-meter='latency'] .st-meter-value")).to_contain_text("ms")
    expect(page.locator("[data-meter='cpu'] .st-meter-value")).to_contain_text("%")
    expect(page.locator("[data-meter='gpu'] .st-meter-value")).to_contain_text("%")
    # the sparkline fills as the pushes arrive
    expect(page.locator("[data-meter='bitrate'] .st-spark polyline")).to_be_attached(timeout=LOADED)


def test_events_list_what_happened(st: App, rig: Rig) -> None:
    go_live(st, rig)
    expect(st.page.locator(".st-events li").first).to_contain_text("Streaming")


def test_palette_goes_live(st: App, rig: Rig) -> None:
    st.press("Control+k")
    st.page.keyboard.type("Go live")
    st.page.keyboard.press("Enter")
    poll(rig, lambda s: s["stream"]["status"] == "live" and s)


def overflow(page: Page) -> dict:
    return page.evaluate(
        "() => ({sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth,"
        " bw: document.body.scrollWidth, pw: (document.querySelector('.st') || {}).scrollWidth,"
        " pc: (document.querySelector('.st') || {}).clientWidth})"
    )


@pytest.mark.parametrize("viewport", [{"width": 1280, "height": 720}, PHONE], ids=["desktop", "phone"])
def test_no_horizontal_overflow_in_any_state(
    st: App, page: Page, rig: Rig, artifacts: Path, viewport: dict[str, int]
) -> None:
    page.set_viewport_size(viewport)
    tag = f"{viewport['width']}x{viewport['height']}"
    st.goto("stream")
    rig.act(
        "stream.set",
        destinations=[
            {"id": "twitch", "kind": "rtmp", "name": "Twitch", "url": "rtmp://live.demo/app"},
            {"id": "backup", "kind": "srt", "name": "Backup SRT", "url": "srt://demo.test:9000"},
            {
                "id": "shaky",
                "kind": "rtmp",
                "name": "A destination with a rather long display name",
                "url": "rtmp://flaky.demo.test/app/with/a/very/long/path/that/keeps/going",
            },
        ],
    )
    expect(dest(page, "A destination with a rather").first).to_be_visible()
    quiet(page)
    page.screenshot(path=str(artifacts / f"stream-idle-{tag}.png"), full_page=False)
    rig.act("stream.start")
    rig.act("record.start", format="mkv")
    expect(word(page)).to_have_text("Degraded")
    tap(page, page.get_by_role("button", name="Add destination"))
    page.get_by_label("Type").select_option("srt")
    page.locator(".st").evaluate("(el) => { el.scrollTop = 0; }")
    page.screenshot(path=str(artifacts / f"stream-live-{tag}.png"), full_page=False)
    page.locator(".st").evaluate("(el) => { el.scrollTop = el.scrollHeight; }")
    page.screenshot(path=str(artifacts / f"stream-bottom-{tag}.png"), full_page=False)
    o = overflow(page)
    assert o["sw"] <= o["cw"] and o["bw"] <= o["cw"] and o["pw"] <= o["pc"], o
    # nothing inside the page pokes out of it either
    pokes = page.evaluate(
        """() => { const root = document.querySelector('.st').getBoundingClientRect();
          return [...document.querySelectorAll('.st *')].filter((el) => { const r = el.getBoundingClientRect();
            return r.width > 0 && !el.closest('.st-events') && (r.right > root.right + 1 || r.left < root.left - 1); })
            .slice(0, 5).map((el) => el.className + ' ' + Math.round(el.getBoundingClientRect().right)); }"""
    )
    assert pokes == [], pokes


def test_phone_tab_bar_fits_ten_pages(st: App, page: Page) -> None:
    page.set_viewport_size(PHONE)
    st.goto("stream")
    rail = page.get_by_role("navigation", name="Pages")
    links = rail.get_by_role("link")
    boxes = [links.nth(i).bounding_box() for i in range(links.count())]
    assert all(b is not None for b in boxes)
    assert max(b["x"] + b["width"] for b in boxes if b) <= PHONE["width"] + 1 or rail.evaluate(
        "(el) => el.scrollWidth > el.clientWidth"
    ), "the tab bar neither fits nor scrolls"
    expect(rail.get_by_role("link", name="Stream")).to_have_attribute("aria-current", "page")
