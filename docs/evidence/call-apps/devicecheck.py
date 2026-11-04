"""Open StreamStack Camera and StreamStack Mic in Chromium the way a call app does, and record what it got.

Usage: python -I devicecheck.py chromium <out-dir> <chrome-binary>   (Edge or Chrome work the same)
One browser, closed at the end. Real devices, no fake capture: the permission prompt is auto-granted
(--use-fake-ui-for-media-stream) and getUserMedia is pinned to the two StreamStack devices by label, as a user picks
them in an app's device menu. The page (devicecheck.html) is served from memory on a throw-away https origin, so
nothing is written or listened on. It reports the device list, the track settings, the mean brightness of a frame (so
a black picture is told apart from a live one) and the microphone level over three seconds (so digital zero is told
apart from a live signal), and screenshots the preview.
"""

import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

kind, out = sys.argv[1], Path(sys.argv[2])
chrome = sys.argv[3] if len(sys.argv) > 3 else None
out.mkdir(parents=True, exist_ok=True)

PAGE = (Path(__file__).parent / "devicecheck.html").read_text()

with sync_playwright() as p:
    if kind != "chromium":
        raise SystemExit("kind must be chromium (Firefox is firefoxcheck.py)")
    browser = p.chromium.launch(
        executable_path=chrome,
        headless=True,
        args=["--use-fake-ui-for-media-stream", "--autoplay-policy=no-user-gesture-required"],
    )
    ctx = browser.new_context(viewport={"width": 900, "height": 700}, permissions=["camera", "microphone"])
    try:
        ctx.route(
            "https://devicecheck.invalid/**", lambda r: r.fulfill(status=200, content_type="text/html", body=PAGE)
        )
        page = ctx.new_page()
        page.on("console", lambda m: print("console:", m.text[:200], flush=True))
        page.on("pageerror", lambda e: print("pageerror:", str(e)[:200], flush=True))
        page.goto("https://devicecheck.invalid/", wait_until="domcontentloaded")
        deadline = time.time() + 40
        result = None
        while time.time() < deadline:
            result = page.evaluate("window.result")
            if result:
                break
            page.wait_for_timeout(500)
        if not result:
            raise SystemExit("no result within 40 s (permission prompt or hung capture)")
        shot = out / f"{kind}-streamstack-devices.png"
        page.screenshot(path=str(shot))
        (out / f"{kind}-streamstack-devices.json").write_text(json.dumps(result, indent=1) + "\n")
        print(json.dumps(result, indent=1))
        print("version:", browser.version)
    finally:
        browser.close()
