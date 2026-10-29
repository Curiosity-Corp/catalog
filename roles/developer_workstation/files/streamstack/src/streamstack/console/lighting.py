"""The Console's lighting controller: every Logitech Litra on the bus, and a simulated pair for --demo and tests.

State is read back from the lights where the protocol allows it (power, brightness, temperature all have a get), so
the buttons on a light show up in the Console within a few seconds. Hotplug is a sysfs rescan every few seconds:
no udev monitor, no extra dependency. All device I/O is serialised by one lock; `state()` never waits on it.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from streamstack import cli, config, lightauto, lighting, lightplan, store
from streamstack.console import lightauto as panel_module
from streamstack.console.errors import ActionError, Superseded
from streamstack.lighting import Found, LightingError, Litra, Transport
from streamstack.saving import LatestWriter, Nothing

log = logging.getLogger(__name__)

Notify = Callable[[str, str], None]
Scan = Callable[[], list[Found]]
Expect = tuple[bool | None, int | None, int | None]  # power, lumen, kelvin a caller believes a light shows
Opener = Callable[[Found], Transport]

RESCAN_SECONDS = 3.0
READBACK_SECONDS = 5.0
SETTLE_SECONDS = 1.5  # after a write, the light's own answer is not trusted over what we just set
MAX_NAME = 40
UNKNOWN_BRIGHTNESS = 50
UNKNOWN_KELVIN = 4500

BUILTIN_PRESETS: dict[str, dict[str, Any]] = {
    "Key light": {"on": True, "brightness": 80, "kelvin": 5000, "blurb": "Bright and neutral, for the face"},
    "Soft": {"on": True, "brightness": 45, "kelvin": 4200, "blurb": "Gentle fill, flattering on camera"},
    "Warm evening": {"on": True, "brightness": 35, "kelvin": 2700, "blurb": "Cosy, lamp-like"},
    "Daylight": {"on": True, "brightness": 100, "kelvin": 5600, "blurb": "Full strength, daylight white"},
    "Off": {"on": False, "brightness": None, "kelvin": None, "blurb": "All lights off"},
}
BUILTIN_ORDER = tuple(BUILTIN_PRESETS)


def _no_notify(level: str, text: str) -> None:
    return None


def _number(params: dict[str, Any], key: str, low: float, high: float) -> float:
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ActionError(f"{key} must be a number", 400)
    return float(min(high, max(low, value)))


def _flag(params: dict[str, Any], key: str = "on") -> bool:
    value = params.get(key)
    if not isinstance(value, bool):
        raise ActionError(f"{key} must be true or false", 400)
    return value


def _preset_name(params: dict[str, Any]) -> str:
    name = params.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ActionError("name is required", 400)
    name = name.strip()
    if len(name) > MAX_NAME or any(ord(c) < 32 for c in name):
        raise ActionError(f"A preset name is at most {MAX_NAME} characters, without control characters.", 400)
    return name


PRESET_PERCENT_TOLERANCE = 3.0  # percent of brightness a light may differ from a preset and still match it


class PresetLibrary:
    """Built-in lighting presets plus the user's, saved in ~/.config/streamstack/lighting.json (mode 600)."""

    def __init__(self, path: Path | None = None, *, ephemeral: bool = False) -> None:
        """`ephemeral`: still atomic, but no fsync and no backup (the demo rig's presets need no disk flush)."""
        self.path = path
        self.ephemeral = ephemeral
        self.lock = threading.Lock()  # serialises edits; held for the edit, never across the disk write
        self._writer: LatestWriter[dict[str, dict]] = LatestWriter(self._write)

    def file(self) -> Path:
        return self.path or config.user_env_file().parent / "lighting.json"

    def user(self) -> dict[str, dict[str, Any]]:
        # a preset being saved is listed from the moment it is accepted, not when its fsyncs finish (issue #168)
        data = self._writer.pending()
        if isinstance(data, Nothing):
            data = store.read_json(self.file(), {}, validate=store.is_object)
        saved: dict[str, dict[str, Any]] = {}
        for name, values in data.items() if isinstance(data, dict) else ():
            try:
                brightness, kelvin = values["brightness"], values["kelvin"]
                if not (isinstance(brightness, int) and isinstance(kelvin, int)) or isinstance(brightness, bool):
                    raise TypeError("brightness and kelvin must be whole numbers")
                saved[str(name)] = {
                    "on": True,
                    "brightness": int(min(100, max(1, brightness))),
                    "kelvin": lighting.clamp_kelvin(kelvin),
                }
            except (KeyError, TypeError):
                continue  # a hand-edited bad entry is skipped, never fatal
        return saved

    def listing(self) -> list[dict[str, Any]]:
        items = [{"name": n, "builtin": True, **BUILTIN_PRESETS[n]} for n in BUILTIN_ORDER]
        items += [{"name": n, "builtin": False, "blurb": "Saved by you", **v} for n, v in sorted(self.user().items())]
        return items

    def get(self, name: str) -> dict[str, Any]:
        for item in self.listing():
            if item["name"] == name:
                return item
        raise ActionError(f"No lighting preset called {name}.", 400)

    def _write(self, presets: dict[str, dict]) -> None:
        store.write_json(self.file(), presets, mode=0o600, backup=not self.ephemeral, durable=not self.ephemeral)

    def save(self, name: str, brightness: int, kelvin: int) -> None:
        if name.casefold() in {n.casefold() for n in BUILTIN_PRESETS}:
            raise ActionError(f"{name} is a built-in preset; pick another name.", 400)
        with self.lock:
            presets = self.user()
            presets[name] = {"brightness": brightness, "kelvin": kelvin}
            self._writer.publish(presets)
        try:
            self._writer.flush()
        except OSError as exc:
            raise ActionError(f"Could not save the preset: {exc.strerror or exc}", 409) from exc

    def delete(self, name: str) -> None:
        if name in BUILTIN_PRESETS:
            raise ActionError(f"{name} is a built-in preset and cannot be deleted.", 400)
        with self.lock:
            presets = self.user()
            if name not in presets:
                raise ActionError(f"No saved lighting preset called {name}.", 400)
            del presets[name]
            self._writer.publish(presets)
        try:
            self._writer.flush()
        except OSError as exc:
            raise ActionError(f"Could not delete the preset: {exc.strerror or exc}", 409) from exc


