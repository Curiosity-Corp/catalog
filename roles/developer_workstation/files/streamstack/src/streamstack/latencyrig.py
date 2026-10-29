"""Glass-to-glass latency per path, measured with a time stamp painted into the picture (#87, #89).

Three ways to run it, each a different leg of the chain (see docs/camera-proof.md for what each number means):

- `feed` writes stamped frames into a loopback node at a steady rate (a stand-in camera: no hardware needed);
- `measure` opens a camera by label in headless Chromium, decodes the stamp in every frame and subtracts it from its own
  clock; with `feed` into a scratch node it measures loopback + browser, with `feed` into the engine's camera input and
  `measure` on StreamStack Camera it adds the engine;
- `glass` shows the stamp full-window on screen so a real camera can film it, and measures through that camera: the
  whole chain from the screen to the browser, including the display and the camera.

All of it runs on one clock (this host's), so no synchronisation is needed. The stamp codec is `latency.py`; the browser
carries the same decoder as JavaScript (`tests/e2e/test_camera_probe.py` proves they agree).

Safety: `feed` writes a video node, so it refuses the program and camera nodes, any node a process already holds, and
anything that is not a v4l2loopback node. It is meant for a scratch node (`modprobe v4l2loopback video_nr=50
card_label="Latency scratch" exclusive_caps=1`), never for /dev/video42 or /dev/video43 while a call may be running.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from streamstack import config, latency, system

DEFAULT_SIZE = (1280, 720)
DEFAULT_FPS = 30

# Both pages share this: the codec (mirror of latency.py) and nothing else.
CODEC_JS = """
const crc8 = (bytes) => {
  let c = 0;
  for (const b of bytes) {
    c ^= b;
    for (let i = 0; i < 8; i++) c = (c & 0x80) ? ((c << 1) ^ 7) & 255 : (c << 1) & 255;
  }
  return c;
};
const timeBytes = (ms) => {
  const out = [];
  let v = ms;
  for (let i = 0; i < 6; i++) { out.unshift(v % 256); v = Math.floor(v / 256); }
  return out;
};
const bitsFor = (ms) => {
  const t = timeBytes(ms);
  const bits = [1, 0, 1, 0];
  for (const byte of [...t, crc8(t)]) for (let i = 7; i >= 0; i--) bits.push((byte >> i) & 1);
  return bits;
};
const stripHeight = (h) => Math.max(8, Math.min(96, Math.floor(h / 6)));
const decodeRow = (row, width) => {
  const block = Math.floor(width / 64);
  if (block < 4) return null;
  const s = (i) => { const c = i * block + (block >> 1); return Math.floor((row[c - 1] + row[c] + row[c + 1]) / 3); };
  const hi = (s(0) + s(2)) / 2, lo = (s(1) + s(3)) / 2;
  if (hi - lo < 48) return null;
  const th = (hi + lo) / 2;
  const bits = [];
  for (let i = 0; i < 60; i++) bits.push(s(i) > th ? 1 : 0);
  if (bits[0] !== 1 || bits[1] !== 0 || bits[2] !== 1 || bits[3] !== 0) return null;
  const bytes = [];
  for (let k = 0; k < 7; k++) { let v = 0; for (let i = 0; i < 8; i++) v = v * 2 + bits[4 + k * 8 + i]; bytes.push(v); }
  const t = bytes.slice(0, 6);
  if (crc8(t) !== bytes[6]) return null;
  let ms = 0; for (const b of t) ms = ms * 256 + b;
  return ms;
};
"""

MEASURE_JS = (
    CODEC_JS
    + """
