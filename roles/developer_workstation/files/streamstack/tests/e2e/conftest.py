"""Fixtures for the Console end-to-end suite.

One demo server for the whole session (``streamstack console --demo --no-window --port 0`` in a subprocess with an
isolated HOME/XDG tree), one browser per engine, a fresh context per test, and an autouse reset that puts the shared
rig back to its starting state, so tests never depend on each other's leftovers.

Run it with ``make e2e`` or ``pytest tests/e2e`` (see README). Needs ``playwright`` and its browsers.
"""

from __future__ import annotations

import os
import queue
import re
import site
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, expect, sync_playwright

from support import ACTION_TIMEOUT, EXPECT_TIMEOUT, App, DiskProbe, Rig, poll

REPO = Path(__file__).resolve().parents[2]
ARTIFACTS = Path(os.environ.get("E2E_ARTIFACTS", REPO / "e2e-artifacts"))
URL_LINE = re.compile(r"StreamStack Console: (http://127\.0\.0\.1:\d+/)")
TOKEN_QUERY = re.compile(r"\?token=([^#\s]+)")
STARTUP_SECONDS = 30
ALL_BROWSERS = ("chromium", "firefox")
DESKTOP = {"width": 1280, "height": 720}
# The demo rig's lighting engine stands still at this moment, in New York: the daylight schedule depends on the hour
# and the place, and a run must not depend on when it ran or on the zone of the host or the CI runner.
# `scripts/e2e_lighting_soak.py` sets E2E_DEMO_CLOCK to run the lighting suites at several hours.
DEMO_CLOCK = "2026-06-21T12:00:00-04:00"

# Page JS: collect CSP violations before any page script runs.
INIT_SCRIPT = """
window.__csp = [];
document.addEventListener('securitypolicyviolation', (e) => {
  window.__csp.push(`${e.violatedDirective} ${e.blockedURI}`);
});
// The event stream's life, for the failure report (issue #161): when it opened, failed, and last pushed a state.
window.__sse = { opened: 0, errors: 0, frames: 0, last: null, log: [] };
if (window.EventSource) {
  const Native = window.EventSource;
  window.EventSource = class extends Native {
    constructor(url, init) {
      super(url, init);
      const note = (what) => {
        const sse = window.__sse;
        sse.log.push(`${Math.round(performance.now())} ms ${what}`);
        if (sse.log.length > 40) sse.log.shift();
      };
      this.addEventListener('open', () => { window.__sse.opened += 1; note('open'); });
      this.addEventListener('error', () => { window.__sse.errors += 1; note('error'); });
      this.addEventListener('state', () => {
        window.__sse.frames += 1;
        window.__sse.last = Math.round(performance.now());
      });
    }
  };
}
"""


PROBES: list[DiskProbe] = []


def pytest_configure(config: pytest.Config) -> None:
    expect.set_options(timeout=EXPECT_TIMEOUT)
    config.addinivalue_line("markers", "cross_browser: also run this test in Firefox (the smoke subset)")


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "browser_name" not in metafunc.fixturenames:
        return
    wanted = [b for b in os.environ.get("E2E_BROWSERS", ",".join(ALL_BROWSERS)).split(",") if b]
    names = wanted if metafunc.definition.get_closest_marker("cross_browser") else wanted[:1] or ["chromium"]
    metafunc.parametrize("browser_name", names, scope="session")


@pytest.fixture(autouse=True)
def isolated_home() -> None:
    """Override the unit-test fixture: the browsers must keep the real HOME to find their binaries."""
    return None


# ---------------------------------------------------------------------------
# The rig
# ---------------------------------------------------------------------------


def _pump(stream, sink: "queue.Queue[str | None]", log: list[str]) -> None:  # noqa: UP037
    for line in iter(stream.readline, ""):
        log.append(line.rstrip())
        sink.put(line)
    sink.put(None)


