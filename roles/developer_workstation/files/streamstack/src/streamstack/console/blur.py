"""Background blur for the Console: the `Background Blur` filter on each camera source, over obs-websocket.

`BlurControl` is owned by `VideoController`, which hands it OBS connections: `refresh()` runs on the state
thread (events connection) and `act()` runs on an action thread (control connection). `DemoBlur` is the same
contract on the simulated rig. The state it adds to the snapshot is `video.blur` (docs/console.md):

    available    the plugin's filter kind is registered in the running OBS (None: OBS is not connected)
    plugin       what the files say: installed, version, models, devices (see streamstack.blur.Plugin.public)
    sources      per camera source: {present, enabled, strength, rate}
    on           any camera source has the blur enabled
    cost         the per-frame cost verdict against the gate (streamstack.blur.CostGuard.verdict)

obs-websocket requests used (obs-websocket 5 protocol): GetSourceFilterKindList, GetSourceFilter,
CreateSourceFilter, SetSourceFilterSettings, SetSourceFilterEnabled, GetStats.
"""

from __future__ import annotations

import copy
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from streamstack import blur
from streamstack.blur import FILTER_KIND, FILTER_NAME, CostGuard, Plugin
from streamstack.console.errors import ActionError

Notify = Callable[[str, str], None]

KIND_REFRESH_SECONDS = 15.0
STATS_MIN_GAP = 0.9
PLUGIN_TTL = 30.0
OVER_TITLE = "Background blur costs more than its budget"
OVER_HINT = "Camera page: set the blur smoothness to Light, or switch the blur off"
NO_CAMERA = "No camera source in OBS has the Lighting & Color filter yet."


class Conn(Protocol):
    def call(self, request: str, **data: Any) -> dict: ...


def _filter_kinds(conn: Conn) -> list[str]:
    """The filter kinds the running OBS offers (GetSourceFilterKindList answers `sourceFilterKinds`)."""
    return list(conn.call("GetSourceFilterKindList").get("sourceFilterKinds") or [])


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


def missing_message(plugin: Plugin, kind_present: bool | None) -> str:
    if not plugin.installed:
        return f"Background blur needs the {blur.PLUGIN_NAME} OBS plugin: {blur.INSTALL_HINT}."
    if not plugin.models_ok:
        return f"The {blur.PLUGIN_NAME} models are missing: reinstall the package, then restart OBS."
    return f"{blur.PLUGIN_NAME} is installed but OBS has not loaded it: restart OBS."


