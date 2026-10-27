"""`streamstack deck run`: the process that owns the Stream Deck.

It is a terminal for the Console: it opens the USB device, draws the keys the Console says to draw
(`state.deck.surface`), sends presses back as `deck.press`, and keeps the Console told which deck it holds
(`deck.attach`, every few seconds).
What a key does, which page is open and what is lit all live in the Console (console/deck.py), so a Console window and
the deck always agree. Here there is only device I/O, drawing, brightness and the sleep timer.

It reconnects when the deck is unplugged and plugged in again, waits for the Console when there is none (and starts a
headless one, unless told not to), and never needs more than the `deck` extra: `streamdeck` and Pillow are imported
when the service starts, not when StreamStack does.
"""

from __future__ import annotations

import json
import logging
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol

from streamstack import system
from streamstack.console import launcher
from streamstack.deck import render

log = logging.getLogger("streamstack.deck")

ATTACH_SECONDS = 5.0  # how often the Console is told the deck is still here
SCAN_SECONDS = 2.0  # how often to look for a deck while none is open
AUTOSTART_SECONDS = 30.0  # at most one headless Console start per this long
TIMEOUT = 3.0
STOP_HINT = (
    "If the Stream Deck app (streamdeck-linux-gui) is running it holds the device: `pkill streamdeck`, then retry."
)
UDEV_HINT = "Check /etc/udev/rules.d/60-streamdeck.rules (uaccess) and replug the deck."
HIDAPI_HINT = "Install the hidapi library: sudo apt install libhidapi-libusb0"


class ServiceError(RuntimeError):
    """The deck cannot be used right now; the message says why and what to try."""


@dataclass(frozen=True)
class DeviceInfo:
    name: str
    serial: str
    rows: int
    cols: int
    size: int
    image_format: dict[str, Any]


class Device(Protocol):
    """What the service needs from a deck (the `streamdeck` library's, or a fake in the tests)."""

    def info(self) -> DeviceInfo: ...
    def connected(self) -> bool: ...
    def set_image(self, key: int, data: bytes) -> None: ...
    def set_brightness(self, percent: int) -> None: ...
    def set_callback(self, callback: Callable[[int, bool], None]) -> None: ...
    def close(self) -> None: ...


Opener = Callable[[], Device | None]


# -- the real device -------------------------------------------------------------------------------------------------


class LibraryDevice:
    """A deck opened through the `streamdeck` library."""

    def __init__(self, deck: Any) -> None:
        self.deck = deck
        fmt = deck.key_image_format()
        rows, cols = deck.key_layout()
        self._info = DeviceInfo(
            name=str(deck.deck_type()),
            serial=str(deck.get_serial_number()),
            rows=rows,
            cols=cols,
            size=int(fmt["size"][0]),
            image_format=dict(fmt),
        )

    def info(self) -> DeviceInfo:
        return self._info

    def connected(self) -> bool:
        return bool(self.deck.connected())

    def set_image(self, key: int, data: bytes) -> None:
        self.deck.set_key_image(key, data)

    def set_brightness(self, percent: int) -> None:
        self.deck.set_brightness(percent)

    def set_callback(self, callback: Callable[[int, bool], None]) -> None:
        self.deck.set_key_callback(lambda _deck, key, state: callback(key, bool(state)))

    def close(self) -> None:
        try:
            self.deck.reset()
        except Exception as exc:  # a deck that was just unplugged cannot be reset; closing is all that is left
            log.debug("deck reset failed: %s", exc)
        try:
            self.deck.close()
        except Exception as exc:
            log.debug("deck close failed: %s", exc)


def library_opener() -> Opener:
    """An opener for the first Stream Deck the `streamdeck` library finds. Raises ServiceError when it cannot work."""
    try:
        from StreamDeck.DeviceManager import DeviceManager
        from StreamDeck.Transport.Transport import TransportError
    except ImportError as exc:
        raise ServiceError("The deck extra is not installed: pip install 'streamstack[deck]' (needs Pillow).") from exc
    if not render.available():
        raise ServiceError("Pillow is not installed: pip install 'streamstack[deck]'.")

    def open_first() -> Device | None:
        try:
            decks = DeviceManager().enumerate()
        except Exception as exc:  # the library raises ProbeError (a bare Exception subclass) without hidapi
            raise ServiceError(f"Cannot look for decks: {exc}. {HIDAPI_HINT}") from exc
        failure: Exception | None = None
        for deck in decks:
            try:
                deck.open()
                deck.reset()
                return LibraryDevice(deck)
            except TransportError as exc:
                failure = exc
                try:
                    deck.close()
                except Exception as close_exc:
                    log.debug("deck close after failed open: %s", close_exc)
        if failure is not None:
            raise ServiceError(f"A Stream Deck is plugged in but cannot be opened ({failure}). {STOP_HINT} {UDEV_HINT}")
        return None

    return open_first


