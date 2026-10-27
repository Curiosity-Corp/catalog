"""The `hardware` section of the Console state: what the probe found and the Plan resolved from it (ADR 0003).

`HardwareController` probes on a background thread (at start, on every hot-plug event, and on `hardware.refresh`) and
serves the cached result, because the hub reads the state once a second and a probe runs tools. `DemoHardware`
replays a packaged recording of a real host, so the demo rig and the end-to-end tests show a studio without one; it
alone offers `hardware.demo_host` to switch between the recorded hosts.

PipeWire node names carry the USB serial of the device. The probe and the Plan keep them (the audio chain needs the
real name); the Console state shows them with the serial replaced by `<serial>`.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any

from streamstack.console.errors import ActionError
from streamstack.hardware import camera, doctor, inventory, probe, roles, schema
from streamstack.hardware.hotplug import Change, Monitor
from streamstack.hardware.model import Snapshot
from streamstack.hardware.plan import Plan, resolve
from streamstack.hardware.redact import redact, redact_text

log = logging.getLogger("streamstack.console.hardware")

Notify = Callable[[str, str], None]

OVERRIDE_KEYS = (
    "STREAMSTACK_RIG",
    "STREAMSTACK_ENGINE_RENDER_NODE",
    "STREAMSTACK_CANON_RENDER_NODE",
    "STREAMSTACK_STREAM_RENDER_NODE",
    "STREAMSTACK_MICROPHONE",
    "STREAMSTACK_INTERFACE",
    "STREAMSTACK_CAMLINK_MODE",
    "STREAMSTACK_CAMLINK_4K",
    "STREAMSTACK_CAMERA_LABEL",
    "STREAMSTACK_PROGRAM_LABEL",
)
HISTORY = 8
DEFAULT_DEMO_HOST = "dm313red"


def overrides_from(cfg: Mapping[str, str], env: Mapping[str, str]) -> dict[str, str]:
    """The settings the Plan reads. A process environment variable beats the settings files (ADR 0003)."""
    found = {k: str(cfg.get(k, "")) for k in OVERRIDE_KEYS}
    for key in OVERRIDE_KEYS:
        if env.get(key):
            found[key] = env[key]
    return found


def _device(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "label": row.label,
        "class": row.device_class,
        "required": row.required,
        "expected": row.expected,
        "state": row.state,
        "via": row.via,
        "detail": row.detail,
    }


CONSUMER_LABELS = (
    ("engine", "Colour grade and decode (video engine)"),
    ("blur", "Background blur"),
    ("stream", "Stream encoder"),
    ("canon", "Canon decode"),
)


def video_state(plan: Plan) -> dict[str, Any]:
    """What the video jobs will use, by role: the render node for each, and the cameras and virtual cameras."""
    jobs = []
    for consumer, label in CONSUMER_LABELS:
        choice = roles.choose(consumer, plan, "")
        jobs.append({**choice.to_dict(), "label": label})
    loop = plan.loopback
    cameras = []
    capture = plan.capture
    if capture is not None:
        cameras.append(
            {
                "id": capture.profile_id,
                "label": capture.label,
                "state": capture.state,
                "usb": capture.usb_id,
                "node": capture.node_path,
            }
        )
    canon = plan.device("canon-eos-50d")
    if canon is not None:
        ids = camera.canon_ids()
        cameras.append(
            {
                "id": canon.id,
                "label": canon.label,
                "state": canon.state,
                "usb": ":".join(ids[0]) if ids else None,
                "node": loop.camera_path,
            }
        )
    return {
        "jobs": jobs,
        "cameras": cameras,
        "loopback": {
            "module_loaded": loop.module_loaded,
            "options_match": loop.options_match,
            "options": loop.options,
            "exclusive_caps": loop.exclusive_caps,
            "camera": {"node": loop.camera_path, "label": loop.camera_label, "declared": loop.declared_path("camera")},
            "program": {
                "node": loop.program_path,
                "label": loop.program_label,
                "declared": loop.declared_path("program"),
            },
        },
    }


def plan_state(plan: Plan, snapshot: Snapshot) -> dict[str, Any]:
    """The JSON the frontend renders for one Plan."""
    audio = plan.audio
    iface = audio.interface
    capture = plan.capture
    return {
        "rig": {
            "id": plan.rig.id,
            "label": plan.rig.label,
            "via": plan.rig.via,
            "score": plan.rig.score,
            "candidates": [{"id": ident, "score": score} for ident, score in plan.rig.candidates],
        },
        "devices": [_device(row) for row in plan.devices],
        "gpus": [
            {
                "pci_id": g.pci_id,
                "profile": g.profile_id,
                "name": g.name,
                "driver": g.driver,
                "node": g.node_path,
                "access": g.access,
                "encode": list(g.encode_codecs),
                "decode": list(g.decode_codecs),
                "probed": g.probed,
                "roles": list(g.roles),
            }
            for g in plan.gpus
        ],
        "roles": {
            r.role: {"node": r.node_path, "pci_id": r.pci_id, "profile": r.profile_id, "name": r.name, "via": r.via}
            for r in plan.gpu_roles
        },
        "encoder": {
            "codec": plan.encoder.codec,
            "ffmpeg": plan.encoder.ffmpeg,
            "hardware": plan.encoder.hardware,
            "node": plan.encoder.node_path,
            "verified": plan.encoder.verified,
            "reason": plan.encoder.reason,
        },
        "audio": {
            "interface": None
            if iface is None
            else {
                "id": iface.profile_id,
                "label": iface.label,
                "driver": iface.driver,
                "usb": iface.usb_id,
                "card": redact(iface.pipewire_card),
                "off": iface.card_off,
                "via": iface.via,
            },
            "rate": audio.rate,
            "format": audio.format,
            "bits": audio.bits,
            "resamples": audio.resamples,
            "channels": audio.capture_channels,
            "microphone": redact(audio.microphone),
            "monitor": redact(audio.monitor),
            "source": audio.source,
        },
        "capture": None
        if capture is None
        else {
            "id": capture.profile_id,
            "label": capture.label,
            "state": capture.state,
            "node": capture.node_path,
            "usb": capture.usb_id,
            "modes": [f"{m.width}x{m.height}@{m.fps:g} {m.fourcc}" for m in capture.modes],
            "best": None
            if capture.best_mode is None
            else f"{capture.best_mode.width}x{capture.best_mode.height}@{capture.best_mode.fps:g} "
            f"{capture.best_mode.fourcc}",
            "modes_state": capture.modes_state,
        },
        "loopback": {
            "camera": {"node": plan.loopback.camera_path, "label": plan.loopback.camera_label},
            "program": {"node": plan.loopback.program_path, "label": plan.loopback.program_label},
        },
        "video": video_state(plan),
        "deck": plan.deck,
        "lights": plan.lights,
        "notices": [{"id": n.id, "level": n.level, "text": redact_text(n.text)} for n in plan.notices],
        "sources": [s.to_dict() | {"error": redact_text(s.error)} for s in snapshot.sources],
    }


def empty_state() -> dict[str, Any]:
    return {
        "checked": False,
        "demo": False,
        "taken_at": None,
        "hotplug": {"mode": "stopped", "error": None},
        "rig": None,
        "devices": [],
        "gpus": [],
        "roles": {},
        "encoder": None,
        "audio": None,
        "capture": None,
        "loopback": None,
        "video": None,
        "deck": False,
        "lights": False,
        "notices": [],
        "sources": [],
        "changes": [],
        "error": None,
        "hosts": [],
        "host": None,
    }


def describe_change(change: Change) -> str | None:
    """One line for a toast: which devices came or went. None when no device the rig knows changed."""
    before = {d.id: d for d in change.old_plan.devices}
    lines = []
    for row in change.new_plan.devices:
        was = before.get(row.id)
        if was is not None and (was.state == "absent") != (row.state == "absent"):
            lines.append(f"{row.label} {'connected' if row.state != 'absent' else 'disconnected'}")
    return "; ".join(lines) or None


class HardwareController:
    def __init__(
        self,
        cfg: Mapping[str, str] | None = None,
        *,
        take: Callable[[], Snapshot] = probe.snapshot,
        catalogue: schema.Catalogue | None = None,
        env: Mapping[str, str] | None = None,
        watch: bool = True,
        debounce: float = 1.0,
    ) -> None:
        self.cfg = dict(cfg or {})
        self.notify: Notify = lambda level, text: None
        self._take = take
        self._catalogue = catalogue
        self._env = os.environ if env is None else env
        self._watch = watch
        self._debounce = debounce
        self._lock = threading.Lock()
        self._state = empty_state()
        self._plan: Plan | None = None
        self._snapshot: Snapshot | None = None
        self._changes: deque[dict[str, Any]] = deque(maxlen=HISTORY)
        self._monitor: Monitor | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._first = threading.Event()  # set when the first probe has finished, well or badly
        self._gave_up = False

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._boot, name="hardware", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        monitor, self._monitor = self._monitor, None
        if monitor is not None:
            monitor.stop()
        thread, self._thread = self._thread, None
        if thread and thread.is_alive():
            thread.join(timeout=10)

    def _boot(self) -> None:
        try:
            snapshot = self._probe_now()
        except Exception as exc:  # the controller must outlive a failed first probe and say so in its state
            log.exception("first hardware probe failed")
            self._fail(f"The hardware probe failed: {type(exc).__name__}: {exc}")
            return
        if self._watch and not self._stop.is_set():
            monitor = Monitor(self._probe, self._resolve, self._changed, debounce=self._debounce)
            self._monitor = monitor
            monitor.start(snapshot)
            self._set_hotplug(monitor)

    # -- probing --------------------------------------------------------------

    def _catalogue_now(self) -> schema.Catalogue:
        return self._catalogue if self._catalogue is not None else schema.packaged()

    def _overrides(self) -> dict[str, str]:
        return overrides_from(self.cfg, self._env)

    def _probe(self) -> Snapshot:
        return self._take()

    def _resolve(self, snapshot: Snapshot) -> Plan:
        return resolve(snapshot, self._catalogue_now(), self._overrides())

    def _probe_now(self) -> Snapshot:
        snapshot = self._probe()
        self._publish(snapshot, self._resolve(snapshot))
        return snapshot

    def _publish(self, snapshot: Snapshot, plan: Plan) -> None:
        state = {
            **empty_state(),
            **plan_state(plan, snapshot),
            "checked": True,
            "taken_at": snapshot.taken_at or time.time(),
        }
        with self._lock:
            self._snapshot, self._plan = snapshot, plan
            state["changes"] = list(self._changes)
            state["hotplug"] = self._state["hotplug"]
            state["demo"] = self._state["demo"]
            state["hosts"], state["host"] = self._state["hosts"], self._state["host"]
            self._state = state
        self._first.set()

    def _fail(self, message: str) -> None:
        with self._lock:
            self._state = {**self._state, "checked": True, "error": message}
        self._first.set()

    def _set_hotplug(self, monitor: Monitor) -> None:
        with self._lock:
            self._state = {**self._state, "hotplug": {"mode": monitor.mode, "error": monitor.error}}

    def _changed(self, change: Change) -> None:
        entry = {
            "at": time.time(),
            "sources": list(change.sources),
            "sections": list(change.sections),
            "text": describe_change(change),
        }
        with self._lock:
            self._changes.appendleft(entry)
        self._publish(change.new, change.new_plan)
        monitor = self._monitor
        if monitor is not None:
            self._set_hotplug(monitor)
        if entry["text"]:
            self.notify("info", f"Hardware: {entry['text']}")

    # -- reads ----------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        with self._lock:
            return self._state

    def plan(self) -> Plan | None:
        """The current Plan, for other controllers (None until the first probe finishes)."""
        with self._lock:
            return self._plan

    def wait_plan(self, timeout: float = 15.0) -> Plan | None:
        """The current Plan, waiting for the first probe if it has not finished (the video engine and the stream
        service choose their GPU from it, and starting on a guess would be wrong on a two-GPU machine). The wait
        happens once: if the probe never finishes, later calls return at once with None."""
        waiting = not self._first.is_set() and not self._gave_up
        if waiting and not self._first.wait(timeout):
            self._gave_up = True
            log.warning("the hardware probe has not finished after %.0f s; choosing GPUs without a Plan", timeout)
        return self.plan()

    def snapshot(self) -> Snapshot | None:
        with self._lock:
            return self._snapshot

    def health_checks(self) -> list[dict[str, str]]:
        """The doctor's `hardware` group for the System page, from the cached probe (empty until the first one)."""
        with self._lock:
            plan, snapshot = self._plan, self._snapshot
        if plan is None or snapshot is None:
            return []
        return doctor.checks(plan, snapshot, self._catalogue_now())

    # -- actions --------------------------------------------------------------

    def act(self, action: str, params: dict[str, Any]) -> str | None:
        if action == "hardware.demo_host":
            raise ActionError("Switching the recorded host is only available in the demo rig.", 409)
        if action != "hardware.refresh":
            raise ActionError(f"Unknown action {action!r}.", 400)
        monitor = self._monitor
        if monitor is not None:
            monitor.refresh()
            self._set_hotplug(monitor)
            if self._plan is None:
                self._probe_now()
        else:
            self._probe_now()
        return "Hardware probed again."


