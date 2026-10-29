"""True-peak meters: the clip latch on the Live, Audio and Mini mic meters, and the LUFS readout on the Audio page.

The demo rig clips on demand (``audio.demo_clip``) and keeps the latch on the server like the real controller does:
it stays lit until cleared, and a clear from one window clears every window.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from typing import Any

import pytest
from playwright.sync_api import Locator, Page, expect

from support import HTTP_TIMEOUT, LOADED, TOKEN_HEADER, App, Rig

LIT = ("data-on", "")


def lamp(page: Page, source: str = "mic") -> Locator:
    return page.locator(f"ss-meter[source='{source}'] .clip").first


def expect_lit(button: Locator) -> None:
    expect(button).to_have_attribute(*LIT, timeout=LOADED)
    expect(button).to_have_attribute("aria-label", "Clipped, press to clear")


def expect_dark(button: Locator) -> None:
    expect(button).not_to_have_attribute(*LIT, timeout=LOADED)
    expect(button).to_have_attribute("aria-label", "Clip indicator")


@pytest.mark.parametrize("route", ["live", "audio", "mini"])
def test_clip_lamp_latches_until_clicked(app: App, rig: Rig, route: str) -> None:
    app.goto(route)
    page = app.page
    expect_dark(lamp(page))
    rig.act("audio.demo_clip")
    expect_lit(lamp(page))
    page.wait_for_timeout(1500)  # the old lamp let go after 3 s; a latch does not
    expect_lit(lamp(page))
    lamp(page).click()
    expect_dark(lamp(page))
    page.wait_for_timeout(1000)  # frames keep arriving: the server must agree it is clear
    expect_dark(lamp(page))


def test_clip_lamp_clear_reaches_the_server(app: App, rig: Rig) -> None:
    """Clearing is a server action, so another window (or the next page) does not show a stale clip."""
    app.goto("live")
    rig.act("audio.demo_clip")
    expect_lit(lamp(app.page))
    lamp(app.page).click()
    expect_dark(lamp(app.page))
    app.goto("audio")
    app.page.wait_for_timeout(800)
    expect_dark(lamp(app.page))


def test_clip_latch_survives_a_page_change(app: App, rig: Rig) -> None:
    app.goto("live")
    rig.act("audio.demo_clip")
    expect_lit(lamp(app.page))
    app.goto("audio")
    expect_lit(lamp(app.page))
    app.goto("mini")
    expect_lit(lamp(app.page))


def test_clip_latch_survives_muting(app: App, rig: Rig) -> None:
    app.goto("live")
    rig.act("audio.demo_clip")
    expect_lit(lamp(app.page))
    app.press("m")
    app.page.wait_for_timeout(600)
    expect_lit(lamp(app.page))


def test_clear_action_clears_every_open_window(app: App, rig: Rig) -> None:
    app.goto("live")
    rig.act("audio.demo_clip")
    expect_lit(lamp(app.page))
    rig.act("audio.clip_clear")
    expect_dark(lamp(app.page))


def test_voice_meter_has_its_own_lamp(app: App, rig: Rig) -> None:
    app.goto("audio")
    rig.act("audio.demo_clip")
    expect_lit(lamp(app.page, "voice"))
    lamp(app.page, "voice").click()
    expect_dark(lamp(app.page, "voice"))
    expect_dark(lamp(app.page, "mic"))


# ---------------------------------------------------------------------------
# LUFS on the Audio page
# ---------------------------------------------------------------------------

LUFS_VALUE = re.compile(r"^−\d{1,2}\.\d$")  # "−23.4": a typographic minus, one decimal


def readout(page: Page, who: str, term: str) -> Locator:
    """`term` is M (momentary) or S (short-term)."""
    index = 0 if term == "M" else 1
    return page.locator(f".lufs-row[data-who='{who}'] .lufs-v b").nth(index)


def test_lufs_readout_shows_momentary_and_short_term(app: App) -> None:
    app.goto("audio")
    page = app.page
    expect(page.locator(".lufs-unit")).to_have_text("LUFS")
    # the demo talks in phrases: both windows read a real loudness within a few seconds
    for who in ("mic", "voice"):
        for term in ("M", "S"):
            expect(readout(page, who, term)).to_have_text(LUFS_VALUE, timeout=LOADED)


def test_lufs_readout_falls_to_a_dash_when_muted(app: App, rig: Rig) -> None:
    app.goto("audio")
    page = app.page
    expect(readout(page, "mic", "M")).to_have_text(LUFS_VALUE, timeout=LOADED)
    rig.act("audio.mute", on=True)
    expect(readout(page, "mic", "M")).to_have_text("–", timeout=LOADED)
    expect(readout(page, "mic", "S")).to_have_text("–", timeout=LOADED)


def test_lufs_readout_has_explanatory_titles(app: App) -> None:
    app.goto("audio")
    titles = app.page.locator(".lufs-v").evaluate_all("(els) => els.map((e) => e.title)")
    assert sum("Momentary" in t for t in titles) == 2
    assert sum("Short-term" in t for t in titles) == 2


def stream_meters(rig: Rig, seconds: float, grace: float = 20.0) -> list[tuple[dict[str, Any], int]]:
    """Read the SSE stream for ``seconds`` (and, on a loaded runner, up to ``grace`` more until the first meters
    frame lands) and return every meters frame with its size on the wire."""
    req = urllib.request.Request(rig.base + "api/events", headers={TOKEN_HEADER: rig.token})
    frames: list[tuple[dict[str, Any], int]] = []
    start = time.monotonic()
    end = start + seconds
    event = ""
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as res:
        while time.monotonic() < end or (not frames and time.monotonic() < start + seconds + grace):
            raw = res.readline()
            if not raw:
                break
            line = raw.decode().rstrip("\n")
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:") and event == "meters":
                frames.append((json.loads(line[5:]), len(raw)))
    return frames


def test_meter_frames_carry_true_peak_loudness_and_latch(rig: Rig) -> None:
    frames = stream_meters(rig, 1.5)
    assert frames, "no meter frames arrived"
    for frame, _ in frames:
        for source in ("mic", "voice"):
            keys = {"peak_db", "sample_db", "rms_db", "clip", "latched", "lufs_m", "lufs_s"}
            assert set(frame[source]) == keys | ({"gr_db"} if source == "voice" else set())
            assert frame[source]["sample_db"] <= frame[source]["peak_db"]
    rig.act("audio.demo_clip")
    assert stream_meters(rig, 0.5)[-1][0]["mic"]["latched"] is True


def test_meter_stream_stays_within_a_sane_bandwidth(rig: Rig) -> None:
    """Snappier meters must not flood the connection: about 30 frames a second of a few hundred bytes each."""
    frames = stream_meters(rig, 3.0)
    assert 30 <= len(frames) <= 120, len(frames)  # 30 Hz, give or take what a loaded CI runner delivers
    per_second = sum(size for _, size in frames) / 3.0
    assert per_second < 20_000, f"{per_second:.0f} B/s"
