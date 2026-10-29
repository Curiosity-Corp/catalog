"""The WebRTC (WHEP) preview on the Live page: it shows the program picture, a scene switch reaches the screen, its
latency is measured, and MJPEG takes over when WebRTC is unavailable or the picture freezes (issue #103,
docs/whep-preview.md)."""

from __future__ import annotations

import json
import os
import statistics
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from support import LOADED, SCALE, App, Rig, poll

ARTIFACTS = Path(os.environ.get("E2E_ARTIFACTS", Path(__file__).resolve().parents[2] / "e2e-artifacts"))
PROGRAM = "ss-preview[source='program']"
TARGET_MS = 1000  # the acceptance of issue #103: glass to glass under one second
ROUNDS, SWITCHES = 3, 4  # latency: the quietest of three rounds of four switches counts
# The shared CI runners are CPU-starved (infrastructure#639), so the latency figure is reported there and gated only
# where the runner is uncontended: set E2E_LATENCY_GATE=1 on such a runner (the developer machine, a dedicated pod).
GATE_LATENCY = os.environ.get("E2E_LATENCY_GATE") == "1"
SEEN_MS = int(20_000 * SCALE)  # the functional bound: a scene switch must reach the screen well inside this

# Runs in the page. Reads the program preview as a 32x18 grey signature from whichever element shows it (the <video>
# over WebRTC, the <img> after MJPEG took over), so a switch is found on screen whatever the transport. A scene's
# look is learned only once it is stable and differs from the previous one, and a switch is timed only after the
# picture is confirmed to show the scene it switches away from: a stale picture can neither fake a fast switch nor
# hide a slow one.
PICTURE = """
const host = document.querySelector("ss-preview[source='program']");
const canvas = document.createElement('canvas');
canvas.width = 32; canvas.height = 18;
const ctx = canvas.getContext('2d', { willReadFrequently: true });
const sign = () => {
  const el = host.dataset.transport === 'mjpeg' ? host.image : host.video;
  if (!el || !host.live) return null;
  ctx.drawImage(el, 0, 0, 32, 18);
  const d = ctx.getImageData(0, 0, 32, 18).data;
  const out = new Float32Array(576);
  for (let i = 0; i < 576; i++) out[i] = 0.3 * d[i * 4] + 0.59 * d[i * 4 + 1] + 0.11 * d[i * 4 + 2];
  return out;
};
const dist = (a, b) => {
  if (!a || !b) return Infinity;
  let s = 0;
  for (let i = 0; i < 576; i++) s += Math.abs(a[i] - b[i]);
  return s / 576;
};
const frame = () => new Promise((r) => requestAnimationFrame(r));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const scene = (name) => fetch('api/action', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', 'X-StreamStack-Token': token },
  body: JSON.stringify({ action: 'video.scene', name }),
}).then((res) => { if (!res.ok) throw new Error('scene ' + name + ': HTTP ' + res.status); });
// Wait for `done(signature)` on animation frames; the elapsed ms from `began`, or null at the deadline.
const until = async (began, limit, done) => {
  while (performance.now() - began < limit) {
    await frame();
    if (done(sign())) return performance.now() - began;
  }
  return null;
};
// Switch to `name` and learn its look: the picture settles (reads 400 ms apart agree) on something not like `other`.
const learn = async (name, other, limit) => {
  await scene(name);
  const began = performance.now();
  let previous = null, at = 0;
  const found = await until(began, limit, (now) => {
    if (!now || (other && dist(now, other) < 15)) { previous = null; return false; }
    if (!previous || dist(now, previous) >= 1.5) { previous = now; at = performance.now(); return false; }
    return performance.now() - at >= 400;
  });
  return found === null ? null : sign();
};
// Go to `from`, confirm the picture shows it, switch to `to` and time how long the picture takes to look like `to`.
const timeSwitch = async (looks, from, to, limit) => {
  await scene(from);
  const settled = await until(performance.now(), limit, (now) => dist(now, looks[from]) < 10);
  if (settled === null) return { ms: null, transport: host.dataset.transport || '', stage: 'from' };
  await sleep(300);
  const began = performance.now();
  await scene(to);
  const ms = await until(began, limit, (now) => dist(now, looks[to]) < Math.min(10, dist(now, looks[from])));
  return { ms, transport: host.dataset.transport || '', stage: ms === null ? 'to' : 'seen' };
};
const learnBoth = async (scenes, limit) => {
  const first = await learn(scenes[0], null, limit);
  const second = first && await learn(scenes[1], first, limit);
  if (!first || !second) throw new Error('the picture never settled on ' + scenes[first ? 1 : 0]);
  if (dist(first, second) < 15) throw new Error('the two scenes look alike; cannot time a switch');
  return { [scenes[0]]: first, [scenes[1]]: second };
};
"""

