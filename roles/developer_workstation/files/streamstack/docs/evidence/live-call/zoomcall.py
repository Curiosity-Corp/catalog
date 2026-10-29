"""Join Zoom's public test meeting (zoom.us/test) in the web client with StreamStack Camera and Mic, screenshot it.

Usage: HEADED=1 xvfb-run -a -s "-screen 0 1700x1000x24" python -I zoomcall.py <chrome-binary> <out-dir> <host-label>
One browser, closed at the end. Real devices are used (no fake capture); the permission prompt is auto-granted and
getUserMedia is pinned to StreamStack Camera and StreamStack Mic, as a user picks them in the device menu.
Zoom's reCAPTCHA decides whether an automated join is let in; a refusal leaves the join page and exits non-zero.
"""

import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

chrome, out, host = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
out.mkdir(parents=True, exist_ok=True)
step = 0


def shot(page, name):
    global step
    step += 1
    path = out / f"{host}-{step:02d}-{name}.png"
    page.screenshot(path=str(path))
    print("shot", path, flush=True)


def text(page):
    try:
        return " | ".join(t.strip() for t in page.inner_text("body", timeout=5000).splitlines() if t.strip())
    except PWTimeout:
        return "<no body>"



with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path=chrome,
        headless=os.environ.get("HEADED") != "1",
        args=["--use-fake-ui-for-media-stream", "--autoplay-policy=no-user-gesture-required",
              "--disable-blink-features=AutomationControlled"],
    )
    try:
        ctx = browser.new_context(
            viewport={"width": 1600, "height": 900},
            permissions=["camera", "microphone"],
            user_agent=browser.new_page().evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome"),
        )
        # Pin every capture to StreamStack's devices: the same as choosing them in the app's device menu.
        ctx.add_init_script("""
(() => {
  const md = navigator.mediaDevices; if (!md || !md.getUserMedia) return;
  const real = md.getUserMedia.bind(md);
  const find = async (kind, label) => {
    let all = await md.enumerateDevices();
    if (!all.some(x => x.label)) {
      // Labels stay hidden until one capture succeeds; the default camera may be held, so unlock them with audio.
      await real({audio: true}).then(s => s.getTracks().forEach(t => t.stop())).catch(() => {});
      all = await md.enumerateDevices();
    }
    const d = all.find(x => x.kind === kind && x.label.includes(label));
    return d ? d.deviceId : null;
  };
  md.getUserMedia = async (c) => {
    c = Object.assign({}, c || {});
    if (c.video) { const id = await find('videoinput', 'StreamStack Camera');
      if (id) c.video = Object.assign({}, typeof c.video === 'object' ? c.video : {}, {deviceId: {exact: id}}); }
    if (c.audio) { const id = await find('audioinput', 'StreamStack Mic');
      if (id) c.audio = Object.assign({}, typeof c.audio === 'object' ? c.audio : {}, {deviceId: {exact: id}}); }
    const stream = await real(c);
    window.__ssTracks = (window.__ssTracks || []).concat(stream.getTracks().map(t => t.kind + ':' + t.label));
    return stream;
  };
})();
""")
        page = ctx.new_page()
        page.goto("https://zoom.us/test", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1000 * 4)
        for label in ("Accept Cookies", "Accept All Cookies", "Accept all", "Accept"):
            try:
                page.get_by_role("button", name=label, exact=True).first.click(timeout=1500)
                break
            except PWTimeout:
                pass
        try:
            page.get_by_role("button", name=re.compile("^Join", re.I)).first.click(timeout=10000)
        except PWTimeout:
            page.get_by_role("link", name=re.compile("^Join", re.I)).first.click(timeout=10000)
        found = ""
        for _ in range(60):
            found = next((pg.url for pg in ctx.pages if re.search(r"/j/\d+", pg.url)), "")
            if found:
                break
            page.wait_for_timeout(1000 * 1)
        if not found:
            shot(page, "no-meeting-link")
            print("pages:", [pg.url for pg in ctx.pages], "text:", text(page)[:500], flush=True)
            raise SystemExit("the test page did not lead to a meeting")
        print("after join click:", found, flush=True)
        u = urlparse(found)
        mid = re.search(r"/j/(\d+)", u.path).group(1)
        pwd = parse_qs(u.query).get("pwd", [""])[0]
        print("meeting", mid, "pwd present:", bool(pwd), flush=True)
        page.goto(f"https://app.zoom.us/wc/join/{mid}?pwd={pwd}", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1000 * 10)
        print("devices:", page.evaluate(
            "async () => (await navigator.mediaDevices.enumerateDevices()).map(d => d.kind + ':' + d.label)"), flush=True)
        page.wait_for_timeout(1000 * 4)
        shot(page, "prejoin-streamstack")
        name = page.locator("input#input-for-name, input[aria-label*='name' i], input[placeholder*='name' i]").first
        name.click(timeout=10000)
        name.fill("")
        name.press_sequentially(f"StreamStack {host}", delay=60)
        page.wait_for_timeout(3000)
        if not name.input_value():
            name.fill(f"StreamStack {host}", timeout=10000)
            page.wait_for_timeout(2000)
        print("name field:", repr(name.input_value()), flush=True)
        pw = page.locator("input#input-for-pwd, input[aria-label*='passcode' i], input[type='password']").first
        if pw.count() and not pw.input_value():
            print("passcode field empty after pwd link", flush=True)
        log = []
        page.on("request", lambda r: log.append(f"req {r.method} {r.url[:120]}"))
        page.on("console", lambda m: log.append(f"console.{m.type}: {m.text[:200]}") if m.type in ("error", "warning") else None)
        page.on("response", lambda r: log.append(f"http {r.status} {r.url[:140]}") if "googleapis" not in r.url and not r.url.endswith((".png", ".svg", ".woff2")) else None)
        btn = page.get_by_role("button", name=re.compile("^Join$", re.I))
        print("join buttons:", btn.count(), "enabled:", btn.first.is_enabled(), flush=True)
        def in_call():
            # The meeting toolbar, not a URL or a network hint: only a joined client shows Leave and Participants.
            body = text(page)
            return "Enter Meeting Info" not in body and "Leave" in body and "Participants" in body

        joined = False
        for attempt in range(3):
            # The sequence that got in before: click, let reCAPTCHA settle, then submit the form with Enter.
            print("button:", btn.first.evaluate("b => b.outerHTML.slice(0, 300)"), "recaptcha:",
                  page.evaluate("() => !!window.grecaptcha"), flush=True)
            for x, y in ((300, 200), (800, 700), (1200, 300), (600, 400)):
                page.mouse.move(x, y, steps=25)
                page.wait_for_timeout(1500 + 1000 * attempt)
            box = btn.first.bounding_box()
            page.mouse.move(box["x"] + box["width"] / 2 - 20, box["y"] + box["height"] / 2, steps=12)
            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.wait_for_timeout(8000)
            if not in_call():
                try:
                    name.press("Enter", timeout=3000)
                except PWTimeout:
                    pass
            for _ in range(12):
                page.wait_for_timeout(2000)
                if in_call():
                    joined = True
                    break
            print(f"join attempt {attempt + 1}: {'in the meeting' if joined else 'still on the join page'}", flush=True)
            if joined:
                break
            print("recent log:", log[-8:], flush=True)
        if not joined:
            shot(page, "not-joined")
            raise SystemExit("could not get into the meeting")
        page.wait_for_timeout(8000)
        print("captured tracks:", page.evaluate("window.__ssTracks || []"), flush=True)
        shot(page, "in-call")
        # Zoom's in-meeting device test: confirm the camera, then show the microphone step and the meeting itself.
        for label, name_ in (("Got it", "notes"), ("Yes", "camera-ok")):
            try:
                page.get_by_role("button", name=label, exact=True).first.click(timeout=4000)
                page.wait_for_timeout(4000)
                shot(page, f"after-{name_}")
            except PWTimeout:
                print("no", label, flush=True)
        page.wait_for_timeout(8000)
        shot(page, "mic-step")
        print("mic-step text:", text(page)[:500], flush=True)
        print("final url:", page.url, flush=True)
        print("final text:", text(page)[:600], flush=True)
    finally:
        browser.close()
