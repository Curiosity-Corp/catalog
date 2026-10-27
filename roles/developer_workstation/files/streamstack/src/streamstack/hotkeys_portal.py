"""Global hotkeys on Wayland: the XDG Desktop Portal `org.freedesktop.portal.GlobalShortcuts` interface over D-Bus.

Wayland lets no program grab keys behind the compositor's back. The portal is the sanctioned way: StreamStack
registers its shortcuts, the desktop (KDE Plasma 6, GNOME 48 and later, Hyprland's portal) asks the person once to
confirm them and lets them move any shortcut to another key, and then reports `Activated` while any window has focus.

    CreateSession -> BindShortcuts([(id, {description, preferred_trigger})]) -> Activated(session, id, time, {})

A shortcut's id is its canonical combo ("ctrl+alt+m"), so an `Activated` maps straight back to a binding. The person
may rebind a shortcut in the desktop's own settings; the id stays, so the binding keeps working.

Needs the optional `shortcuts` extra (jeepney, a pure-Python D-Bus client). One thread owns the D-Bus connection.
The bind step can wait for the person to answer a dialog, so `set_combos` returns "pending" at once and the final
answer arrives through `on_status`.
"""

from __future__ import annotations

import contextlib
import os
import queue
import secrets
import threading
from collections.abc import Callable
from typing import Any

from streamstack import hotkeys

try:  # the extra is optional: availability() says what is missing
    from jeepney import DBusAddress, HeaderFields, MatchRule, MessageType, message_bus, new_method_call
    from jeepney.io.blocking import DBusConnection, open_dbus_connection
except ImportError:  # pragma: no cover - exercised when the extra is absent
    DBusAddress = HeaderFields = MatchRule = MessageType = message_bus = new_method_call = None
    DBusConnection = open_dbus_connection = None

PORTAL_NAME = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SHORTCUTS_IFACE = "org.freedesktop.portal.GlobalShortcuts"
REQUEST_IFACE = "org.freedesktop.portal.Request"
CALL_TIMEOUT = 5.0
BIND_TIMEOUT = 300.0  # the person may take a while to answer the desktop's dialog
POLL_SECONDS = 0.2


def available() -> str | None:
    """None when the portal backend can run on this machine, else why not (a reason a person can act on)."""
    if open_dbus_connection is None:
        return (
            "Install the shortcuts extra (pip install 'streamstack[shortcuts]') to use the desktop's shortcut portal."
        )
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS") and not os.path.exists(f"/run/user/{os.getuid()}/bus"):
        return "There is no D-Bus session bus."
    return None


def _variant(value: str) -> tuple[str, str]:
    return ("s", value)


class PortalError(RuntimeError):
    """The portal refused or does not exist; the message is for the person."""