# -- the Console ------------------------------------------------------------------------------------------------------


class ConsoleClient:
    """Talks to the running Console (found through its runtime file): actions out, state in."""

    def __init__(self, autostart: bool = True) -> None:
        self.autostart = autostart
        self._started_at = -AUTOSTART_SECONDS
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def _running(self) -> dict | None:
        info = launcher.read_running()
        if info or not self.autostart or time.monotonic() - self._started_at < AUTOSTART_SECONDS:
            return info
        self._started_at = time.monotonic()
        log.info("no Console is running; starting a headless one")
        try:
            system.detach([*launcher_command(), "console", "--no-window"])
        except OSError as exc:
            log.warning("could not start the Console: %s", exc)
        return None

    def post(self, action: str, params: dict | None = None) -> bool:
        """Run a Console action; False when the Console is not there or refused (logged, never raised)."""
        info = launcher.read_running()
        if info is None:
            return False
        body = json.dumps({"action": action, **(params or {})}).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{info['port']}/api/action",
            data=body,
            headers={"Content-Type": "application/json", "X-StreamStack-Token": info["token"]},
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT):
                return True
        except urllib.error.HTTPError as exc:
            log.info("the Console refused %s: HTTP %s", action, exc.code)
        except (urllib.error.URLError, OSError) as exc:
            log.info("the Console did not answer %s: %s", action, exc)
        return False

    def states(self) -> Iterator[dict | None]:
        """Console snapshots as they arrive; None once when the connection is lost. Reconnects forever."""
        backoff = 1.0
        while not self._stop.is_set():
            info = self._running()
            if info is None:
                yield None
                self._stop.wait(backoff)
                backoff = min(backoff * 1.5, 5.0)
                continue
            backoff = 1.0
            try:
                yield from self._stream(info)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                log.info("lost the Console: %s", exc)
            yield None
            self._stop.wait(1.0)

    def _stream(self, info: dict) -> Iterator[dict]:
        url = f"http://127.0.0.1:{info['port']}/api/events"
        request = urllib.request.Request(url, headers={"X-StreamStack-Token": info["token"]})
        with urllib.request.urlopen(request, timeout=30) as response:
            event = ""
            for raw in response:
                if self._stop.is_set():
                    return
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:") and event == "state":
                    snapshot = json.loads(line[5:])
                    if isinstance(snapshot, dict):
                        yield snapshot


def launcher_command() -> list[str]:
    from streamstack.lightservice import streamstack_command

    return streamstack_command().split()


# -- the service ------------------------------------------------------------------------------------------------------

OFFLINE = {
    "i": 0,
    "empty": False,
    "label": "Console offline",
    "icon": "alert",
    "lit": True,
    "tone": "amber",
    "rev": "offline",
}


