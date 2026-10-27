"""The Console's call awareness: is a call on, and "You are muted" when you talk into a muted call.

`CallController` owns a `streamstack.callstate.CallMonitor` (the shared primitive: who has the camera, who captures
audio) and adds the talking-while-muted warning on top. Other controllers ask it, never the devices:

    rig.call.snapshot()          # CallSnapshot: state idle / joining / in_call, service, microphone state
    rig.call.subscribe(cb)       # cb(before, after) on every change, on the monitor's thread
    rig.call.active()            # True while a call is on or starting (what the echo chirp and Pre-flight check)

Talking while muted
-------------------
The Console's meter loop hands every frame of the raw microphone to `on_meters`. While the state is `in_call` and the
call's microphone is muted (its capture stream is corked, muted or absent, or an adapter reported it muted), the
`SpeechDetector` (threshold above the room's noise floor plus a 400 ms hold) decides whether you are speaking, and the
`MutedTalkWatcher` raises one toast per episode (`call.muted_warning` in the state counts them, the Console can beep).
Everything is local: nothing is sent anywhere and nothing is recorded.

The Console's own Mute pad mutes the microphone itself, so the meter hears silence and there is nothing to detect: that
mute is already shown on every page. What this catches is a mute in the call app (its button, a headset button) that
leaves StreamStack's microphone live.

Privacy: it never opens the camera and never reads call audio (see `streamstack.callstate`).

Optional adapters (off by default, `call.set adapters=true`): an adapter that knows the call app's true mute state (a
companion browser extension, a Teams bridge) reports it with the `call.report_mute` action; that report then replaces
the inference from capture streams for as long as it is fresh.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from streamstack import callstate, config, speech
from streamstack import store as filestore
from streamstack.callstate import AppEvidence, CallSnapshot, CallState, CaptureStream, Gathered
from streamstack.console.audio import bool_param, number_param
from streamstack.console.errors import ActionError

log = logging.getLogger(__name__)

Notify = Callable[[str, str], None]
Listener = Callable[[CallSnapshot, CallSnapshot], None]

SETTINGS_FILE = "console-call.json"
COOLDOWN_RANGE = (5.0, 300.0)
SENSITIVITIES = tuple(speech.SENSITIVITIES)
DEFAULT_SETTINGS: dict[str, Any] = {
    "adapters": False,  # accept `call.report_mute` from an adapter (experimental)
    "muted_warning": {"enabled": True, "sensitivity": speech.DEFAULT_SENSITIVITY, "cooldown_s": 20.0, "sound": False},
}
DEMO_SETTLE_S = 1.0
DEMO_LEAVE_S = 0.6
DEMO_POLL_S = 0.25
DEMO_TALK_DB = -20.0  # `call.demo talk=true`: a voice well above any room
DEMO_QUIET_DB = -65.0  # `call.demo talk=false`: a quiet room
SOURCE_NAME_MAX = 40


def default_settings() -> dict[str, Any]:
    return {"adapters": DEFAULT_SETTINGS["adapters"], "muted_warning": dict(DEFAULT_SETTINGS["muted_warning"])}


def live_settings_path() -> Path:
    return config.state_dir() / SETTINGS_FILE


def read_settings(path: Path) -> dict[str, Any]:
    """The saved settings, anything missing or invalid replaced by its default (a damaged file is the store's job)."""
    saved = filestore.read_json(path, {}, validate=filestore.is_object)
    settings = default_settings()
    if isinstance(saved.get("adapters"), bool):
        settings["adapters"] = saved["adapters"]
    warn = saved.get("muted_warning")
    if isinstance(warn, dict):
        mw = settings["muted_warning"]
        for key in ("enabled", "sound"):
            if isinstance(warn.get(key), bool):
                mw[key] = warn[key]
        if warn.get("sensitivity") in SENSITIVITIES:
            mw["sensitivity"] = warn["sensitivity"]
        cooldown = warn.get("cooldown_s")
        if isinstance(cooldown, (int, float)) and not isinstance(cooldown, bool):
            mw["cooldown_s"] = max(COOLDOWN_RANGE[0], min(COOLDOWN_RANGE[1], float(cooldown)))
    return settings


def empty_state() -> dict[str, Any]:
    """The `call` object of a rig without call awareness."""
    return {
        "available": False,
        "state": CallState.IDLE.value,
        "active": False,
        "service": None,
        "app": None,
        "apps": [],
        "camera": False,
        "mic": "none",
        "reason": "",
        "since": None,
        "sources": {"audio": True, "video": True},
        "external": None,
        "settings": {"adapters": False},
        "muted_warning": {
            **DEFAULT_SETTINGS["muted_warning"],
            "count": 0,
            "last_at": None,
            "talking": False,
            "range": list(COOLDOWN_RANGE),
            "sensitivities": list(SENSITIVITIES),
        },
        "demo": False,
    }


class CallController:
    """Call state for the Console, the warning, its settings and the `call.*` actions."""

    def __init__(
        self,
        cfg: Mapping[str, str],
        notify: Notify | None = None,
        *,
        monitor: callstate.CallMonitor | None = None,
        own_muted: Callable[[], bool | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        settings_path: Path | None = None,
    ) -> None:
        self.notify = notify
        self.own_muted = own_muted  # the Console's own mute (set by the rig); None = unknown
        self._clock = clock
        self._lock = threading.RLock()
        self._path = settings_path  # None: settings live in memory only (the demo rig, tests)
        self._settings = read_settings(self._path) if self._path is not None else default_settings()
        self.monitor = monitor or callstate.CallMonitor(cfg, clock=clock)
        mw = self._settings["muted_warning"]
        self._vad = speech.SpeechDetector(mw["sensitivity"])
        self._watcher = speech.MutedTalkWatcher(mw["cooldown_s"])
        self._since_wall: float | None = None
        self._last_wall: float | None = None
        self.monitor.subscribe(self._on_change)

    # -- the public primitive ---------------------------------------------------

    def snapshot(self) -> CallSnapshot:
        return self.monitor.snapshot()

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        return self.monitor.subscribe(listener)

    def active(self) -> bool:
        """A call is on or starting (state joining or in_call)."""
        return self.monitor.snapshot().active

    # -- lifecycle --------------------------------------------------------------

    def start(self) -> None:
        self.monitor.start()

    def stop(self) -> None:
        self.monitor.stop()

    def _on_change(self, before: CallSnapshot, after: CallSnapshot) -> None:
        with self._lock:
            if before.state is not after.state:
                self._since_wall = time.time() if after.active else None
            if not after.in_call:
                self._vad.reset()  # a new call starts from a fresh noise floor

    # -- the warning ------------------------------------------------------------

    def on_meters(self, frame: Mapping[str, Any], now: float | None = None) -> None:
        """One meter frame (the raw microphone's levels), from the hub's meter loop at about 30 Hz."""
        mic = frame.get("mic")
        level = mic.get("rms_db") if isinstance(mic, Mapping) else None
        if not isinstance(level, (int, float)):
            return
        now = self._clock() if now is None else now
        snap = self.monitor.snapshot()
        with self._lock:
            warn = self._settings["muted_warning"]
            speaking = self._vad.feed(now, float(level))  # always, so the noise floor is known when it is switched on
            if not warn["enabled"]:
                self._watcher.update(now, in_call=False, muted=False, speaking=False)
                return
            own = self.own_muted() if self.own_muted is not None else None
            fire = self._watcher.update(now, in_call=snap.in_call, muted=snap.muted or own is True, speaking=speaking)
            if fire:
                self._last_wall = time.time()
        if fire and self.notify is not None:
            who = snap.service or "The call"
            self.notify("warn", f"You are muted. {who} cannot hear you.")

    # -- state ------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        snap = self.monitor.snapshot()
        health = self.monitor.health()
        external = self.monitor.external()
        with self._lock:
            warn = self._settings["muted_warning"]
            return {
                **snap.public(),
                "available": True,
                "since": _iso(self._since_wall),
                "sources": {"audio": health.audio, "video": health.video},
                "external": {"muted": external.muted, "source": external.source}
                if external is not None and snap.active
                else None,
                "settings": {"adapters": self._settings["adapters"]},
                "muted_warning": {
                    **warn,
                    "count": self._watcher.count,
                    "last_at": _iso(self._last_wall),
                    "talking": self._watcher.talking,
                    "range": list(COOLDOWN_RANGE),
                    "sensitivities": list(SENSITIVITIES),
                },
                "demo": False,
            }

    # -- actions ----------------------------------------------------------------

    def act(self, action: str, params: dict[str, Any]) -> str | None:
        if action == "call.set":
            return self._act_set(params)
        if action == "call.report_mute":
            return self._act_report_mute(params)
        raise ActionError(f"unknown call action {action!r}", 400)

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            filestore.write_json(self._path, self._settings)
        except OSError as exc:
            log.warning("cannot save %s: %s", self._path, exc)
            raise ActionError(f"The setting could not be saved: {exc}", 409) from exc

    def _act_set(self, params: dict[str, Any]) -> str | None:
        known = {"enabled", "sensitivity", "cooldown_s", "sound", "adapters"}
        unknown = sorted(set(params) - known)
        if unknown:
            raise ActionError(f"unknown call setting {unknown[0]!r}", 400)
        if not params:
            raise ActionError("nothing to change", 400)
        changes: dict[str, Any] = {}
        for key in ("enabled", "sound", "adapters"):
            if key in params:
                changes[key] = bool_param(params, key)
        if "sensitivity" in params:
            if params["sensitivity"] not in SENSITIVITIES:
                raise ActionError(f"sensitivity must be one of {', '.join(SENSITIVITIES)}", 400)
            changes["sensitivity"] = params["sensitivity"]
        if "cooldown_s" in params:
            changes["cooldown_s"] = round(
                number_param(params, "cooldown_s", low=COOLDOWN_RANGE[0], high=COOLDOWN_RANGE[1]), 1
            )
        with self._lock:
            if "adapters" in changes:
                self._settings["adapters"] = changes.pop("adapters")
                if not self._settings["adapters"]:
                    self.monitor.report_mute(None, "")  # switching adapters off withdraws what they said
            self._settings["muted_warning"].update(changes)
            mw = self._settings["muted_warning"]
            self._vad.set_sensitivity(mw["sensitivity"])
            self._watcher.cooldown_s = mw["cooldown_s"]
            self._save()
        return None

    def _act_report_mute(self, params: dict[str, Any]) -> str | None:
        if not self._settings["adapters"]:
            raise ActionError("Mute reports from adapters are off. Turn them on first (call.set adapters).", 409)
        muted = params.get("muted")
        if muted is not None and not isinstance(muted, bool):
            raise ActionError("muted must be true, false or null", 400)
        source = params.get("source")
        if not isinstance(source, str) or not source.strip() or len(source) > SOURCE_NAME_MAX:
            raise ActionError(f"source must name the adapter (up to {SOURCE_NAME_MAX} characters)", 400)
        self.monitor.report_mute(muted, source.strip())
        return None


def _iso(wall: float | None) -> str | None:
    return None if wall is None else time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(wall))


# ---------------------------------------------------------------------------
# The simulated twin
# ---------------------------------------------------------------------------


class ScriptedCall:
    """The demo rig's evidence: a test or the owner pretends a call app opens the camera and a capture stream."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.app: str | None = None
        self.camera = True
        self.capture = True
        self.corked = False
        self.title = ""
        self.talk: bool | None = None  # True: loud mic, False: a quiet room, None: whatever the meters say

    def reset(self) -> None:
        with self._lock:
            self.app, self.camera, self.capture, self.corked, self.title = None, True, True, False, ""
            self.talk = None

    def gather(self) -> Gathered:
        with self._lock:
            if not self.app:
                return Gathered([], True, True)
            name = callstate.app_name(self.app)
            label = name.label if name else self.app
            streams = (CaptureStream(label, self.app.lower(), 4242, self.corked, False),) if self.capture else ()
            evidence = AppEvidence(
                label, bool(name and name.browser), self.camera, streams, (self.title,) if self.title else ()
            )
            return Gathered([evidence], True, True)


class DemoCall(CallController):
    """A call controller over a scripted call app: nothing is probed, every state can be reached by `call.demo`."""

    def __init__(self, notify: Notify | None = None, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.script = ScriptedCall()
        monitor = callstate.CallMonitor(
            {},
            source=self.script.gather,
            detector=callstate.CallDetector(settle_s=DEMO_SETTLE_S, leave_s=DEMO_LEAVE_S, clock=clock),
            clock=clock,
            poll_s=DEMO_POLL_S,
        )
        super().__init__({}, notify, monitor=monitor, clock=clock)

    def state(self) -> dict[str, Any]:
        return {**super().state(), "demo": True}

    def on_meters(self, frame: Mapping[str, Any], now: float | None = None) -> None:
        talk = self.script.talk
        if talk is not None:  # a test scripts the room: loud talking, or a quiet room
            frame = {"mic": {"rms_db": DEMO_TALK_DB if talk else DEMO_QUIET_DB}}
        super().on_meters(frame, now)

    def act(self, action: str, params: dict[str, Any]) -> str | None:
        if action == "call.demo":
            return self._demo(params)
        if action == "call.demo_reset":
            self.script.reset()
            self.monitor.reset()  # not the leave hold: a muted call left over from the last test must not outlive it
            self.monitor.poll_once()
            with self._lock:
                self._settings = default_settings()
                mw = self._settings["muted_warning"]
                self._vad.set_sensitivity(mw["sensitivity"])
                self._vad.reset()
                self._watcher = speech.MutedTalkWatcher(mw["cooldown_s"])
                self._last_wall = None
            return None
        return super().act(action, params)

    def _demo(self, params: dict[str, Any]) -> str | None:
        app = params.get("app")
        if app is not None and (not isinstance(app, str) or not app.strip() or len(app) > 40):
            raise ActionError("app must be a call app name such as Teams, or null to end the call", 400)
        script = self.script
        for key in ("camera", "capture", "corked"):
            if key in params and not isinstance(params[key], bool):
                raise ActionError(f"{key} must be true or false", 400)
        talk = params.get("talk")
        if talk is not None and not isinstance(talk, bool):
            raise ActionError("talk must be true, false or null", 400)
        title = params.get("title", "")
        if not isinstance(title, str) or len(title) > 120:
            raise ActionError("title must be text", 400)
        with script._lock:
            script.app = app.strip() if app else None
            script.camera = params.get("camera", True)
            script.capture = params.get("capture", True)
            script.corked = params.get("corked", False)
            script.title = title
            script.talk = talk
        return None