async ({ pattern, seconds }) => {
  const out = { devices: [], matched: null, error: null, samples: [], frames: 0, undecoded: 0, size: null };
  try { (await navigator.mediaDevices.getUserMedia({ video: true })).getTracks().forEach((t) => t.stop()); }
  catch (e) { out.error = `${e.name}: ${e.message}`; }
  out.devices = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === 'videoinput')
    .map((d) => ({ id: d.deviceId, label: d.label }));
  const hit = out.devices.find((d) => new RegExp(pattern, 'i').test(d.label));
  if (!hit) return out;
  out.matched = hit.label;
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ video: { deviceId: { exact: hit.id },
      width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 30 } } });
  } catch (e) { out.error = `${e.name}: ${e.message}`; return out; }
  const video = document.createElement('video');
  video.muted = true; video.srcObject = stream;
  await video.play();
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  const started = performance.now();
  await new Promise((resolve) => {
    const onFrame = () => {
      const w = video.videoWidth, h = video.videoHeight;
      if (w && h) {
        out.size = [w, h];
        canvas.width = w; canvas.height = h;
        ctx.drawImage(video, 0, 0, w, h);
        const y = Math.floor(stripHeight(h) / 2);
        const px = ctx.getImageData(0, y, w, 1).data;
        const row = new Array(w);
        for (let x = 0; x < w; x++) row[x] = 0.299 * px[x * 4] + 0.587 * px[x * 4 + 1] + 0.114 * px[x * 4 + 2];
        const sent = decodeRow(row, w);
        const now = Date.now();
        out.frames += 1;
        if (sent === null) out.undecoded += 1; else out.samples.push(now - sent);
      }
      if (performance.now() - started >= seconds * 1000) return resolve();
      video.requestVideoFrameCallback(onFrame);
    };
    video.requestVideoFrameCallback(onFrame);
    setTimeout(resolve, seconds * 1000 + 3000);
  });
  stream.getTracks().forEach((t) => t.stop());
  return out;
}
"""
)

# The screen side of a glass run: the stamp, full width, repainted every animation frame.
STAMP_PAGE = (
    """<!doctype html><meta charset="utf-8"><title>StreamStack latency stamp</title>