class DemoHardware(HardwareController):
    """Replays a packaged recording of a real host. No probe runs and nothing is touched."""

    def __init__(self, host: str = DEFAULT_DEMO_HOST, **kwargs: Any) -> None:
        super().__init__(watch=False, env={}, **kwargs)
        self._host = host if host in inventory.packaged_hosts() else DEFAULT_DEMO_HOST
        self._state = {
            **empty_state(),
            "demo": True,
            "hosts": inventory.packaged_hosts(),
            "host": self._host,
        }
        self._take = lambda: inventory.packaged_snapshot(self._host)
        self.start_sync()

    def start_sync(self) -> None:
        """The demo state is ready before the first request: replaying a file takes a few milliseconds."""
        self._probe_now()

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def act(self, action: str, params: dict[str, Any]) -> str | None:
        if action == "hardware.refresh":
            self._probe_now()
            return "Hardware probed again."
        if action == "hardware.demo_host":
            host = params.get("host")
            if not isinstance(host, str) or host not in inventory.packaged_hosts():
                raise ActionError(f"host must be one of: {', '.join(inventory.packaged_hosts())}.", 400)
            self._host = host
            with self._lock:
                self._state = {**self._state, "host": host}
            self._probe_now()
            return f"Showing the {host} recording."
        raise ActionError(f"Unknown action {action!r}.", 400)
