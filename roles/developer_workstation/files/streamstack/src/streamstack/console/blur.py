"""Background blur for the Console: the `video.blur` state and action contract, and the simulated rig's blur.

The real rig has no mask yet (it arrives with the OpenVINO segmentation in the native video engine), so
`NativeVideo` reports `blur.native` with `available: False` and answers `video.blur` with a 409. `DemoBlur` is the
full contract on the simulated rig, so the Camera and Live pages and their tests keep exercising every state.
The state it adds to the snapshot is `video.blur` (docs/console.md):

    available    the mask backend is present (None: not known)
    plugin       the mask backend: installed, version, models, devices (see streamstack.blur.Plugin.public)
    sources      per camera: {present, enabled, strength, rate}
    on           any camera has the blur enabled
    cost         the per-frame cost verdict against the gate (docs/hardware-ai-evaluation.md)
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable

from streamstack import blur
from streamstack.blur import Plugin
from streamstack.console.errors import ActionError

Notify = Callable[[str, str], None]

OVER_TITLE = "Background blur costs more than its budget"
OVER_HINT = "Camera page: set the blur smoothness to Light, or switch the blur off"


def empty_state() -> dict:
    return {
        "available": None,
        "plugin": {
            "installed": False,
            "version": None,
            "models_ok": False,
            "loaded": None,
            "devices": ["cpu"],
            "device_labels": ["CPU"],
            "device": "cpu",
            "hint": "",
        },
        "sources": {},
        "on": False,
        "strength": blur.DEFAULT_STRENGTH,
        "rate": blur.DEFAULT_RATE,
        "limits": {"strength": [blur.MIN_STRENGTH, blur.MAX_STRENGTH], "rate": [blur.MIN_RATE, blur.MAX_RATE]},
        "presets": dict(blur.RATE_PRESETS),
        "cost": {"state": "off", "detail": "Blur is off.", "limits": {"p95_ms": blur.GATE_P95_MS, "cpu_percent": 1.0}},
    }


def parse_action(params: dict) -> tuple[bool | None, int | None, int | None, str | None]:
    """(on, strength, rate, source) from the action's params; ActionError(400) when one is malformed."""
    on = params.get("on")
    if on is not None and not isinstance(on, bool):
        raise ActionError("on must be true or false.", 400)
    try:
        strength = blur.validate_strength(params["strength"]) if params.get("strength") is not None else None
        rate = blur.validate_rate(params["rate"]) if params.get("rate") is not None else None
    except blur.BlurError as exc:
        raise ActionError(str(exc), 400) from exc
    source = params.get("source")
    if source is not None and (not isinstance(source, str) or not source or len(source) > 200):
        raise ActionError("source must be the name of a camera.", 400)
    if on is None and strength is None and rate is None:
        raise ActionError("Say what to change: on, strength or rate.", 400)
    return on, strength, rate, source


def message_for(on: bool | None, strength: int | None, rate: int | None) -> str:
    if on is not None:
        return "Background blur on" if on else "Background blur off"
    if strength is not None:
        return f"Blur strength {strength}"
    return f"Blur mask rate {rate}"


def missing_message(plugin: Plugin) -> str:
    return f"Background blur is not available yet: {blur.INSTALL_HINT}."


def _entry(present: bool, enabled: bool = False, strength: int = blur.DEFAULT_STRENGTH, rate: int = blur.DEFAULT_RATE):
    return {"present": present, "enabled": enabled, "strength": strength, "rate": rate}


def _copy(state: dict) -> dict:
    return copy.deepcopy(state)


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------


