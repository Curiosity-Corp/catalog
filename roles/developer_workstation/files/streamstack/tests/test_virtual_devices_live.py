"""StreamStack Camera and StreamStack Mic through a real browser: enumerateDevices, getUserMedia and a WebRTC loop.

What this proves, on a host where the v4l2loopback module is loaded with the StreamStack configuration
(packaging/modprobe.d) and this user may open /dev/video43: a feed in the engine's format makes the node list as
"StreamStack Camera" in Chromium, a 1280x720 capture opens and carries a real (not black) picture, and the picture
survives an RTCPeerConnection. If the voice chain is running, "StreamStack Mic" must list and open as well.

What it cannot prove: Teams and Zoom themselves (they need accounts; evidence in docs/evidence/virtual-devices), a call
with another person, or a physical camera. On CI runners and on any host without the module, the file is SKIPPED with
the reason; it never fails for a missing device. It refuses to touch a node somebody has open (a call in progress).
"""

from __future__ import annotations

import functools
import http.server
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from streamstack import config, system, virtualdevices

# The autouse unit fixture moves HOME; the browsers live under the real one. Resolve that before any test runs.
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache" / "ms-playwright"))

FIXTURES = Path(__file__).parent / "fixtures" / "virtual_devices"
DEVICE = config.DEFAULTS["STREAMSTACK_PROGRAM_DEVICE"]
LABEL = config.DEFAULTS["STREAMSTACK_PROGRAM_LABEL"]


def holders(device: str) -> list[int]:
    """Pids that have `device` open, as far as this user can see."""
    target = os.path.realpath(device)
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if any(os.readlink(fd) == target for fd in (entry / "fd").iterdir()):
                found.append(int(entry.name))
        except OSError:
            continue
    return found


@pytest.fixture(scope="module")
def feed() -> Iterator[str]:
    if system.video_name(DEVICE) != LABEL:
        pytest.skip(f"{DEVICE} is not {LABEL!r}: the v4l2loopback module is not loaded with the StreamStack options")
    if not virtualdevices.can_open(DEVICE):
        pytest.skip(f"this user cannot open {DEVICE} (needs the video group; see docs/adr/0004)")
    if shutil.which("ffmpeg") is None or shutil.which("v4l2-ctl") is None:
        pytest.skip("ffmpeg and v4l2-ctl are needed to feed and inspect the node")
    if busy := holders(DEVICE):
        pytest.skip(f"{DEVICE} is open in process(es) {busy}: not disturbing a call or the engine")
    proc = subprocess.Popen(
        [
            *("ffmpeg", "-nostdin", "-loglevel", "error", "-re", "-f", "lavfi"),
            *("-i", "testsrc2=size=1920x1080:rate=30", "-pix_fmt", "yuyv422", "-f", "v4l2", DEVICE),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not system.loopback_has_producer(DEVICE):
            if proc.poll() is not None:
                pytest.skip(f"ffmpeg could not write {DEVICE}: {(proc.stderr.read() if proc.stderr else '')[:200]}")
            time.sleep(0.2)
        assert system.loopback_has_producer(DEVICE), "the node never advertised Video Capture"
        yield DEVICE
    finally:
        system.reap(proc)


def test_the_node_offers_what_call_apps_take(feed: str) -> None:
    report = virtualdevices.camera_report(feed, LABEL)
    assert report.name == LABEL
    assert report.exclusive_caps is True
    assert ("YUYV", 1920, 1080, 30.0) in report.modes
    assert virtualdevices.camera_checks(report)[-1]["state"] == "ok"


@pytest.fixture(scope="module")
def page_url() -> Iterator[str]:

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return None

    handler = functools.partial(Quiet, directory=str(FIXTURES))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/loopback.html"
    finally:
        server.shutdown()


def test_chromium_lists_opens_and_loops_the_camera(feed: str, page_url: str) -> None:
    sync_api = pytest.importorskip("playwright.sync_api", reason="playwright is not installed")
    mic_up = virtualdevices.mic_report().present
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(args=["--use-fake-ui-for-media-stream"])
        except sync_api.Error as exc:
            pytest.skip(f"no Chromium to drive: {str(exc)[:120]}")
        try:
            context = browser.new_context(permissions=["camera", "microphone"])
            page = context.new_page()
            page.goto(page_url)
            page.wait_for_function("window.result && window.result.done", timeout=60_000)
            result = page.evaluate("window.result")
        finally:
            browser.close()
    assert result["error"] is None, result["error"]
    listed = {(d["kind"], d["label"]) for d in result["devices"]}
    assert ("videoinput", LABEL) in listed
    camera = result["camera"]
    assert (camera["label"], camera["width"], camera["height"], camera["state"]) == (LABEL, 1280, 720, "live")
    assert camera["fps"] >= 24
    assert camera["contrast"] > 50, "the picture is flat: black or a single colour"
    assert result["rtc"]["state"] == "connected" and result["rtc"]["frames"] > 0
    if mic_up:
        assert ("audioinput", "StreamStack Mic") in listed
        assert result["mic"]["label"] == "StreamStack Mic"
