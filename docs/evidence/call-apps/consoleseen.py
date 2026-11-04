"""Show the Console's "Seen by apps" card while a browser really holds StreamStack Camera and Mic (#87 acceptance).

Usage: python -I consoleseen.py <chrome-binary> <out-dir>
Two Chromium browsers, both closed at the end. Browser A opens the two StreamStack devices as a call app does
(devicecheck.html, real devices) and stays open. Browser B opens the running Console's System page, waits for its
"Seen by apps" card to name A, and screenshots the card (and the page). The Console's address and token come from
`streamstack console --url` at run time and are never printed or written; the screenshots show the page only.
Nothing in the Console is restarted or changed.
"""

import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

chrome, out = sys.argv[1], Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
page_html = (Path(__file__).parent / "devicecheck.html").read_text()
url = subprocess.run(["streamstack", "console", "--url"], capture_output=True, text=True, check=True).stdout.strip()
system_url = url + "#/system"

with sync_playwright() as p:
    a = p.chromium.launch(executable_path=chrome, headless=True, args=["--use-fake-ui-for-media-stream"])
    b = p.chromium.launch(executable_path=chrome, headless=True)
    try:
        ctx_a = a.new_context(permissions=["camera", "microphone"])
        ctx_a.route(
            "https://devicecheck.invalid/**",
            lambda r: r.fulfill(status=200, content_type="text/html", body=page_html),
        )
        pa = ctx_a.new_page()
        pa.goto("https://devicecheck.invalid/", wait_until="domcontentloaded")
        pa.wait_for_function("window.result", timeout=40000)
        print("A holds:", pa.evaluate("window.result.tracks"), flush=True)
        pb = b.new_context(viewport={"width": 1280, "height": 1000}).new_page()
        pb.goto(system_url, wait_until="domcontentloaded", timeout=30000)
        pb.wait_for_timeout(1000)
        if "#/system" not in pb.url:
            pb.evaluate("location.hash = '#/system'")
        card = pb.locator("[data-ref=seen]")
        card.wait_for(timeout=30000)
        for _ in range(40):  # the card refreshes every few seconds
            if pb.locator(".seen-card[data-state='ok']").count():
                break
            pb.wait_for_timeout(1000)
        pb.wait_for_timeout(1500)
        print("seen note:", pb.locator("[data-ref=seenNote]").inner_text(), flush=True)
        print("cards:", [c.get_attribute("aria-label") for c in pb.locator(".seen-card").all()], flush=True)
        card.screenshot(path=str(out / "console-seen-by-apps-live.png"))
        pb.screenshot(path=str(out / "console-system-live.png"), full_page=True)
    finally:
        b.close()
        a.close()
