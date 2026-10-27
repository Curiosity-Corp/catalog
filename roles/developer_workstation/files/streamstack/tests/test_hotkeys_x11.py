"""The X11 hotkey backend against a private Xvfb: real XGrabKey, real key events (xdotool), no touch on the desktop."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator

import pytest

from streamstack import hotkeys_x11
from streamstack.hotkeys_x11 import X11Backend

pytestmark = pytest.mark.skipif(not (shutil.which("Xvfb") and shutil.which("xdotool")), reason="needs Xvfb and xdotool")


@pytest.fixture
def display(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    read, write = os.pipe()
    proc = subprocess.Popen(
        ["Xvfb", "-displayfd", str(write), "-screen", "0", "640x480x24", "-nolisten", "tcp"],
        pass_fds=[write],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.close(write)
    try:
        number = os.read(read, 16).decode().strip()
        assert number, "Xvfb did not report a display"
        name = f":{number}"
        monkeypatch.setenv("DISPLAY", name)
        yield name
    finally:
        os.close(read)
        proc.terminate()
        proc.wait(5)


class Keys:
    """Collects combos from a backend and types keys into the display."""

    def __init__(self, display: str) -> None:
        self.display = display
        self.got: list[str] = []
        self._seen = threading.Event()

    def __call__(self, combo: str) -> None:
        self.got.append(combo)
        self._seen.set()

    def type(self, keys: str) -> None:
        self._seen.clear()
        subprocess.run(["xdotool", "key", keys], env={**os.environ, "DISPLAY": self.display}, check=True, timeout=10)

    def wait(self, count: int, seconds: float = 3.0) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if len(self.got) >= count:
                return True
            time.sleep(0.02)
        return False


@pytest.fixture
def keys(display: str) -> Keys:
    return Keys(display)


def test_a_held_combo_is_delivered(display: str, keys: Keys) -> None:
    backend = X11Backend(keys)
    assert backend.start() is None
    try:
        assert backend.set_combos(["ctrl+alt+m", "xf86audiomute"]) == {"ctrl+alt+m": "ok", "xf86audiomute": "ok"}
        keys.type("ctrl+alt+m")
        assert keys.wait(1)
        keys.type("XF86AudioMute")
        assert keys.wait(2)
        assert keys.got == ["ctrl+alt+m", "xf86audiomute"]
    finally:
        backend.stop()


def test_other_keys_are_not_delivered(display: str, keys: Keys) -> None:
    backend = X11Backend(keys)
    assert backend.start() is None
    try:
        backend.set_combos(["ctrl+alt+m"])
        keys.type("ctrl+m")
        keys.type("alt+m")
        keys.type("ctrl+alt+shift+m")
        time.sleep(0.3)
        assert keys.got == []
    finally:
        backend.stop()


def test_numlock_and_capslock_do_not_hide_a_combo(display: str, keys: Keys) -> None:
    backend = X11Backend(keys)
    assert backend.start() is None
    try:
        backend.set_combos(["ctrl+alt+m"])
        keys.type("Caps_Lock")  # toggles the lock state on; the combo must still match
        keys.type("ctrl+alt+m")
        assert keys.wait(1)
        keys.type("Caps_Lock")
    finally:
        backend.stop()


def test_changing_the_set_releases_the_old_combos(display: str, keys: Keys) -> None:
    backend = X11Backend(keys)
    assert backend.start() is None
    try:
        backend.set_combos(["ctrl+alt+m"])
        backend.set_combos(["ctrl+alt+n"])
        keys.type("ctrl+alt+m")
        time.sleep(0.3)
        assert keys.got == []
        keys.type("ctrl+alt+n")
        assert keys.wait(1)
        assert backend.set_combos([]) == {}
        keys.type("ctrl+alt+n")
        time.sleep(0.3)
        assert keys.got == ["ctrl+alt+n"]
    finally:
        backend.stop()


def test_a_combo_another_client_holds_is_a_conflict(display: str, keys: Keys) -> None:
    first, second = X11Backend(keys), X11Backend(lambda combo: None)
    assert first.start() is None and second.start() is None
    try:
        assert first.set_combos(["ctrl+alt+m"]) == {"ctrl+alt+m": "ok"}
        assert second.set_combos(["ctrl+alt+m", "ctrl+alt+k"]) == {"ctrl+alt+m": "conflict", "ctrl+alt+k": "ok"}
        keys.type("ctrl+alt+m")
        assert keys.wait(1)  # the first client still has it
    finally:
        second.stop()
        first.stop()


def test_stopping_gives_the_keys_back(display: str, keys: Keys) -> None:
    first = X11Backend(keys)
    assert first.start() is None
    first.set_combos(["ctrl+alt+m"])
    first.stop()
    second = X11Backend(lambda combo: None)
    assert second.start() is None
    try:
        assert second.set_combos(["ctrl+alt+m"]) == {"ctrl+alt+m": "ok"}
    finally:
        second.stop()


def test_a_failing_handler_does_not_stop_the_listener(display: str) -> None:
    calls: list[str] = []

    def handler(combo: str) -> None:
        calls.append(combo)
        raise RuntimeError("boom")

    keys = Keys(display)
    backend = X11Backend(handler)
    assert backend.start() is None
    try:
        backend.set_combos(["ctrl+alt+m"])
        keys.type("ctrl+alt+m")
        time.sleep(0.2)
        keys.type("ctrl+alt+m")
        deadline = time.monotonic() + 3
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert calls == ["ctrl+alt+m", "ctrl+alt+m"]
        assert backend.failure and "boom" in backend.failure
    finally:
        backend.stop()


def test_without_a_display_the_reason_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    assert "DISPLAY" in (hotkeys_x11.available() or "")
    backend = X11Backend(lambda combo: None)
    assert "DISPLAY" in (backend.start() or "")
    assert backend.set_combos(["ctrl+alt+m"]) == {"ctrl+alt+m": "unavailable"}
    backend.stop()  # never started: a no-op


def test_an_unreachable_display_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY", ":65000")
    backend = X11Backend(lambda combo: None)
    assert backend.start() == "Could not open the X11 display."
    backend.stop()


def test_adding_a_combo_keeps_the_ones_already_held(display: str, keys: Keys) -> None:
    backend = X11Backend(keys)
    assert backend.start() is None
    try:
        backend.set_combos(["ctrl+alt+m"])
        held = dict(backend._held)
        assert backend.set_combos(["ctrl+alt+m", "ctrl+alt+n"]) == {"ctrl+alt+m": "ok", "ctrl+alt+n": "ok"}
        assert set(held) <= set(backend._held)  # the first grab was never let go
        keys.type("ctrl+alt+m")
        assert keys.wait(1)
    finally:
        backend.stop()
