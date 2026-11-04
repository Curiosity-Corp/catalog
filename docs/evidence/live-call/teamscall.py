"""Two-party Microsoft Teams call: dm313red (StreamStack Camera + Mic) and a guest with fake media. Screenshots and getStats.

Usage:
  BW_ITEM=<bitwarden item id of the organiser's Microsoft login> \
  HEADED=1 xvfb-run -a -s "-screen 0 1600x1000x24" python -I teamscall.py <profile-dir> <out-dir> <host-label>

Party A is the organiser, signed in to Teams web in a persistent Chromium profile (<profile-dir>), with real devices:
getUserMedia is pinned to StreamStack Camera and StreamStack Mic by label (labels are unlocked with an audio capture first),
as a user picks them in the device menu. Party B is a second Chromium with Chromium's fake media devices that joins the
same meeting as an anonymous guest and is admitted from the lobby by A. The two browsers are the only ones open, and both
are closed at the end. The meeting link is created with Teams "Meet now" and is only ever held in memory: it is never
printed, written to the out dir or put in a screenshot. If the profile is not signed in, the login runs from Bitwarden
(`bw get`, BW_SESSION from the environment) with the values piped into the form fields and never printed. The page's
WebAuthn is hidden so Entra falls back to password and a TOTP code.

Proof of delivery is B's WebRTC getStats: inbound video framesDecoded and inbound audio totalAudioEnergy, sampled twice.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

profile, out, host = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
out.mkdir(parents=True, exist_ok=True)
HEADED = os.environ.get("HEADED") == "1"
BW_ITEM = os.environ.get("BW_ITEM", "")

HOOK = """
(() => {
  try { delete window.PublicKeyCredential; } catch (e) {}
  const Orig = window.RTCPeerConnection; if (!Orig) return;
  window.__pcs = [];
  window.RTCPeerConnection = function (...a) { const pc = new Orig(...a); window.__pcs.push(pc); return pc; };
  window.RTCPeerConnection.prototype = Orig.prototype;
  Object.setPrototypeOf(window.RTCPeerConnection, Orig);
})();
"""

PIN = """
(() => {
  const md = navigator.mediaDevices; if (!md || !md.getUserMedia) return;
  const real = md.getUserMedia.bind(md);
  const find = async (kind, label) => {
    let all = await md.enumerateDevices();
    if (!all.some(x => x.label)) {
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
    let stream;
    for (let attempt = 0; attempt < 6; attempt++) {
      try {
        // Chromium sets the format (VIDIOC_S_FMT) on open, which v4l2loopback refuses with EBUSY while the Console's
        // live-preview ffmpeg streams from the node. Release that reader, then open at once; once Chromium holds the
        // node the preview can come back beside it.
        if (c.video) await window.__ssFree();
        stream = await real(c); break;
      } catch (e) { window.__ssErr = (window.__ssErr || []).concat(e.name + ': ' + e.message); if (!c.video || attempt === 5) throw e; }
    }
    window.__ssTracks = (window.__ssTracks || []).concat(stream.getTracks().map(t => t.kind + ':' + t.label));
    return stream;
  };
})();
"""

STATS = """
async () => {
  const r = {pcs: (window.__pcs || []).length, inboundVideo: [], inboundAudio: [], outboundVideo: [], outboundAudio: []};
  for (const pc of window.__pcs || []) {
    let rep; try { rep = await pc.getStats(); } catch (e) { continue; }
    rep.forEach(s => {
      if (s.type === 'inbound-rtp' && s.kind === 'video') r.inboundVideo.push({framesReceived: s.framesReceived, framesDecoded: s.framesDecoded,
        frameWidth: s.frameWidth, frameHeight: s.frameHeight, bytesReceived: s.bytesReceived, packetsReceived: s.packetsReceived});
      if (s.type === 'inbound-rtp' && s.kind === 'audio') r.inboundAudio.push({totalAudioEnergy: s.totalAudioEnergy, audioLevel: s.audioLevel,
        totalSamplesDuration: s.totalSamplesDuration, bytesReceived: s.bytesReceived, packetsReceived: s.packetsReceived});
      if (s.type === 'outbound-rtp' && s.kind === 'video') r.outboundVideo.push({framesSent: s.framesSent, frameWidth: s.frameWidth,
        frameHeight: s.frameHeight, bytesSent: s.bytesSent});
      if (s.type === 'outbound-rtp' && s.kind === 'audio') r.outboundAudio.push({bytesSent: s.bytesSent, packetsSent: s.packetsSent});
    });
  }
  return r;
}
"""


PREVIEW_ARGV = ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "v4l2", "-i", "/dev/video43"]


def free_preview(*_):
    """Stop the Console's live-preview reader on /dev/video43 (matched by its exact argv, by PID; the Console restarts it)."""
    stopped = 0
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            argv = (d / "cmdline").read_bytes().split(b"\0")[:-1]
        except OSError:
            continue
        if [a.decode(errors="replace") for a in argv[:8]] == PREVIEW_ARGV:
            try:
                os.kill(int(d.name), 9)
                stopped += 1
            except OSError as e:
                print("free_preview:", e, flush=True)
    return stopped


def log(*a):
    print(*a, flush=True)


def shot(page, name):
    path = out / f"{host}-teams-{name}.png"
    page.screenshot(path=str(path))
    log("shot", path.name)


def body(page):
    try:
        return " | ".join(t.strip() for t in page.inner_text("body", timeout=4000).splitlines() if t.strip())
    except (PWTimeout, PWError):
        return ""


def bw(field):
    return subprocess.run(["bw", "get", field, BW_ITEM], capture_output=True, text=True, check=True).stdout.strip()


def sign_in(page):
    """Drive the Microsoft login to the Teams shell; secrets go straight into the fields and are never printed."""
    page.goto("https://teams.cloud.microsoft/v2/", wait_until="domcontentloaded", timeout=60000)
    for _ in range(24):
        page.wait_for_timeout(4000)
        text = body(page)
        if "login" not in page.url and "teams.cloud.microsoft" in page.url and "Calendar" in text:
            return
        if "Verify your identity" in text:
            page.get_by_text("Use a verification code").first.click()
        elif "Pick an account" in text:
            page.locator("[data-test-id], .table-row, div[role=button]").filter(has_text="@").first.click()
        elif page.locator("input[type=email]:visible").count():
            page.fill("input[type=email]", bw("username"))
            page.get_by_role("button", name="Next").click()
        elif page.locator("input[type=password]:visible").count():
            page.fill("input[type=password]", bw("password"))
            page.get_by_role("button", name="Sign in", exact=True).click()
        elif page.locator("input[name=otc]:visible").count():
            page.fill("input[name=otc]", bw("totp"))
            page.get_by_role("button", name="Verify").click()
        elif "Stay signed in" in text:
            page.get_by_role("button", name="Yes").click()
    raise SystemExit("could not sign in to Teams")


def calendar_frame(page):
    return next((f for f in page.frames if f.get_by_text("Get a link to share").count()), None)


def meet_now_link(page):
    """Teams web > Calendar > Meet now > Get a link to share. Returns the join URL (kept in memory only)."""
    page.goto("https://teams.cloud.microsoft/v2/", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(12000)
    page.get_by_role("button", name="Calendar").first.click()
    page.wait_for_timeout(8000)
    page.mouse.click(1290, 78)  # the Meet now button lives in the calendar's iframe, so click it where it is drawn
    fr = None
    for _ in range(20):
        fr = calendar_frame(page)
        if fr:
            break
        page.wait_for_timeout(1000)
    if not fr:
        shot(page, "debug-no-meetnow-dialog")
        raise SystemExit("Meet now dialog did not open")
    fr.get_by_role("textbox").first.fill("StreamStack call test")
    fr.get_by_text("Get a link to share").first.click()
    page.wait_for_timeout(5000)
    for el in fr.locator("input, textarea").all():
        v = el.input_value()
        if v.startswith("https://"):
            return v
    raise SystemExit("no meeting link in the dialog")


def click_any(page, labels, timeout=1500):
    for label in labels:
        for loc in (page.get_by_role("button", name=label), page.get_by_role("link", name=label)):
            try:
                loc.first.click(timeout=timeout)
                return label
            except (PWTimeout, PWError):
                continue
    return None


def join(page, who, guest_name=None):
    """From the join link to inside the call: launcher, name, prejoin, Join now."""
    for _ in range(40):
        page.wait_for_timeout(3000)
        text = body(page)
        if re.search(r"\bLeave\b", text) and ("Chat" in text or "People" in text) and "Join now" not in text:
            return True
        if guest_name:
            nm = page.locator("input[placeholder*='name' i], input[data-tid*='name' i]")
            if nm.count() and nm.first.is_visible() and not nm.first.input_value():
                nm.first.click()
                nm.first.press_sequentially(guest_name, delay=50)
        step = click_any(page, ["Continue on this browser", "Join now", "Join", "Allow", "Got it", "Dismiss", "Continue without audio or video"])
        log(who, "step:", step, "|", text[:120])
    return False


def main():
    with sync_playwright() as p:
        common = ["--autoplay-policy=no-user-gesture-required", "--use-fake-ui-for-media-stream",
                  "--disable-blink-features=AutomationControlled"]
        ctx_a = p.chromium.launch_persistent_context(
            profile, headless=not HEADED, viewport={"width": 1500, "height": 900},
            permissions=["camera", "microphone"], args=common)
        browser_b = ctx_b = None
        summary = {"party_a": "dm313red, signed in, StreamStack Camera + StreamStack Mic", "party_b": "anonymous guest, Chromium fake media"}
        try:
            ctx_a.expose_function("__ssFree", free_preview)
            ctx_a.add_init_script(HOOK)
            ctx_a.add_init_script(PIN)
            a = ctx_a.pages[0] if ctx_a.pages else ctx_a.new_page()
            sign_in(a)
            link = meet_now_link(a)
            log("meeting link created (not printed)")
            a.goto(link, wait_until="domcontentloaded", timeout=60000)
            if not join(a, "A"):
                shot(a, "a-join-failed")
                raise SystemExit("A could not join")
            # Teams joins with camera off and the mic as set at prejoin: turn the camera on and unmute, by the buttons' own labels.
            for _ in range(2):
                labels = a.evaluate("[...document.querySelectorAll('button')].map(b => b.getAttribute('aria-label') || '').filter(l => /camera|mic/i.test(l))")
                log("A toolbar labels:", labels)
                if any(re.search(r"turn camera on|camera is off|start (my )?(video|camera)", l, re.I) for l in labels):
                    a.get_by_role("button", name=re.compile(r"turn camera on|camera is off", re.I)).first.click(timeout=4000)
                if any(re.search(r"unmute|mic is (off|muted)|turn mic on", l, re.I) for l in labels):
                    a.get_by_role("button", name=re.compile(r"unmute|mic is (off|muted)|turn mic on", re.I)).first.click(timeout=4000)
                a.wait_for_timeout(6000)
            log("A gUM errors:", a.evaluate("window.__ssErr || []"))
            log("A captured tracks:", a.evaluate("window.__ssTracks || []"))
            summary["a_captured_tracks"] = a.evaluate("window.__ssTracks || []")
            a.wait_for_timeout(5000)
            shot(a, "a-in-call")

            browser_b = p.chromium.launch(headless=not HEADED, args=common + ["--use-fake-device-for-media-stream"])
            ctx_b = browser_b.new_context(viewport={"width": 1500, "height": 900}, permissions=["camera", "microphone"])
            ctx_b.add_init_script(HOOK)
            b = ctx_b.new_page()
            b.goto(link, wait_until="domcontentloaded", timeout=60000)
            admitted = False
            people_open = False
            for _ in range(60):
                b.wait_for_timeout(3000)
                click_any(b, ["Continue on this browser"])
                nm = b.locator("input[placeholder*='name' i]")
                if nm.count() and nm.first.is_visible() and not nm.first.input_value():
                    nm.first.click()
                    nm.first.press_sequentially("StreamStack guest", delay=50)
                    shot(b, "b-prejoin")
                click_any(b, ["Join now"])
                tb = body(b)
                if re.search(r"\bLeave\b", tb) and "Join now" not in tb and "waiting" not in tb.lower() and "someone in the meeting" not in tb.lower():
                    admitted = True
                    break
                if "lobby" in tb.lower() or "let you in" in tb.lower() or "waiting" in tb.lower():
                    if not (out / f"{host}-teams-b-lobby.png").exists():
                        shot(b, "b-lobby")
                    adm = click_any(a, ["Admit", "Admit all", "View lobby"], timeout=800)
                    if not adm and not people_open:
                        click_any(a, ["People"], timeout=1500)
                        people_open = True
                        a.wait_for_timeout(2000)
                        shot(a, "a-lobby-admit")
                        log("A labels:", a.evaluate("[...document.querySelectorAll('button')].map(b => b.getAttribute('aria-label') || b.innerText).filter(Boolean).slice(0, 40)"))
                    log("lobby: A admit click:", adm)
            summary["b_admitted"] = admitted
            log("B admitted:", admitted)
            if not admitted:
                shot(b, "b-not-admitted")
                shot(a, "a-not-admitted")
                raise SystemExit("B never got into the call")
            a.wait_for_timeout(20000)
            s1 = b.evaluate(STATS)
            b.wait_for_timeout(8000)
            s2 = b.evaluate(STATS)
            sa = a.evaluate(STATS)
            shot(b, "b-in-call-receiving-a")
            shot(a, "a-in-call-with-b")
            summary["b_inbound_t0"], summary["b_inbound_t1"] = s1, s2
            summary["a_outbound"] = {"video": sa["outboundVideo"], "audio": sa["outboundAudio"]}
            iv0 = (s1["inboundVideo"] or [{}])[0]
            iv1 = (s2["inboundVideo"] or [{}])[0]
            ia1 = (s2["inboundAudio"] or [{}])[0]
            summary["verdict"] = {
                "video_framesDecoded": iv1.get("framesDecoded"),
                "video_frames_growing": (iv1.get("framesDecoded") or 0) > (iv0.get("framesDecoded") or 0),
                "video_resolution": f"{iv1.get('frameWidth')}x{iv1.get('frameHeight')}",
                "audio_totalAudioEnergy": ia1.get("totalAudioEnergy"),
                "audio_audioLevel": ia1.get("audioLevel"),
            }
            log(json.dumps(summary["verdict"]))
            # Leave, then end the meeting for everyone: B leaves first, A last.
            for pg_, who in ((b, "B"), (a, "A")):
                try:
                    pg_.get_by_role("button", name=re.compile(r"^Leave", re.I)).first.click(timeout=6000)
                    pg_.wait_for_timeout(1500)
                    click_any(pg_, ["End meeting", "Leave"], timeout=2500)
                    pg_.wait_for_timeout(3000)
                    log(who, "left")
                except (PWTimeout, PWError) as e:
                    log(who, "leave click failed:", type(e).__name__)
        finally:
            (out / f"{host}-teams-getstats.json").write_text(json.dumps(summary, indent=2) + "\n")
            for closer in (ctx_b, browser_b, ctx_a):
                try:
                    closer and closer.close()
                except PWError as e:
                    log("close:", e)


main()
