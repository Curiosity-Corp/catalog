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

    def __init__(self, audio: Any, video: Any, studio: Any, demo: bool = False) -> None:
        self.audio, self.video, self.studio = audio, video, studio
        self.demo = demo
        self.on_toast: Notify | None = None  # the hub sets this
        self._started = False
        self._lock = threading.Lock()
        for controller in (audio, video, studio):
            controller.notify = self.notify

    # -- construction ---------------------------------------------------------

    @classmethod
    def live(cls, cfg: dict[str, str] | None = None) -> Rig:
        """The real rig. The domain modules are imported here so a broken one cannot stop `streamstack --version`."""
        from streamstack import config
        from streamstack.console.audio import AudioController
        from streamstack.console.studio import StudioController
        from streamstack.console.video import VideoController

        cfg = cfg or config.load_settings()
        return cls(AudioController(cfg), VideoController(cfg), StudioController(cfg))

    @classmethod
    def demo(cls) -> Rig:
        """A simulated rig: nothing is probed or touched, every action works."""
        from streamstack.console.audio import DemoAudio
        from streamstack.console.studio import DemoStudio
        from streamstack.console.video import DemoVideo

        return cls(DemoAudio(), DemoVideo(), DemoStudio(), demo=True)

    # -- lifecycle ------------------------------------------------------------

    def _controllers(self) -> tuple[Any, ...]:
        return (self.studio, self.video, self.audio)

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

    def _health(self, health: dict) -> dict:
        """The studio's health list plus the audio controller's live checks (they replace same-id entries)."""
        live = getattr(self.audio, "health_checks", None)
        extra = live() if callable(live) else []
        if not extra:
            return health
        ids = {c["id"] for c in extra}
        checks = [c for c in health["checks"] if c["id"] not in ids]
        at = next((i + 1 for i, c in enumerate(checks) if c["id"] == "microphone"), len(checks))
        checks[at:at] = extra
        states = {c["state"] for c in checks}
        summary = "fail" if "fail" in states else "warn" if "warn" in states else "ok"
        return {**health, "summary": summary, "checks": checks}

    def snapshot(self) -> dict:
        studio = self.studio.state()
        return {
            "version": __version__,
            "demo": self.demo,
            "studio": studio["studio"],
            "video": self.video.state(),
            "audio": self.audio.state(),
            "auto": studio["auto"],
            "health": self._health(studio["health"]),
            "system": studio["system"],
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
        else:
            raise ActionError(f"Unknown action {action!r}.", 400)
        return controller.act(action, params)

    # -- media ----------------------------------------------------------------

    def frame(self, source: str, width: int) -> bytes | None:
        return self.video.frame(source, width)

    def clip_path(self, clip_id: str) -> Path | None:
        return self.audio.clip_path(clip_id)
