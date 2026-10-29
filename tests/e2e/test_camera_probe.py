"""The camera probe and the latency stamp decoder, in a real browser with Chromium's fake camera.

No kernel module and no camera: Chromium's own test device stands in. This proves the probe lists a camera, opens it,
sees frames and says so, that it says "not listed" for a camera that is absent (the dm313red failure), and that the
JavaScript stamp decoder the latency rig runs in the browser reads exactly what the Python encoder writes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from playwright.sync_api import Playwright

from streamstack import camprobe, latency, latencyrig

REPO = Path(__file__).resolve().parents[2]
NOW = 1_795_000_123_456


def probe(label: str, seconds: float) -> dict:
    """Run the probe in its own process: the suite's session-wide Playwright driver holds an asyncio loop in this one,
    and a second sync Playwright cannot start inside it (pipeline 96925)."""
    code = (
        "import json; from streamstack import camprobe; "
        f"print(json.dumps(camprobe.run({label!r}, seconds={seconds!r}, fake=True)))"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False,
        env={**os.environ, "PYTHONPATH": str(REPO / "src")},
    )  # fmt: skip
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_probe_passes_on_a_listed_camera_that_delivers_frames() -> None:
    report = probe(camprobe.FAKE_LABEL, 2.0)
    rows = camprobe.evaluate(report, camprobe.FAKE_LABEL)
    assert camprobe.passed(rows), rows
    assert report["frames"] >= camprobe.MIN_FRAMES and report["distinct"] > 2


def test_the_probe_reports_a_camera_the_browser_does_not_list() -> None:
    report = probe("StreamStack Camera", 1.0)
    rows = camprobe.evaluate(report, "StreamStack Camera")
    assert [r.id for r in rows if not r.ok] == ["listed"]
    assert "StreamStack Camera is not listed" in rows[0].title
    assert camprobe.FAKE_LABEL in rows[0].detail  # what a call app would offer instead


def test_the_command_exits_zero_when_listed_and_one_when_not() -> None:
    def run(*extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(REPO / "tools/camera_probe.py"), "--fake", "--seconds", "1.5", *extra],
            capture_output=True, text=True, timeout=120, check=False,
        )  # fmt: skip

    good = run()
    assert good.returncode == 0, good.stdout + good.stderr
    bad = run("--label", "StreamStack Camera")
    assert bad.returncode == 1
    assert "is not listed" in bad.stdout and "fix:" in bad.stdout


@pytest.fixture
def page(playwright_driver: Playwright):
    browser = playwright_driver.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
    try:
        pg = browser.new_page()
        pg.set_content("<!doctype html><title>codec</title>")
        pg.evaluate(f"window.codec = (() => {{ {latencyrig.CODEC_JS}; return {{ decodeRow, bitsFor }}; }})()")
        yield pg
    finally:
        browser.close()


def test_the_browser_decoder_reads_what_the_python_encoder_wrote(page) -> None:
    width, height = 1280, 720
    luma = latency.background(width, height)
    latency.paint_stamp(luma, width, height, NOW)
    y = latency.strip_height(height) // 2
    row = list(luma[y * width : (y + 1) * width])
    assert page.evaluate("([row, w]) => window.codec.decodeRow(row, w)", [row, width]) == NOW


def test_the_browser_and_python_agree_on_the_bits_and_on_rejecting_damage(page) -> None:
    assert page.evaluate("(ms) => window.codec.bitsFor(ms)", NOW) == latency.bits_for(NOW)
    width = 1280
    flat = [100] * width
    assert page.evaluate("([row, w]) => window.codec.decodeRow(row, w)", [flat, width]) is None
