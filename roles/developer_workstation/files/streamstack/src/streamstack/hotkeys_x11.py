"""Global hotkeys on X11: `XGrabKey` on the root window through libX11 (ctypes, no extra package).

A grab makes the X server deliver the combo to StreamStack whichever window has focus, including a call window, and
keeps the key from reaching that window. It is the only mechanism on a plain X11 session; on Wayland the portal backend
(hotkeys_portal.py) does the same job, and XWayland grabs would only see X clients, so this backend is not used there.

One thread owns the display connection (Xlib is not thread-safe). `set_combos` hands it the full list of combos to hold;
it grabs each one with every NumLock / CapsLock variation, and reports per combo whether it worked:

* "ok": held;
* "conflict": another program already holds it (`BadAccess`): the desktop, a window manager or another tool;
* "unmapped": the keyboard layout has no key with that name.

Pressing a held combo calls `on_combo("ctrl+alt+m")` on the backend's thread; keep the callback quick.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import os
import queue
import select
import threading
from collections.abc import Callable
from typing import Any

from streamstack import hotkeys

KEY_PRESS = 2
MAPPING_NOTIFY = 34
MAPPING_KEYBOARD = 1
BAD_ACCESS = 10
GRAB_MODE_ASYNC = 1
MOD_MASK = {"shift": 1, "ctrl": 4, "alt": 8, "super": 64}
LOCK_MASK, NUM_MASK = 2, 16
_IGNORED = LOCK_MASK | NUM_MASK
_RELEVANT = 1 | 4 | 8 | 64
_LOCK_VARIANTS = (0, LOCK_MASK, NUM_MASK, LOCK_MASK | NUM_MASK)


class _XKeyEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p), ("window", ctypes.c_ulong), ("root", ctypes.c_ulong),
        ("subwindow", ctypes.c_ulong), ("time", ctypes.c_ulong), ("x", ctypes.c_int), ("y", ctypes.c_int),
        ("x_root", ctypes.c_int), ("y_root", ctypes.c_int), ("state", ctypes.c_uint),
        ("keycode", ctypes.c_uint), ("same_screen", ctypes.c_int),
    ]  # fmt: skip


class _XMappingEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p), ("window", ctypes.c_ulong), ("request", ctypes.c_int),
        ("first_keycode", ctypes.c_int), ("count", ctypes.c_int),
    ]  # fmt: skip


class _XEvent(ctypes.Union):
    _fields_ = [
        ("type", ctypes.c_int), ("xkey", _XKeyEvent), ("xmapping", _XMappingEvent), ("pad", ctypes.c_long * 24),
    ]  # fmt: skip


class _XErrorEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int), ("display", ctypes.c_void_p), ("resourceid", ctypes.c_ulong),
        ("serial", ctypes.c_ulong), ("error_code", ctypes.c_ubyte), ("request_code", ctypes.c_ubyte),
        ("minor_code", ctypes.c_ubyte),
    ]  # fmt: skip


_ErrorHandler = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(_XErrorEvent))


def _load() -> ctypes.CDLL | None:
    name = ctypes.util.find_library("X11") or "libX11.so.6"
    try:
        lib = ctypes.CDLL(name)
    except OSError:
        return None
    lib.XOpenDisplay.restype = ctypes.c_void_p
    lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    lib.XDefaultRootWindow.restype = ctypes.c_ulong
    lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    lib.XConnectionNumber.argtypes = [ctypes.c_void_p]
    lib.XStringToKeysym.restype = ctypes.c_ulong
    lib.XStringToKeysym.argtypes = [ctypes.c_char_p]
    lib.XKeysymToKeycode.restype = ctypes.c_ubyte
    lib.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    lib.XGrabKey.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ]  # fmt: skip
    lib.XUngrabKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_ulong]
    lib.XPending.argtypes = [ctypes.c_void_p]
    lib.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.POINTER(_XEvent)]
    lib.XRefreshKeyboardMapping.argtypes = [ctypes.POINTER(_XMappingEvent)]
    lib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.XSetErrorHandler.argtypes = [_ErrorHandler]
    lib.XSetErrorHandler.restype = ctypes.c_void_p
    return lib


def available() -> str | None:
    """None when X11 grabs can work here, else the reason they cannot."""
    if not os.environ.get("DISPLAY"):
        return "There is no X11 display (DISPLAY is not set)."
    if _load() is None:
        return "libX11 is not installed."
    return None


class X11Backend:
    """Holds a set of combos on the X11 root window. `set_combos` is thread-safe and returns each combo's status."""

    name = "x11"

    def __init__(
        self, on_combo: Callable[[str], None], on_status: Callable[[dict[str, str]], None] | None = None
    ) -> None:
        self._on_combo = on_combo
        self._on_status = on_status  # told the new statuses when a layout change moved the keys
        self._thread: threading.Thread | None = None
        self._wake_r = self._wake_w = -1
        self._commands: queue.Queue[tuple[str, list[str], queue.Queue[dict[str, str]] | None]] = queue.Queue()
        self._held: dict[tuple[int, int], str] = {}
        self._combos: list[str] = []
        self._status: dict[str, str] = {}
        self._errors: list[int] = []
        self._handler = _ErrorHandler(self._on_error)  # kept alive for as long as the backend lives
        self._ready = threading.Event()
        self.failure: str | None = None

    # -- control (any thread) ---------------------------------------------------

    def start(self) -> str | None:
        """Open the connection and start listening. None on success, else why it could not."""
        reason = available()
        if reason:
            return reason
        self._wake_r, self._wake_w = os.pipe()
        self._thread = threading.Thread(target=self._run, name="hotkeys-x11", daemon=True)
        self._thread.start()
        self._ready.wait(5.0)
        return self.failure

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._commands.put(("stop", [], None))
        self._poke()
        thread.join(3.0)
        self._thread = None
        for fd in (self._wake_r, self._wake_w):
            with contextlib.suppress(OSError):
                os.close(fd)
        self._wake_r = self._wake_w = -1

    def set_combos(self, combos: list[str], timeout: float = 5.0) -> dict[str, str]:
        """Hold exactly `combos`; returns {combo: "ok" | "conflict" | "unmapped"}."""
        if self._thread is None:
            return dict.fromkeys(combos, "unavailable")
        answer: queue.Queue[dict[str, str]] = queue.Queue(1)
        self._commands.put(("grab", list(combos), answer))
        self._poke()
        try:
            return answer.get(timeout=timeout)
        except queue.Empty:
            return dict.fromkeys(combos, "unavailable")

    def _poke(self) -> None:
        with contextlib.suppress(OSError):
            os.write(self._wake_w, b"x")

    # -- the thread that owns the display -----------------------------------------

    def _on_error(self, display: int, event: Any) -> int:
        self._errors.append(event.contents.error_code)
        return 0

    def _run(self) -> None:
        lib = _load()
        if lib is None:
            self.failure = "libX11 is not installed."
            self._ready.set()
            return
        lib.XSetErrorHandler(self._handler)
        display = lib.XOpenDisplay(None)
        if not display:
            self.failure = "Could not open the X11 display."
            self._ready.set()
            return
        root = lib.XDefaultRootWindow(display)
        xfd = lib.XConnectionNumber(display)
        self._ready.set()
        try:
            self._loop(lib, display, root, xfd)
        finally:
            lib.XCloseDisplay(display)

    def _loop(self, lib: ctypes.CDLL, display: int, root: int, xfd: int) -> None:
        event = _XEvent()
        while True:
            while lib.XPending(display):
                lib.XNextEvent(display, ctypes.byref(event))
                if event.type == KEY_PRESS:
                    combo = self._held.get((event.xkey.keycode, event.xkey.state & _RELEVANT))
                    if combo:
                        self._deliver(combo)
                elif event.type == MAPPING_NOTIFY and event.xmapping.request == MAPPING_KEYBOARD:
                    # the layout changed: the same names may now sit on other keycodes
                    lib.XRefreshKeyboardMapping(ctypes.byref(event.xmapping))
                    if self._combos:
                        status = self._grab(lib, display, root, self._combos)
                        if status != self._status and self._on_status:
                            self._on_status(status)
                        self._status = status
            ready, _, _ = select.select([xfd, self._wake_r], [], [], 1.0)
            if self._wake_r in ready:
                with contextlib.suppress(OSError):
                    os.read(self._wake_r, 4096)
                while True:
                    try:
                        command, combos, answer = self._commands.get_nowait()
                    except queue.Empty:
                        break
                    if command == "stop":
                        self._release_all(lib, display, root)
                        return
                    result = self._grab(lib, display, root, combos)
                    self._status = result
                    if answer is not None:
                        answer.put(result)

    def _deliver(self, combo: str) -> None:
        try:
            self._on_combo(combo)
        except Exception as exc:
            self.failure = f"a hotkey handler failed: {exc}"

    def _release_all(self, lib: ctypes.CDLL, display: int, root: int) -> None:
        for keycode, mask in list(self._held):
            for lock in _LOCK_VARIANTS:
                lib.XUngrabKey(display, keycode, mask | lock, root)
        lib.XSync(display, 0)
        self._held.clear()

    def _grab(self, lib: ctypes.CDLL, display: int, root: int, combos: list[str]) -> dict[str, str]:
        """Hold exactly `combos`. Grabs that are still right are left alone, so a press is never lost to a re-grab."""
        self._combos = list(combos)
        wanted: dict[str, tuple[int, int] | None] = {}
        for combo in combos:
            mods, key = hotkeys.split_combo(combo)
            keycode = lib.XKeysymToKeycode(display, lib.XStringToKeysym(hotkeys.keysym(key).encode()))
            mask = 0
            for mod in mods:
                mask |= MOD_MASK[mod]
            wanted[combo] = (keycode, mask) if keycode else None
        keep = {slot for slot in wanted.values() if slot is not None and self._held.get(slot) is not None}
        for slot in [s for s in self._held if s not in keep]:
            for lock in _LOCK_VARIANTS:
                lib.XUngrabKey(display, slot[0], slot[1] | lock, root)
            del self._held[slot]
        lib.XSync(display, 0)
        status: dict[str, str] = {}
        for combo, slot in wanted.items():
            if slot is None:
                status[combo] = "unmapped"
                continue
            if slot in self._held:
                self._held[slot] = combo
                status[combo] = "ok"
                continue
            keycode, mask = slot
            won: list[int] = []
            for lock in _LOCK_VARIANTS:
                self._errors.clear()
                lib.XGrabKey(display, keycode, mask | lock, root, 0, GRAB_MODE_ASYNC, GRAB_MODE_ASYNC)
                lib.XSync(display, 0)  # errors arrive here, so BadAccess is known before we answer
                if BAD_ACCESS not in self._errors:
                    won.append(lock)
            if len(won) < len(_LOCK_VARIANTS):
                # someone else holds it: give back only the variations that are ours (an ungrab of a key another
                # client holds is not ours to send)
                for lock in won:
                    lib.XUngrabKey(display, keycode, mask | lock, root)
                lib.XSync(display, 0)
                status[combo] = "conflict"
                continue
            self._held[slot] = combo
            status[combo] = "ok"
        return status