SWITCH_SCRIPT = (
    "async ({ token, scenes, switches, limit }) => {"
    + PICTURE
    + """
  const looks = await learnBoth(scenes, limit);
  const out = [];
  for (let i = 0; i < switches; i++) out.push(await timeSwitch(looks, scenes[i % 2], scenes[(i + 1) % 2], limit));
  return out;
}
"""
)

ROUNDS_SCRIPT = (
    "async ({ token, scenes, rounds, switches, limit }) => {"
    + PICTURE
    + """
  const looks = await learnBoth(scenes, limit);
  const out = [];
  for (let r = 0; r < rounds; r++) {
    const round = [];
    for (let i = 0; i < switches; i++) round.push(await timeSwitch(looks, scenes[i % 2], scenes[(i + 1) % 2], limit));
    out.push(round);
  }
  return out;
}
"""
)


def end_sessions(rig: Rig) -> None:
    """A browser context that was closed leaves its session to time out; the next test must not see it."""
    for session in rig.request("GET", "api/whep", token=rig.token)[1]["sessions"]:
        rig.request("DELETE", f"api/whep/{session['id']}", token=rig.token)


@pytest.fixture(autouse=True)
def no_leftover_sessions(rig: Rig) -> Iterator[None]:
    end_sessions(rig)
    yield
    end_sessions(rig)


@pytest.fixture
def live(app: App, rig: Rig) -> App:
    status, body = rig.request("GET", "api/whep", token=rig.token)
    assert status == 200 and body["available"], f"the e2e environment must have aiortc (pip install .[e2e]): {body}"
    return app.goto("live")


def showing_whep(page: Page) -> None:
    preview = page.locator(PROGRAM)
    expect(preview).to_have_attribute("data-transport", "whep", timeout=LOADED)
    expect(preview.locator("video")).to_be_visible()
    expect(preview.locator("img")).to_be_hidden()
    video = preview.evaluate_handle("(el) => el.video")
    assert video.evaluate("(v) => v.videoWidth > 0 && v.videoHeight > 0 && !v.paused")


def test_the_program_preview_arrives_over_webrtc(live: App, rig: Rig) -> None:
    showing_whep(live.page)
    stats = poll(rig, lambda _s: rig.request("GET", "api/whep", token=rig.token)[1]["sessions"] or None)
    assert len(stats) == 1 and stats[0]["source"] == "program"
    assert stats[0]["codec"] in ("VP8", "H264")
    live.page.locator(PROGRAM).evaluate("async (el) => { window.__whep = await el.stats(); }")
    figures = live.page.evaluate("window.__whep")
    assert figures["transport"] == "whep" and figures["codec"].startswith("video/")


def test_leaving_the_page_ends_the_session(live: App, rig: Rig) -> None:
    showing_whep(live.page)
    live.hash_to("system")
    poll(rig, lambda _s: rig.request("GET", "api/whep", token=rig.token)[1]["sessions"] == [] or None)


def program_scenes(rig: Rig) -> list[str]:
    scenes = ["Canon", "Be Right Back"]
    names = [s["name"] for s in rig.state()["video"]["scenes"]]
    assert all(s in names for s in scenes), names
    return scenes


