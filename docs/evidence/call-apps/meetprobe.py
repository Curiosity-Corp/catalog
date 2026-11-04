"""What Google Meet shows without a Google account (#87, Meet rung b). Nothing is typed, nothing is signed in.

Usage: python -I meetprobe.py <chrome-binary> <out-dir>
One Chromium, closed at the end. Opens meet.google.com/landing, /new and a made-up meeting code, prints where each
ends up and the page text, and screenshots the three. It does not open the camera or microphone: the point is whether
Meet's device check is reachable at all without signing in.
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

chrome, out = sys.argv[1], Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=chrome, headless=True, args=["--use-fake-ui-for-media-stream"])
    try:
        ctx = browser.new_context(viewport={"width": 1280, "height": 800}, permissions=["camera", "microphone"])
        page = ctx.new_page()
        for name, url in (
            ("landing", "https://meet.google.com/landing"),
            ("new", "https://meet.google.com/new"),
            ("made-up-code", "https://meet.google.com/abc-defg-hij"),
        ):
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(6000)
            final = page.url.split("?")[0]
            body = " | ".join(t.strip() for t in page.inner_text("body").splitlines() if t.strip())
            print(f"{url} -> {final}\n   {body[:220]}")
            page.screenshot(path=str(out / f"meet-anonymous-{name}.png"))
    finally:
        browser.close()