class BlurControl:
    """Reads and drives the blur filter on the real OBS."""

    def __init__(
        self,
        notify: Notify | None = None,
        *,
        detect: Callable[[], Plugin] = blur.detect,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._notify = notify
        self._detect = detect
        self._clock = clock
        self._lock = threading.RLock()
        self._state = empty_state()
        self._guard = CostGuard()
        self._plugin: Plugin | None = None
        self._plugin_at = -PLUGIN_TTL
        self._kind: bool | None = None
        self._kind_at = -KIND_REFRESH_SECONDS
        self._stats_at = -STATS_MIN_GAP
        self._last_cost = "off"

    # -- shared -------------------------------------------------------------

    def notify(self, level: str, text: str) -> None:
        if self._notify:
            self._notify(level, text)

    def state(self) -> dict:
        plugin = self.plugin().public()  # known before OBS connects, so the install hint shows while OBS is down
        with self._lock:
            snapshot = _copy(self._state)
        snapshot["plugin"] = plugin
        return snapshot

    def reset(self) -> None:
        """OBS went away: forget what it said (the plugin's files are still what they were)."""
        with self._lock:
            keep = self._state["plugin"]
            self._state = empty_state()
            self._state["plugin"] = keep
            self._guard.reset()
            self._kind = None
            self._kind_at = -KIND_REFRESH_SECONDS
            self._last_cost = "off"

    def plugin(self) -> Plugin:
        """The installed plugin, re-read at most every PLUGIN_TTL seconds (file scans are cheap but not free)."""
        now = self._clock()
        with self._lock:
            if self._plugin is not None and now - self._plugin_at < PLUGIN_TTL:
                return self._plugin
        try:
            found = self._detect()
        except OSError:
            found = self._plugin or Plugin(installed=False)  # a flaky read must not flip the control around
        with self._lock:
            self._plugin, self._plugin_at = found, now
        return found

    # -- state thread ---------------------------------------------------------

    def refresh(self, conn: Conn, sources: list[str], structural: bool = False) -> None:
        """Re-read the filter on every camera source and sample OBS's stats. Request failures are skipped."""
        plugin = self.plugin()
        now = self._clock()
        if structural or self._kind is None or now - self._kind_at > KIND_REFRESH_SECONDS:
            try:
                kinds = _filter_kinds(conn)
                self._kind = FILTER_KIND in kinds
                self._kind_at = now
            except RuntimeError:
                pass  # OBS refused this request; keep the last answer
        entries: dict[str, dict] = {}
        if self._kind:
            for name in sources:
                entries[name] = self._read_filter(conn, name)
        else:
            entries = {name: _entry(False) for name in sources}
        any_on = any(e["enabled"] for e in entries.values())
        if self._kind and now - self._stats_at >= STATS_MIN_GAP:
            self._stats_at = now
            self._sample(conn, any_on)
        with self._lock:
            first = next((e for e in entries.values() if e["present"]), None)
            self._state.update(
                available=bool(self._kind),
                plugin=plugin.public(),
                sources=entries,
                on=any_on,
                strength=first["strength"] if first else self._state["strength"],
                rate=first["rate"] if first else self._state["rate"],
                cost=self._cost(),
            )
        self._announce_cost()

    @staticmethod
    def _read_filter(conn: Conn, name: str) -> dict:
        try:
            data = conn.call("GetSourceFilter", sourceName=name, filterName=FILTER_NAME)
        except RuntimeError:
            return _entry(False)  # the source has no Background Blur filter yet
        strength, rate = blur.read_settings(data.get("filterSettings") or {})
        return _entry(True, bool(data.get("filterEnabled")), strength, rate)

    def _sample(self, conn: Conn, blur_on: bool) -> None:
        try:
            sample = blur.parse_stats(conn.call("GetStats"))
        except RuntimeError:
            return
        if sample is not None:
            with self._lock:
                self._guard.observe(sample, blur_on)

    def _cost(self) -> dict:
        with self._lock:
            return self._guard.verdict()

    def _announce_cost(self) -> None:
        with self._lock:
            cost = self._state["cost"]
            now, before = cost["state"], self._last_cost
            self._last_cost = now
        if now == "over" and before != "over":
            reasons = "; ".join(cost.get("reasons") or [])
            self.notify("warn", f"Background blur is over its budget: {reasons}. Lower the mask rate or switch it off.")

    # -- actions --------------------------------------------------------------

    def act(self, conn: Conn, params: dict, sources: list[str], selected: str | None = None) -> str:
        """video.blur: on / strength / rate for one camera (`source`) or for all of them."""
        on, strength, rate, source = parse_action(params)
        if not sources:
            raise ActionError(NO_CAMERA, 409)
        if source is not None and source not in sources:
            raise ActionError(f"Unknown camera source: {source}", 400)
        targets = [source] if source else list(sources)
        self._need_plugin(conn, on)
        patch = blur.settings_patch(strength, rate)
        for name in targets:
            self._apply(conn, name, on, patch)
        with self._lock:
            for name in targets:
                entry = self._state["sources"].setdefault(name, _entry(True))
                entry.update(present=True)
                if on is not None:
                    entry["enabled"] = on
                if strength is not None:
                    entry["strength"] = strength
                if rate is not None:
                    entry["rate"] = rate
            self._state["on"] = any(e["enabled"] for e in self._state["sources"].values())
            if strength is not None or rate is not None:
                self._state.update(strength=strength or self._state["strength"], rate=rate or self._state["rate"])
        return message_for(on, strength, rate)

    def _need_plugin(self, conn: Conn, on: bool | None) -> None:
        """Turning the blur off must always work; anything else needs the filter kind in the running OBS."""
        if on is False:
            return
        try:
            kinds = _filter_kinds(conn)
        except RuntimeError as exc:
            raise ActionError(f"OBS would not list its filters: {exc}", 409) from exc
        present = FILTER_KIND in kinds
        with self._lock:
            self._kind, self._kind_at = present, self._clock()
        if not present:
            raise ActionError(missing_message(self.plugin(), present), 409)

    def _apply(self, conn: Conn, name: str, on: bool | None, patch: dict[str, int]) -> None:
        """Make sure the source has the filter, then write the settings and the enabled flag."""
        try:
            data = conn.call("GetSourceFilter", sourceName=name, filterName=FILTER_NAME)
            created = False
        except RuntimeError:
            data = None
            created = True
        try:
            if created:
                if on is False:
                    return  # nothing to switch off
                values = {**blur.low_latency_settings(), **patch}
                conn.call(
                    "CreateSourceFilter",
                    sourceName=name,
                    filterName=FILTER_NAME,
                    filterKind=FILTER_KIND,
                    filterSettings=values,
                )
                if on is True:
                    return  # OBS creates a filter enabled
                conn.call("SetSourceFilterEnabled", sourceName=name, filterName=FILTER_NAME, filterEnabled=False)
                return
            values = dict(patch)
            current = (data or {}).get("filterSettings") or {}
            if on and not _positive(current.get("blur_background")) and "blur_background" not in values:
                values["blur_background"] = blur.DEFAULT_STRENGTH  # 0 would remove the background, not blur it
            if values:
                conn.call(
                    "SetSourceFilterSettings",
                    sourceName=name,
                    filterName=FILTER_NAME,
                    filterSettings=values,
                    overlay=True,
                )
            if on is not None:
                conn.call("SetSourceFilterEnabled", sourceName=name, filterName=FILTER_NAME, filterEnabled=on)
        except RuntimeError as exc:
            raise ActionError(f"OBS would not change the blur on {name}: {exc}", 409) from exc

    # -- health ---------------------------------------------------------------

    def health_checks(self) -> list[dict[str, str]]:
        """Live findings that replace the static "background-blur" check; empty while there is nothing to add."""
        with self._lock:
            state = _copy(self._state)
        plugin = self.plugin()
        base = {"id": "background-blur", "group": "video"}
        if state["available"] is None:
            return []  # OBS is not connected: the static check from the files stands
        if not state["available"]:
            if not plugin.installed:
                return []  # the static check already says the plugin is missing
            return [
                {
                    **base,
                    "state": "warn",
                    "title": f"{blur.PLUGIN_NAME} is installed but OBS has not loaded it",
                    "detail": f"{FILTER_KIND} is not among OBS's filter kinds",
                    "hint": "restart OBS (plugins load at start)",
                }
            ]
        cost = state["cost"]
        if cost["state"] == "over":
            return [
                {
                    **base,
                    "state": "warn",
                    "title": OVER_TITLE,
                    "detail": "; ".join(cost.get("reasons") or []),
                    "hint": OVER_HINT,
                }
            ]
        if state["on"] or not plugin.installed:
            device = ", ".join(plugin.device_labels)
            title = f"Background blur is on ({device})" if state["on"] else "Background blur is available in OBS"
            return [
                {**base, "state": "ok", "title": title, "detail": cost["detail"] if state["on"] else "", "hint": ""}
            ]
        return []


def _positive(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 1


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
        return Plugin(installed=True, version="1.4.1", models_ok=True, loaded=True, devices=("cpu",)).public()

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
                raise ActionError(missing_message(Plugin(installed=False), False), 409)
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
        return [
            blur.health_check(
                plugin=Plugin(
                    installed=True,
                    version="1.4.1",
                    library="/usr/local/lib/obs-plugins/obs-backgroundremoval.so",
                    models_ok=True,
                )
            )
        ]
