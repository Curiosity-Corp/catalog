"""The voice chain's state and its live link to PipeWire.

`ChainModel` is the single source of truth for what the chain should sound like: the current parameters, the preset
they started from, the denoise engine, the A/B bypass and the loudness target. It is pure (no PipeWire, no threads
besides a lock) so the real Console controller and the demo twin share it and report the same state.

`ChainRuntime` makes PipeWire match the model: it owns the `pipewire -c` host process (see `runtime`), pushes
changed controls to the live graph at up to 20 times a second (only the controls that changed, so a fader drag is a
handful of tiny messages and never a graph rebuild), crossfades the A/B switch, and restarts the host only when the
graph's *structure* changes (the microphone it reads or the denoise engine it is built with).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..hardware import voice
from ..hardware.plan import Plan
from . import defaults, graph, leveller, presets, runtime, stages

log = logging.getLogger(__name__)

WRITE_INTERVAL = 0.05
SUPERVISE_INTERVAL = 1.0
RELINK_MIN_S = 30.0  # between two rebuilds of the host because its capture reads the wrong (or no) microphone
UNLINKED_GRACE_S = 10.0  # how long the microphone may be present while the capture is still unlinked, before a rebuild
DEFAULT_RETRY_S = 5.0  # between tries to make StreamStack Mic the default source (pw-metadata not ready, failed)
FADE_STEPS = 6
HP_KEYS = (f"{stages.HP1}:Freq", f"{stages.HP2}:Freq")
FADE_STEP_S = 0.02
# Moving a biquad corner leaves a click behind (measured on the isolated
# rig, docs/audio-chain.md); every other control changes smoothly. So a high-pass corner is sent once its value has
# been still for this long, which turns a fader drag into one tick instead of a stream of them.
HP_SETTLE_S = 0.3


class ChainError(ValueError):
    """A request the chain cannot honour; `status` is the HTTP status the Console answers with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def estimate_reduction(params: dict[str, float | bool], rms_db: float, peak_db: float, auto_db: float = 0.0) -> float:
    """Gain reduction (dB, 0 or more) of the compressor and the limiter for a signal at these input levels.

    PipeWire does not expose a LADSPA plugin's output (metering) ports, so the real reduction cannot be read back.
    This applies the plugins' static curves to the microphone's level instead: the compressor's threshold, ratio
    and soft knee to the RMS level, the limiter's ceiling to the peak after the makeup gain. It follows the controls
    exactly and ignores attack and release, so it is the steady-state reduction, labelled as an estimate.
    """
    reduction = 0.0
    level = rms_db
    if params.get("comp_on", True):
        thr = float(params["comp_threshold_db"])
        ratio = float(params["comp_ratio"])
        knee = float(params["comp_knee_db"])
        over = level - thr
        slope = 1.0 - 1.0 / ratio
        if knee > 0 and abs(over) <= knee / 2:
            reduction = slope * (over + knee / 2) ** 2 / (2 * knee)
        elif over > 0:
            reduction = slope * over
    if params.get("limiter_on", True):
        makeup = float(params["makeup_db"]) if params.get("gain_on", True) else 0.0
        out_peak = peak_db - reduction + makeup + auto_db
        ceiling = float(params["limiter_ceiling_db"])
        if out_peak > ceiling:
            reduction += out_peak - ceiling
    return round(max(0.0, reduction), 1)


