"""The Wayland hotkey backend against a fake GlobalShortcuts portal on a private D-Bus session bus."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

pytest.importorskip("jeepney")  # the optional `shortcuts` extra
from portal_support import FakePortal, have_bus, private_bus

from streamstack import hotkeys_portal
from streamstack.hotkeys_portal import PortalBackend

pytestmark = pytest.mark.skipif(not have_bus(), reason="needs dbus-daemon")


@pytest.fixture
def bus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    with private_bus(tmp_path) as address:
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", address)
        yield address


@pytest.fixture
def portal(bus: str) -> Iterator[FakePortal]:
    fake = FakePortal(bus)
    yield fake
    fake.close()


def wait_for(check: Callable[[], bool], seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


class Sink:
    def __init__(self) -> None:
        self.combos: list[str] = []
        self.statuses: list[dict[str, str]] = []
        self.lock = threading.Lock()

    def combo(self, combo: str) -> None:
        with self.lock:
            self.combos.append(combo)

    def status(self, status: dict[str, str]) -> None:
        with self.lock:
            self.statuses.append(status)


def test_combos_are_bound_and_activations_delivered(bus: str, portal: FakePortal) -> None:
    sink = Sink()
    backend = PortalBackend(sink.combo, sink.status, address=bus)
    assert backend.start() is None
    try:
        assert backend.set_combos(["ctrl+alt+m", "f9"]) == {"ctrl+alt+m": "pending", "f9": "pending"}
        assert wait_for(lambda: sink.statuses)
        assert sink.statuses[0] == {"ctrl+alt+m": "ok", "f9": "ok"}
        assert sorted(i for i, _ in portal.bound) == ["ctrl+alt+m", "f9"]
        info = dict(portal.bound)["ctrl+alt+m"]
        assert info["preferred_trigger"][1] == "CTRL+ALT+m"
        assert "Ctrl+Alt+M" in info["description"][1]
        portal.activate("ctrl+alt+m")
        assert wait_for(lambda: sink.combos == ["ctrl+alt+m"])
        assert backend.trigger_text["f9"] == "F9"
    finally:
        backend.stop()
    assert portal.closed


def test_a_shortcut_the_desktop_leaves_out_is_refused(bus: str, portal: FakePortal) -> None:
    portal.refuse = {"f9"}
    sink = Sink()
    backend = PortalBackend(sink.combo, sink.status, address=bus)
    assert backend.start() is None
    try:
        backend.set_combos(["ctrl+alt+m", "f9"])
        assert wait_for(lambda: sink.statuses)
        assert sink.statuses[0] == {"ctrl+alt+m": "ok", "f9": "refused"}
    finally:
        backend.stop()


def test_a_cancelled_dialog_refuses_everything(bus: str, portal: FakePortal) -> None:
    portal.response_code = 1
    sink = Sink()
    backend = PortalBackend(sink.combo, sink.status, address=bus)
    assert backend.start() is None
    try:
        backend.set_combos(["ctrl+alt+m"])
        assert wait_for(lambda: sink.statuses)
        assert sink.statuses[0] == {"ctrl+alt+m": "refused"}
    finally:
        backend.stop()


def test_activations_of_combos_no_longer_wanted_or_other_sessions_are_ignored(bus: str, portal: FakePortal) -> None:
    sink = Sink()
    backend = PortalBackend(sink.combo, sink.status, address=bus)
    assert backend.start() is None
    try:
        backend.set_combos(["ctrl+alt+m"])
        assert wait_for(lambda: sink.statuses)
        portal.activate("ctrl+alt+k")  # never asked for
        portal.activate("ctrl+alt+m", session="/org/freedesktop/portal/desktop/session/other/x")
        portal.activate("ctrl+alt+m")
        assert wait_for(lambda: sink.combos == ["ctrl+alt+m"])
        time.sleep(0.3)
        assert sink.combos == ["ctrl+alt+m"]
    finally:
        backend.stop()


def test_a_desktop_without_the_interface_says_so(bus: str, portal: FakePortal) -> None:
    portal.has_interface = False
    backend = PortalBackend(lambda c: None, address=bus)
    reason = backend.start()
    assert reason and "GlobalShortcuts" in reason
    assert backend.set_combos(["f9"]) == {"f9": "unavailable"}
    backend.stop()


def test_no_portal_on_the_bus_says_so(bus: str) -> None:
    backend = PortalBackend(lambda c: None, address=bus)
    reason = backend.start()
    assert reason and "portal" in reason
    backend.stop()


def test_a_failing_handler_does_not_stop_the_listener(bus: str, portal: FakePortal) -> None:
    calls: list[str] = []

    def handler(combo: str) -> None:
        calls.append(combo)
        raise RuntimeError("boom")

    sink = Sink()
    backend = PortalBackend(handler, sink.status, address=bus)
    assert backend.start() is None
    try:
        backend.set_combos(["f9"])
        assert wait_for(lambda: sink.statuses)
        portal.activate("f9")
        portal.activate("f9")
        assert wait_for(lambda: len(calls) == 2)
        assert backend.failure and "boom" in backend.failure
    finally:
        backend.stop()


def test_without_a_session_bus_the_reason_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setattr(hotkeys_portal.os.path, "exists", lambda path: False)
    assert hotkeys_portal.available() == "There is no D-Bus session bus."


def test_without_the_extra_the_reason_says_how_to_install_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hotkeys_portal, "open_dbus_connection", None)
    assert "streamstack[shortcuts]" in (hotkeys_portal.available() or "")
    assert "streamstack[shortcuts]" in (PortalBackend(lambda c: None).start() or "")
