"""The rig facade: audio, video and studio controllers behind one state snapshot and one action router."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from streamstack import __version__
from streamstack.console.errors import ActionError
from streamstack.console.interface import empty_state as no_interface


def _no_lighting() -> dict:
    from streamstack.console.lighting import empty_state

    return empty_state()


ACTION_NAME = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")
Notify = Callable[[str, str], None]


class Rig:
    """Routes actions by prefix and assembles the snapshot the frontend renders.

    Controllers are injected so tests and `--demo` can swap the real hardware
    for simulations. The rig hands each of them a `notify(level, text)`
    callable as the attribute `notify` (level is "info", "ok", "warn" or
    "error"); a controller calls it from any thread to raise a toast in every
    open window.
    """

    def __init__(
        self,
        audio: Any,
        video: Any,
        studio: Any,
        demo: bool = False,
        lighting: Any = None,
        interface: Any = None,
        framing: Any = None,
    ) -> None:
        self.audio, self.video, self.studio = audio, video, studio
        self.lighting = lighting  # optional: a rig without lights reports an empty lighting object
        self.interface = interface  # optional: a rig without an audio interface profile reports none present
        self.framing = framing  # optional: auto-framing (framing.* actions, "framing" state)
        self.demo = demo
        self.on_toast: Notify | None = None  # the hub sets this
        self._started = False
        self._lock = threading.Lock()
        for controller in (audio, video, studio, lighting, interface, framing):
            if controller is not None:
                controller.notify = self.notify

    # -- construction ---------------------------------------------------------

    @classmethod
    def live(cls, cfg: dict[str, str] | None = None) -> Rig:
        """The real rig. The domain modules are imported here so a broken one cannot stop `streamstack --version`."""
        from streamstack import config
        from streamstack.console.audio import AudioController
        from streamstack.console.framing import FramingController
        from streamstack.console.interface import InterfaceController
        from streamstack.console.lighting import LightingController
        from streamstack.console.studio import StudioController
        from streamstack.console.video import VideoController

        cfg = cfg or config.load_settings()
        video = VideoController(cfg)
        return cls(
            AudioController(cfg),
            video,
            StudioController(cfg),
            lighting=LightingController(),
            interface=InterfaceController(cfg),
            framing=FramingController(cfg, video),
        )

    @classmethod
    def demo(cls) -> Rig:
        """A simulated rig: nothing is probed or touched, every action works."""
        from streamstack.console.audio import DemoAudio
        from streamstack.console.framing import DemoFraming
        from streamstack.console.interface import DemoInterface
        from streamstack.console.lighting import DemoLighting
        from streamstack.console.studio import DemoStudio
        from streamstack.console.video import DemoVideo

        return cls(
            DemoAudio(),
            DemoVideo(),
            DemoStudio(),
            demo=True,
            lighting=DemoLighting(),
            interface=DemoInterface(),
            framing=DemoFraming(),
        )

    # -- lifecycle ------------------------------------------------------------

    def _controllers(self) -> tuple[Any, ...]:
        # started in this order, stopped in reverse: framing needs the video link, so it stops first
        controllers = (self.studio, self.video, self.audio)
        controllers = (*controllers, *(c for c in (self.lighting, self.interface) if c is not None))
        return controllers if self.framing is None else (*controllers, self.framing)

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        for controller in self._controllers():
            controller.start()

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._started = False
        for controller in reversed(self._controllers()):
            try:
                controller.stop()
            except Exception as exc:  # one controller failing must not leave the others running
                self.notify("error", f"{type(controller).__name__} did not stop cleanly: {exc}")

    # -- notifications --------------------------------------------------------

    def notify(self, level: str, text: str) -> None:
        if self.on_toast:
            self.on_toast(level if level in ("info", "ok", "warn", "error") else "info", str(text))

    # -- state ----------------------------------------------------------------

    @staticmethod
    def _live_checks(controller: Any) -> list[dict]:
        live = getattr(controller, "health_checks", None)
        return live() if callable(live) else []

    def _health(self, health: dict) -> dict:
        """The studio's health list plus the controllers' live checks (they replace same-id entries)."""
        extra = self._live_checks(self.audio)
        lights = self._live_checks(self.lighting)
        blurs = self._live_checks(self.video)
        if not (extra or lights or blurs):
            return health
        ids = {c["id"] for c in extra + lights + blurs}
        checks = [c for c in health["checks"] if c["id"] not in ids]
        at = next((i + 1 for i, c in enumerate(checks) if c["id"] == "microphone"), len(checks))
        checks[at:at] = extra
        # video-group checks go right after the encoder (blur) or the last video check (lights)
        at = next((i + 1 for i, c in enumerate(checks) if c["id"] == "encoder"), len(checks))
        checks[at:at] = blurs
        after = max((i + 1 for i, c in enumerate(checks) if c.get("group") == "video"), default=len(checks))
        checks[after:after] = lights
        states = {c["state"] for c in checks}
        summary = "fail" if "fail" in states else "warn" if "warn" in states else "ok"
        return {**health, "summary": summary, "checks": checks}

    def _framing_health(self, health: dict) -> dict:
        """The framing controller's live check replaces the static "Auto-framing" one (it shows device and speed)."""
        live = getattr(self.framing, "health_checks", None)
        extra = live() if callable(live) else []
        if not extra:
            return health
        checks = [c for c in health["checks"] if c["id"] != "framing"]
        at = next((i + 1 for i, c in enumerate(checks) if c["id"] == "encoder"), len(checks))
        checks[at:at] = extra
        states = {c["state"] for c in checks}
        return {
            **health,
            "summary": "fail" if "fail" in states else "warn" if "warn" in states else "ok",
            "checks": checks,
        }

    def snapshot(self) -> dict:
        studio = self.studio.state()
        return {
            "version": __version__,
            "demo": self.demo,
            "studio": studio["studio"],
            "video": self.video.state(),
            "audio": self.audio.state(),
            "auto": studio["auto"],
            "health": self._framing_health(self._health(studio["health"])),
            "framing": self.framing.state() if self.framing is not None else None,
            "system": studio["system"],
            "lighting": self.lighting.state() if self.lighting is not None else _no_lighting(),
            "interface": self.interface.state() if self.interface is not None else no_interface(),
        }

    def meters(self) -> dict:
        frame = dict(self.audio.meters())
        frame.setdefault("t", time.time())
        frame.setdefault("mic", None)
        frame.setdefault("voice", None)
        return frame

    # -- actions --------------------------------------------------------------

    def act(self, action: object, params: dict | None = None) -> str | None:
        """Run one action; returns an optional message. Raises ActionError for bad input or an absent helper."""
        if not isinstance(action, str) or not ACTION_NAME.fullmatch(action) or len(action) > 40:
            raise ActionError("Unknown action.", 400)
        params = params or {}
        prefix = action.split(".", 1)[0]
        if prefix == "audio":
            controller = self.audio
        elif prefix == "video":
            controller = self.video
        elif prefix in ("studio", "auto", "health", "open"):
            controller = self.studio
        elif prefix == "lighting" and self.lighting is not None:
            controller = self.lighting
        elif prefix == "interface" and self.interface is not None:
            controller = self.interface
        elif prefix == "framing":
            if self.framing is None:
                raise ActionError("Auto-framing is not part of this rig.", 409)
            controller = self.framing
        else:
            raise ActionError(f"Unknown action {action!r}.", 400)
        return controller.act(action, params)

    # -- media ----------------------------------------------------------------

    def frame(self, source: str, width: int) -> bytes | None:
        return self.video.frame(source, width)

    def director_sheet(self) -> bytes | None:
        """The contact sheet Claude was last shown (a JPEG), or None before the first pass."""
        sheet = getattr(self.studio, "sheet", None)
        return sheet() if callable(sheet) else None

    def clip_path(self, clip_id: str) -> Path | None:
        return self.audio.clip_path(clip_id)
