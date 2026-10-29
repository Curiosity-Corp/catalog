"""`streamstack console`: start (or find) the Console server and open its window.

One server per login: its port and token live in $XDG_RUNTIME_DIR/streamstack-console.json
(mode 600), so a second launch only opens another window on the running one.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shlex
import shutil
import signal
import socket
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

from streamstack import config, store, system

BROWSER_ENV = "STREAMSTACK_CONSOLE_BROWSER"
BROWSERS = (
    "google-chrome",
    "google-chrome-stable",
    "microsoft-edge",
    "microsoft-edge-stable",
    "chromium",
    "chromium-browser",
)
WINDOW_SIZE = (1360, 860)
MINI_SIZE = (420, 760)
WINDOW_IDLE_EXIT = 600.0  # seconds with no window open before a server we started for a window quits


# ---------------------------------------------------------------------------
# The runtime file: who is serving, where, with which token
# ---------------------------------------------------------------------------


def runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/streamstack-{os.getuid()}")


def runtime_file() -> Path:
    return runtime_dir() / "streamstack-console.json"


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # someone else's process: the pid exists
    return True


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def read_running() -> dict | None:
    """The live server's {"pid", "port", "token"}, or None; a file left by a dead process is ignored."""
    info = store.read_json(runtime_file(), None)
    if not isinstance(info, dict):
        return None
    pid, port, token = info.get("pid"), info.get("port"), info.get("token")
    if not (isinstance(pid, int) and isinstance(port, int) and isinstance(token, str) and token):
        return None
    return info if pid_alive(pid) and port_open(port) else None


def write_runtime(pid: int, port: int, token: str) -> None:
    path = runtime_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # holds the Console token: private, atomic (a second launch must never read half of it); a runtime file is
    # worthless after a reboot, so no fsync and no backup copy of the token is kept
    store.write_json(path, {"pid": pid, "port": port, "token": token}, mode=0o600, backup=False, durable=False)


def remove_runtime(pid: int) -> None:
    """Delete the runtime file, but only when it is ours (a newer server may own it by now)."""
    try:
        if json.loads(runtime_file().read_text()).get("pid") == pid:
            runtime_file().unlink()
    except (OSError, ValueError, AttributeError):
        pass


@contextlib.contextmanager
def startup_lock() -> Iterator[None]:
    """Serialise 'is one running? else start one' so two launches never both start a server."""
    path = runtime_dir() / "streamstack-console.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


def find_browser() -> list[str] | None:
    override = os.environ.get(BROWSER_ENV, "").strip()
    if override:
        parts = shlex.split(override)
        return parts if parts and shutil.which(parts[0]) else None
    for name in BROWSERS:
        if shutil.which(name):
            return [name]
    return None


def window_url(url: str, mini: bool) -> str:
    return url + "#/mini" if mini else url


def log_address(port: int, mini: bool = False) -> str:
    """The line the Console logs when it serves: the address WITHOUT its access token.

    stdout of the headless service lands in the systemd journal, which other processes of the user can read and
    log shippers collect; the token grants control of the rig, so it is only printed by `streamstack console --url`.
    """
    address = window_url(f"http://127.0.0.1:{port}/", mini)
    return f"StreamStack Console: {address} (access token not logged: `streamstack console --url`)"


def print_url(mini: bool = False) -> int:
    """`streamstack console --url`: the running Console's full address, token included, to stdout. Never starts one."""
    running = read_running()
    if not running:
        print("streamstack: no Console is running (start it with `streamstack console`)", file=sys.stderr)
        return 1
    print(window_url(f"http://127.0.0.1:{running['port']}/?token={running['token']}", mini), flush=True)
    return 0


def app_argv(browser: list[str], url: str, mini: bool = False) -> list[str]:
    """A Chrome/Edge/Chromium app window (no tabs or address bar) with its own profile."""
    width, height = MINI_SIZE if mini else WINDOW_SIZE
    profile = config.cache_dir() / "console-browser"
    return [
        *browser,
        f"--app={window_url(url, mini)}",
        "--class=StreamStack",
        f"--user-data-dir={profile}",
        f"--window-size={width},{height}",
        "--no-first-run",
        "--no-default-browser-check",
    ]


def open_window(url: str, mini: bool = False) -> bool:
    """Open the Console; False when there is nothing to open it with (the URL is printed anyway)."""
    browser = find_browser()
    if browser:
        argv = app_argv(browser, url, mini)
    elif shutil.which("xdg-open"):
        argv = ["xdg-open", window_url(url, mini)]
    else:
        return False
    try:
        system.detach(argv)
    except OSError as exc:
        print(f"streamstack: could not open a window: {exc}", file=sys.stderr)
        return False
    return True


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def run_console(
    demo: bool = False,
    port: int = 0,
    window: bool = True,
    mini: bool = False,
    idle_exit: float | None = None,
    poll: float = 2.0,
    url_only: bool = False,
) -> int:
    from streamstack.console.rig import Rig
    from streamstack.console.server import ConsoleServer

    if url_only:
        return print_url(mini)
    with startup_lock():
        running = read_running()
        if running:
            url = f"http://127.0.0.1:{running['port']}/?token={running['token']}"
            if window and not open_window(url, mini):
                print("streamstack: no browser found; `streamstack console --url` prints the address", file=sys.stderr)
            print(log_address(running["port"], mini), flush=True)
            return 0
        try:
            server = ConsoleServer(Rig.demo() if demo else Rig.live(), port=port)
        except (OSError, ImportError) as exc:
            print(f"streamstack: could not start the Console: {exc}", file=sys.stderr)
            return 1
        write_runtime(os.getpid(), server.port, server.token)

    stop = threading.Event()
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda _signum, _frame: stop.set())
    try:
        server.start()
        url = server.url()
        print(log_address(server.port, mini), flush=True)
        if window and not open_window(url, mini):
            print("streamstack: no browser found; `streamstack console --url` prints the address", file=sys.stderr)
        limit = WINDOW_IDLE_EXIT if idle_exit is None and window else (idle_exit or 0.0)
        while not stop.wait(poll):
            if limit and server.hub.idle_seconds() > limit:
                print("StreamStack Console: no window open, shutting down", file=sys.stderr)
                break
    finally:
        server.close()
        remove_runtime(os.getpid())
    return 0


def main(argv: list[str] | None = None) -> int:
    """The `streamstack-console` launcher: same as `streamstack console`."""
    from streamstack import cli

    return cli.main(["console", *(sys.argv[1:] if argv is None else argv)])
