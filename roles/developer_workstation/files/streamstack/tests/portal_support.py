"""A private D-Bus session bus and a fake XDG GlobalShortcuts portal for the portal backend's tests.

The fake speaks the real wire protocol with jeepney: the Properties.Get probe, CreateSession and BindShortcuts (each
answered by a Request object and a Response signal), Session.Close, and `activate` to emit `Activated`.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from jeepney import DBusAddress, HeaderFields, MessageType, new_error, new_method_return, new_signal
from jeepney.bus_messages import message_bus
from jeepney.io.blocking import open_dbus_connection

PORTAL = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"
IFACE = "org.freedesktop.portal.GlobalShortcuts"


def have_bus() -> bool:
    return shutil.which("dbus-daemon") is not None


@contextmanager
def private_bus(tmp_path: Path) -> Iterator[str]:
    """A session bus of its own; yields its address."""
    proc = subprocess.Popen(
        ["dbus-daemon", "--session", "--nofork", "--print-address", f"--address=unix:path={tmp_path / 'bus'}"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        address = proc.stdout.readline().strip()
        assert address, "dbus-daemon did not print an address"
        yield address
    finally:
        proc.terminate()
        proc.wait(5)


class FakePortal:
    """Owns org.freedesktop.portal.Desktop on `address`. Behaviour knobs are plain attributes."""

    def __init__(self, address: str) -> None:
        self.conn = open_dbus_connection(bus=address)
        self.send_lock = threading.Lock()
        self.has_interface = True
        self.response_code = 0  # what BindShortcuts answers: 0 ok, 1 cancelled, 2 failed
        self.refuse: set[str] = set()  # shortcut ids the "desktop" leaves out of its answer
        self.session = f"{PATH}/session/test/fake1"
        self.bound: list[tuple[str, dict]] = []
        self.closed = False
        self._stop = threading.Event()
        reply = self.conn.send_and_get_reply(message_bus.RequestName(PORTAL))
        assert reply.header.message_type is MessageType.method_return
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(3)
        self.conn.close()

    def _send(self, msg: Any) -> None:
        with self.send_lock:
            self.conn.send(msg)

    def _request(self, call: Any, token: str) -> str:
        sender = call.header.fields[HeaderFields.sender].lstrip(":").replace(".", "_")
        return f"{PATH}/request/{sender}/{token}"

    def _respond(self, path: str, code: int, results: dict) -> None:
        signal = new_signal(
            DBusAddress(path, interface="org.freedesktop.portal.Request"), "Response", "ua{sv}", (code, results)
        )
        self._send(signal)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                msg = self.conn.receive(timeout=0.1)
            except TimeoutError:
                continue
            if msg.header.message_type is not MessageType.method_call:
                continue
            self._handle(msg)

    def _handle(self, msg: Any) -> None:
        member = msg.header.fields.get(HeaderFields.member)
        interface = msg.header.fields.get(HeaderFields.interface)
        if interface == "org.freedesktop.DBus.Properties" and member == "Get":
            if not self.has_interface:
                self._send(new_error(msg, "org.freedesktop.DBus.Error.UnknownInterface", "s", ("no such interface",)))
            else:
                self._send(new_method_return(msg, "v", (("u", 1),)))
        elif member == "CreateSession":
            token = msg.body[0]["handle_token"][1]
            path = self._request(msg, token)
            self._send(new_method_return(msg, "o", (path,)))
            self._respond(path, 0, {"session_handle": ("s", self.session)})
        elif member == "BindShortcuts":
            _session, shortcuts, _parent, options = msg.body
            path = self._request(msg, options["handle_token"][1])
            self.bound = [(ident, info) for ident, info in shortcuts]
            self._send(new_method_return(msg, "o", (path,)))
            answered = [
                (ident, {"trigger_description": ("s", info["preferred_trigger"][1].replace("+", " + "))})
                for ident, info in shortcuts
                if ident not in self.refuse
            ]
            self._respond(path, self.response_code, {"shortcuts": ("a(sa{sv})", answered)})
        elif member == "Close":
            self.closed = True
            self._send(new_method_return(msg))

    def activate(self, shortcut: str, session: str | None = None) -> None:
        signal = new_signal(
            DBusAddress(PATH, interface=IFACE), "Activated", "osta{sv}", (session or self.session, shortcut, 0, {})
        )
        self._send(signal)