@pytest.fixture(scope="session")
def rig(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Rig]:
    home = tmp_path_factory.mktemp("home")
    runtime = Path(tmp_path_factory.mktemp("run"))
    runtime.chmod(0o700)
    env = {
        **os.environ,
        "HOME": str(home),
        # the isolated HOME hides `pip install --user` packages (CI installs the e2e extras that way): keep the real
        # user site so the demo server can import the extras (aiortc for the WebRTC preview)
        "PYTHONUSERBASE": os.environ.get("PYTHONUSERBASE") or site.getuserbase(),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_RUNTIME_DIR": str(runtime),
        "PYTHONPATH": os.pathsep.join(filter(None, [str(REPO / "src"), os.environ.get("PYTHONPATH", "")])),
        "PYTHONUNBUFFERED": "1",
        "NO_COLOR": "1",
        "STREAMSTACK_DEMO_CLOCK": os.environ.get("E2E_DEMO_CLOCK", DEMO_CLOCK),
        # one log line per action (name, answer, milliseconds): the failure report quotes them (issue #161)
        "STREAMSTACK_CONSOLE_ACTION_LOG": "1",
    }
    env.pop("STREAMSTACK_ENV_FILE", None)
    cmd = [sys.executable, "-m", "streamstack", "console", "--demo", "--no-window", "--port", "0"]
    proc = subprocess.Popen(
        cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    log: list[str] = []
    # the demo server writes under `home`: time fsyncs there for the whole run, so a stall can be pinned on the disk
    probe = DiskProbe(home).start()
    PROBES.append(probe)
    lines: queue.Queue[str | None] = queue.Queue()
    threading.Thread(target=_pump, args=(proc.stdout, lines, log), daemon=True).start()
    try:
        found = None
        while found is None:
            try:
                line = lines.get(timeout=STARTUP_SECONDS)
            except queue.Empty:
                pytest.fail(f"console did not start within {STARTUP_SECONDS}s:\n" + "\n".join(log))
            if line is None:
                pytest.fail(f"console exited ({proc.poll()}) before it was ready:\n" + "\n".join(log))
            found = URL_LINE.search(line)
        # The log line carries no token (it lands in the journal); `console --url` prints the tokenised address.
        shown = subprocess.run(
            [sys.executable, "-m", "streamstack", "console", "--url"],
            env=env,
            capture_output=True,
            text=True,
            timeout=STARTUP_SECONDS,
            check=False,
        )
        token = TOKEN_QUERY.search(shown.stdout)
        if shown.returncode != 0 or token is None:
            pytest.fail(f"`console --url` gave no token (exit {shown.returncode}): {shown.stderr.strip()}")
        server = Rig(base=found.group(1), token=token.group(1), log=log, home=home, probe=probe)
        server.state()  # ready: the API answers with the token
        yield server
    finally:
        probe.stop()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


@pytest.fixture(scope="session")
def rig_baseline(rig: Rig) -> dict:
    """The facts the reset restores: first scene, default preset and so on."""
    state = rig.state()
    return {
        "scene": state["video"]["program_scene"],
        "preset": state["audio"]["chain"]["preset"],
        "virtualcam": state["video"]["virtualcam"],
        "auto": state["auto"]["running"],
        "source": state["video"]["selected_source"],
    }


@pytest.fixture(autouse=True)
def reset_rig(request: pytest.FixtureRequest) -> Iterator[None]:
    """Put the shared demo rig back as it started, before and after each browser test."""
    if "rig" not in request.fixturenames:
        yield
        return
    rig: Rig = request.getfixturevalue("rig")
    base: dict = request.getfixturevalue("rig_baseline")

    def reset() -> None:
        # Auto and studio actions finish a moment later in the demo: never start from (or leave) a busy rig.
        s = poll(
            rig,
            lambda st: (
                not st["auto"]["busy"]
                and not st["studio"]["busy"]
                and st["audio"]["mic_check"]["state"] not in ("recording-raw", "recording-processed")
                and st["audio"]["mic"]["revive"]["state"] != "reviving"
                and st["audio"]["outputs"]["test"]["state"] != "playing"
                and st
            ),
        )
        rig.act("hotkeys.demo.reset")
        rig.act("meetings.demo.reset")
        rig.act("deck.reset")  # the default profile on the simulated 4x4 deck, first page
        rig.act("slots.demo.reset")  # no time-of-day profiles, the demo clock back where it started
        if s["hardware"]["host"] != "dm313red":
            rig.act("hardware.demo_host", host="dm313red")  # the recorded studio the demo starts with
        if not s["studio"]["running"]:
            rig.act("studio.start")
            poll(rig, lambda st: st["studio"]["running"] and not st["studio"]["busy"])
        if s["video"]["privacy"]:
            rig.act("video.privacy", on=False)
        if s["audio"]["mic"].get("stalled"):
            rig.act("audio.demo_stall", on=False)
        rig.act("audio.demo_check_hold", on=False)
        rig.act("audio.clip_clear")
        fr = s["framing"]
        if fr["handoff"]:
            rig.act("framing.handoff", on=False)
        if fr["subjects"] != "group":
            rig.act("framing.subjects", subjects="group")
        if fr["shot"] != "auto":
            rig.act("framing.shot", shot="auto")
        if fr["mode"] != "off":
            rig.act("framing.mode", mode="off")
        rig.act("call.demo_reset")  # no call, the warning at its defaults
        rig.act("network.demo_reset")  # a healthy link, default targets, no bandwidth result
        rig.act("echo.demo_reset")  # no check, no results, the scripted coupling cleared
        rig.act("preflight.demo_reset")  # no faults, no result, no history
        poll(rig, lambda st: st["call"]["state"] == "idle")  # the call has left (it has a leave hysteresis)
        rig.act("safejoin.demo_reset")  # Warn mode, nothing held, no events
        rig.act("audio.demo_output_reset")  # outputs plugged, first the default
        rig.act("audio.mute", on=False)
        rig.act("audio.bypass", on=False)
        rig.act("audio.gain", db=0)
        if s["video"]["program_scene"] != base["scene"]:
            rig.act("video.scene", name=base["scene"])
        if s["video"]["selected_source"] != base["source"]:
            rig.act("video.source", name=base["source"])
        if not all(engine["available"] for engine in s["audio"]["denoise"]["engines"]):
            rig.act("audio.demo_deepfilternet", on=True)  # the demo rig may have lost its DeepFilterNet library
        chain = s["audio"]["chain"]
        if chain.get("held"):
            rig.act("audio.demo_chain_held", on=False)
        if chain["preset"] != base["preset"] or chain["custom"]:
            rig.act("audio.preset", name=base["preset"])
        if chain["loudness"]["on"] or chain["loudness"]["target_lufs"] != -16.0:
            rig.act("audio.loudness", on=False, target_lufs=-16)
        for source in s["video"]["sources"]:
            rig.act("video.look.reset", source=source)
        blur = s["video"]["blur"]
        if blur.get("native"):
            rig.act("video.blur.arc", mode="off")  # the plain demo blur is the default; native states are per test
            blur = rig.state()["video"]["blur"]
        if not blur["plugin"]["installed"]:
            rig.act("video.blur.plugin", installed=True)
        if blur["on"] or any(e["enabled"] or e["strength"] != 8 or e["rate"] != 2 for e in blur["sources"].values()):
            rig.act("video.blur", on=False, strength=8, rate=2)
        for look in s["video"]["looks"]:
            if not look["builtin"]:
                rig.act("video.looks.delete", name=look["name"])
        pr = s["presets"]
        for profile in pr["profiles"]:
            if not profile["builtin"]:
                rig.act("presets.profile.delete", name=profile["name"])
        if pr["platform"] or pr["tier"] or pr["last_apply"] or pr["profile"]:
            rig.act("presets.clear")
        stream = s["stream"]
        if stream["status"] != "unavailable":
            if any(d["active"] for d in stream["destinations"]):
                rig.act("stream.stop")
            if stream["recording"]["active"]:
                rig.act("record.stop")
            rig.act(
                "stream.set",
                codec="auto",
                encoder="auto",
                tier="source",
                fps=0,
                bitrate_kbps=0,
                rate_control="cbr",
                keyint_s=2.0,
                quality=0,
                audio_bitrate_kbps=128,
                destinations=[
                    {"id": "twitch", "kind": "rtmp", "name": "Twitch", "url": "rtmp://live.demo/app"},
                    {"id": "backup", "kind": "srt", "name": "Backup SRT", "url": "srt://demo.test:9000"},
                ],
            )
            poll(rig, lambda st: not any(e["swapping"] for e in st["stream"]["encoders"]))
        if s["framing"]["mode"] != "off":
            rig.act("framing.mode", mode="off")
        if s["eyeline"]["enabled"] or s["eyeline"]["calibrated"] or s["eyeline"]["reminder"]["on"]:
            rig.act("eyeline.demo.reset")
        if s["video"]["recording"]["active"]:
            rig.act("video.record", on=False)
        if s["video"]["streaming"]["active"]:
            rig.act("video.stream", on=False)
        if s["video"]["virtualcam"] != base["virtualcam"]:
            rig.act("video.virtualcam", on=base["virtualcam"])
        # The lights, their automation and the pretend camera go back in ONE server-side step the engine cannot see the
        # middle of: moved through the normal actions they would be a change by hand, and the engine would hold them.
        rig.act("lighting.demo.reset")
        for preset in s["lighting"]["presets"]:
            if not preset["builtin"]:
                rig.act("lighting.presets.delete", name=preset["name"])
        if s["auto"]["running"] != base["auto"]:
            rig.act("auto.on" if base["auto"] else "auto.off")
            poll(rig, lambda st: not st["auto"]["busy"] and st["auto"]["running"] == base["auto"])

    reset()
    yield
    reset()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):
    """A failed browser test carries what the page posted, what the server answered and what its disk was doing."""
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or report.passed:
        return
    funcargs = getattr(item, "funcargs", {})
    holder = funcargs.get("app") or funcargs.get("rig")
    if holder is None:
        return
    try:
        text = holder.evidence()
    except Exception as exc:  # a failing report must never hide the failure it reports on
        text = f"evidence unavailable: {type(exc).__name__}: {exc}"
    report.sections.append(("e2e evidence", text))


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    for probe in PROBES:
        terminalreporter.write_line(probe.summary())


