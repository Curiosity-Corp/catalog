"""The rig facade: audio, video and studio controllers behind one state snapshot and one action router."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from streamstack import __version__, store
from streamstack.console.errors import ActionError
from streamstack.console.interface import empty_state as no_interface


def no_deck() -> dict:
    from streamstack.console.deck import empty_state

    return empty_state()


def no_slots() -> dict:
    from streamstack.console.slots import empty_state

    return empty_state()


def no_hotkeys() -> dict:
    from streamstack.console.hotkeys import empty_state

    return empty_state()


def empty_diagnostics() -> dict:
    return {
        "telemetry": {
            "enabled": False, "path": "/metrics", "url": None, "scrapes": 0, "last_scrape": None, "token_set": False,
        },
        "crashes": {"dir": "", "reports": []},
    }  # fmt: skip


def _no_lighting() -> dict:
    from streamstack.console.lighting import empty_state

    return empty_state()


def _presets(dispatch: Callable[[str, dict], str | None]) -> Any:
    from streamstack.console.presets import PresetsController

    return PresetsController(dispatch)


def _no_hardware() -> dict:
    from streamstack.console.hardware import empty_state

    return empty_state()


def _no_call() -> dict:
    from streamstack.console.call import empty_state

    return empty_state()


def _no_stream() -> dict:
    from streamstack.console.stream import empty_state

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
        presets: Any = None,
        stream: Any = None,
        deck: Any = None,
        diagnostics: Any = None,
        hardware: Any = None,
        call: Any = None,
        slots: Any = None,
        hotkeys: Any = None,
    ) -> None:
        self.audio, self.video, self.studio = audio, video, studio
        self.lighting = lighting  # optional: a rig without lights reports an empty lighting object
        self.interface = interface  # optional: a rig without an audio interface profile reports none present
        self.framing = framing  # optional: auto-framing (framing.* actions, "framing" state)
        self.presets = presets if presets is not None else _presets(self.act)  # presets.* actions and state
        self.stream = stream  # optional: recording and streaming (stream.* and record.* actions, "stream" state)
        self.deck = deck  # optional: Stream Deck profile and keys (deck.* actions, "deck" state)
        self.diagnostics = diagnostics  # optional: telemetry and crash reports (telemetry.*, crash.* actions)
        self.hardware = hardware  # optional: the hardware probe and Plan (hardware.* actions, "hardware" state)
        self.call = call  # optional: call awareness (call.* actions, "call" state, `call.snapshot()` for features)
        self.slots = slots  # optional: time-of-day look profiles (slots.* actions, "slots" state)
        self.hotkeys = hotkeys  # optional: global hotkeys and MIDI (hotkeys.* actions, "hotkeys" state)
        self.demo = demo
        self.on_toast: Notify | None = None  # the hub sets this
        self._started = False
        self._lock = threading.Lock()
        self._unsubscribe_store: Callable[[], None] | None = None
        controllers = (
            audio,
            video,
            studio,
            lighting,
            interface,
            framing,
            self.presets,
            stream,
            deck,
            hardware,
            call,
            slots,
            hotkeys,
        )
        for controller in controllers:
            if controller is not None:
                controller.notify = self.notify
        if deck is not None:
            deck.rig = self  # pressing a key runs a Console action
        if call is not None and callable(getattr(audio, "mic_muted", None)):
            call.own_muted = audio.mic_muted  # the Console's own mute: the microphone is silent, nothing to detect
        if slots is not None:
            slots.dispatch = self.act  # a profile is applied with the actions a person would press
            slots.rig = self  # a profile reads what is on and what changed from the snapshot
        if hotkeys is not None:
            hotkeys.rig = self  # a hotkey or a MIDI control runs a Console action

    # -- construction ---------------------------------------------------------

    @classmethod
    def live(cls, cfg: dict[str, str] | None = None) -> Rig:
        """The real rig. The domain modules are imported here so a broken one cannot stop `streamstack --version`."""
        from streamstack import config
        from streamstack.console.audio import AudioController
        from streamstack.console.call import CallController, live_settings_path
        from streamstack.console.deck import DeckController
        from streamstack.console.diagnostics import Diagnostics
        from streamstack.console.framing import FramingController
        from streamstack.console.hardware import HardwareController
        from streamstack.console.hotkeys import HotkeysController
        from streamstack.console.interface import InterfaceController
        from streamstack.console.lighting import LightingController
        from streamstack.console.native import NativeVideo
        from streamstack.console.slots import SlotsController
        from streamstack.console.stream import StreamController
        from streamstack.console.studio import StudioController

        config.retire_leftovers()
        cfg = cfg or config.load_settings()
        hardware = HardwareController(cfg)
        plan = hardware.wait_plan  # the video engine and the stream pick their GPU from the Plan by role
        video = NativeVideo(cfg, plan=plan)
        stream = StreamController(cfg, plan=plan)
        # a running stream or recording reads /dev/video43 through its own ffmpeg (plumbing to the reader scan): it
        # must keep the engine awake
        video.add_demand("streaming", lambda: stream.service.running)
        audio = AudioController(cfg)
        audio.plan_source = hardware.plan  # the voice chain follows the Plan: microphone by USB id, rate, quantum
        audio.plan_refresh = lambda: hardware.act("hardware.refresh", {})
        lighting = LightingController()
        call = CallController(cfg, settings_path=live_settings_path())
        slots = SlotsController(lambda action, params: None, in_call=call.active)
        return cls(
            audio,
            video,
            StudioController(cfg),
            lighting=lighting,
            interface=InterfaceController(cfg, call_active=call.active),
            framing=FramingController(cfg, video),
            stream=stream,
            deck=DeckController(),
            diagnostics=Diagnostics(cfg),
            hardware=hardware,
            call=call,
            slots=slots,
            hotkeys=HotkeysController(),
        )

    @classmethod
    def demo(cls) -> Rig:
        """A simulated rig: nothing is probed or touched, every action works."""
        from streamstack.console.audio import DemoAudio
        from streamstack.console.call import DemoCall
        from streamstack.console.deck import DemoDeck
        from streamstack.console.diagnostics import Diagnostics
        from streamstack.console.framing import DemoFraming
        from streamstack.console.hardware import DemoHardware
        from streamstack.console.hotkeys import DemoHotkeys
        from streamstack.console.interface import DemoInterface
        from streamstack.console.lighting import DemoLighting
        from streamstack.console.slots import DemoSlots
        from streamstack.console.stream import DemoStream
        from streamstack.console.studio import DemoStudio
        from streamstack.console.video import DemoVideo

        lighting = DemoLighting()
        call = DemoCall()
        slots = DemoSlots(lambda action, params: None, in_call=call.active)
        return cls(
            DemoAudio(),
            DemoVideo(),
            DemoStudio(),
            demo=True,
            lighting=lighting,
            interface=DemoInterface(),
            framing=DemoFraming(),
            stream=DemoStream(),
            deck=DemoDeck(),
            diagnostics=Diagnostics(),
            hardware=DemoHardware(),
            call=call,
            slots=slots,
            hotkeys=DemoHotkeys(),
        )

    # -- lifecycle ------------------------------------------------------------

    def _controllers(self) -> tuple[Any, ...]:
        # started in this order, stopped in reverse: framing needs the video link, so it stops first
        controllers = (self.studio, self.video, self.audio)
        controllers = controllers if self.hardware is None else (self.hardware, *controllers)
        controllers = (*controllers, *(c for c in (self.lighting, self.interface) if c is not None))
        controllers = controllers if self.framing is None else (*controllers, self.framing)
        controllers = controllers if self.stream is None else (*controllers, self.stream)
        controllers = controllers if self.call is None else (*controllers, self.call)
        controllers = controllers if self.deck is None else (*controllers, self.deck)
        controllers = controllers if self.hotkeys is None else (*controllers, self.hotkeys)
        return controllers if self.slots is None else (*controllers, self.slots)  # last: it acts through the others

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        # a file the store had to set aside raises a toast in every window; the System page keeps the notice
        self._unsubscribe_store = store.subscribe(self._store_warning)
        for controller in self._controllers():
            controller.start()
        if self.diagnostics is not None:
            self.diagnostics.start()

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._started = False
        if self._unsubscribe_store:
            self._unsubscribe_store()
            self._unsubscribe_store = None
        if self.diagnostics is not None:
            self.diagnostics.stop()
        for controller in reversed(self._controllers()):
            try:
                controller.stop()
            except Exception as exc:  # one controller failing must not leave the others running
                self.notify("error", f"{type(controller).__name__} did not stop cleanly: {exc}")

    # -- notifications --------------------------------------------------------

    def notify(self, level: str, text: str) -> None:
        if self.on_toast:
            self.on_toast(level if level in ("info", "ok", "warn", "error") else "info", str(text))

    def _store_warning(self, warning: store.StoreWarning) -> None:
        what = {
            "restored-backup": "was damaged; the last good copy was restored",
            "defaults": "was damaged; it was set aside and the defaults are used",
            "ignored-lines": "has lines that were ignored",
            "unreadable": "cannot be read",
        }.get(warning.action, "needs a look")
        self.notify("warn", f"{warning.path} {what}. See the System page.")

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
        devices = self._live_checks(self.hardware)
        if not (extra or lights or blurs or devices):
            return health
        ids = {c["id"] for c in extra + lights + blurs + devices}
        checks = devices + [c for c in health["checks"] if c["id"] not in ids]
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
        snap = {
            "version": __version__,
            "demo": self.demo,
            "studio": studio["studio"],
            "video": self.video.state(),
            "audio": self.audio.state(),
            "auto": studio["auto"],
            "health": self._framing_health(self._health(studio["health"])),
            "framing": self.framing.state() if self.framing is not None else None,
            "system": studio["system"],
            "virtual": studio["virtual"],
            "lighting": self.lighting.state() if self.lighting is not None else _no_lighting(),
            "interface": self.interface.state() if self.interface is not None else no_interface(),
            "presets": self.presets.state(),
            "stream": self.stream.state() if self.stream is not None else _no_stream(),
            "storage": {"warnings": store.warnings()},
            "diagnostics": self.diagnostics.state() if self.diagnostics is not None else empty_diagnostics(),
            "hardware": self.hardware.state() if self.hardware is not None else _no_hardware(),
            "call": self.call.state() if self.call is not None else _no_call(),
        }
        snap["slots"] = self.slots.state(snap) if self.slots is not None else no_slots()
        snap["hotkeys"] = self.hotkeys.state(snap) if self.hotkeys is not None else no_hotkeys()
        snap["deck"] = self.deck.state(snap) if self.deck is not None else no_deck()  # last: its keys show the rest
        return snap

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
        elif prefix == "presets":
            controller = self.presets
        elif prefix in ("telemetry", "crash"):
            if self.diagnostics is None:
                raise ActionError("Telemetry and crash reports are not part of this rig.", 409)
            return self.diagnostics.act(action, params)
        elif prefix == "hardware":
            if self.hardware is None:
                raise ActionError("Hardware probing is not part of this rig.", 409)
            return self.hardware.act(action, params)
        elif prefix == "call":
            if self.call is None:
                raise ActionError("Call awareness is not part of this rig.", 409)
            return self.call.act(action, params)
        elif prefix == "storage":
            return self._storage_act(action, params)
        elif prefix == "deck":
            if self.deck is None:
                raise ActionError("Stream Deck support is not part of this rig.", 409)
            controller = self.deck
        elif prefix == "slots":
            if self.slots is None:
                raise ActionError("Time-of-day profiles are not part of this rig.", 409)
            controller = self.slots
        elif prefix == "hotkeys":
            if self.hotkeys is None:
                raise ActionError("Hotkeys are not part of this rig.", 409)
            controller = self.hotkeys
        elif prefix == "framing":
            if self.framing is None:
                raise ActionError("Auto-framing is not part of this rig.", 409)
            controller = self.framing
        elif prefix in ("stream", "record"):
            if self.stream is None:
                if prefix == "stream":  # the Presets page reads 501 as "pending": its choice is saved for later
                    raise ActionError("The streaming backend is not part of this rig (stream.set is pending).", 501)
                raise ActionError("Recording and streaming are not part of this rig.", 409)
            controller = self.stream
        else:
            raise ActionError(f"Unknown action {action!r}.", 400)
        return controller.act(action, params)

    @staticmethod
    def _storage_act(action: str, params: dict) -> str | None:
        if action != "storage.dismiss":
            raise ActionError(f"Unknown action {action!r}.", 400)
        ident = params.get("id")
        if ident is not None and (not isinstance(ident, str) or len(ident) > 16):
            raise ActionError("id must be a warning id.", 400)
        store.dismiss(ident)
        return None

    # -- media ----------------------------------------------------------------

    def frame(self, source: str, width: int) -> bytes | None:
        return self.video.frame(source, width)

    def director_sheet(self) -> bytes | None:
        """The contact sheet Claude was last shown (a JPEG), or None before the first pass."""
        sheet = getattr(self.studio, "sheet", None)
        return sheet() if callable(sheet) else None

    def clip_path(self, clip_id: str) -> Path | None:
        return self.audio.clip_path(clip_id)
