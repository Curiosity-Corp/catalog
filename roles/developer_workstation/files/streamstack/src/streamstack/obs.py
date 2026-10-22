"""OBS Studio through obs-websocket 5 (built into OBS 28 and later)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
from collections import deque
from pathlib import Path

from streamstack import system

COLOR_FILTER = "Lighting & Color"
LUT_FILTER = "Cinematic LUT"
SHARPEN_FILTER = "Sharpen"

# obs-websocket event subscription bits (EventSubscription in the protocol docs).
EVENTS_SCENES = 1 << 2
EVENTS_FILTERS = 1 << 5
EVENTS_OUTPUTS = 1 << 6
EVENTS_SCENE_ITEMS = 1 << 7


def websocket_config() -> Path:
    return Path.home() / ".config/obs-studio/plugin_config/obs-websocket/config.json"


def ensure_websocket() -> str:
    """Enable OBS's local websocket with a per-user random password.

    OBS rewrites this file when it exits, so it is only edited while OBS is
    not running. Returns "ready", "restart-obs" or "edited".
    """
    path = websocket_config()
    try:
        config = json.loads(path.read_text())
    except (OSError, ValueError):
        config = {}
    if config.get("server_enabled") and config.get("auth_required") and config.get("server_password"):
        return "ready"
    if system.running("obs"):
        return "restart-obs"
    config.update(
        {
            "server_enabled": True,
            "auth_required": True,
            "server_password": config.get("server_password") or base64.urlsafe_b64encode(os.urandom(24)).decode(),
            "server_port": config.get("server_port", 4455),
            "alerts_enabled": config.get("alerts_enabled", False),
            "first_load": False,
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(config, handle, indent=4)
    return "edited"


def auth_response(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest())
    return base64.b64encode(hashlib.sha256(secret + challenge.encode()).digest()).decode()


class OBS:
    """A small synchronous obs-websocket 5 client (python3-websocket)."""

    def __init__(self, timeout: float = 10, events: int = 0) -> None:
        import websocket

        # One lock per connection: a request and its reply must not interleave
        # with another thread's, and events (when subscribed) queue in `backlog`.
        self.lock = threading.RLock()
        self.timeout = timeout
        self.backlog: deque[dict] = deque(maxlen=512)

        config = json.loads(websocket_config().read_text())
        self.ws = websocket.create_connection(f"ws://127.0.0.1:{config.get('server_port', 4455)}", timeout=timeout)
        hello = json.loads(self.ws.recv())["d"]
        identify: dict = {"rpcVersion": 1, "eventSubscriptions": events}
        if "authentication" in hello:
            auth = hello["authentication"]
            identify["authentication"] = auth_response(config["server_password"], auth["salt"], auth["challenge"])
        self.ws.send(json.dumps({"op": 1, "d": identify}))
        reply = json.loads(self.ws.recv())
        if reply.get("op") != 2:
            raise RuntimeError("OBS refused the websocket login")
        self.counter = 0

    def call(self, request: str, **data) -> dict:
        with self.lock:
            self.counter += 1
            rid = str(self.counter)
            self.ws.send(json.dumps({"op": 6, "d": {"requestType": request, "requestId": rid, "requestData": data}}))
            while True:
                message = json.loads(self.ws.recv())
                if message.get("op") == 5:
                    self.backlog.append(message["d"])
                elif message.get("op") == 7 and message["d"].get("requestId") == rid:
                    status = message["d"]["requestStatus"]
                    if not status.get("result"):
                        raise RuntimeError(f"{request}: {status.get('comment', status.get('code'))}")
                    return message["d"].get("responseData", {})

    def wait_event(self, timeout: float) -> dict | None:
        """The next subscribed event, or None after `timeout` seconds. Raises when the socket is gone."""
        import websocket

        with self.lock:
            if self.backlog:
                return self.backlog.popleft()
            self.ws.settimeout(timeout)
            try:
                while True:
                    message = json.loads(self.ws.recv())
                    if message.get("op") == 5:
                        return message["d"]
            except (websocket.WebSocketTimeoutException, TimeoutError):
                return None
            finally:
                try:
                    self.ws.settimeout(self.timeout)
                except (OSError, ValueError, AttributeError):
                    pass  # the socket is closed; the caller sees that on its next call

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: S110 - closing a dead socket must never raise
            pass

    def camera_sources(self) -> list[str]:
        """Enabled sources in the program scene that carry the StreamStack colour filter."""
        scene = self.call("GetCurrentProgramScene")["currentProgramSceneName"]
        names = []
        for item in self.call("GetSceneItemList", sceneName=scene)["sceneItems"]:
            if not item.get("sceneItemEnabled", True):
                continue
            try:
                self.call("GetSourceFilter", sourceName=item["sourceName"], filterName=COLOR_FILTER)
            except RuntimeError:
                continue
            names.append(item["sourceName"])
        return names

    def screenshot(self, source: str, path: Path, width: int = 768) -> Path:
        data = self.call(
            "GetSourceScreenshot", sourceName=source, imageFormat="jpg", imageWidth=width, imageCompressionQuality=82
        )["imageData"]
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(base64.b64decode(data.split(",", 1)[1]))
        return path

    def filter_settings(self, source: str, name: str) -> tuple[bool, dict]:
        data = self.call("GetSourceFilter", sourceName=source, filterName=name)
        return data["filterEnabled"], data["filterSettings"]

    def set_filter(self, source: str, name: str, values: dict) -> None:
        self.call("SetSourceFilterSettings", sourceName=source, filterName=name, filterSettings=values, overlay=True)

    def enable_filter(self, source: str, name: str, enabled: bool) -> None:
        self.call("SetSourceFilterEnabled", sourceName=source, filterName=name, filterEnabled=enabled)