# ---------------------------------------------------------------------------
# Browsers and pages
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def playwright_driver() -> Iterator[Playwright]:
    with sync_playwright() as pw:
        yield pw


@pytest.fixture(scope="session")
def browser(playwright_driver: Playwright, browser_name: str) -> Iterator[Browser]:
    engine = getattr(playwright_driver, browser_name)
    # /dev/shm is tiny in containers; Chromium otherwise crashes or stalls under load.
    args = ["--disable-dev-shm-usage"] if browser_name == "chromium" else []
    instance = engine.launch(headless=True, args=args)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def context(browser: Browser) -> Iterator[BrowserContext]:
    ctx = browser.new_context(viewport=DESKTOP, reduced_motion="no-preference", service_workers="block")
    ctx.add_init_script(INIT_SCRIPT)
    ctx.set_default_timeout(ACTION_TIMEOUT)
    ctx.set_default_navigation_timeout(ACTION_TIMEOUT * 2)
    try:
        yield ctx
    finally:
        ctx.close()


@pytest.fixture
def problems() -> list[str]:
    """Everything the page reported as wrong: uncaught exceptions and console errors."""
    return []


@pytest.fixture
def page(context: BrowserContext, problems: list[str]) -> Page:
    pg = context.new_page()
    pg.on("pageerror", lambda err: problems.append(f"pageerror: {err}"))
    pg.on("console", lambda msg: msg.type == "error" and problems.append(f"console.error: {msg.text}"))
    return pg


@pytest.fixture
def app(page: Page, rig: Rig, problems: list[str]) -> Iterator[App]:
    yield App(page, rig)
    uncaught = [p for p in problems if p.startswith("pageerror")]
    assert not uncaught, "uncaught exceptions in the page:\n" + "\n".join(uncaught)


@pytest.fixture(scope="session")
def artifacts() -> Path:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    return ARTIFACTS