def test_a_scene_switch_reaches_the_preview(live: App, rig: Rig) -> None:
    """Functional: every switch of the program scene shows on the preview, with a bound that a starved runner meets.

    The picture is read from whichever transport shows it, so the test holds whether WebRTC or MJPEG carries it."""
    showing_whep(live.page)
    args = {"token": rig.token, "scenes": program_scenes(rig), "switches": 6, "limit": SEEN_MS}
    seen = live.page.evaluate(SWITCH_SCRIPT, args)
    missed = [s for s in seen if s["ms"] is None]
    assert not missed, f"a scene switch never reached the screen within {SEEN_MS} ms: {seen}"


def test_a_frozen_webrtc_picture_falls_back_to_mjpeg(live: App, rig: Rig) -> None:
    """A WebRTC picture that stops moving must not stay on screen as a stale program picture: MJPEG takes over."""
    showing_whep(live.page)
    preview = live.page.locator(PROGRAM)
    preview.evaluate("(el) => el.video.pause()")  # the decoded frame count stands still, the connection stays up
    expect(preview).to_have_attribute("data-transport", "mjpeg", timeout=LOADED + 10_000)
    expect(preview.locator("img")).to_be_visible()
    seen = live.page.evaluate(
        SWITCH_SCRIPT, {"token": rig.token, "scenes": program_scenes(rig), "switches": 2, "limit": SEEN_MS}
    )
    assert all(s["ms"] is not None and s["transport"] == "mjpeg" for s in seen), seen


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


def test_preview_latency_is_measured(live: App, rig: Rig) -> None:
    """The sub-second claim of issue #103 as a measured number: reported on every run, gated where the runner is quiet.

    Like the framing cost gate, the figure that counts is the quietest of several rounds: a starved runner inflates
    rounds, it cannot make one faster than the pipeline is."""
    showing_whep(live.page)
    args = {"token": rig.token, "scenes": program_scenes(rig), "rounds": ROUNDS, "switches": SWITCHES, "limit": SEEN_MS}
    rounds = live.page.evaluate(ROUNDS_SCRIPT, args)
    over_whep = [[s["ms"] for s in r if s["ms"] is not None and s["transport"] == "whep"] for r in rounds]
    medians = [statistics.median(r) for r in over_whep if r]
    everything = [x for r in over_whep for x in r]
    report = {
        "target_ms": TARGET_MS,
        "gated": GATE_LATENCY,
        "best_round_median_ms": min(medians) if medians else None,
        "median_ms": statistics.median(everything) if everything else None,
        "p95_ms": percentile(everything, 0.95) if everything else None,
        "max_ms": max(everything) if everything else None,
        "rounds": rounds,
    }
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "whep-latency.json").write_text(json.dumps(report, indent=2))
    print(f"WHEP preview glass-to-glass latency: {json.dumps({k: v for k, v in report.items() if k != 'rounds'})}")
    assert everything or not GATE_LATENCY, f"no switch was seen over WebRTC: {rounds}"
    if GATE_LATENCY:
        assert min(medians) < TARGET_MS, f"quietest round median {min(medians):.0f} ms (rounds {rounds})"


def test_mjpeg_takes_over_when_the_console_has_no_webrtc(app: App, rig: Rig) -> None:
    page = app.page
    page.route(
        "**/api/whep?*",
        lambda route: route.fulfill(
            status=501, content_type="application/json", body='{"ok": false, "error": "aiortc is not installed"}'
        ),
    )
    app.goto("live")
    preview = page.locator(PROGRAM)
    expect(preview).to_have_attribute("data-transport", "mjpeg", timeout=LOADED)
    expect(preview.locator("img")).to_be_visible()
    expect(preview.locator("video")).to_be_hidden()
    assert page.evaluate("document.querySelector(\"ss-preview[source='program']\").live")
    assert rig.request("GET", "api/whep", token=rig.token)[1]["sessions"] == []


def test_mjpeg_takes_over_when_no_picture_arrives(app: App, rig: Rig) -> None:
    page = app.page
    # The session is created but the answer never completes the connection: the page gives up after a few seconds.
    page.add_init_script("window.RTCPeerConnection.prototype.setRemoteDescription = () => new Promise(() => {});")
    app.goto("live")
    expect(page.locator(PROGRAM)).to_have_attribute("data-transport", "mjpeg", timeout=LOADED + 10_000)
    expect(page.locator(PROGRAM).locator("img")).to_be_visible()
