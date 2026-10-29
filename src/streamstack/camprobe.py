"""Does a browser list "StreamStack Camera" and get frames from it? (#87, #89)

What Teams, Zoom and Meet do first, done by a headless Chromium (or Edge, with `channel="msedge"`): enumerate the
cameras, find one by its label, open it with getUserMedia and watch the frames. The verdicts are plain rows
(`evaluate`) so a person, a script and CI read the same thing. `tools/camera_probe.py` is the command line.

Why the listing check matters: v4l2loopback runs with `exclusive_caps=1`, so a node nothing writes is not a camera and
Chromium does not list it (the dm313red failure of 2026-10-27, where Teams showed only the Cam Link). The row for that
case says so and names the fix.

`fake=True` swaps the real cameras for Chromium's own test device (label `fake_device_0`), which is how CI runs it: no
kernel module, no camera. It proves the probe, not the machine. The real proof is the same command on the studio PC.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_LABEL = "StreamStack Camera"
FAKE_LABEL = "fake_device_0"
FIREFOX_FAKE_LABEL = "Default Video Device"  # what Firefox calls its own test camera
MIN_FRAMES = 10
MIN_FPS = 10.0
MIN_LUMA = 8.0  # mean 8-bit luma below this is a black picture
MIN_CONTRAST = 1.0  # luma standard deviation below this is a flat picture

PROBE_URL = "http://localhost/streamstack-camera-probe"
FIREFOX_FAKE_PREFS = {"media.navigator.streams.fake": True, "media.navigator.permission.disabled": True}
FAKE_ARGS = ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"]
REAL_ARGS = ["--use-fake-ui-for-media-stream"]  # auto-accept the permission prompt only; the cameras stay real

# Runs in the page. Labels are empty until a getUserMedia has succeeded once, so it warms up with the default camera
# (opened for reading, closed at once), lists, then opens the matching camera and samples its frames.
PROBE_JS = """
async ({ pattern, flags, seconds, width, height, fps }) => {
  const out = { devices: [], matched: null, opened: false, error: null, settings: null, frames: 0, distinct: 0,
                seconds: 0, luma_mean: null, luma_std: null };
  try {
    const warm = await navigator.mediaDevices.getUserMedia({ video: true });
    warm.getTracks().forEach((t) => t.stop());
  } catch (e) {
    out.error = `${e.name}: ${e.message}`;
  }
  const all = await navigator.mediaDevices.enumerateDevices();
  out.devices = all.filter((d) => d.kind === 'videoinput').map((d) => ({ id: d.deviceId, label: d.label }));
  const wanted = new RegExp(pattern, flags);
  const hit = out.devices.find((d) => wanted.test(d.label));
  if (!hit) return out;
  out.matched = hit.label;
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: { deviceId: { exact: hit.id }, width: { ideal: width }, height: { ideal: height },
               frameRate: { ideal: fps } } });
  } catch (e) {
    out.error = `${e.name}: ${e.message}`;
    return out;
  }
  out.opened = true;
  out.error = null;
  const track = stream.getVideoTracks()[0];
  out.settings = track.getSettings();
  const video = document.createElement('video');
  video.muted = true;
  video.playsInline = true;
  video.srcObject = stream;
  await video.play();
  const canvas = document.createElement('canvas');
  canvas.width = 64; canvas.height = 36;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  const next = (cb) =>
    video.requestVideoFrameCallback ? video.requestVideoFrameCallback(cb) : requestAnimationFrame(cb);
  let last = null;
  let sum = 0, sumsq = 0, n = 0;
  const started = performance.now();
  await new Promise((resolve) => {
    const onFrame = () => {
      out.frames += 1;
      ctx.drawImage(video, 0, 0, 64, 36);
      const px = ctx.getImageData(0, 0, 64, 36).data;
      let hash = 0;
      for (let i = 0; i < px.length; i += 4) {
        const y = 0.299 * px[i] + 0.587 * px[i + 1] + 0.114 * px[i + 2];
        hash = (hash * 31 + Math.round(y)) | 0;
        sum += y; sumsq += y * y; n += 1;
      }
      if (hash !== last) out.distinct += 1;
      last = hash;
      if (performance.now() - started >= seconds * 1000) return resolve();
      next(onFrame);
    };
    next(onFrame);
    setTimeout(resolve, seconds * 1000 + 3000);  // no frames at all: do not hang
  });
  out.seconds = (performance.now() - started) / 1000;
  if (n) {
    out.luma_mean = sum / n;
    out.luma_std = Math.sqrt(Math.max(0, sumsq / n - (sum / n) ** 2));
  }
  stream.getTracks().forEach((t) => t.stop());
  return out;
}
"""


@dataclass(frozen=True)
class Row:
    id: str
    ok: bool
    title: str
    detail: str = ""
    hint: str = ""


def evaluate(
    report: dict, label: str = DEFAULT_LABEL, *, min_frames: int = MIN_FRAMES, min_fps: float = MIN_FPS
) -> list[Row]:
    """Turn the page's report into verdict rows. Stops at the first stage that failed, like a call app would."""
    listed = [d["label"] for d in report.get("devices", [])]
    seen = ", ".join(listed) if listed else "no cameras at all"
    if report.get("matched") is None:
        return [
            Row(
                "listed",
                False,
                f"{label} is not listed by the browser",
                f"the browser lists: {seen}",
                "with exclusive_caps=1 a node nothing writes is not a camera: check that the engine or the bridge that "
                "feeds it runs (streamstack doctor, video-chain row)",
            )
        ]
    rows = [Row("listed", True, f"{report['matched']} is listed", f"the browser lists: {seen}")]
    if not report.get("opened"):
        rows.append(
            Row(
                "opens",
                False,
                f"{report['matched']} could not be opened",
                report.get("error") or "",
                "another program may hold the node: streamstack loopback guard",
            )
        )
        return rows
    settings = report.get("settings") or {}
    size = f"{settings.get('width', '?')}x{settings.get('height', '?')}"
    rows.append(Row("opens", True, f"{report['matched']} opens at {size}"))
    seconds = report.get("seconds") or 0.0
    frames = int(report.get("frames") or 0)
    rate = frames / seconds if seconds > 0 else 0.0
    if frames < min_frames or rate < min_fps:
        rows.append(
            Row(
                "frames",
                False,
                f"too few frames: {frames} in {seconds:.1f} s ({rate:.1f} fps)",
                f"needs at least {min_frames} frames and {min_fps:g} fps",
                "the producer is stalled or slow: streamstack doctor",
            )
        )
        return rows
    rows.append(Row("frames", True, f"{frames} frames in {seconds:.1f} s ({rate:.1f} fps)"))
    if int(report.get("distinct") or 0) < 2:
        rows.append(
            Row(
                "moving",
                False,
                "the picture is frozen: every frame is identical",
                hint="a stopped producer or a held last frame",
            )
        )
    else:
        rows.append(Row("moving", True, f"the picture changes ({report['distinct']} distinct frames)"))
    mean, std = report.get("luma_mean"), report.get("luma_std")
    if mean is None or mean < MIN_LUMA:
        rows.append(
            Row(
                "picture",
                False,
                "the picture is black",
                f"mean luma {mean if mean is not None else 'n/a'}",
                "a covered lens, privacy mode or a slate with nothing on it",
            )
        )
    elif std is not None and std < MIN_CONTRAST:
        rows.append(
            Row("picture", False, "the picture is flat", f"luma spread {std:.1f}", "a solid colour: no camera signal")
        )
    else:
        rows.append(Row("picture", True, f"the picture has content (luma {mean:.0f}, spread {std:.0f})"))
    return rows


