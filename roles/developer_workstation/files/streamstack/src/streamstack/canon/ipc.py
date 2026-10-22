"""A tiny local control channel between the Console / CLI and the Canon bridge: one JSON line in, one out."""

from __future__ import annotations

import contextlib
import json
import os
import socket
import socketserver
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from streamstack import config

MAX_REQUEST = 16 * 1024
SOCKET_NAME = "streamstack-canon.sock"


class BridgeUnavailable(Exception):
    """The bridge is not running (or not answering) on its socket."""


def socket_path() -> Path:
    override = os.environ.get("STREAMSTACK_CANON_SOCKET")
    if override:
        return Path(override)
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return (Path(runtime) if runtime else config.state_dir()) / SOCKET_NAME


def request(payload: dict[str, Any], timeout: float = 25.0, path: Path | None = None) -> dict[str, Any]:
    """Send one request and return the bridge's answer. Raises BridgeUnavailable when nothing answers."""
    target = path or socket_path()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(target))
            sock.sendall(json.dumps(payload).encode("utf-8") + b"\n")
            data = bytearray()
            while not data.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
                if len(data) > 4 * 1024 * 1024:
                    raise BridgeUnavailable("the bridge sent an oversized answer")
    except (OSError, TimeoutError) as exc:
        raise BridgeUnavailable(f"Canon bridge not reachable at {target}: {exc}") from exc
    try:
        answer = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeUnavailable(f"the bridge answered with something unreadable: {exc}") from exc
    if not isinstance(answer, dict):
        raise BridgeUnavailable("the bridge answered with something unexpected")
    return answer


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True
    handler_fn: Callable[[dict[str, Any]], dict[str, Any]]


class _Handler(socketserver.StreamRequestHandler):
    server: _Server

    def handle(self) -> None:
        self.request.settimeout(10.0)
        try:
            line = self.rfile.readline(MAX_REQUEST + 1)
        except (OSError, TimeoutError):
            return
        if len(line) > MAX_REQUEST:
            answer: dict[str, Any] = {"ok": False, "error": "request too large"}
        else:
            try:
                req = json.loads(line.decode("utf-8"))
                if not isinstance(req, dict):
                    raise ValueError("a request is a JSON object")
                answer = self.server.handler_fn(req)
            except (UnicodeDecodeError, ValueError) as exc:
                answer = {"ok": False, "error": f"bad request: {exc}"}
        try:
            self.wfile.write(json.dumps(answer).encode("utf-8") + b"\n")
        except OSError:
            return  # the client went away; nothing to tell it


def serve(handler: Callable[[dict[str, Any]], dict[str, Any]], path: Path | None = None) -> tuple[_Server, Path]:
    """Start the socket server on a background thread; the caller shuts it down with `server.shutdown()`."""
    target = path or socket_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        target.unlink()  # a stale socket from a crashed bridge
    old = os.umask(0o177)  # the socket is created owner-only
    try:
        server = _Server(str(target), _Handler)
    finally:
        os.umask(old)
    server.handler_fn = handler
    threading.Thread(target=server.serve_forever, daemon=True, name="canon-ipc").start()
    return server, target
