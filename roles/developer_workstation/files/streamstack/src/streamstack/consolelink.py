"""A client for the running Console's local API: how out-of-process code reaches the native video engine.

The native engine lives inside the Console process (`streamstack.console.native.NativeVideo`): a look change is a
few bytes on the ffmpeg child's stdin, so the one place that can change the picture is the Console. Anything that
runs in its own process (the auto director, `streamstack auto reset`) goes through the same door the Console's own
window uses: `POST /api/action` for changes, `GET /api/state` for what is in place, `GET /api/preview.jpg` for the
graded picture the call receives. The port and the token come from the runtime file the Console writes (mode 600,
see console.launcher), so there is nothing to configure and nothing listens beyond 127.0.0.1.

The surface is deliberately the one the director's video actuator needs: which camera is on air, its look, a
picture of the program output, and `set_look`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from streamstack import store
from streamstack.console import launcher

TOKEN_HEADER = "X-StreamStack-Token"  # noqa: S105 - the name of a header, not a secret
LOOK_KEYS = (
    "brightness",
    "contrast",
    "gamma",
    "saturation",
    "warmth",
    "lut_enabled",
    "lut_amount",
    "sharpen_enabled",
)


class ConsoleUnavailable(RuntimeError):
    """The Console is not running (or not answering): start it with `streamstack console`."""


class ConsoleRefused(RuntimeError):
    """The Console answered and said no (the message is its own words)."""


class ConsoleLink:
    """One Console, found through the runtime file. Every call is a short, bounded HTTP request."""

    def __init__(self, port: int, token: str, timeout: float = 5.0) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self.token = token
        self.timeout = timeout

    @classmethod
    def find(cls, timeout: float = 5.0) -> ConsoleLink | None:
        """The running Console, or None when no live one is registered."""
        info = launcher.read_running()
        if info is None:
            return None
        return cls(int(info["port"]), str(info["token"]), timeout)

    def close(self) -> None:
        """Nothing is held open between calls; present so callers can treat every link alike."""

    def _request(self, method: str, path: str, body: dict | None = None) -> bytes:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header(TOKEN_HEADER, self.token)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise ConsoleRefused(_error_text(exc)) from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ConsoleUnavailable(f"the Console is not answering ({exc})") from exc

    def state(self) -> dict:
        """The video block of the Console's snapshot."""
        raw = self._request("GET", "/api/state")
        try:
            video = json.loads(raw)["video"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ConsoleUnavailable(f"the Console sent an unreadable state ({exc})") from exc
        if not isinstance(video, dict):
            raise ConsoleUnavailable("the Console sent no video state")
        return video

    def section(self, name: str) -> dict:
        """One object of the Console's snapshot (`preflight`, `call`, ...)."""
        raw = self._request("GET", "/api/state")
        try:
            value = json.loads(raw)[name]
        except (ValueError, KeyError, TypeError) as exc:
            raise ConsoleUnavailable(f"the Console sent no {name} state ({exc})") from exc
        if not isinstance(value, dict):
            raise ConsoleUnavailable(f"the Console sent an unreadable {name} state")
        return value

    def act(self, action: str, **params: object) -> None:
        self._request("POST", "/api/action", {"action": action, **params})

    def camera_sources(self) -> list[str]:
        """The camera the program output is showing and the director may grade; empty while the engine is not
        on air (camera absent, no signal, program camera switched off) or privacy has the picture blacked out."""
        video = self.state()
        engine = video.get("engine") or {}
        active = video.get("selected_source")
        if not active or engine.get("state") != "running" or video.get("privacy"):
            return []
        return [str(active)]

    def screenshot(self, source: str, path: Path, width: int = 768) -> Path:
        """One JPEG of the graded program picture (what Teams and Zoom receive), written to `path`."""
        image = self._request("GET", f"/api/preview.jpg?source=program&width={int(width)}")
        store.atomic_write_bytes(path, image, mode=0o600)
        return path

    def look(self, source: str) -> dict:
        """The look in place on `source` (the on-air camera): the eight values the director moves."""
        video = self.state()
        if video.get("selected_source") != source:
            raise ConsoleRefused(f"{source} is not the camera the video engine is editing.")
        look = video.get("look") or {}
        return {key: look[key] for key in LOOK_KEYS if key in look}

    def set_look(self, source: str, values: dict) -> None:
        """Apply look values now, between two frames, without a restart."""
        self.act("video.look", source=source, values=values)


def _error_text(exc: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(exc.read())
        return str(payload.get("error") or exc.reason)
    except (ValueError, OSError, AttributeError):
        return str(exc.reason)