class ChainModel:
    """What the chain should be. Thread-safe; every change bumps `revision`."""

    def __init__(
        self,
        library: Callable[[], dict[str, presets.Preset]] = presets.load_all,
        *,
        store: Path | None = None,
        engines: Callable[[], list[str]] | None = None,
        rig_key: str | None = None,
    ) -> None:
        self._library = library
        self._store = store
        self._engines = engines or (lambda: graph.available_engines())  # looked up late: tests swap it
        self._lock = threading.RLock()
        self.revision = 0
        self._dirty = False
        self._mtime = 0.0
        self._rig_key = rig_key or self._stored_active() or "default"
        self._level = leveller.Leveller()
        self.loudness_on = False
        self.bypass = False
        self.preset = presets.DEFAULT_PRESET
        self.engine = "deepfilternet"
        self.params: dict[str, float | bool] = stages.defaults()
        self._base: dict[str, float | bool] = stages.defaults()
        self._base_engine = "deepfilternet"
        self._load_preset(self.preset, strict=False)
        self.restore()

    # -- presets ------------------------------------------------------------

    def presets(self) -> dict[str, presets.Preset]:
        return self._library()

    def _find(self, name: str) -> presets.Preset:
        found = self._library().get(name)
        if found is None:
            raise ChainError(f"Unknown voice preset {name!r}.", 400)
        return found

    def _load_preset(self, name: str, *, strict: bool) -> None:
        preset = (
            self._find(name) if strict else self._library().get(name) or self._library().get(presets.DEFAULT_PRESET)
        )
        if preset is None:
            self.preset, self._base, self.params = name, stages.defaults(), stages.defaults()
            return
        have = self._engines()
        if strict and preset.strict_engine and preset.engine not in have:
            raise ChainError(missing_engine_text(preset.engine), 409)
        self.preset = preset.name
        self._base = preset.resolved()
        self.params = dict(self._base)
        self.engine = self._base_engine = preset.engine

    def load_preset(self, name: str) -> None:
        with self._lock:
            self._load_preset(name, strict=True)
            self._changed()

    def reset(self) -> None:
        """Back to the chosen preset's own values."""
        with self._lock:
            self.params = dict(self._base)
            self.engine = self._base_engine
            self._changed()

    def save_as_preset(self, name: str, blurb: str = "") -> presets.Preset:
        """Keep the current sound as a user preset and make it the chosen one."""
        if not presets.NAME_OK.match(name):
            raise ChainError("The preset name must be 1 to 80 characters without slashes.", 400)
        with self._lock:
            preset = presets.Preset(name, blurb, self.engine, False, presets.delta(self.params), True)
            try:
                presets.save_user(preset)
            except presets.PresetError as exc:
                raise ChainError(str(exc), 409) from exc
            self.preset, self._base, self._base_engine = name, preset.resolved(), self.engine
            self.params = dict(self._base)
            self._changed()
            return preset

    # -- live changes ---------------------------------------------------------

    def set_params(self, changes: dict[str, Any]) -> None:
        try:
            clean = stages.validate(changes)
        except ValueError as exc:
            raise ChainError(str(exc), 400) from exc
        if not clean:
            raise ChainError("give at least one chain setting", 400)
        with self._lock:
            self.params.update(clean)
            self._changed()

    def set_engine(self, engine: str) -> None:
        if engine not in stages.DENOISE_ENGINES:
            raise ChainError("engine must be one of " + ", ".join(stages.DENOISE_ENGINES), 400)
        if engine not in self._engines():
            raise ChainError(missing_engine_text(engine), 409)
        with self._lock:
            self.engine = engine
            self._changed()

    def set_bypass(self, on: bool) -> None:
        with self._lock:
            self.bypass = on
            self._changed()

    def set_loudness(self, on: bool | None = None, target: float | None = None) -> None:
        with self._lock:
            if target is not None:
                if target not in stages.TARGETS_LUFS:
                    raise ChainError("target must be one of " + ", ".join(f"{t:g}" for t in stages.TARGETS_LUFS), 400)
                self._level.set_target(target)
            if on is not None:
                if on and not self.loudness_on:
                    self._level.reset(self._level.auto_db)
                self.loudness_on = on
            self._changed()

    def level_step(self, dt: float, lufs_m: float | None, muted: bool, mic_lufs: float | None = None) -> float:
        """Advance the leveller; returns `auto_db` (bumps the revision only when the gain moved)."""
        with self._lock:
            before = self._level.auto_db
            static = float(self.params["makeup_db"]) if self.params.get("gain_on", True) else 0.0
            after = self._level.update(
                dt, lufs_m, muted=muted, enabled=self.loudness_on, mic_lufs=mic_lufs, static_db=static
            )
            if after != before:
                self.revision += 1
            return after

    @property
    def auto_db(self) -> float:
        with self._lock:
            return self._level.auto_db

    @property
    def target(self) -> float:
        with self._lock:
            return self._level.target

    # -- derived --------------------------------------------------------------

    def active_engine(self) -> str:
        """The engine the graph is built with: the wanted one, else the best installed, else none."""
        with self._lock:
            wanted = self.engine
            strict = (self._library().get(self.preset) or presets.Preset(self.preset)).strict_engine
        have = self._engines()
        if strict and wanted not in have:
            return "off"
        return graph.pick_engine(wanted, have)

    def controls(self) -> dict[str, float]:
        with self._lock:
            return stages.controls(
                self.params, bypass=self.bypass, auto_db=self._level.auto_db, engine=self.active_engine()
            )

    def custom(self) -> bool:
        with self._lock:
            return self.params != self._base or self.engine != self._base_engine

    def view(self) -> dict[str, Any]:
        with self._lock:
            have = self._engines()
            status = self._level.status()
            return {
                "preset": self.preset,
                "presets": [preset_view(p, have) for p in self._library().values()],
                "bypass": self.bypass,
                "custom": self.custom(),
                "engine": self.engine,
                "engine_active": self.active_engine(),
                "engines": [engine_view(e, have) for e in stages.DENOISE_ENGINES],
                "stages": [
                    {
                        "id": s.id,
                        "label": s.label,
                        "blurb": s.blurb,
                        "on": bool(self.params[stages.on_key(s.id)]),
                        "params": [
                            {
                                "key": p.key,
                                "label": p.label,
                                "unit": p.unit,
                                "low": p.low,
                                "high": p.high,
                                "default": p.default,
                                "step": p.step,
                                "hint": p.hint,
                                "value": self.params[p.key],
                            }
                            for p in stages.PARAMS
                            if p.stage == s.id
                        ],
                    }
                    for s in stages.STAGES
                ],
                "loudness": {
                    "on": self.loudness_on,
                    "target_lufs": self._level.target,
                    "targets": list(stages.TARGETS_LUFS),
                    "auto_db": status.auto_db,
                    "level_lufs": status.level_lufs,
                    "state": status.state,
                    "window_db": stages.LEVEL_WINDOW_DB,
                    "true_peak_db": stages.TRUE_PEAK_CEILING_DB,
                },
            }

    # -- persistence (per rig: the microphone the profile belongs to) -------------

    def _changed(self) -> None:
        self.revision += 1
        self._dirty = True

    def set_rig(self, key: str) -> None:
        """Switch to another rig profile (another microphone) and restore what was saved for it."""
        if key and key != self._rig_key:
            self.flush()
            self._rig_key = key
            self.restore()
            self._dirty = True  # records which rig is active, so the next start continues with it
            self.flush()

    def _stored_active(self) -> str | None:
        active = self._read_store().get("active")
        return active if isinstance(active, str) and active else None

    def _store_mtime(self) -> float:
        try:
            return self._store.stat().st_mtime if self._store else 0.0
        except OSError:
            return 0.0

    def sync_external(self) -> bool:
        """Re-read the saved state when another process (the Auto director, `streamstack chain`) changed it."""
        mtime = self._store_mtime()
        with self._lock:
            if mtime <= self._mtime or self._dirty:
                return False
            self._mtime = mtime
            self.restore()
            return True

    def _read_store(self) -> dict[str, Any]:
        if self._store is None:
            return {}
        try:
            data = json.loads(self._store.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def restore(self) -> None:
        entry = self._read_store().get("rigs", {}).get(self._rig_key)
        if not isinstance(entry, dict):
            return
        with self._lock:
            name = entry.get("preset")
            if isinstance(name, str) and name in self._library():
                with contextlib.suppress(ChainError):
                    self._load_preset(name, strict=False)
            with contextlib.suppress(ValueError):
                self.params = stages.merged(self.params, entry.get("params", {}))
            if entry.get("engine") in stages.DENOISE_ENGINES:
                self.engine = entry["engine"]
            self.bypass = bool(entry.get("bypass", False))
            loud = entry.get("loudness", {})
            if isinstance(loud, dict):
                if loud.get("target_lufs") in stages.TARGETS_LUFS:
                    self._level.set_target(float(loud["target_lufs"]))
                self.loudness_on = bool(loud.get("on", False))
                auto = loud.get("auto_db")
                if isinstance(auto, (int, float)) and not isinstance(auto, bool):
                    self._level.reset(float(auto))
            self.revision += 1
            self._dirty = False
            self._mtime = self._store_mtime()

    def flush(self) -> bool:
        """Write the state if it changed since the last write; False on a write error (logged)."""
        if self._store is None:
            return True
        with self._lock:
            if not self._dirty:
                return True
            self._dirty = False
            data = self._read_store()
            data["active"] = self._rig_key
            rigs = data.setdefault("rigs", {})
            rigs[self._rig_key] = {
                "preset": self.preset,
                "params": self.params,
                "engine": self.engine,
                "bypass": self.bypass,
                "loudness": {
                    "on": self.loudness_on,
                    "target_lufs": self._level.target,
                    "auto_db": round(self._level.auto_db, 2),
                },
            }
        tmp = self._store.with_suffix(".json.tmp")
        try:
            self._store.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self._store)
            self._mtime = self._store_mtime()
        except OSError as exc:
            log.warning("cannot save the voice chain settings: %s", exc)
            self._dirty = True
            return False
        return True


