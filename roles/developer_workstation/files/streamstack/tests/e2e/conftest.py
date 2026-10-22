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
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, expect, sync_playwright

from support import ACTION_TIMEOUT, EXPECT_TIMEOUT, App, Rig, poll

REPO = Path(__file__).resolve().parents[2]
ARTIFACTS = Path(os.environ.get("E2E_ARTIFACTS", REPO / "e2e-artifacts"))
URL_LINE = re.compile(r"StreamStack Console: (http://127\.0\.0\.1:\d+/)\?token=(\S+?)(?:#\S*)?\s*$")
STARTUP_SECONDS = 30
ALL_BROWSERS = ("chromium", "firefox")
# The two simulated Litra Beams of the demo rig: light id -> (on, brightness %, kelvin).
LIGHT_BASELINE = {"c901-usb-demo-1": (True, 70, 5000), "c901-usb-demo-2": (True, 55, 4200)}
DESKTOP = {"width": 1280, "height": 720}

# Page JS: collect CSP violations before any page script runs.
INIT_SCRIPT = """
window.__csp = [];
document.addEventListener('securitypolicyviolation', (e) => {
  window.__csp.push(`${e.violatedDirective} ${e.blockedURI}`);
});
"""


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
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_RUNTIME_DIR": str(runtime),
        "PYTHONPATH": os.pathsep.join(filter(None, [str(REPO / "src"), os.environ.get("PYTHONPATH", "")])),
        "PYTHONUNBUFFERED": "1",
        "NO_COLOR": "1",
    }
    env.pop("STREAMSTACK_ENV_FILE", None)
    cmd = [sys.executable, "-m", "streamstack", "console", "--demo", "--no-window", "--port", "0"]
    proc = subprocess.Popen(
        cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    log: list[str] = []
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
        server = Rig(base=found.group(1), token=found.group(2), log=log)
        server.state()  # ready: the API answers with the token
        yield server
    finally:
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
                and st
            ),
        )
        if not s["studio"]["running"]:
            rig.act("studio.start")
            poll(rig, lambda st: st["studio"]["running"] and not st["studio"]["busy"])
        if s["video"]["privacy"]:
            rig.act("video.privacy", on=False)
        if s["audio"]["mic"].get("stalled"):
            rig.act("audio.demo_stall", on=False)
        rig.act("audio.clip_clear")
        rig.act("audio.mute", on=False)
        rig.act("audio.bypass", on=False)
        rig.act("audio.gain", db=0)
        if s["video"]["program_scene"] != base["scene"]:
            rig.act("video.scene", name=base["scene"])
        if s["video"]["selected_source"] != base["source"]:
            rig.act("video.source", name=base["source"])
        if s["audio"]["chain"]["preset"] != base["preset"] or s["audio"]["tone"]["custom"]:
            rig.act("audio.preset", name=base["preset"])
        for source in s["video"]["sources"]:
            rig.act("video.look.reset", source=source)
        blur = s["video"]["blur"]
        if not blur["plugin"]["installed"]:
            rig.act("video.blur.plugin", installed=True)
        if blur["on"] or any(e["enabled"] or e["strength"] != 8 or e["rate"] != 2 for e in blur["sources"].values()):
            rig.act("video.blur", on=False, strength=8, rate=2)
        for look in s["video"]["looks"]:
            if not look["builtin"]:
                rig.act("video.looks.delete", name=look["name"])
        if s["framing"]["mode"] != "off":
            rig.act("framing.mode", mode="off")
        if s["video"]["recording"]["active"]:
            rig.act("video.record", on=False)
        if s["video"]["streaming"]["active"]:
            rig.act("video.stream", on=False)
        if s["video"]["virtualcam"] != base["virtualcam"]:
            rig.act("video.virtualcam", on=base["virtualcam"])
        for light in s["lighting"]["lights"]:
            start = LIGHT_BASELINE.get(light["id"])
            if start is None:
                continue
            on, percent, kelvin = start
            rig.act("lighting.brightness", id=light["id"], percent=percent)
            rig.act("lighting.temperature", id=light["id"], kelvin=kelvin)
            rig.act("lighting.power", id=light["id"], on=on)
        for preset in s["lighting"]["presets"]:
            if not preset["builtin"]:
                rig.act("lighting.presets.delete", name=preset["name"])
        if s["auto"]["running"] != base["auto"]:
            rig.act("auto.on" if base["auto"] else "auto.off")
            poll(rig, lambda st: not st["auto"]["busy"] and st["auto"]["running"] == base["auto"])

    reset()
    yield
    reset()


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