<style>html,body{margin:0;background:#000;overflow:hidden}canvas{display:block;width:100vw;height:30vh}
p{color:#888;font:16px sans-serif;margin:12px}</style>
<canvas id="c" width="1280" height="200"></canvas>
<p>Point the camera at the black and white strip above so it fills the picture's top. Leave this window in front.</p>
<script>"""
    + CODEC_JS
    + """
const c = document.getElementById('c'), g = c.getContext('2d');
const block = 20;
const paint = () => {
  const bits = bitsFor(Date.now());
  g.fillStyle = '#000'; g.fillRect(0, 0, c.width, c.height);
  bits.forEach((b, i) => { if (b) { g.fillStyle = '#fff'; g.fillRect(i * block, 0, block, c.height); } });
  requestAnimationFrame(paint);
};
paint();
</script>"""
)

PAGE_URL = "http://localhost/streamstack-latency-measure"
STAMP_URL = "http://localhost/streamstack-latency-stamp"
BROWSER_ARGS = ["--use-fake-ui-for-media-stream", "--disable-dev-shm-usage"]


class RigError(RuntimeError):
    """The rig refused to run, or could not."""


def refuse_unsafe_node(device: str, cfg: dict[str, str] | None = None) -> None:
    """Raise RigError unless `device` is a free v4l2loopback node that is not one the Console feeds."""
    settings = cfg if cfg is not None else config.load_settings()
    protected = {
        os.path.realpath(path)
        for path in (
            "/dev/video42",
            "/dev/video43",
            settings.get("STREAMSTACK_PROGRAM_DEVICE", ""),
            settings.get("STREAMSTACK_CAMERA_DEVICE", ""),
        )
        if path
    }
    real = os.path.realpath(device)
    if real in protected:
        raise RigError(
            f"{device} is a node the Console feeds (program or camera): the rig writes only a scratch node, "
            "for example: modprobe v4l2loopback video_nr=50 card_label='Latency scratch' exclusive_caps=1"
        )
    if not os.path.exists(real):
        raise RigError(f"{device} does not exist")
    info = system.run(["v4l2-ctl", "-d", real, "--info"])
    if info is None or info.returncode != 0:
        raise RigError(f"cannot read {device} with v4l2-ctl (is v4l-utils installed?)")
    if "loopback" not in info.stdout.lower():
        raise RigError(f"{device} is not a v4l2loopback node (a real capture device must never be written)")
    held = holders(real)
    if held:
        raise RigError(f"{device} is already held by {', '.join(held)}")


def holders(device: str) -> list[str]:
    """`name (pid)` of every other process with `device` open, read from /proc only."""
    found: list[str] = []
    for proc in Path("/proc").glob("[0-9]*"):
        if proc.name == str(os.getpid()):
            continue
        try:
            for fd in (proc / "fd").iterdir():
                if os.path.realpath(fd) == device:
                    found.append(f"{(proc / 'comm').read_text().strip()} ({proc.name})")
                    break
        except OSError:
            continue  # the process ended, or it is not ours to inspect
    return found


def ffmpeg_argv(device: str, size: tuple[int, int], fps: int) -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "rawvideo", "-pix_fmt", "yuyv422", "-video_size", f"{size[0]}x{size[1]}", "-framerate", str(fps),
        "-i", "-", "-f", "v4l2", "-pix_fmt", "yuyv422", device,
    ]  # fmt: skip


def feed(
    device: str,
    seconds: float,
    *,
    size: tuple[int, int] = DEFAULT_SIZE,
    fps: int = DEFAULT_FPS,
    stop: threading.Event | None = None,
    clock: Callable[[], float] = time.time,
) -> int:
    """Write stamped frames to a scratch loopback node for `seconds` (until `stop` is set); frames written."""
    refuse_unsafe_node(device)
    width, height = size
    base = latency.background(width, height)
    try:
        proc = subprocess.Popen(ffmpeg_argv(device, size, fps), stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise RigError(f"cannot start ffmpeg: {exc}") from exc
    written = 0
    interval = 1.0 / fps
    began = time.monotonic()
    try:
        while (stop is None or not stop.is_set()) and time.monotonic() - began < seconds:
            frame = bytearray(base)
            latency.paint_stamp(frame, width, height, int(clock() * 1000))
            assert proc.stdin is not None  # noqa: S101 - PIPE was requested above
            proc.stdin.write(latency.yuyv(bytes(frame)))
            written += 1
            time.sleep(max(0.0, began + written * interval - time.monotonic()))
    except BrokenPipeError as exc:
        err = (proc.stderr.read().decode(errors="replace").strip() if proc.stderr else "") or "no output"
        raise RigError(f"ffmpeg stopped writing {device}: {err}") from exc
    finally:
        with contextlib.suppress(OSError):
            if proc.stdin:
                proc.stdin.close()
        system.reap(proc)
    return written


def measure(label: str, seconds: float, *, channel: str | None = None) -> dict:
    """Open the camera named `label` in headless Chromium and return latency samples (ms) with a summary."""
    return _in_browser(label, seconds, channel, headed_stamp=False)


def glass(label: str, seconds: float, *, channel: str | None = None) -> dict:
    """Show the stamp on screen (headed Chromium) and measure it through the camera named `label`."""
    return _in_browser(label, seconds, channel, headed_stamp=True)


def _in_browser(label: str, seconds: float, channel: str | None, *, headed_stamp: bool) -> dict:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RigError("playwright is not installed: pip install playwright && playwright install chromium") from exc
    try:
        with sync_playwright() as pw:
            shown = pw.chromium.launch(headless=False, args=["--window-size=1300,500"]) if headed_stamp else None
            reader = pw.chromium.launch(headless=True, args=BROWSER_ARGS, channel=channel)
            try:
                if shown is not None:
                    screen = shown.new_page(viewport={"width": 1280, "height": 400})
                    screen.route(STAMP_URL, lambda r: r.fulfill(body=STAMP_PAGE, content_type="text/html"))
                    screen.goto(STAMP_URL)
                page = reader.new_context(permissions=["camera"]).new_page()
                page.route(
                    PAGE_URL,
                    lambda r: r.fulfill(body="<!doctype html><title>measure</title>", content_type="text/html"),
                )
                page.goto(PAGE_URL)
                raw = page.evaluate(MEASURE_JS, {"pattern": re.escape(label), "seconds": seconds})
            finally:
                reader.close()
                if shown is not None:
                    shown.close()
    except PlaywrightError as exc:
        raise RigError(f"the browser could not run: {exc}") from exc
    raw["label"] = label
    raw["summary"] = latency.summarize(raw["samples"])
    return raw
