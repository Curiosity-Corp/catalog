"""Safe join: when a call app takes the camera, the Console keeps you off air until you press Go live.

At 6 am or midnight people join a call before the picture, the lights or the microphone are ready. This controller
watches `rig.call` (the call-awareness primitive, #45) and, depending on the setting "On call start":

    off     nothing happens
    warn    one notification: "Teams has the camera. You are on air." (the default)
    safe    privacy on and the microphone muted, a banner on Live and Mini says "You are not on air: press Go live"

Go live (`safejoin.go_live`: the banner button, Space, the palette, a Stream Deck key) releases what Safe join took,
both together. Rules that keep it from ever fighting the person:

* The trigger is a call app holding the camera. Nothing else counts: an OBS preview or the mic check are not call
  apps, so they never reach this code.
* It acts once per call. After Go live, or after you released something by hand, it never takes it again in that call.
* It only holds what it took. A microphone that was already muted, or privacy that was already on, is yours: Go live
  leaves it alone, and so does the end of the call.
* Pressing unmute or privacy off yourself during the hold simply ends that part of the hold.
* When the call ends while it still holds something, it gives it back (so the desk is never left muted and private).
* An optional timer (`auto_release_s`, 0 = never) goes live by itself.

Lights are not touched here: camera follow in the lighting automation starts from the same camera use, on its own.
Every call start leaves one entry in the event list (`safejoin.events`), newest first, with what was done.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from streamstack import config
from streamstack import store as filestore
from streamstack.callstate import CallSnapshot
from streamstack.console.audio import number_param
from streamstack.console.errors import ActionError

log = logging.getLogger(__name__)

Notify = Callable[[str, str], None]
Snapshot = Callable[[], Mapping[str, Any]]
RunAction = Callable[[str, dict[str, Any]], str | None]

SETTINGS_FILE = "console-safejoin.json"
MODES = ("off", "warn", "safe")
DEFAULT_MODE = "warn"
RELEASE_RANGE = (0.0, 600.0)
EVENTS_MAX = 20
SETTLE_S = 2.0  # after it acts, the snapshot may still show the old value: not a manual release yet
POLL_S = 0.25  # how often a hold looks at the rig (the meter loop runs at about 30 Hz)


def live_settings_path() -> Path:
    return config.state_dir() / SETTINGS_FILE


def default_settings() -> dict[str, Any]:
    return {"mode": DEFAULT_MODE, "auto_release_s": 0.0}


def read_settings(path: Path) -> dict[str, Any]:
    saved = filestore.read_json(path, {}, validate=filestore.is_object)
    settings = default_settings()
    if saved.get("mode") in MODES:
        settings["mode"] = saved["mode"]
    timer = saved.get("auto_release_s")
    if isinstance(timer, (int, float)) and not isinstance(timer, bool):
        settings["auto_release_s"] = max(RELEASE_RANGE[0], min(RELEASE_RANGE[1], float(timer)))
    return settings


def empty_state() -> dict[str, Any]:
    """The `safejoin` object of a rig without it."""
    return {
        "available": False,
        "mode": DEFAULT_MODE,
        "modes": list(MODES),
        "auto_release_s": 0.0,
        "range": list(RELEASE_RANGE),
        "holding": False,
        "held": {"privacy": False, "mute": False},
        "service": None,
        "since": None,
        "release_in_s": None,
        "events": [],
        "demo": False,
    }


def _iso(wall: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(wall))


def _dig(state: Mapping[str, Any], *path: str) -> Any:
    node: Any = state
    for key in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


class SafeJoinController:
    """The setting, the hold, Go live and the event list."""

    def __init__(
        self,
        notify: Notify | None = None,
        *,
        settings_path: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.notify = notify
        self._clock, self._wall = clock, wall
        self._lock = threading.RLock()
        self._path = settings_path  # None: settings live in memory only (the demo rig, tests)
        self._settings = read_settings(settings_path) if settings_path is not None else default_settings()
        self._snapshot: Snapshot | None = None
        self._run: RunAction | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._events: list[dict[str, Any]] = []
        self._in_call = False  # a call is on (state joining or in_call)
        self._handled = False  # this call has had its one decision
        self._held = {"privacy": False, "mute": False}
        self._service: str | None = None
        self._since: float | None = None  # monotonic: when the hold began
        self._since_wall: float | None = None
        self._next_poll = 0.0

    # -- wiring --------------------------------------------------------------------

    def bind(self, snapshot: Snapshot, run: RunAction) -> None:
        """The rig's whole-state snapshot (to see privacy and mute) and its action runner (to set them)."""
        self._snapshot, self._run = snapshot, run

    def attach(self, call: Any) -> None:
        """Follow a call controller (anything with `subscribe(cb(before, after))` and `snapshot()`)."""
        if self._unsubscribe is not None:
            self._unsubscribe()
        self._unsubscribe = call.subscribe(self._on_call)
        self._on_call(CallSnapshot(), call.snapshot())  # a call already on when the Console started counts

    def start(self) -> None:
        return None

    def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    # -- the call -------------------------------------------------------------------

    def _on_call(self, before: CallSnapshot, after: CallSnapshot) -> None:
        with self._lock:
            if not after.active:
                if self._in_call:
                    self._call_ended()
                return
            if not self._in_call:
                self._in_call, self._handled = True, False
            if after.camera and not self._handled:
                self._handled = True
                self._engage(after)

    def _engage(self, call: CallSnapshot) -> None:
        mode = self._settings["mode"]
        service = call.service or "A call app"
        if mode == "off":
            return
        if mode == "warn":
            self._record(service, mode, [], [])
            self._tell("warn", f"{service} has the camera. You are on air: check privacy and the microphone.")
            return
        state = self._state_now()
        did: list[str] = []
        kept: list[str] = []
        held = {"privacy": False, "mute": False}
        if _dig(state, "video", "privacy") is True:
            kept.append("privacy was already on")
        elif self._do("video.privacy", {"on": True}, "privacy on", did):
            held["privacy"] = True
        if _dig(state, "audio", "mic", "muted") is True:
            kept.append("the microphone was already muted")
        elif _dig(state, "audio", "mic", "present") is False:
            kept.append("no microphone to mute")
        elif self._do("audio.mute", {"on": True}, "microphone muted", did):
            held["mute"] = True
        self._held = held
        self._service = service
        self._since, self._since_wall = self._clock(), self._wall()
        self._next_poll = self._since + SETTLE_S
        self._record(service, mode, did, kept)
        if any(held.values()):
            self._tell("warn", f"{service} has the camera. You are not on air: press Go live.")
        else:
            self._tell("info", f"{service} has the camera. {'; '.join(kept) or 'Nothing to hold'}.")

    def _do(self, action: str, params: dict[str, Any], label: str, did: list[str]) -> bool:
        if self._run is None:
            return False
        try:
            self._run(action, params)
        except ActionError as exc:
            log.warning("safe join: %s refused: %s", action, exc)
            did.append(f"{label} failed: {exc}")
            return False
        except Exception as exc:  # an actuator may fail in any way (OBS, pactl); one failure must not stop the other
            log.exception("safe join: %s failed", action)
            did.append(f"{label} failed: {exc}")
            return False
        did.append(label)
        return True

    def _call_ended(self) -> None:
        self._in_call, self._handled = False, False
        if self._holding():
            self._release("the call ended")

    # -- the hold -------------------------------------------------------------------

    def _holding(self) -> bool:
        return any(self._held.values())

    def _state_now(self) -> Mapping[str, Any]:
        if self._snapshot is None:
            return {}
        try:
            return self._snapshot()
        except Exception:  # the snapshot is the whole rig; a failure there must not break the call callback
            log.exception("safe join: cannot read the rig state")
            return {}

    def on_meters(self, frame: Mapping[str, Any], now: float | None = None) -> None:
        """The hub's meter loop: while holding, notice a manual release and run the optional timer."""
        now = self._clock() if now is None else now
        with self._lock:
            if not self._holding() or now < self._next_poll:
                return
            self._next_poll = now + POLL_S
            timer = self._settings["auto_release_s"]
            if timer and self._since is not None and now - self._since >= timer:
                self._release("the timer ran out")
                return
        state = self._state_now()
        with self._lock:
            if not self._holding():
                return
            by_hand = []
            if self._held["privacy"] and _dig(state, "video", "privacy") is False:
                self._held["privacy"] = False
                by_hand.append("privacy")
            if self._held["mute"] and _dig(state, "audio", "mic", "muted") is False:
                self._held["mute"] = False
                by_hand.append("the microphone")
            if by_hand:
                self._note(f"You released {' and '.join(by_hand)} yourself.")
                if not self._holding():
                    self._since = self._since_wall = None
                    self._tell("ok", "You are on air.")

    def _release(self, why: str) -> list[str]:
        """Give back what is still held. Caller holds the lock."""
        did: list[str] = []
        state = self._state_now()
        if self._held["privacy"] and _dig(state, "video", "privacy") is not False:
            self._do("video.privacy", {"on": False}, "privacy off", did)
        if self._held["mute"] and _dig(state, "audio", "mic", "muted") is not False:
            self._do("audio.mute", {"on": False}, "microphone unmuted", did)
        self._held = {"privacy": False, "mute": False}
        self._since = self._since_wall = None
        self._note(f"Released: {', '.join(did) or 'nothing left to release'} ({why}).")
        return did

    # -- the event list --------------------------------------------------------------

    def _record(self, service: str, mode: str, did: list[str], kept: list[str]) -> None:
        self._events.insert(
            0,
            {
                "at": _iso(self._wall()),
                "service": service,
                "mode": mode,
                "did": did,
                "kept": kept,
                "notes": [],
            },
        )
        del self._events[EVENTS_MAX:]

    def _note(self, text: str) -> None:
        if self._events:
            self._events[0]["notes"].append(f"{time.strftime('%H:%M:%S', time.localtime(self._wall()))} {text}")

    def _tell(self, level: str, text: str) -> None:
        if self.notify is not None:
            self.notify(level, text)

    # -- state and actions --------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        with self._lock:
            timer = self._settings["auto_release_s"]
            left = None
            if timer and self._since is not None and self._holding():
                left = max(0.0, round(timer - (self._clock() - self._since), 1))
            return {
                "available": True,
                "mode": self._settings["mode"],
                "modes": list(MODES),
                "auto_release_s": timer,
                "range": list(RELEASE_RANGE),
                "holding": self._holding(),
                "held": dict(self._held),
                "service": self._service if self._holding() else None,
                "since": _iso(self._since_wall) if self._since_wall is not None and self._holding() else None,
                "release_in_s": left,
                "events": [
                    dict(e, did=list(e["did"]), kept=list(e["kept"]), notes=list(e["notes"])) for e in self._events
                ],
                "demo": False,
            }

    def act(self, action: str, params: dict[str, Any]) -> str | None:
        if action == "safejoin.set":
            return self._act_set(params)
        if action == "safejoin.go_live":
            return self._act_go_live()
        raise ActionError(f"unknown safe join action {action!r}", 400)

    def _act_go_live(self) -> str | None:
        with self._lock:
            if not self._holding():
                return "Nothing is held: you are on air."
            did = self._release("Go live")
            self._handled = True  # this call has had its decision: never hold again
        self._tell("ok", "You are on air.")
        return "You are on air." if did else "Nothing to release."

    def _act_set(self, params: dict[str, Any]) -> str | None:
        unknown = sorted(set(params) - {"mode", "auto_release_s"})
        if unknown:
            raise ActionError(f"unknown safe join setting {unknown[0]!r}", 400)
        if not params:
            raise ActionError("nothing to change", 400)
        changes: dict[str, Any] = {}
        if "mode" in params:
            if params["mode"] not in MODES:
                raise ActionError(f"mode must be one of {', '.join(MODES)}", 400)
            changes["mode"] = params["mode"]
        if "auto_release_s" in params:
            changes["auto_release_s"] = round(
                number_param(params, "auto_release_s", low=RELEASE_RANGE[0], high=RELEASE_RANGE[1]), 1
            )
        with self._lock:
            self._settings.update(changes)
            self._save()
        return None

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            filestore.write_json(self._path, self._settings)
        except OSError as exc:
            log.warning("cannot save %s: %s", self._path, exc)
            raise ActionError(f"The setting could not be saved: {exc}", 409) from exc


class DemoSafeJoin(SafeJoinController):
    """The demo rig's twin: settings in memory, and a reset for the e2e suite."""

    def state(self) -> dict[str, Any]:
        return {**super().state(), "demo": True}

    def act(self, action: str, params: dict[str, Any]) -> str | None:
        if action == "safejoin.demo_reset":
            with self._lock:
                self._settings = default_settings()
                self._events, self._held = [], {"privacy": False, "mute": False}
                self._since = self._since_wall = None
                self._service = None
            return None
        return super().act(action, params)