class _Slot:
    """One light the controller knows about."""

    def __init__(self, found: Found) -> None:
        self.found = found
        self.device: Litra | None = None
        self.error: str | None = None
        self.error_kind: str | None = None
        self.power: bool | None = None
        self.lumen: int | None = None
        self.kelvin: int | None = None
        self.last_write = float("-inf")
        self.last_read = float("-inf")
        self.identifying = False


class LightingController:
    def __init__(
        self,
        scan: Scan | None = None,
        opener: Opener | None = None,
        library: PresetLibrary | None = None,
        plan: lightplan.Store | None = None,
        rescan_seconds: float = RESCAN_SECONDS,
        readback_seconds: float = READBACK_SECONDS,
        blink_seconds: float = 0.25,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.notify: Notify = _no_notify
        self._scan = scan or lighting.discover
        self._open = opener or (lambda found: lighting.HidrawTransport(found.node))
        self.library = library or PresetLibrary()
        self.plan = plan or lightplan.Store()
        self.panel: panel_module.AutomationPanel | None = panel_module.AutomationPanel(self.plan)
        self.engine: Any = None  # the in-process automation engine of the demo rig; the real one is a service
        self.rescan_seconds, self.readback_seconds, self.blink_seconds = rescan_seconds, readback_seconds, blink_seconds
        self._clock = clock
        self._slots: dict[str, _Slot] = {}
        self._scan_error: str | None = None
        self._scanned = False
        self._lock = threading.Lock()  # guards the slot fields and the lists
        self._io = threading.RLock()  # serialises device I/O
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        if self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="console-lighting", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        with self._io, self._lock:
            for slot in self._slots.values():
                self._close(slot)

    def _loop(self) -> None:
        first = True
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self._rescan(announce=not first)
                self._readback()
            except Exception:  # a controller bug must not end hotplug for good
                log.exception("lighting scan failed")
            first = False
            self._wake.wait(self.rescan_seconds)

    def poll_once(self) -> None:
        """One rescan and read-back now, on the caller's thread (tests, and the first state before start())."""
        self._rescan(announce=False)
        self._readback(force=True)

    # -- discovery and hotplug --------------------------------------------------

    @staticmethod
    def _close(slot: _Slot) -> None:
        if slot.device:
            slot.device.close()
            slot.device = None

    def _rescan(self, announce: bool) -> None:
        try:
            found = {f.id: f for f in self._scan()}
            scan_error = None
        except LightingError as exc:
            found, scan_error = {}, exc.message
        messages: list[tuple[str, str]] = []
        with self._io:
            with self._lock:
                self._scan_error, self._scanned = scan_error, True
                for light_id in [i for i in self._slots if i not in found]:
                    slot = self._slots.pop(light_id)
                    self._close(slot)
                    messages.append(("warn", f"{slot.found.model.name} was unplugged"))
                for light_id, light in found.items():
                    if light_id not in self._slots:
                        self._slots[light_id] = _Slot(light)
                        messages.append(("ok", f"{light.model.name} connected"))
                    else:
                        self._slots[light_id].found = light
                slots = list(self._slots.values())
            for slot in slots:
                if slot.device is None:
                    self._connect(slot, messages)
        if announce:
            for level, text in messages:
                self.notify(level, text)

    def _connect(self, slot: _Slot, messages: list[tuple[str, str]]) -> None:
        """Open a slot's device and read its state. Caller holds the I/O lock."""
        previous = slot.error
        try:
            device = Litra(slot.found, self._open(slot.found), self._clock)
        except LightingError as exc:
            with self._lock:
                slot.error, slot.error_kind = exc.message, exc.kind
            if exc.message != previous:
                messages.append(("error", f"{slot.found.model.name}: {exc.message}"))
            return
        with self._lock:
            slot.device, slot.error, slot.error_kind = device, None, None
        self._read_state(slot)

    def _read_state(self, slot: _Slot) -> None:
        """Ask the light for power, brightness and temperature. Caller holds the I/O lock."""
        device = slot.device
        if device is None:
            return
        try:
            power, lumen, kelvin = device.read_power(), device.read_lumen(), device.read_kelvin()
        except LightingError as exc:
            self._fail(slot, exc)
            return
        with self._lock:
            slot.last_read = self._clock()
            if self._clock() - slot.last_write < SETTLE_SECONDS:
                return
            slot.power = power if power is not None else slot.power
            slot.lumen = lumen if lumen is not None else slot.lumen
            slot.kelvin = kelvin if kelvin is not None else slot.kelvin

    def _fail(self, slot: _Slot, exc: LightingError) -> None:
        with self._lock:
            self._close(slot)
            slot.error, slot.error_kind = exc.message, exc.kind

    def _readback(self, force: bool = False) -> None:
        now = self._clock()
        with self._io:
            with self._lock:
                due = [
                    s
                    for s in self._slots.values()
                    if s.device and (force or now - s.last_read >= self.readback_seconds) and not s.identifying
                ]
            for slot in due:
                self._read_state(slot)

    # -- state ----------------------------------------------------------------

    def _view(self, slot: _Slot, label: str, plan: dict[str, Any] | None = None) -> dict[str, Any]:
        model = slot.found.model
        brightness = lighting.lumen_to_percent(model, slot.lumen) if slot.lumen is not None else None
        mine = lightplan.light_settings(plan or lightplan.defaults(), slot.found.id)
        return {
            "id": slot.found.id,
            "name": mine["name"] or label,
            "default_name": label,
            "role": mine["role"],
            "limits": {k: mine[k] for k in ("min_brightness", "max_brightness", "min_kelvin", "max_kelvin")},
            "model": model.name,
            "pid": f"{model.pid:04x}",
            "connected": slot.device is not None,
            "error": slot.error,
            "error_kind": slot.error_kind,
            "power": slot.power,
            "brightness": brightness,
            "lumen": slot.lumen,
            "lumen_range": [model.lumen_min, model.lumen_max],
            "kelvin": slot.kelvin,
            "identifying": slot.identifying,
        }

    def _labels(self) -> dict[str, str]:
        """Names for the cards: the model, numbered when several lights share one."""
        slots = sorted(self._slots.values(), key=lambda s: s.found.id)
        counts: dict[str, int] = {}
        for slot in slots:
            counts[slot.found.model.name] = counts.get(slot.found.model.name, 0) + 1
        seen: dict[str, int] = {}
        labels = {}
        for slot in slots:
            name = slot.found.model.name
            seen[name] = seen.get(name, 0) + 1
            labels[slot.found.id] = f"{name} {seen[name]}" if counts[name] > 1 else name
        return labels

    @staticmethod
    def _master(lights: list[dict[str, Any]]) -> dict[str, Any]:
        usable = [x for x in lights if x["connected"]]
        lit = [x for x in usable if x["power"]]
        basis = lit or usable
        levels = [x["brightness"] for x in basis if x["brightness"] is not None]
        temps = [x["kelvin"] for x in basis if x["kelvin"] is not None]
        return {
            "on": bool(lit),
            "all_on": bool(usable) and len(lit) == len(usable),
            "brightness": round(sum(levels) / len(levels)) if levels else UNKNOWN_BRIGHTNESS,
            "kelvin": lighting.clamp_kelvin(sum(temps) / len(temps)) if temps else UNKNOWN_KELVIN,
        }

    @staticmethod
    def _active_preset(presets: list[dict[str, Any]], lights: list[dict[str, Any]]) -> str | None:
        usable = [x for x in lights if x["connected"]]
        if not usable:
            return None
        best: tuple[float, str] | None = None
        for preset in presets:
            if not preset["on"]:
                if all(x["power"] is False for x in usable):
                    return preset["name"]
                continue
            if not all(x["power"] and x["brightness"] is not None and x["kelvin"] is not None for x in usable):
                continue
            # A lumen step is about 0.3 %, and a light rounds to its own steps: match within a few percent and one
            # Kelvin step, and prefer the closest preset when several qualify.
            gap = max(
                max(abs(x["brightness"] - preset["brightness"]) / PRESET_PERCENT_TOLERANCE for x in usable),
                max(abs(x["kelvin"] - preset["kelvin"]) / lighting.KELVIN_STEP for x in usable),
            )
            if gap <= 1 and (best is None or gap < best[0]):
                best = (gap, preset["name"])
        return best[1] if best else None

    def _problem(self, lights: list[dict[str, Any]]) -> dict[str, Any] | None:
        if self._scan_error:
            return {"kind": "scan", "message": self._scan_error}
        denied = [x for x in lights if x["error_kind"] == "permission"]
        if denied:
            return {"kind": "permission", "message": f"{denied[0]['error']}"}
        broken = [x for x in lights if x["error"]]
        if broken:
            return {"kind": "io", "message": f"{broken[0]['name']}: {broken[0]['error']}"}
        return None

    def camera_in_use(self) -> bool:
        """Whether an app has the camera open (the stand-in for "a call is running" until call detection lands)."""
        if self.panel is None:
            return False
        try:
            return self.panel.camera_in_use()
        except (OSError, ValueError) as exc:
            log.info("camera use unknown: %s", exc)
            return False

    def state(self) -> dict[str, Any]:
        presets = self.library.listing()
        plan = self.plan.load()
        with self._lock:
            labels = self._labels()
            lights = [
                self._view(s, labels[s.found.id], plan) for s in sorted(self._slots.values(), key=lambda s: s.found.id)
            ]
            scanned = self._scanned
            problem = self._problem(lights)
        return {
            "automation": self.panel.state(lights) if self.panel is not None else panel_module.empty_automation(),
            "scanned": scanned,
            "lights": lights,
            "master": self._master(lights),
            "presets": presets,
            "preset": self._active_preset(presets, lights),
            "problem": problem,
            "kelvin_range": [lighting.KELVIN_MIN, lighting.KELVIN_MAX],
            "kelvin_step": lighting.KELVIN_STEP,
        }

    def lights_view(self) -> list[dict[str, Any]]:
        """Every light as the page sees it (names and roles included), without presets or the automation block."""
        plan = self.plan.load()
        with self._lock:
            labels = self._labels()
            return [
                self._view(s, labels[s.found.id], plan) for s in sorted(self._slots.values(), key=lambda s: s.found.id)
            ]

    def write(
        self,
        light_id: str,
        *,
        power: bool | None = None,
        percent: float | None = None,
        kelvin: float | None = None,
        expect: Expect | None = None,
    ) -> None:
        """Set one light for the automation engine. Raises ActionError when the light cannot be written.

        With `expect` (what the engine believes the light shows) the write is a compare-and-set: under the device
        lock, so no person's write can land between the check and the write, it raises `Superseded` and writes
        nothing when the light no longer shows that. This is what keeps a fade or a schedule step from landing on
        top of a change somebody made a moment ago."""
        self._write_all({"id": light_id}, power=power, percent=percent, kelvin=kelvin, expect=expect)

    def health_checks(self) -> list[dict[str, str]]:
        state = self.state()
        lights = state["lights"]
        if state["problem"] and state["problem"]["kind"] == "scan":
            return [
                cli.check(
                    "video", "lighting", "warn", "Litra lights could not be looked for", state["problem"]["message"]
                )
            ]
        if not lights:
            return [cli.check("video", "lighting", "ok", "no Logitech Litra light connected", "optional")]
        names = ", ".join(x["name"] for x in lights)
        denied = [x for x in lights if x["error_kind"] == "permission"]
        if denied:
            return [
                cli.check(
                    "video",
                    "lighting",
                    "fail",
                    f"{len(denied)} of {len(lights)} Litra lights cannot be opened (permission denied)",
                    names,
                    f"install the udev rule {lighting.UDEV_RULE_FILE} (docs/lighting.md), then replug the lights",
                )
            ]
        broken = [x for x in lights if x["error"]]
        if broken:
            return [
                cli.check(
                    "video",
                    "lighting",
                    "warn",
                    f"{broken[0]['name']} does not answer",
                    broken[0]["error"] or "",
                    "unplug and replug the light",
                )
            ]
        return [
            cli.check(
                "video", "lighting", "ok", f"{len(lights)} Litra light{'s' if len(lights) != 1 else ''} ready", names
            )
        ]

    # -- actions --------------------------------------------------------------

    def act(self, action: str, params: dict[str, Any]) -> str | None:
        if action == "lighting.power":
            return self._write_all(params, power=_flag(params))
        if action == "lighting.brightness":
            return self._write_all(params, percent=_number(params, "percent", 1, 100))
        if action == "lighting.temperature":
            return self._write_all(params, kelvin=_number(params, "kelvin", lighting.KELVIN_MIN, lighting.KELVIN_MAX))
        if action == "lighting.preset":
            return self._apply_preset(_preset_name(params))
        if action == "lighting.presets.save":
            return self._save_preset(_preset_name(params))
        if action == "lighting.presets.delete":
            name = _preset_name(params)
            self.library.delete(name)
            return f"Deleted lighting preset {name}"
        if action == "lighting.identify":
            return self._identify(params)
        if action == "lighting.rescan":
            self._wake.set()
            return "Looking for lights..."
        if action == "lighting.demo.reset":
            return self._demo_reset()
        if action.startswith(("lighting.auto.", "lighting.light.", "lighting.service.", "lighting.demo.")):
            if self.panel is None:
                raise ActionError("Lighting automation is not part of this rig.", 409)
            return self.panel.act(action, params, self._known_light)
        raise ActionError(f"Unknown action {action!r}.", 400)

    def _demo_reset(self) -> str | None:
        raise ActionError("Only the demo rig can reset its lights for a test.", 409)

    def _known_light(self, light_id: object) -> str:
        with self._lock:
            if not isinstance(light_id, str) or light_id not in self._slots:
                raise ActionError(f"No light called {light_id}.", 400)
        return light_id

    def _targets(self, params: dict[str, Any]) -> list[_Slot]:
        target = params.get("id", "all")
        if not isinstance(target, str):
            raise ActionError('id must be a light id or "all"', 400)
        with self._lock:
            if target == "all":
                slots = [s for s in sorted(self._slots.values(), key=lambda s: s.found.id) if s.device]
                if not slots:
                    broken = next((s for s in self._slots.values() if s.error), None)
                    raise ActionError(broken.error if broken and broken.error else "No Litra light is connected.", 409)
                return slots
            slot = self._slots.get(target)
            if slot is None:
                raise ActionError(f"No light called {target}.", 400)
            if slot.device is None:
                raise ActionError(slot.error or f"{slot.found.model.name} is not connected.", 409)
            return [slot]

    def _write_all(
        self,
        params: dict[str, Any],
        *,
        power: bool | None = None,
        percent: float | None = None,
        kelvin: float | None = None,
        expect: Expect | None = None,
    ) -> str | None:
        slots = self._targets(params)
        failures: list[str] = []
        with self._io:
            for slot in slots:
                if expect is not None:
                    self._require_unchanged(slot, expect)
                try:
                    self._write(slot, power, percent, kelvin)
                except LightingError as exc:
                    self._fail(slot, exc)
                    failures.append(f"{slot.found.model.name}: {exc.message}")
        if failures and len(failures) == len(slots):
            raise ActionError(failures[0], 409)
        return "; ".join(failures) or None

    def _require_unchanged(self, slot: _Slot, expect: Expect) -> None:
        """Raise Superseded unless the light still shows what the caller expects. Caller holds the I/O lock."""
        power, lumen, kelvin = expect
        with self._lock:
            shown = (slot.power, slot.lumen, slot.kelvin)
        if (
            (power is not None and shown[0] is not None and shown[0] != power)
            or (lumen is not None and shown[1] is not None and abs(shown[1] - lumen) >= lightauto.LUMEN_EPS)
            or (kelvin is not None and shown[2] is not None and abs(shown[2] - kelvin) >= lightauto.KELVIN_EPS)
        ):
            raise Superseded()

    def _write(self, slot: _Slot, power: bool | None, percent: float | None, kelvin: float | None) -> None:
        """Send values to one light and remember them. Caller holds the I/O lock."""
        device = slot.device
        if device is None:
            raise LightingError("not connected", "gone")
        lumen = kelv = None
        if kelvin is not None:
            kelv = device.set_kelvin(round(kelvin))
        if percent is not None:
            lumen = device.set_lumen(lighting.percent_to_lumen(slot.found.model, percent))
        if power is not None:
            device.set_power(power)
        with self._lock:
            slot.last_write = self._clock()
            if kelv is not None:
                slot.kelvin = kelv
            if lumen is not None:
                slot.lumen = lumen
            if power is not None:
                slot.power = power

    def _apply_preset(self, name: str) -> str:
        preset = self.library.get(name)
        slots = self._targets({"id": "all"})
        failures = []
        with self._io:
            for slot in slots:
                try:
                    if preset["on"]:
                        self._write(slot, True, preset["brightness"], preset["kelvin"])
                    else:
                        self._write(slot, False, None, None)
                except LightingError as exc:
                    self._fail(slot, exc)
                    failures.append(f"{slot.found.model.name}: {exc.message}")
        if failures and len(failures) == len(slots):
            raise ActionError(failures[0], 409)
        return "; ".join(failures) or f"Lighting: {name}"

    def _save_preset(self, name: str) -> str:
        with self._lock:
            lights = [self._view(s, "") for s in self._slots.values() if s.device]
        if not lights:
            raise ActionError("No Litra light is connected.", 409)
        master = self._master(lights)
        self.library.save(name, int(master["brightness"]), int(master["kelvin"]))
        return f"Saved lighting preset {name}"

    def _identify(self, params: dict[str, Any]) -> str:
        slots = self._targets(params)
        with self._lock:
            if any(s.identifying for s in slots):
                raise ActionError("Already flashing that light.", 409)
            for slot in slots:
                slot.identifying = True
        threading.Thread(target=self._blink, args=(slots,), name="console-lighting-blink", daemon=True).start()
        return "Flashing the light..."

    def _blink(self, slots: list[_Slot]) -> None:
        """Pulse each light between its dimmest and brightest, then put it back exactly as it was."""
        try:
            with self._io:
                saved = [(s, s.power, s.lumen) for s in slots]
                try:
                    for _ in range(3):
                        for level in (100, 1):
                            for slot in slots:
                                self._write(slot, True, level, None)
                            time.sleep(self.blink_seconds)
                finally:
                    for slot, power, lumen in saved:
                        try:
                            percent = lighting.lumen_to_percent(slot.found.model, lumen) if lumen is not None else None
                            self._write(slot, bool(power), percent, None)
                        except LightingError as exc:
                            self._fail(slot, exc)
        except LightingError as exc:
            for slot in slots:
                self._fail(slot, exc)
            self.notify("error", exc.message)
        except Exception:
            log.exception("identify failed")
        finally:
            with self._lock:
                for slot in slots:
                    slot.identifying = False


# ---------------------------------------------------------------------------
# The simulated rig
# ---------------------------------------------------------------------------

DEMO_LIGHTS = (
    (lighting.MODELS[0xC901], "usb-demo-1", True, 70, 5000),
    (lighting.MODELS[0xC901], "usb-demo-2", True, 55, 4200),
)


class DemoLighting(LightingController):
    """Two simulated Litra Beams speaking the real protocol through SimulatedTransport."""

    def __init__(
        self,
        blink_seconds: float = 0.05,
        library: PresetLibrary | None = None,
        plan: lightplan.Store | None = None,
    ) -> None:
        found = []
        devices: dict[str, lighting.SimulatedTransport] = {}
        for model, phys, on, percent, kelvin in DEMO_LIGHTS:
            light = Found(
                id=f"{model.pid:04x}-{phys}", node=Path("/dev/null"), model=model, phys=phys, name=f"Demo {model.name}"
            )
            found.append(light)
            devices[light.id] = lighting.SimulatedTransport(
                model, on=on, lumen=lighting.percent_to_lumen(model, percent), kelvin=kelvin
            )
        super().__init__(
            scan=lambda: list(found),
            opener=lambda light: devices[light.id],
            library=library or PresetLibrary(ephemeral=True),
            plan=plan or lightplan.Store(ephemeral=True),  # a demo's settings are not worth an fsync per change
            blink_seconds=blink_seconds,
        )
        panel_module.attach_demo(self)

    def _demo_reset(self) -> str | None:
        """Back to the rig's start: the lights, the automation settings, the pretend camera and director, the engine.

        One step under the engine's lock (see `Automation.reset`), so the engine never sees the lights move and
        cannot hold them as a change by hand. A person's write afterwards is held as usual."""
        panel, engine = self.panel, self.engine
        if panel is None or engine is None or panel.scripted is None:
            raise ActionError("Lighting automation is not part of this rig.", 409)

        def restore() -> None:
            panel.scripted.app = None
            panel.scripted.director = False
            data = lightplan.defaults()  # one save: the engine is held off while this runs
            data["resume"] = self.plan.load()["resume"]
            data["schedule"]["enabled"] = False
            self.plan.save(data)
            start = {f"{m.pid:04x}-{phys}": (on, percent, kelvin) for m, phys, on, percent, kelvin in DEMO_LIGHTS}
            with self._io:
                for slot in self._slots.values():
                    if slot.device is None or slot.found.id not in start:
                        continue
                    on, percent, kelvin = start[slot.found.id]
                    try:
                        self._write(slot, on, percent, kelvin)
                    except LightingError as exc:
                        raise ActionError(f"The demo light could not be reset: {exc.message}", 500) from exc
                    with self._lock:
                        slot.last_write = float("-inf")
                        slot.identifying = False

        engine.reset(restore)
        return None

    def start(self) -> None:
        self.poll_once()  # the simulated lights are there from the first snapshot
        super().start()
        if self.engine is not None:
            self.engine.start()

    def stop(self) -> None:
        if self.engine is not None:
            self.engine.stop()
        super().stop()


def empty_state() -> dict[str, Any]:
    """The lighting object for a rig without a lighting controller."""
    return {
        "scanned": True,
        "lights": [],
        "master": {"on": False, "all_on": False, "brightness": UNKNOWN_BRIGHTNESS, "kelvin": UNKNOWN_KELVIN},
        "presets": PresetLibrary().listing(),
        "preset": None,
        "problem": None,
        "kelvin_range": [lighting.KELVIN_MIN, lighting.KELVIN_MAX],
        "kelvin_step": lighting.KELVIN_STEP,
        "automation": panel_module.empty_automation(),
    }