def missing_engine_text(engine: str) -> str:
    info = graph.PLUGINS[engine]
    return (
        f"{info['name']} needs the {info['file']} LADSPA plugin ({info['package']}); "
        "install it, see docs/audio-chain.md."
    )


def engine_view(engine: str, installed: list[str]) -> dict[str, Any]:
    info = graph.PLUGINS[engine]
    here = engine in installed
    return {
        "id": engine,
        "label": info["name"],
        "delay_ms": round(graph.CHAIN_DELAY_S[engine] * 1000),
        "available": here,
        "reason": None if here else missing_engine_text(engine),
    }


def preset_view(preset: presets.Preset, installed: list[str]) -> dict[str, Any]:
    missing = preset.strict_engine and preset.engine not in installed
    return {
        "name": preset.name,
        "label": preset.label,
        "blurb": preset.blurb,
        "engine": preset.engine,
        "user": preset.user,
        "unavailable": missing_engine_text(preset.engine) if missing else None,
    }


def structure_signature(engine: str, microphone: str | None, rate: int = graph.RATE, quantum: int | None = None) -> str:
    """Identifies the graph's structure; the host restarts only when this changes."""
    text = f"{engine}|{microphone or ''}|{graph.GRAPH_VERSION}"
    if rate != graph.RATE or quantum:  # a graph at the conferencing rate with no quantum keeps its earlier signature
        text += f"|{rate}|{quantum or ''}"
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class ChainRuntime:
    """Keeps PipeWire matching a ChainModel."""

    def __init__(
        self,
        model: ChainModel,
        state_dir: Path,
        *,
        runner: runtime.Runner = runtime.default_runner,
        autostart: bool = True,
        manage_default: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.model = model
        self.state_dir = state_dir
        self.conf = runtime.conf_path(state_dir)
        self._manage_default = manage_default
        self._default_done = False  # StreamStack Mic has been made (or deliberately not made) the default source
        self._default_retry_at = 0.0
        self._relink_at = 0.0  # no rebuild for a wrong capture link before this time
        self._unlinked_since: float | None = None
        self.default_result: str | None = None  # what the last claim or release said (see audiochain/defaults.py)
        self._runner = runner
        self._autostart = autostart
        self._clock = clock
        self._microphone: str | None = None
        self._plan: Plan | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._sent: dict[str, float] = {}
        self._seen_rev = -1
        self._node_id: int | None = None
        self._nodes: runtime.Nodes | None = None
        self._signature: str | None = None
        self._mix = 0.0
        self._hp_since: float | None = None  # when the high-pass corner last changed, while it waits to be sent
        self._hp_last: float | None = None
        self._next_check = 0.0
        self.error: str | None = None
        self.running = False
        self.missing = graph.missing_required()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="audio-chain", daemon=True)
        self._thread.start()

    def stop(self, *, keep_host: bool = True) -> None:
        """Stop pushing changes. The host keeps running by default (a call survives a Console restart)."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        self.model.flush()
        if not keep_host:
            runtime.stop_host(self.conf)
            self.release_default()

    def poke(self) -> None:
        self._wake.set()

    def set_microphone(self, name: str | None) -> None:
        """Read this raw microphone (no Plan: the command line and the tests)."""
        with self._lock:
            self._microphone = name
        self._wake.set()

    def set_plan(self, plan: Plan | None) -> None:
        """Follow the hardware Plan: the microphone, the graph rate and the quantum come from its interface profile.
        Called again after every hot-plug change; the host is rebuilt only when the graph's structure changes."""
        with self._lock:
            self._plan = plan
        self._wake.set()

    @property
    def microphone(self) -> str | None:
        target = self.target()
        return target.microphone if target is not None else self._microphone

    def target(self) -> voice.VoiceTarget | None:
        """What the Plan says the chain should read and how to run, for the engine the graph is built with."""
        with self._lock:
            plan = self._plan
        return voice.voice_target(plan, self.model.active_engine()) if plan is not None else None

    def rate(self) -> int:
        target = self.target()
        return target.rate if target is not None else graph.RATE

    def quantum(self) -> int | None:
        target = self.target()
        return target.quantum if target is not None else None

    def declined(self) -> str | None:
        """Why the chain will not be started (no microphone to read), or None. A running host is left alone."""
        target = self.target()
        return target.reason if target is not None and target.microphone is None else None

    # -- the loop -------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # the writer must outlive any one bad round
                log.exception("voice chain update failed")
            self._wake.wait(WRITE_INTERVAL)
            self._wake.clear()

    def tick(self) -> None:
        now = self._clock()
        if now >= self._next_check:
            self._next_check = now + SUPERVISE_INTERVAL
            self.supervise()
        self.model.flush()
        if self.model.revision != self._seen_rev:
            self._seen_rev = self.model.revision
            self.push()

    def desired_signature(self) -> str:
        return structure_signature(self.model.active_engine(), self.microphone, self.rate(), self.quantum())

    def write_conf(self) -> bool:
        text = graph.build_config(
            self.model.params,
            engine=self.model.active_engine(),
            microphone=self.microphone,
            bypass=self.model.bypass,
            auto_db=self.model.auto_db,
            rate=self.rate(),
            quantum=self.quantum(),
        )
        return runtime.write_conf(self.conf, text + f"\n# structure {self.desired_signature()}\n")

    def _running_signature(self) -> str | None:
        try:
            for line in reversed(self.conf.read_text().splitlines()):
                if line.startswith("# structure "):
                    return line.split()[-1]
        except OSError:
            return None
        return None

    def supervise(self) -> None:
        """Make sure the host runs the graph we want and the nodes are there."""
        self.missing = graph.missing_required()
        if self.missing:
            self.error = "Install " + ", ".join(self.missing) + " (LADSPA plugins) for the voice chain."
            self.running = False
            self.release_default()
            return
        pids = runtime.host_pids(self.conf)
        wanted = self.desired_signature()
        declined = self.declined()
        stale = bool(pids) and self._running_signature() != wanted and declined is None
        if declined is not None and not pids:
            with self._lock:  # nothing to read: say why instead of publishing a silent microphone
                self._nodes, self._node_id, self.running, self.error = None, None, False, declined
            self.release_default()
            return
        if stale and self._autostart:
            log.info("voice chain structure changed; restarting its host")
            runtime.stop_host(self.conf)
            pids = []
            self._nodes = None
            self._node_id = None
        if not pids and self._autostart:
            self.write_conf()
            self._sent = {}
            if runtime.start_host(self.conf, self.conf.with_suffix(".log")) is None:
                self.error = "Cannot start the voice chain (is pipewire installed?)."
                self.running = False
                self.release_default()
                return
        elif pids and self._running_signature() is None:
            self.write_conf()
        nodes = runtime.find_nodes(self._runner)
        with self._lock:
            self._nodes = nodes
            self.running = bool(nodes and nodes.present)
            self._node_id = nodes.control_id if nodes else None
            if self.running:
                self.error = None
            elif not pids:
                self.error = "The voice chain is not running."
            else:
                self.error = "The voice chain is starting."
        if self.running and nodes is not None and self._relink_wrong_capture(nodes):
            return
        if self.running and not self._sent:
            self._seen_rev = -1  # a fresh graph: send everything once
        if self.running:
            self._claim_default()
        else:
            self._default_done = False  # the next time it runs, claim again (a restart keeps the earlier claim)

    def _relink_wrong_capture(self, nodes: runtime.Nodes) -> bool:
        """The capture must read the Plan's microphone and nothing else (issue #139). When it is linked to another
        source, or stays unlinked although the microphone is there, rebuild the host (at most every RELINK_MIN_S): a
        fresh host links to the pinned target. True when the host was stopped; the next round starts it again."""
        mic = self.microphone
        links = nodes.links
        if not self._autostart or not mic or mic == graph.NODE_NAME or not links.present or mic not in links.available:
            self._unlinked_since = None  # the microphone is not there (the capture waits for it) or nothing to judge
            return False
        now = self._clock()
        wrong = links.wrong(mic)
        if links.linked_to(mic) and not wrong:
            self._unlinked_since = None
            return False
        if not wrong:
            if self._unlinked_since is None:
                self._unlinked_since = now
            if now - self._unlinked_since < UNLINKED_GRACE_S:
                return False
        if now < self._relink_at:
            return False
        log.warning(
            "voice chain capture is %s instead of %s; rebuilding its host",
            "linked to " + ", ".join(wrong) if wrong else "not linked",
            mic,
        )
        self._relink_at = now + RELINK_MIN_S
        self._unlinked_since = None
        runtime.stop_host(self.conf)
        with self._lock:
            self._nodes, self._node_id, self.running = None, None, False
            self.error = "The voice chain is relinking to its microphone."
        self._default_done = False
        return True

    def _claim_default(self) -> None:
        """Once the chain is up, make StreamStack Mic the default source: apps on "Default" get the processed voice."""
        if not self._manage_default or self._default_done:
            return
        now = self._clock()
        if now < self._default_retry_at:
            return
        mic = self.microphone
        if mic is None or mic == graph.NODE_NAME:
            # No explicit microphone: the chain reads the default source, which must not become itself.
            self.release_default()
            self._default_done = True
            return
        self.default_result = defaults.claim(self.state_dir, self._runner)
        if self.default_result in ("claimed", "held", "user-changed"):
            self._default_done = True
        else:
            self._default_retry_at = now + DEFAULT_RETRY_S
            log.warning("StreamStack Mic could not be made the default source: %s", self.default_result)

    def release_default(self) -> str:
        """Give the default source back to the raw microphone (the chain stopped, or has nothing to read)."""
        if not self._manage_default:
            return "none"
        self.default_result = defaults.release(self.state_dir, self.microphone, self._runner)
        return self.default_result

    def push(self) -> None:
        """Send the controls that changed since the last push; the A/B mix is crossfaded."""
        node = self._node_id
        if node is None:
            return
        controls = self.model.controls()
        target_mix = controls.get(f"{stages.MIX}:Gain 2", 0.0)
        mix_keys = {f"{stages.MIX}:Gain 1", f"{stages.MIX}:Gain 2"}
        changed = {k: v for k, v in controls.items() if k not in mix_keys and self._sent.get(k) != v}
        hp_keys = [k for k in changed if k in HP_KEYS and k in self._sent]  # a fresh graph is sent at once
        if hp_keys:
            hz = changed[hp_keys[0]]
            now = self._clock()
            if hz != self._hp_last:
                self._hp_last, self._hp_since = hz, now
            if self._hp_since is not None and now - self._hp_since < HP_SETTLE_S:
                for key in hp_keys:
                    del changed[key]
                self._seen_rev = -1  # look again on the next round
        else:
            self._hp_since = self._hp_last = None
        if changed:
            if not runtime.set_controls(node, changed, self._runner):
                self._node_id = None  # the node went away: supervise looks it up again
                return
            self._sent.update(changed)
        if abs(target_mix - self._mix) > 1e-6:
            step = (target_mix - self._mix) / FADE_STEPS
            for i in range(FADE_STEPS):
                value = target_mix if i == FADE_STEPS - 1 else self._mix + step * (i + 1)
                if not runtime.set_controls(node, stages.bypass_controls(False, value), self._runner):
                    self._node_id = None
                    return
                if i < FADE_STEPS - 1:
                    time.sleep(FADE_STEP_S)
            self._mix = target_mix

    def status(self) -> dict[str, Any]:
        target = self.target()
        with self._lock:
            nodes = self._nodes
            return {
                "graph_rate": target.rate if target else graph.RATE,
                "graph_rate_reason": target.rate_reason if target else "no hardware plan: the conferencing rate",
                "quantum": target.quantum if target else None,
                "target": target.public() if target else None,
                "running": self.running,
                "available": not self.missing,
                "missing": list(self.missing),
                "error": self.error,
                "source": graph.NODE_NAME,
                "description": graph.DESCRIPTION,
                "rate": graph.RATE,
                "contract": nodes.contract() if nodes and nodes.present else None,
            }
