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
from typing import Any

from playwright.sync_api import Locator, Page, expect

ROUTES = ("live", "camera", "audio", "auto", "system", "mini")
# What proves each route has mounted (placeholders count until their real page lands).
ROUTE_READY = {
    "live": ".cockpit",
    "camera": ".cam",
    "audio": ".au",
    "auto": ".aut",
    "system": ".sys",
    "mini": ".mini",
}
TOKEN_HEADER = "X-StreamStack-Token"
# Waits are upper bounds on conditions, never sleeps. Shared CI runners are several times slower than a desk machine,
# so every timeout scales with E2E_TIMEOUT_SCALE (the CI job sets it).
SCALE = float(os.environ.get("E2E_TIMEOUT_SCALE", "1"))
LOADED = int(15_000 * SCALE)
ACTION_TIMEOUT = int(10_000 * SCALE)
EXPECT_TIMEOUT = int(5_000 * SCALE)


@dataclass
class Rig:
    """The demo server started by the ``rig`` fixture, and its HTTP API as the tests need it."""

    base: str  # http://127.0.0.1:<port>/
    token: str
    log: list[str] = field(default_factory=list)

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
            with urllib.request.urlopen(req, timeout=10) as res:
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


def poll(rig: Rig, probe: Callable[[dict[str, Any]], Any], timeout: float = 10.0 * SCALE) -> Any:
    """Wait until ``probe(state)`` is truthy on the server (an action has landed) and return its value."""
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = probe(rig.state())
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"server state never matched; last probe value {value!r}")


class App:
    """A Console page open in a browser, with the locators every suite needs."""

    def __init__(self, page: Page, rig: Rig) -> None:
        self.page = page
        self.rig = rig

    # ---- navigation ----
    def goto(self, route: str = "live", *, wait_ready: bool = True) -> App:
        self.page.goto(f"{self.rig.url}#/{route}")
        if wait_ready:
            self.wait_ready(route)
        return self

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