class DemoBlur:
    """The same contract on the simulated rig. Its cost model follows the real one: fewer inferences, less cost."""

    def __init__(self, sources: tuple[str, ...], notify: Notify | None = None) -> None:
        self._sources = sources
        self._notify = notify
        self._lock = threading.RLock()
        self.installed = True
        self.entries = {name: _entry(True) for name in sources}
        self._announced = False

    def notify(self, level: str, text: str) -> None:
        if self._notify:
            self._notify(level, text)

    def _plugin(self) -> dict:
        if not self.installed:
            return {**Plugin(installed=False).public(), "hint": blur.INSTALL_HINT}
        return Plugin(installed=True, version="1.0", models_ok=True, loaded=True, devices=("cpu",)).public()

    def _cost(self) -> dict:
        on = [e for e in self.entries.values() if e["enabled"]]
        limits = {"p95_ms": blur.GATE_P95_MS, "cpu_percent": blur.GATE_CPU_PERCENT}
        if not on:
            return {"state": "off", "detail": "Blur is off.", "limits": limits, "samples": 0}
        worst = max(on, key=lambda e: (1 / e["rate"], e["strength"]))
        added_ms = round(1.4 + 6.8 / worst["rate"] + 0.05 * worst["strength"], 2)
        added_cpu = round(0.9 / worst["rate"], 2)
        reasons = (
            [f"frame time +{added_ms:.1f} ms (limit {blur.GATE_P95_MS:g})"] if added_ms >= blur.GATE_P95_MS else []
        )
        return {
            "state": "over" if reasons else "ok",
            "detail": f"+{added_ms:.1f} ms per frame, +{added_cpu:.1f}% CPU, 0 dropped",
            "reasons": reasons,
            "added_ms": added_ms,
            "added_cpu": added_cpu,
            "dropped": 0,
            "limits": limits,
            "samples": blur.WINDOW_SAMPLES,
        }

    def state(self) -> dict:
        with self._lock:
            state = empty_state()
            first = next(iter(self.entries.values()))
            state.update(
                available=self.installed,
                plugin=self._plugin(),
                sources={n: dict(e) for n, e in self.entries.items()},
                on=any(e["enabled"] for e in self.entries.values()),
                strength=first["strength"],
                rate=first["rate"],
                cost=self._cost(),
            )
            return state

    def act(self, params: dict) -> str:
        on, strength, rate, source = parse_action(params)
        with self._lock:
            if source is not None and source not in self.entries:
                raise ActionError(f"Unknown camera source: {source}", 400)
            if on is not False and not self.installed:
                raise ActionError(missing_message(Plugin(installed=False)), 409)
            for name in [source] if source else list(self.entries):
                entry = self.entries[name]
                if on is not None:
                    entry["enabled"] = on
                if strength is not None:
                    entry["strength"] = strength
                if rate is not None:
                    entry["rate"] = rate
            over = self._cost()["state"] == "over"
            if over and not self._announced:
                self.notify("warn", "Background blur is over its budget: " + "; ".join(self._cost()["reasons"]))
            self._announced = over
        return message_for(on, strength, rate)

    def set_installed(self, installed: bool) -> str:
        """Demo only: pretend the plugin is (not) installed, so the install hint can be seen and tested."""
        with self._lock:
            self.installed = installed
            if not installed:
                for entry in self.entries.values():
                    entry["enabled"] = False
        return "Plugin installed" if installed else "Plugin removed"

    def health_checks(self) -> list[dict[str, str]]:
        with self._lock:
            installed, cost = self.installed, self._cost()
            on = any(e["enabled"] for e in self.entries.values())
        if not installed:
            return [blur.health_check(plugin=Plugin(installed=False))]
        if cost["state"] == "over":
            return [
                {
                    "id": "background-blur",
                    "group": "video",
                    "state": "warn",
                    "title": OVER_TITLE,
                    "detail": "; ".join(cost["reasons"]),
                    "hint": OVER_HINT,
                }
            ]
        if on:
            return [
                {
                    "id": "background-blur",
                    "group": "video",
                    "state": "ok",
                    "title": "Background blur is on (CPU)",
                    "detail": cost["detail"],
                    "hint": "",
                }
            ]
        return [blur.health_check(plugin=Plugin(installed=True, version="1.0", models_ok=True))]