class PortalBackend:
    """Registers combos with the GlobalShortcuts portal and reports activations. See the module notes."""

    name = "portal"

    def __init__(
        self,
        on_combo: Callable[[str], None],
        on_status: Callable[[dict[str, str]], None] | None = None,
        address: str | None = None,
    ) -> None:
        self._on_combo = on_combo
        self._on_status = on_status
        self._bus = address or "SESSION"
        self._thread: threading.Thread | None = None
        self._commands: queue.Queue[tuple[str, list[str]]] = queue.Queue()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._conn: Any = None
        self._session: str | None = None
        self._wanted: set[str] = set()
        self._pending: dict[str, tuple[str, list[str]]] = {}  # request path -> (what, combos)
        self.failure: str | None = None
        self.trigger_text: dict[str, str] = {}  # combo -> what the desktop says the shortcut is now bound to

    # -- control (any thread) ---------------------------------------------------

    def start(self) -> str | None:
        """Connect and create the session. None on success, else why it could not."""
        reason = available()
        if reason:
            return reason
        self._thread = threading.Thread(target=self._run, name="hotkeys-portal", daemon=True)
        self._thread.start()
        self._ready.wait(CALL_TIMEOUT * 3)
        if not self._ready.is_set():
            return "The desktop portal did not answer."
        return self.failure

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(3.0)
        self._thread = None

    def set_combos(self, combos: list[str], timeout: float = 0.0) -> dict[str, str]:
        """Ask the desktop for these combos. Answers "pending" now; `on_status` gets "ok" or "refused" later."""
        if self._thread is None or self._session is None:
            return dict.fromkeys(combos, "unavailable")
        self._commands.put(("bind", list(combos)))
        return dict.fromkeys(combos, "pending")

    # -- the thread that owns the connection ----------------------------------------

    def _run(self) -> None:
        try:
            self._conn = open_dbus_connection(bus=self._bus)
        except (OSError, ValueError) as exc:
            self.failure = f"Could not connect to the D-Bus session bus: {exc}"
            self._ready.set()
            return
        try:
            self._open_session()
        except PortalError as exc:
            self.failure = str(exc)
            self._ready.set()
            self._conn.close()
            return
        self._ready.set()
        try:
            self._loop()
        finally:
            with contextlib.suppress(OSError, PortalError):
                self._close_session()
            self._conn.close()

    def _address(self, path: str = PORTAL_PATH, interface: str = SHORTCUTS_IFACE) -> Any:
        return DBusAddress(path, bus_name=PORTAL_NAME, interface=interface)

    def _call(self, msg: Any) -> Any:
        try:
            reply = self._conn.send_and_get_reply(msg, timeout=CALL_TIMEOUT)
        except TimeoutError as exc:
            raise PortalError("The desktop portal did not answer.") from exc
        except OSError as exc:
            raise PortalError(f"The D-Bus connection failed: {exc}") from exc
        if reply.header.message_type is MessageType.error:
            name = reply.header.fields.get(HeaderFields.error_name, "")
            detail = reply.body[0] if reply.body else ""
            if "ServiceUnknown" in str(name) or "NameHasNoOwner" in str(name):
                raise PortalError("There is no desktop portal on this session (xdg-desktop-portal is not running).")
            raise PortalError(f"{name}: {detail}".strip(": "))
        return reply

    def _request_path(self, token: str) -> str:
        sender = self._conn.unique_name.lstrip(":").replace(".", "_")
        return f"/org/freedesktop/portal/desktop/request/{sender}/{token}"

    def _watch(self, path: str) -> None:
        rule = MatchRule(type="signal", interface=REQUEST_IFACE, member="Response", path=path)
        self._call(message_bus.AddMatch(rule))

    def _watch_activations(self) -> None:
        rule = MatchRule(type="signal", interface=SHORTCUTS_IFACE, path=PORTAL_PATH)
        self._call(message_bus.AddMatch(rule))

    def _open_session(self) -> None:
        # the portal needs the interface to exist: ask for its version first so a missing one gives a clear reason
        version = new_method_call(
            self._address(interface="org.freedesktop.DBus.Properties"), "Get", "ss", (SHORTCUTS_IFACE, "version")
        )
        try:
            self._call(version)
        except PortalError as exc:
            if "UnknownInterface" in str(exc) or "UnknownProperty" in str(exc) or "InvalidArgs" in str(exc):
                raise PortalError(
                    "This desktop has no GlobalShortcuts portal (KDE Plasma 6 and GNOME 48 or later have one)."
                ) from exc
            raise
        self._watch_activations()
        token, session_token = f"ss{secrets.token_hex(6)}", f"ss{secrets.token_hex(6)}"
        path = self._request_path(token)
        self._watch(path)
        options = {"handle_token": _variant(token), "session_handle_token": _variant(session_token)}
        self._call(new_method_call(self._address(), "CreateSession", "a{sv}", (options,)))
        results = self._wait_response(path, CALL_TIMEOUT * 2)
        session = results.get("session_handle")
        if not session or not isinstance(session[1], str):
            raise PortalError("The desktop portal did not create a shortcuts session.")
        self._session = session[1]

    def _close_session(self) -> None:
        if self._session:
            msg = new_method_call(self._address(self._session, "org.freedesktop.portal.Session"), "Close")
            self._call(msg)
            self._session = None

    def _wait_response(self, path: str, timeout: float) -> dict[str, Any]:
        """Wait for the Response of one request, handing other messages (activations) on as they come."""
        deadline = timeout
        waited = 0.0
        while waited < deadline:
            try:
                msg = self._conn.receive(timeout=POLL_SECONDS)
            except TimeoutError:
                waited += POLL_SECONDS
                continue
            outcome = self._dispatch(msg)
            if outcome and outcome[0] == path:
                code, results = outcome[1], outcome[2]
                if code != 0:
                    raise PortalError(
                        "The shortcut request was cancelled." if code == 1 else "The desktop refused the request."
                    )
                return results
        raise PortalError("The desktop portal did not answer.")

    def _dispatch(self, msg: Any) -> tuple[str, int, dict[str, Any]] | None:
        """Handle one incoming message; returns (request path, code, results) for a Response."""
        header = msg.header
        if header.message_type is not MessageType.signal:
            return None
        member = header.fields.get(HeaderFields.member)
        if member == "Response":
            code, results = msg.body
            return header.fields.get(HeaderFields.path, ""), int(code), results
        if member == "Activated" and len(msg.body) >= 2:
            session, shortcut = msg.body[0], msg.body[1]
            if session == self._session and shortcut in self._wanted:
                self._deliver(shortcut)
        return None

    def _deliver(self, combo: str) -> None:
        try:
            self._on_combo(combo)
        except Exception as exc:
            self.failure = f"a hotkey handler failed: {exc}"

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                command, combos = self._commands.get_nowait()
            except queue.Empty:
                pass
            else:
                if command == "bind":
                    self._bind(combos)
                continue
            try:
                msg = self._conn.receive(timeout=POLL_SECONDS)
            except TimeoutError:
                continue
            except OSError as exc:
                self.failure = f"The D-Bus connection ended: {exc}"
                return
            outcome = self._dispatch(msg)
            if outcome:
                self._finish(outcome)

    def _finish(self, outcome: tuple[str, int, dict[str, Any]]) -> None:
        pending = self._pending.pop(outcome[0], None)
        if pending is None:
            return
        _, combos = pending
        self._report(combos, outcome[1], outcome[2])

    def _report(self, combos: list[str], code: int, results: dict[str, Any]) -> None:
        bound: dict[str, str] = {}
        if code == 0:
            entry = results.get("shortcuts")
            for ident, info in entry[1] if entry else []:
                trigger = info.get("trigger_description")
                bound[ident] = trigger[1] if trigger else ""
        status = {c: ("ok" if c in bound else "refused") for c in combos}
        self.trigger_text.update({c: t for c, t in bound.items() if t})
        if self._on_status:
            self._on_status(status)

    def _bind(self, combos: list[str]) -> None:
        self._wanted = set(combos)
        if not combos:
            return
        token = f"ss{secrets.token_hex(6)}"
        path = self._request_path(token)
        shortcuts = [
            (
                combo,
                {"description": _variant(f"StreamStack {hotkeys.describe_combo(combo)}"),
                 "preferred_trigger": _variant(hotkeys.xdg_trigger(combo))},
            )
            for combo in combos
        ]  # fmt: skip
        try:
            self._watch(path)
            msg = new_method_call(
                self._address(), "BindShortcuts", "oa(sa{sv})sa{sv}",
                (self._session, shortcuts, "", {"handle_token": _variant(token)}),
            )  # fmt: skip
            self._call(msg)
        except PortalError as exc:
            self.failure = str(exc)
            if self._on_status:
                self._on_status(dict.fromkeys(combos, "refused"))
            return
        self._pending[path] = ("bind", combos)