def passed(rows: list[Row]) -> bool:
    return all(row.ok for row in rows)


class BrowserMissing(RuntimeError):
    """The requested browser or channel is not installed here (a skip for the matrix, not a failure)."""


def fake_label(browser: str = "chromium") -> str:
    return FIREFOX_FAKE_LABEL if browser == "firefox" else FAKE_LABEL


def run(
    label: str = DEFAULT_LABEL,
    *,
    seconds: float = 3.0,
    fake: bool = False,
    channel: str | None = None,
    browser: str = "chromium",
    size: tuple[int, int] = (1280, 720),
    fps: int = 30,
) -> dict:
    """Launch the browser, run the probe page and return its raw report. Raises RuntimeError when it cannot run."""
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # the optional e2e extra
        raise RuntimeError(
            "playwright is not installed: pip install playwright && playwright install chromium"
        ) from exc
    firefox = browser == "firefox"
    if browser not in ("chromium", "firefox"):
        raise ValueError(f"unknown browser {browser!r}: chromium or firefox")
    try:
        with sync_playwright() as pw:
            if firefox:
                prefs = FIREFOX_FAKE_PREFS if fake else {"media.navigator.permission.disabled": True}
                instance = pw.firefox.launch(headless=True, firefox_user_prefs=prefs)
            else:
                args = [*(FAKE_ARGS if fake else REAL_ARGS), "--disable-dev-shm-usage"]
                instance = pw.chromium.launch(headless=True, args=args, channel=channel)
            try:
                context = instance.new_context() if firefox else instance.new_context(permissions=["camera"])
                page = context.new_page()
                # getUserMedia needs a secure context: http://localhost is one, and the route answers it offline.
                page.route(
                    PROBE_URL,
                    lambda route: route.fulfill(body="<!doctype html><title>probe</title>", content_type="text/html"),
                )
                page.goto(PROBE_URL)
                return page.evaluate(
                    PROBE_JS,
                    {
                        "pattern": re.escape(label),
                        "flags": "i",
                        "seconds": seconds,
                        "width": size[0],
                        "height": size[1],
                        "fps": fps,
                    },
                )
            finally:
                instance.close()
    except PlaywrightError as exc:
        text = str(exc)
        if "Executable doesn't exist" in text or "is not found" in text or "distribution" in text:
            raise BrowserMissing(f"{channel or browser} is not installed here") from exc
        raise RuntimeError(f"the browser could not run the probe: {exc}") from exc