class Service:
    """The deck loop. Everything outside (device, Console, pictures, time) is injected so tests can fake it."""

    def __init__(
        self,
        opener: Opener,
        client: Any,
        clock: Callable[[], float] = time.monotonic,
        draw: Callable[[dict, int], Any] | None = None,
        encode: Callable[[Any, dict], bytes] | None = None,
    ) -> None:
        self.opener, self.client, self._clock = opener, client, clock
        self._draw = draw or render.render
        self._encode = encode or render.to_native
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.device: Device | None = None
        self.info: DeviceInfo | None = None
        self.state: dict | None = None
        self.shown: dict[int, str] = {}
        self.brightness = -1
        self.asleep = False
        self.last_activity = clock()
        self._next_scan = 0.0
        self._next_attach = 0.0
        self._last_error = ""
        self._stop = threading.Event()
        self._native = render.Cache(256)

    # -- events in (any thread) -------------------------------------------------

    def post_state(self, snapshot: dict | None) -> None:
        self.events.put(("state", snapshot))

    def post_press(self, key: int, pressed: bool) -> None:
        if pressed:
            self.events.put(("press", key))

    def stop(self) -> None:
        self._stop.set()
        self.events.put(("stop", None))

    # -- the loop -------------------------------------------------------------------

    def run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    kind, value = self.events.get(timeout=0.5)
                except queue.Empty:
                    kind, value = "tick", None
                if kind == "stop":
                    break
                self.step(kind, value)
        finally:
            self.close_device()

    def step(self, kind: str, value: Any = None) -> None:
        """One event: "state", "press" or "tick" (the loop's own heartbeat)."""
        if kind == "state":
            self.state = value
            self.refresh()
        elif kind == "press":
            self.pressed(value)
        self.tick()

    def tick(self) -> None:
        now = self._clock()
        if self.device is None:
            if now >= self._next_scan:
                self._next_scan = now + SCAN_SECONDS
                self.scan()
            return
        if not self.device.connected():
            log.info("the deck was unplugged")
            self.close_device()
            self.client.post("deck.detach")
            return
        if now >= self._next_attach:
            self._next_attach = now + ATTACH_SECONDS
            self.attach()
        self.sleep_check(now)

    # -- the device -----------------------------------------------------------------

    def scan(self) -> None:
        try:
            device = self.opener()
        except ServiceError as exc:
            message = str(exc)
            if message != self._last_error:
                log.warning("%s", message)
                self._last_error = message
            if self._clock() >= self._next_attach:
                self._next_attach = self._clock() + ATTACH_SECONDS
                self.client.post("deck.attach", {"error": message})
            return
        if device is None:
            return
        self._last_error = ""
        self.device, self.info = device, device.info()
        self.shown.clear()
        self.brightness = -1
        self.asleep = False
        self.last_activity = self._clock()
        device.set_callback(self.post_press)
        log.info("opened %s (%s, %d keys)", self.info.name, self.info.serial, self.info.rows * self.info.cols)
        self._next_attach = 0.0
        self.attach()
        self.refresh()

    def attach(self) -> None:
        if self.info is None:
            return
        self.client.post(
            "deck.attach",
            {
                "name": self.info.name,
                "serial": self.info.serial,
                "rows": self.info.rows,
                "cols": self.info.cols,
                "size": self.info.size,
            },
        )

    def close_device(self) -> None:
        device, self.device, self.info = self.device, None, None
        if device is not None:
            device.close()

    # -- drawing --------------------------------------------------------------------

    def surface_keys(self) -> list[dict]:
        deck = (self.state or {}).get("deck") or {}
        keys = (deck.get("surface") or {}).get("keys")
        if self.state is None or not keys:
            return [dict(OFFLINE)]
        return keys

    def settings(self) -> dict:
        return (((self.state or {}).get("deck") or {}).get("profile") or {}).get("settings") or {}

    def refresh(self) -> None:
        """Push every key whose picture changed, and the brightness."""
        if self.device is None or self.info is None:
            return
        keys = self.surface_keys()
        count = self.info.rows * self.info.cols
        fmt = self.info.image_format
        for index in range(count):
            view = keys[index] if index < len(keys) else {"i": index, "empty": True, "rev": "0"}
            if self.shown.get(index) == view["rev"]:
                continue
            try:
                cache_key = (view["rev"], self.info.size, json.dumps(fmt, default=str, sort_keys=True))
                data = self._native.get(cache_key, lambda v=view: self._encode(self._draw(v, self.info.size), fmt))
                self.device.set_image(index, data)
            except Exception as exc:  # an unplug mid-draw surfaces as a transport error; tick() notices it
                log.info("could not draw key %d: %s", index, exc)
                return
            self.shown[index] = view["rev"]
        self.apply_brightness()

    def apply_brightness(self) -> None:
        if self.device is None:
            return
        want = 0 if self.asleep else int(self.settings().get("brightness", 70))
        if want != self.brightness:
            try:
                self.device.set_brightness(want)
            except Exception as exc:
                log.info("could not set the brightness: %s", exc)
                return
            self.brightness = want

    # -- presses and sleep ----------------------------------------------------------

    def pressed(self, key: int) -> None:
        self.last_activity = self._clock()
        if self.asleep:  # the press that wakes the deck does nothing else
            self.asleep = False
            self.apply_brightness()
            return
        if self.state is None:
            return
        self.client.post("deck.press", {"key": key})

    def sleep_check(self, now: float) -> None:
        minutes = int(self.settings().get("sleep_minutes") or 0)
        if minutes and not self.asleep and now - self.last_activity >= minutes * 60:
            self.asleep = True
            self.apply_brightness()
        elif self.asleep and not minutes:
            self.asleep = False
            self.apply_brightness()


def run(autostart: bool = True) -> int:
    """`streamstack deck run`."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    try:
        opener = library_opener()
    except ServiceError as exc:
        print(f"streamstack: {exc}", file=sys.stderr)
        return 1
    client = ConsoleClient(autostart)
    service = Service(opener, client)

    def feed() -> None:
        for snapshot in client.states():
            service.post_state(snapshot)

    import signal

    def stop(signum: int, _frame: object) -> None:
        log.info("stopping on signal %s", signum)
        client.stop()
        service.stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    threading.Thread(target=feed, name="deck-console", daemon=True).start()
    log.info("deck service running; waiting for a Stream Deck")
    service.run()
    return 0
