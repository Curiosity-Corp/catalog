"""Helpers shared by the e2e suites: the running demo rig (HTTP side) and a thin page object for the Console.

Page suites (camera, audio, auto, system) add their own locators to ``App`` or keep them in their module;
they all start from the ``app`` fixture in ``conftest.py``.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.sync_api import Locator, Page, Request, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeout

ROUTES = ("live", "camera", "audio", "auto", "system", "lighting", "interface", "presets", "stream", "deck", "mini")
# What proves each route has mounted (placeholders count until their real page lands).
ROUTE_READY = {
    "live": ".cockpit",
    "camera": ".cam",
    "audio": ".au",
    "auto": ".aut",
    "system": ".sys",
    "lighting": ".li",
    "interface": ".ifc",
    "presets": ".pr",
    "stream": ".st",
    "deck": ".dk",
    "mini": ".mini",
}
TOKEN_HEADER = "X-StreamStack-Token"
# Waits are upper bounds on conditions, never sleeps. Shared CI runners are several times slower than a desk machine,
# so every timeout scales with E2E_TIMEOUT_SCALE (the CI job sets it).
SCALE = float(os.environ.get("E2E_TIMEOUT_SCALE", "1"))
LOADED = int(15_000 * SCALE)
# Socket timeout for plain HTTP calls to the Console (same variable as tests/http_support.py): a throttled
# runner can stall a call for well over 15 s (the e2e job failed once on a 15 s urlopen).
HTTP_TIMEOUT = float(os.environ.get("STREAMSTACK_TEST_HTTP_TIMEOUT", "90"))
ACTION_TIMEOUT = int(10_000 * SCALE)
EXPECT_TIMEOUT = int(5_000 * SCALE)


@dataclass
class Rig:
    """The demo server started by the ``rig`` fixture, and its HTTP API as the tests need it."""

    base: str  # http://127.0.0.1:<port>/
    token: str
    log: list[str] = field(default_factory=list)
    home: Path | None = None  # the demo server's isolated HOME, for tests that damage or read its files

    @property
    def url(self) -> str:
        return f"{self.base}?token={self.token}"

    def request(self, method: str, path: str, body: Any = None, token: str | None = None) -> tuple[int, Any]:
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if token is not None:
            headers[TOKEN_HEADER] = token
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            # a starved CI runner stalls the server thread too, so the read deadline scales like every wait
            with urllib.request.urlopen(req, timeout=max(HTTP_TIMEOUT, 10 * SCALE)) as res:
                return res.status, json.loads(res.read() or b"null")
        except urllib.error.HTTPError as err:
            raw = err.read()
            try:
                return err.code, json.loads(raw)
            except ValueError:
                return err.code, raw.decode(errors="replace")

    def state(self) -> dict[str, Any]:
        status, body = self.request("GET", "api/state", token=self.token)
        assert status == 200, body
        return body

    def wait_for_state(self, path: str, expected: Any, *, tol: float = 0.0, timeout: float | None = None) -> Any:
        """Wait until the server's state at ``path`` ("audio.mic.gain_db"; integers index lists) equals ``expected``.

        Use it, not a direct ``state()`` read, whenever a UI action has just been made and the test compares the server
        with what the page shows: the page posts on its own connection and a fader drag posts a throttled stream, so
        the server lags the page by a few requests. ``tol`` allows for the page rounding to its step. Returns the value.
        """

        def read(state: dict[str, Any]) -> Any:
            node: Any = state
            for key in path.split("."):
                node = node[int(key)] if isinstance(node, list) else node[key]
            return node

        def matches(value: Any) -> bool:
            if tol and isinstance(value, (int, float)) and isinstance(expected, (int, float)):
                return abs(value - expected) <= tol
            return bool(value == expected)

        def probe(state: dict[str, Any]) -> list[Any] | None:
            value = read(state)
            return [value] if matches(value) else None  # a list, so a matching 0 or False still counts as found

        try:
            found = poll(self, probe) if timeout is None else poll(self, probe, timeout)
        except AssertionError as err:
            raise AssertionError(f"server state {path!r} never became {expected!r}: {err}") from None
        return found[0]

    def act(self, action: str, **params: Any) -> dict[str, Any]:
        status, body = self.request("POST", "api/action", {"action": action, **params}, token=self.token)
        assert status == 200, f"{action} {params}: {status} {body}"
        return body


class FlakyProxy:
    """A TCP pass-through in front of the rig whose connections the test can cut and refuse.

    Playwright's ``set_offline`` does not end an event stream that is already open, so connection-loss tests browse
    through this instead. It rewrites the ``Host`` header, because the Console only answers its own address.
    """

    def __init__(self, rig: Rig) -> None:
        port = re.search(r":(\d+)/$", rig.base)
        assert port, rig.base
        self.backend = int(port.group(1))
        self.token = rig.token
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        self._open: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._refusing = False
        self._closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?token={self.token}"

    def cut(self) -> None:
        """Drop every connection now and refuse new ones until ``heal``."""
        with self._lock:
            self._refusing = True
            doomed = list(self._open)
        for sock in doomed:
            self._shut(sock)

    def heal(self) -> None:
        with self._lock:
            self._refusing = False

    def close(self) -> None:
        self._closed = True
        self.cut()
        with contextlib.suppress(OSError):
            self._listener.close()

    @staticmethod
    def _shut(sock: socket.socket) -> None:
        for step in (lambda: sock.shutdown(socket.SHUT_RDWR), sock.close):
            with contextlib.suppress(OSError):
                step()

    def _accept(self) -> None:
        while not self._closed:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            with self._lock:
                refuse = self._refusing
            if refuse:
                self._shut(client)
                continue
            try:
                upstream = socket.create_connection(("127.0.0.1", self.backend), timeout=5)
            except OSError:
                self._shut(client)
                continue
            upstream.settimeout(None)
            with self._lock:
                self._open.update((client, upstream))
            threading.Thread(target=self._pipe, args=(client, upstream, True), daemon=True).start()
            threading.Thread(target=self._pipe, args=(upstream, client, False), daemon=True).start()

    def _pipe(self, src: socket.socket, dst: socket.socket, rewrite: bool) -> None:
        host_from = f"Host: 127.0.0.1:{self.port}".encode()
        host_to = f"Host: 127.0.0.1:{self.backend}".encode()
        try:
            while chunk := src.recv(65536):
                dst.sendall(chunk.replace(host_from, host_to) if rewrite else chunk)
        except OSError:
            pass  # the other side was cut: that is how a pipe ends
        finally:
            for sock in (src, dst):
                self._shut(sock)
                with self._lock:
                    self._open.discard(sock)


def _lighting_context(rig: Rig) -> str:
    """What the lights and the automation engine looked like when a poll gave up (a failure is read from the log)."""
    try:
        lighting = rig.state()["lighting"]
    except (KeyError, OSError, ValueError) as exc:
        return f"; lighting state unavailable: {exc}"
    lights = [
        {k: light.get(k) for k in ("id", "power", "brightness", "kelvin")} for light in lighting.get("lights", [])
    ]
    status = lighting.get("automation", {}).get("status", {})
    events = [f"{e.get('time')} {e.get('kind')}: {e.get('text')}" for e in status.get("events", [])[:8]]
    return f"; lights {lights}; engine lights {status.get('lights')}; recent events {events}"


def poll_settled(
    rig: Rig, probe: Callable[[dict[str, Any]], Any], quiet: float = 1.0, timeout: float = 10.0 * SCALE
) -> Any:
    """Like ``poll``, but return the value only once it has stopped changing for ``quiet`` seconds.

    A fader drag posts a stream of values; the first one that matches is not the last one the server will take."""
    deadline = time.monotonic() + timeout
    value, since = None, time.monotonic()
    while time.monotonic() < deadline:
        now = time.monotonic()
        current = probe(rig.state())
        if not current or current != value:
            value, since = current, now
        elif now - since >= quiet:
            return value
        time.sleep(0.05)
    raise AssertionError(f"server state never settled; last probe value {value!r}{_lighting_context(rig)}")


def poll(rig: Rig, probe: Callable[[dict[str, Any]], Any], timeout: float = 10.0 * SCALE) -> Any:
    """Wait until ``probe(state)`` is truthy on the server (an action has landed) and return its value."""
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = probe(rig.state())
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"server state never matched; last probe value {value!r}{_lighting_context(rig)}")


class App:
    """A Console page open in a browser, with the locators every suite needs."""

    def __init__(self, page: Page, rig: Rig) -> None:
        self.page = page
        self.rig = rig
        self._in_flight: dict[Request, float] = {}
        page.on("request", lambda request: self._in_flight.__setitem__(request, time.monotonic()))
        page.on("requestfinished", lambda request: self._in_flight.pop(request, None))
        page.on("requestfailed", lambda request: self._in_flight.pop(request, None))

    # ---- navigation ----
    def goto(self, route: str = "live", *, wait_ready: bool = True) -> App:
        """Open a route. Navigation is done at "commit" and the page proves it is up through ``wait_ready``.

        The default ``load`` wait needs every subresource of the module graph, so one slow or stuck request surfaced as
        an anonymous 60 s ``Page.goto`` timeout (issue #124, Firefox on a CPU-starved runner). Waiting for the page's
        own readiness marks says what is missing, and a failure lists the requests still in flight, so the next
        recurrence names the stuck resource instead of only a timeout.
        """
        try:
            self.page.goto(f"{self.rig.url}#/{route}", wait_until="commit")
            if wait_ready:
                self.wait_ready(route)
        except (PlaywrightTimeout, AssertionError) as err:
            raise AssertionError(f"opening #{route} failed: {err}\n{self.describe_in_flight()}") from err
        return self

    def describe_in_flight(self) -> str:
        now = time.monotonic()
        stuck = [
            f"  {now - began:5.1f}s  {request.method} {request.url.split('?')[0]}"
            for request, began in self._in_flight.items()
        ]
        engine = self.page.context.browser.browser_type.name if self.page.context.browser else "?"
        return f"{engine}: {len(stuck)} request(s) still in flight" + ("".join("\n" + line for line in stuck))

    def wait_ready(self, route: str) -> None:
        expect(self.page.locator("body")).to_have_attribute("data-conn", "online", timeout=LOADED)
        expect(self.page.locator(f"#page[data-page='{route}']")).to_be_attached(timeout=LOADED)
        expect(self.page.locator(f"#page[data-page='{route}']").locator(ROUTE_READY[route]).first).to_be_visible(
            timeout=LOADED
        )

    def hash_to(self, route: str) -> None:
        self.page.evaluate("(r) => { location.hash = '#/' + r; }", route)
        self.wait_ready(route)

    # ---- locators ----
    def region(self, name: str) -> Locator:
        return self.page.get_by_role("group", name=name)

    def scene_pad(self, name: str) -> Locator:
        return self.region("Scenes").get_by_role("button", name=re.compile(rf"^{re.escape(name)}\b"))

    def preset_pad(self, label: str) -> Locator:
        return self.region("Voice presets").get_by_role("button", name=re.compile(rf"^{re.escape(label)}\b"))

    def slider(self, name: str) -> Locator:
        return self.page.get_by_role("slider", name=name)

    def switch(self, name: str) -> Locator:
        return self.page.get_by_role("switch", name=name)

    @property
    def mute_button(self) -> Locator:
        return self.page.get_by_role("button", name=re.compile(r"^(Mute mic|MUTED)"))

    @property
    def privacy_button(self) -> Locator:
        return self.page.get_by_role("button", name=re.compile(r"^(Privacy|PRIVACY ON)"))

    @property
    def top_chips(self) -> Locator:
        return self.page.locator("#top-center")

    @property
    def palette(self) -> Locator:
        return self.page.get_by_role("dialog", name="Command palette")

    @property
    def shortcuts(self) -> Locator:
        return self.page.get_by_role("dialog", name="Keyboard shortcuts")

    # ---- gestures ----
    def drag_fader(self, slider: Locator, to: float) -> None:
        """Press on the fader track and drag to ``to`` (0 = bottom/left end, 1 = top/right end) in several moves."""
        rail = slider.locator(".track .rail")
        box = rail.bounding_box()
        assert box is not None, "fader track has no layout"
        horizontal = slider.get_attribute("aria-orientation") == "horizontal"
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        mouse = self.page.mouse
        if horizontal:
            mouse.move(box["x"] + box["width"] * 0.5, cy)
            mouse.down()
            mouse.move(box["x"] + box["width"] * to, cy, steps=8)
        else:
            mouse.move(cx, box["y"] + box["height"] * 0.5)
            mouse.down()
            mouse.move(cx, box["y"] + box["height"] * (1 - to), steps=8)
        mouse.up()

    def press(self, key: str) -> None:
        """A global shortcut. Focus is parked on the body so a focused control cannot swallow it."""
        self.page.evaluate("() => document.activeElement instanceof HTMLElement && document.activeElement.blur()")
        self.page.keyboard.press(key)

    def wait_picture(self) -> None:
        """Wait until the live program picture has decoded a frame (the MJPEG stream is up)."""
        self.page.wait_for_function(
            """() => {
              const pv = document.querySelector('ss-preview');
              const img = pv && pv.shadowRoot.querySelector('img');
              return !!img && !img.hidden && img.naturalWidth > 0;
            }""",
            timeout=LOADED,
        )

    def meter_signature(self, source: str = "mic") -> str:
        """A fingerprint of what the meter canvas currently shows."""
        return self.page.locator(f"ss-meter[source='{source}'] canvas").first.evaluate("(c) => c.toDataURL()")


def quiet(page: Page) -> None:
    """Wait for toasts to clear: they sit over controls (top right, or above the tabs on a phone) and take the pointer.

    Toasts do not time out while the pointer rests on them, so park the pointer in a corner first.
    """
    page.mouse.move(2, 2)
    expect(page.locator("ss-toast .t")).to_have_count(0, timeout=LOADED)


def tap(page: Page, button: Locator) -> None:
    quiet(page)
    button.click()


def hold(page: Page, button: Locator, ms: int) -> None:
    """Press and hold ``button`` for ``ms`` (hold-to-confirm controls fire when the hold completes)."""
    quiet(page)
    button.hover()
    page.mouse.down()
    try:
        page.wait_for_timeout(int(ms * max(1.0, SCALE)))
    finally:
        page.mouse.up()


DRAG_SCRIPT = """
(source, target) => {
  const dt = new DataTransfer();
  const fire = (el, type) => {
    const ev = new DragEvent(type, { bubbles: true, cancelable: true, composed: true, dataTransfer: dt });
    el.dispatchEvent(ev);
    return ev;
  };
  const started = fire(source, 'dragstart');
  const marked = document.body.dataset.dragging || '';
  fire(target, 'dragenter');
  const over = fire(target, 'dragover');
  const dropped = { marked, started: !started.defaultPrevented, accepted: over.defaultPrevented };
  if (dropped.started && dropped.accepted) fire(target, 'drop');
  fire(source, 'dragend');
  return dropped;
}
"""


def drag_onto(page: Page, source: Locator, target: Locator, *, kind: str = "key") -> None:
    """Run an HTML5 drag of ``source`` onto ``target`` through the page's own handlers and check they took it.

    Dispatches ``dragstart``, ``dragenter``, ``dragover``, ``drop`` and ``dragend`` with one shared ``DataTransfer``,
    so the page's handlers run exactly as for a hand drag but nothing depends on how the browser turns mouse events
    into a native drag. That conversion is what a starved runner lost (pipeline 96654: the drop never happened), and
    Playwright's mouse-driven version of it could not be made reliable either (pipeline 96700). The result says
    whether the drag started (the page did not cancel ``dragstart``), whether it marked the body as dragging the
    expected ``kind``, and whether the target accepted it (the page cancels ``dragover`` only for a drop it takes).
    """
    source.scroll_into_view_if_needed()
    result = source.evaluate(DRAG_SCRIPT, target.element_handle())
    assert result["started"], f"the page cancelled the {kind} drag at its start: {result}"
    assert result["marked"] == kind, f"the page did not mark a {kind} drag in progress: {result}"
    assert result["accepted"], f"the drop target did not accept the {kind} drag: {result}"
