"""Console audio: meters, microphone gain and mute, the voice chain (presets, stage faders, loudness), mic check.

`AudioController` drives the real rig: PipeWire through pactl/parec for the microphone and the meters, and the
StreamStack voice chain (`streamstack.audiochain`, a PipeWire filter-chain that StreamStack generates and hosts)
through its `ChainModel` and `ChainRuntime`. `DemoAudio` is a simulated twin with the same methods for ``--demo``,
tests and e2e runs; it shares the same `ChainModel`, so it reports exactly the state the real one does.

The chain's output is the virtual source "StreamStack Mic" (`streamstack_mic`, 48 kHz mono). Its loudness is
metered here (the "voice" reader) and drives the loudness leveller, which adds a slow gain on top of the chain's
makeup gain (see `audiochain.leveller`; one owner of gain, never the microphone's own gain, never 48 V).
"""

from __future__ import annotations

import array
import contextlib
import json
import logging
import math
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import config, micwatch, system
from ..audiochain import chain as chainlib
from ..audiochain import graph as chaingraph
from ..audiochain import migrate, stages
from ..hardware import voice
from ..hardware.plan import Plan
from ..hardware.redact import redact
from . import loudness
from .errors import ActionError
from .outputs import DemoOutputs, Outputs

log = logging.getLogger(__name__)

Notify = Callable[[str, str], None]

SILENCE_DB = -90.0
RATE = loudness.RATE  # the meter tap is the sources' native 48 kHz, 32 bit float, mono
FRAME_BYTES = loudness.FRAME_SAMPLES * 4  # 20 ms
READ_BYTES = FRAME_BYTES * 2
STALE_S = 0.5  # a reader that sent nothing for this long is silent, whatever it last said

GAIN_RANGE = (-30.0, 12.0)
TONE_RANGES: dict[str, tuple[float, float]] = {
    key: (stages.PARAM_BY_KEY[key].low, stages.PARAM_BY_KEY[key].high)
    for key in ("bass_db", "presence_db", "treble_db")
}
BAND_KEYS = tuple(TONE_RANGES)
ATTEN_RANGE = (stages.PARAM_BY_KEY["denoise_atten_db"].low, stages.PARAM_BY_KEY["denoise_atten_db"].high)

PROBE_INTERVAL = 1.0
PLAN_REFRESH_S = 5.0  # at most one hardware re-probe this often when the Plan and PipeWire disagree
CARD_RETRY_S = 30.0  # at most one card-profile switch this often for the same card
RESTART_INTERVAL = 2.0
READER_SILENT_S = 4.0  # our own parec heard nothing this long: corroboration only, never a trigger
READER_IDLE_RESTART = 15.0  # a parec that has produced nothing for this long is killed and started afresh
REVIVE_SHOW_S = 8.0  # how long "the microphone is back" stays in the state
CLIP_TTL = 600.0
MAX_CLIPS = 4
CHECK_SECONDS = (2.0, 15.0)
CHECK_DEFAULT = 6.0
VOICE_SOURCE_NAME = chaingraph.NODE_NAME
LEVEL_INTERVAL = 0.1  # how often the leveller looks at the loudness and moves the gain
DEFAULT_ENGINE = "deepfilternet"

CLIP_ID = re.compile(r"^[0-9a-f]{16}$")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def to_db(linear: float) -> float:
    """A 0..1 amplitude in dBFS, floored at silence."""
    if linear <= 0:
        return SILENCE_DB
    return max(SILENCE_DB, round(20 * math.log10(linear), 1))


def parse_volume_db(volume: object) -> float | None:
    """The first channel's gain in dB from a pactl JSON `volume` object (None when silent or unknown)."""
    if not isinstance(volume, dict):
        return None
    for channel in volume.values():
        if not isinstance(channel, dict):
            continue
        if match := re.match(r"\s*(-?\d+(?:\.\d+)?)\s*dB", str(channel.get("db", ""))):
            return float(match.group(1))
        value = channel.get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return round(60 * math.log10(value / 65536), 2)  # PulseAudio volumes are cubic
        return None
    return None


def parse_sources(text: str) -> list[dict[str, Any]]:
    """Name, description, gain and mute of every source in `pactl -f json list sources`."""
    try:
        sources = json.loads(text)
    except ValueError:
        return []
    found = []
    for entry in sources if isinstance(sources, list) else []:
        if isinstance(entry, dict) and entry.get("name"):
            found.append(
                {
                    "name": str(entry["name"]),
                    "description": str(entry.get("description", "")),
                    "gain_db": parse_volume_db(entry.get("volume")),
                    "muted": bool(entry.get("mute", False)),
                }
            )
    return found


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def number_param(params: dict[str, Any], key: str, *, low: float, high: float, clamped: bool = False) -> float:
    """A required finite number in [low, high] (or clamped into it); anything else is a 400."""
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ActionError(f"{key} must be a number", 400)
    if clamped:
        return float(clamp(value, low, high))
    if not low <= value <= high:
        raise ActionError(f"{key} must be between {low:g} and {high:g}", 400)
    return float(value)


def bool_param(params: dict[str, Any], key: str = "on") -> bool:
    value = params.get(key)
    if not isinstance(value, bool):
        raise ActionError(f"{key} must be true or false", 400)
    return value


# ---------------------------------------------------------------------------
# Parts
# ---------------------------------------------------------------------------


_SILENT_FRAME = (SILENCE_DB, SILENCE_DB, SILENCE_DB, False, SILENCE_DB, SILENCE_DB)


def meter_frame(
    peak_db: float = SILENCE_DB,
    sample_db: float = SILENCE_DB,
    rms_db: float = SILENCE_DB,
    clip: bool = False,
    lufs_m: float = SILENCE_DB,
    lufs_s: float = SILENCE_DB,
    latch: loudness.ClipLatch | None = None,
) -> dict[str, Any]:
    """One source's entry in a `meters` frame. `peak_db` is the true peak (dBTP), `sample_db` the sample peak (dBFS);
    `clip` says this poll reached -1 dBTP or the rail, `latched` stays true until the latch is cleared."""
    return {
        "peak_db": peak_db,
        "sample_db": sample_db,
        "rms_db": rms_db,
        "clip": clip,
        "latched": latch.latched if latch else False,
        "lufs_m": lufs_m,
        "lufs_s": lufs_s,
    }


class Throttle:
    """At most one firing per interval; the newest value submitted wins (leading edge when idle, then trailing)."""

    def __init__(self, interval: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.interval = interval
        self.clock = clock
        self._value: Any = None
        self._pending = False
        self._last = -math.inf

    def submit(self, value: Any) -> None:
        self._value, self._pending = value, True

    def cancel(self) -> None:
        self._value, self._pending = None, False

    def ready_in(self) -> float | None:
        """Seconds until the pending value may fire (0 = now); None when nothing is pending."""
        if not self._pending:
            return None
        return max(0.0, self._last + self.interval - self.clock())

    def take(self) -> Any:
        """The pending value if it may fire now (and record the firing), else None."""
        wait = self.ready_in()
        if wait is None or wait > 1e-6:  # tolerate float dust from the clock arithmetic
            return None
        value, self._value, self._pending = self._value, None, False
        self._last = self.clock()
        return value


class LevelReader:
    """One `parec` child (48 kHz float) turned into 20 ms true-peak, RMS and LUFS frames by a reader thread."""

    def __init__(
        self,
        source: str,
        clock: Callable[[], float] = time.monotonic,
        spawn: Callable[..., subprocess.Popen[bytes]] | None = None,
        idle_since: float | None = None,
        latch: loudness.ClipLatch | None = None,
    ) -> None:
        self.source = source
        self.latch = latch or loudness.ClipLatch()
        self._analyzer = loudness.Analyzer()
        self.started = clock()
        self._clock = clock
        self._origin = self.started if idle_since is None else idle_since
        self._last_bytes: float | None = None
        self._zero_since: float | None = None  # when the current unbroken run of exact-zero frames began
        self._spawn = spawn or subprocess.Popen
        self._lock = threading.Lock()
        self._buffer = b""
        self._last = _SILENT_FRAME
        self._acc_peak = SILENCE_DB
        self._acc_sample = SILENCE_DB
        self._acc_clip = False
        self._fresh = False
        self._stamp = -math.inf
        self._proc: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self.dead = False

    def start(self) -> bool:
        try:
            self._proc = self._spawn(
                [
                    "parec",
                    "--raw",
                    "--format=float32le",
                    f"--rate={RATE}",
                    "--channels=1",
                    "--latency-msec=20",
                    f"--device={self.source}",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            log.warning("cannot start parec for %s: %s", self.source, exc)
            self.dead = True
            return False
        self._thread = threading.Thread(target=self._pump, name=f"meter-{self.source[:24]}", daemon=True)
        self._thread.start()
        return True

    def _pump(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None  # noqa: S101 - set by start()
        try:
            fd = proc.stdout.fileno()
            while chunk := os.read(fd, READ_BYTES):
                self.feed(chunk)
        except (OSError, ValueError):
            pass  # the pipe closed under us: stop() or the source went away
        self.dead = True

    def feed(self, data: bytes) -> None:
        """Consume raw float32le bytes; every complete 20 ms frame updates the levels."""
        if data:
            self._last_bytes = self._clock()
        self._buffer += data
        while len(self._buffer) >= FRAME_BYTES:
            frame, self._buffer = self._buffer[:FRAME_BYTES], self._buffer[FRAME_BYTES:]
            if frame.count(0) == len(frame):  # +0.0 in float32 is four zero bytes: no decoding needed
                if self._zero_since is None:
                    self._zero_since = self._clock()
            else:
                self._zero_since = None
            samples = array.array("f")
            samples.frombytes(frame)
            if sys.byteorder == "big":
                samples.byteswap()
            try:
                levels = self._analyzer.process(samples)
            except (ArithmeticError, ValueError):  # a malformed frame must not kill the reader thread
                log.exception("metering a frame from %s failed", self.source)
                self._analyzer.reset()
                continue
            peak_db, sample_db = to_db(levels.true_peak), to_db(levels.sample_peak)
            with self._lock:
                clipped = self.latch.trip(sample_db, peak_db)
                self._acc_peak = max(self._acc_peak, peak_db) if self._fresh else peak_db
                self._acc_sample = max(self._acc_sample, sample_db) if self._fresh else sample_db
                self._acc_clip = (self._acc_clip or clipped) if self._fresh else clipped
                self._last = (
                    peak_db,
                    sample_db,
                    to_db(levels.rms),
                    clipped,
                    round(levels.lufs_m, 1),
                    round(levels.lufs_s, 1),
                )
                self._fresh = True
                self._stamp = self._clock()

    def idle_since(self) -> float:
        """The clock time the source was last heard from (or when it was first waited for)."""
        return self._origin if self._last_bytes is None else self._last_bytes

    def idle_for(self) -> float:
        """Seconds without a single byte from the source."""
        return max(0.0, self._clock() - self.idle_since())

    def zero_for(self) -> float | None:
        """Seconds of unbroken exact zeros up to the latest frame (digital silence); None while the source delivers
        nothing at all (that is a stall, not digital silence) or the latest frame had signal."""
        if self._zero_since is None or self._clock() - self._stamp > STALE_S:
            return None
        return max(0.0, self._clock() - self._zero_since)

    def take(self) -> dict[str, Any]:
        """Levels since the last call: the loudest peaks (no frame is missed between polls), the latest RMS and LUFS."""
        with self._lock:
            if self._clock() - self._stamp > STALE_S:
                return meter_frame(latch=self.latch)
            peak, sample, rms, clipped, lufs_m, lufs_s = self._last
            if self._fresh:
                peak, sample, clipped = self._acc_peak, self._acc_sample, self._acc_clip
                self._fresh = False
            return meter_frame(peak, sample, rms, clipped, lufs_m, lufs_s, self.latch)

    def latest(self) -> tuple[float, float, float, float] | None:
        """(momentary LUFS, short-term LUFS, true peak dB, RMS dB) of the latest frame without consuming anything
        (the leveller reads this while the Console's meter poll uses `take`); None when the source is stale."""
        with self._lock:
            if self._clock() - self._stamp > STALE_S:
                return None
            peak, _sample, rms, _clipped, lufs_m, lufs_s = self._last
            return lufs_m, lufs_s, peak, rms

    def stop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        with contextlib.suppress(OSError):
            proc.kill()  # parec on a wedged server ignores polite signals, so no SIGINT/SIGTERM round
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            log.warning("parec for %s did not exit after SIGKILL; abandoning it", self.source)
        except OSError:
            pass
        if proc.stdout is not None:
            with contextlib.suppress(OSError, ValueError):
                proc.stdout.close()
        self.dead = True


class ClipStore:
    """Mic-check recordings in a private temp dir: at most `limit`, each kept for `ttl` seconds."""

    def __init__(
        self, clock: Callable[[], float] = time.monotonic, ttl: float = CLIP_TTL, limit: int = MAX_CLIPS
    ) -> None:
        self._clock = clock
        self._ttl = ttl
        self._limit = limit
        self._dir: Path | None = None
        self._lock = threading.Lock()
        self._clips: list[dict[str, Any]] = []

    def reserve(self) -> tuple[str, Path]:
        """A fresh id and the path to record into."""
        with self._lock:
            if self._dir is None:
                self._dir = Path(tempfile.mkdtemp(prefix="streamstack-clips-"))  # mode 700
            clip_id = secrets.token_hex(8)
            return clip_id, self._dir / f"{clip_id}.wav"

    def add(self, clip_id: str, label: str) -> None:
        with self._lock:
            self._clips.append({"id": clip_id, "label": label, "added": self._clock()})
            while len(self._clips) > self._limit:
                self._drop(self._clips.pop(0))

    def _drop(self, clip: dict[str, Any]) -> None:
        if self._dir is not None:
            with contextlib.suppress(OSError):
                (self._dir / f"{clip['id']}.wav").unlink()

    def expire(self) -> None:
        with self._lock:
            now = self._clock()
            for clip in [c for c in self._clips if now - c["added"] > self._ttl]:
                self._clips.remove(clip)
                self._drop(clip)

    def listing(self) -> list[dict[str, str]]:
        self.expire()
        with self._lock:
            return [{"id": c["id"], "label": c["label"], "url": f"api/clips/{c['id']}.wav"} for c in self._clips]

    def path(self, clip_id: str) -> Path | None:
        """The file of a clip, or None; the id must be exactly 16 hex digits so nothing can traverse."""
        if not isinstance(clip_id, str) or not CLIP_ID.fullmatch(clip_id):
            return None
        self.expire()
        with self._lock:
            if self._dir is None or not any(c["id"] == clip_id for c in self._clips):
                return None
            path = self._dir / f"{clip_id}.wav"
        return path if path.is_file() else None

    def clear(self) -> None:
        with self._lock:
            for clip in self._clips:
                self._drop(clip)
            self._clips.clear()

    def close(self) -> None:
        with self._lock:
            self._clips.clear()
            if self._dir is not None:
                shutil.rmtree(self._dir, ignore_errors=True)
                self._dir = None


def _emit(notify: Notify | None, level: str, text: str) -> None:
    if notify is None:
        return
    try:
        notify(level, text)
    except Exception:
        log.exception("toast callback failed")


def _tone_state(params: dict[str, Any], supported: bool, custom: bool) -> dict[str, Any]:
    return {**{k: round(float(params[k]), 2) for k in BAND_KEYS}, "supported": supported, "custom": custom}


def _ranges() -> dict[str, list[float]]:
    return {key: list(rng) for key, rng in TONE_RANGES.items()}


def _empty_check(seconds: float = CHECK_DEFAULT) -> dict[str, Any]:
    return {"state": "idle", "seconds": seconds, "error": None}


def _check_seconds(params: dict[str, Any]) -> float:
    if "seconds" not in params or params["seconds"] is None:
        return CHECK_DEFAULT
    return number_param(params, "seconds", low=CHECK_SECONDS[0], high=CHECK_SECONDS[1])


def _validate_denoise(params: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(params) - {"engine", "atten_limit_db"})
    if unknown:
        raise ActionError(f"unknown noise removal setting: {', '.join(unknown)}", 400)
    if not params:
        raise ActionError("give at least one of engine, atten_limit_db", 400)
    out: dict[str, Any] = {}
    if "engine" in params:
        engine = params["engine"]
        if not isinstance(engine, str) or engine not in stages.DENOISE_ENGINES:
            raise ActionError("engine must be one of " + ", ".join(stages.DENOISE_ENGINES), 400)
        out["engine"] = engine
    if "atten_limit_db" in params:
        out["atten_limit_db"] = round(
            number_param(params, "atten_limit_db", low=ATTEN_RANGE[0], high=ATTEN_RANGE[1]), 1
        )
    return out


def _denoise_state(view: dict[str, Any], params: dict[str, Any], supported: bool) -> dict[str, Any]:
    """The Console's noise removal object, from a chain view (engines) and the parameters (strength)."""
    return {
        "engine": view["engine_active"] if view["engine_active"] != "off" else view["engine"],
        "atten_limit_db": params["denoise_atten_db"],
        "atten_range": list(ATTEN_RANGE),
        "supported": supported,
        "custom": view["custom"],
        "engines": [
            {
                "id": e["id"],
                "label": e["label"],
                "effect": e["label"],
                "delay_ms": e["delay_ms"],
                "available": e["available"],
                "reason": e["reason"],
            }
            for e in view["engines"]
        ],
    }


def _validate_tone(params: dict[str, Any]) -> dict[str, float]:
    unknown = sorted(set(params) - set(TONE_RANGES))
    if unknown:
        raise ActionError(f"unknown tone setting: {', '.join(unknown)}", 400)
    if not params:
        raise ActionError("give at least one of " + ", ".join(TONE_RANGES), 400)
    return {
        key: round(number_param(params, key, low=lo, high=hi), 2)
        for key, (lo, hi) in TONE_RANGES.items()
        if key in params
    }


def _chain_call(fn: Callable[..., None], *args: Any, **kwargs: Any) -> None:
    """Run a ChainModel change, turning its ChainError into the Console's ActionError."""
    try:
        fn(*args, **kwargs)
    except chainlib.ChainError as exc:
        raise ActionError(str(exc), exc.status) from exc


def _chain_changes(params: dict[str, Any]) -> dict[str, Any]:
    """The `audio.chain` payload: {"params": {key: value}}; ChainModel validates the keys and ranges."""
    values = params.get("params")
    if not isinstance(values, dict) or not values:
        raise ActionError("params must be an object of chain settings", 400)
    return values


def _loudness_args(params: dict[str, Any]) -> tuple[bool | None, float | None]:
    on = params.get("on")
    target = params.get("target_lufs")
    if on is not None and not isinstance(on, bool):
        raise ActionError("on must be true or false", 400)
    if target is not None and (isinstance(target, bool) or not isinstance(target, (int, float))):
        raise ActionError("target_lufs must be a number", 400)
    if on is None and target is None:
        raise ActionError("give on and/or target_lufs", 400)
    return on, None if target is None else float(target)


# ---------------------------------------------------------------------------
# The real controller
# ---------------------------------------------------------------------------


class AudioController:
    """Meters, gain/mute, the voice chain, loudness and mic check for the real rig."""

    def __init__(
        self,
        cfg: dict[str, str],
        notify: Notify | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        spawn: Callable[..., subprocess.Popen[bytes]] | None = None,
        stall_probe: Callable[[str, str, bool], micwatch.StallProbe] | None = None,
        verify: Callable[[str], bool | None] | None = None,
        sleep: Callable[[float], object] | None = None,
        signal: Callable[[str, float], micwatch.SignalSample | None] | None = None,
        resync: Callable[[int], Callable[[], str]] | None = None,
    ) -> None:
        self._cfg = cfg
        self.notify = notify
        self._clock = clock
        self._spawn = spawn
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait
        self._monitor = micwatch.StallMonitor(clock)
        self._stall_probe = stall_probe or self._monitor.probe
        self._verify = verify or self._monitor.verify
        self._signal = signal or (lambda mic, seconds: micwatch.capture_signal(mic, seconds, self._sleep))
        self._resync = resync or (lambda card: micwatch.feature_unit_resync(card, sleep=self._sleep))
        self._threads: list[threading.Thread] = []
        self._readers: dict[str, LevelReader | None] = {"mic": None, "voice": None}
        # the clip latches outlive the readers: a restarted parec must not forget that the mic clipped
        self._latches = {"mic": loudness.ClipLatch(), "voice": loudness.ClipLatch()}
        self._parec = True

        self._sources: dict[str, str] = {}
        self._mic = ""
        self._mic_desc = ""
        self._gain: float | None = None
        self._muted: bool | None = None
        self._gain_hold = -math.inf
        self._chain = chainlib.ChainModel(store=config.state_dir() / "audio-chain" / "state.json")
        self._host = chainlib.ChainRuntime(self._chain, config.state_dir())
        self._voice_present = False
        # The hardware Plan (set by the rig): when present it, not a name rule, picks the call microphone.
        self.plan_source: Callable[[], Plan | None] | None = None
        self.plan_refresh: Callable[[], object] | None = None
        self._card_tried: dict[str, float] = {}
        self._refresh_at = -math.inf
        self._level_t = clock()
        self._mic_rms = SILENCE_DB
        self._mic_peak = SILENCE_DB

        self._clips = ClipStore(clock)
        self._check = _empty_check()
        self._check_thread: threading.Thread | None = None

        self._detector = micwatch.StallDetector(clock)
        self._graph: micwatch.StallProbe | None = None
        self._kind = micwatch.KIND_STALL  # what the current fault is: a stall, or digital silence
        self._revive = {"state": "idle", "message": None, "at": -math.inf}
        self._revive_thread: threading.Thread | None = None

        self._outputs = Outputs(
            lambda level, text: _emit(self.notify, level, text),
            store=config.state_dir() / "console-outputs.json",
            clock=clock,
        )

        self._adopt_start_preset(cfg.get("STREAMSTACK_VOICE_PRESET"))

    def _adopt_start_preset(self, configured: str | None) -> None:
        """First run on this rig: import the Easy Effects presets once and start from what was chosen there."""
        imported = migrate.migrate()
        first = not (config.state_dir() / "audio-chain" / "state.json").exists()
        wanted = configured or (migrate.previous_choice() if (imported or first) else None)
        if wanted and first:
            with contextlib.suppress(chainlib.ChainError):
                self._chain.load_preset(wanted)
        elif imported:
            self._chain.revision += 1

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._threads:
            return
        self._parec = system.have("parec")
        self._stop.clear()
        self._probe()
        self._outputs.start()
        self._host.start()
        for target, name in ((self._supervise, "audio-supervisor"), (self._level_loop, "audio-level")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        for thread in self._threads:
            thread.join(timeout=3)
        self._threads.clear()
        if self._revive_thread is not None:
            self._revive_thread.join(timeout=3)
            self._revive_thread = None
        with self._lock:
            readers = [r for r in self._readers.values() if r]
            self._readers = {"mic": None, "voice": None}
        for reader in readers:
            reader.stop()
        self._host.stop()  # the host process keeps running: a call survives a Console restart
        self._clips.close()
        self._outputs.stop()

    # -- background ---------------------------------------------------------

    def _supervise(self) -> None:
        while not self._stop.wait(PROBE_INTERVAL):
            try:
                self._probe()
            except Exception:  # keep the supervisor alive whatever a helper does
                log.exception("audio probe failed")

    def _probe(self) -> None:
        """Refresh everything slow (pactl, the chain's nodes) and (re)start meter readers."""
        listing = system.run(["pactl", "-f", "json", "list", "sources"])
        infos = parse_sources(listing.stdout) if listing and listing.returncode == 0 else []
        sources = {i["name"]: i["description"] for i in infos}
        wanted = self._cfg.get("STREAMSTACK_MICROPHONE", "")
        pickable = {n: d for n, d in sources.items() if n != VOICE_SOURCE_NAME}
        plan = self.plan_source() if self.plan_source is not None else None
        if plan is not None:
            mic = self._follow_plan(plan, pickable)
        else:
            mic = system.pick_microphone(pickable, wanted, system.default_device("source")) if pickable else ""
        info = next((i for i in infos if i["name"] == mic), None)
        with self._lock:
            self._sources = sources
            self._mic, self._mic_desc = mic, sources.get(mic, "")
            if info is not None and self._gain_settled():
                self._gain, self._muted = info["gain_db"], info["muted"]
            elif info is None:
                self._gain = self._muted = None
            self._voice_present = VOICE_SOURCE_NAME in sources
            self._sync_readers(mic, self._voice_present)
            muted = self._muted
        if mic:
            self._chain.set_rig(mic)
        if self._chain.sync_external():
            self._host.poke()
        if plan is None:
            self._host.set_microphone(mic or None)
        self._watch(mic, muted, listing.stdout if listing and listing.returncode == 0 else "")
        self._clips.expire()
        self._outputs.poll()

    def _follow_plan(self, plan: Plan, pickable: dict[str, str]) -> str:
        """The call microphone the Plan names, handed to the chain with the Plan's rate and quantum. A Plan that is
        out of date (its node is gone, or its interface's card was just switched on) asks the hardware controller
        for a fresh probe, at most every few seconds."""
        self._host.set_plan(plan)
        target = self._host.target()
        if target is None:
            return ""
        now = self._clock()
        if target.activate is not None and now - self._card_tried.get(target.activate[0], -math.inf) >= CARD_RETRY_S:
            self._card_tried[target.activate[0]] = now
            if voice.activate_card(*target.activate):
                log.info("switched %s to %s", redact(target.activate[0]), target.activate[1])
                self._refresh_at = now + 1.0
        stale = target.microphone is not None and target.microphone not in pickable
        if (stale or (target.microphone is None and target.interface)) and now >= self._refresh_at:
            self._refresh_at = now + PLAN_REFRESH_S
            if self.plan_refresh is not None:
                try:
                    self.plan_refresh()
                except Exception:  # a failed probe leaves the old Plan; the next round tries again
                    log.exception("hardware re-probe failed")
        return target.microphone if target.microphone in pickable else ""

    def _gain_settled(self) -> bool:
        return self._clock() >= self._gain_hold

    def _sync_readers(self, mic: str, voice_present: bool) -> None:
        wants = {"mic": mic or None, "voice": VOICE_SOURCE_NAME if voice_present else None}
        for slot, source in wants.items():
            reader = self._readers[slot]
            idle_since: float | None = None
            if reader is not None:
                stale = not reader.dead and reader.idle_for() >= READER_IDLE_RESTART
                if reader.source != source or ((reader.dead or stale) and self._due_restart(reader)):
                    if reader.source == source:
                        idle_since = reader.idle_since()  # the silence goes on across the restart
                    reader.stop()
                    self._readers[slot] = reader = None
            if reader is None and source and self._parec:
                fresh = LevelReader(source, self._clock, self._spawn, idle_since, self._latches[slot])
                if fresh.start():
                    self._readers[slot] = fresh

    def _restart_readers(self) -> None:
        """Replace both meter readers with fresh parec children (after a revive their streams are stale)."""
        with self._lock:
            old = [r for r in self._readers.values() if r]
            self._readers = {"mic": None, "voice": None}
        for reader in old:
            reader.stop()
        with self._lock:
            self._sync_readers(self._mic, VOICE_SOURCE_NAME in self._sources)

    # -- the watchdog ---------------------------------------------------------

    def _watch(self, mic: str, muted: bool | None, listing: str) -> None:
        """Judge the microphone's ALSA PCM (a procfs read, once per probe). Never acts on its own."""
        with self._lock:
            if self._revive["state"] == "reviving":
                return  # the revive decides, and a half-resumed node must not count
            reader = self._readers["mic"]
            silent = reader is not None and reader.idle_for() >= READER_SILENT_S
            zero_for = reader.zero_for() if reader is not None else None
        probe: micwatch.StallProbe | None = None
        if mic and listing:
            try:
                probe = self._stall_probe(mic, listing, silent)
                # pointers that move are not proof of audio: a whole window of exact zeros is digital silence
                probe = micwatch.silence_verdict(probe, zero_for)
            except Exception:  # a probe bug must not take the supervisor down
                log.exception("stall probe failed")
        with self._lock:
            self._graph = probe
            if probe is not None and probe.stalled is True:
                self._kind = probe.kind or micwatch.KIND_STALL
            condition = bool(mic) and muted is not True and probe is not None and probe.stalled is True
            event = self._detector.update(condition)
            if event == "started" or (event == "cleared" and self._revive["state"] == "failed"):
                self._revive.update(state="idle", message=None)  # a new stall, or the old failure resolved itself
            cleared_quietly = event == "cleared" and self._revive["state"] != "ok"
        if event == "started":
            title = micwatch.SILENCE_TITLE if self._kind == micwatch.KIND_SILENCE else micwatch.STALL_TITLE
            _emit(self.notify, "warn", f"{title}. Press Revive mic.")
        elif cleared_quietly:
            _emit(self.notify, "ok", "Microphone is delivering audio again")

    def _recheck(self, mic: str) -> bool | None:
        """After a revive step: fresh readers, then the same ALSA signal. True stalled, False healthy, None unknown."""
        self._restart_readers()
        try:
            verdict = self._verify(mic)
        except Exception:
            log.exception("stall verification failed")
            verdict = None
        with self._lock:
            self._graph = None
        return verdict

    def _set_revive(self, state: str, message: str | None) -> None:
        with self._lock:
            self._revive.update(state=state, message=message, at=self._clock())

    def _revive_view(self) -> dict[str, Any]:
        with self._lock:
            if self._revive["state"] == "ok" and self._clock() - self._revive["at"] > REVIVE_SHOW_S:
                self._revive.update(state="idle", message=None)
            return {"state": self._revive["state"], "message": self._revive["message"]}

    def health_checks(self) -> list[dict[str, str]]:
        """The stall check for the Console's health list (live, unlike the 30 s doctor refresh)."""
        with self._lock:
            if not self._mic:
                return []
            stalled = self._detector.stalled
            graph = self._graph
            known = graph is not None and graph.stalled is not None
            kind = self._kind
        item = micwatch.check_item(stalled if (stalled or known) else None, graph.detail if graph else "", kind)
        return [item] if item else []

    def _due_restart(self, reader: LevelReader) -> bool:
        return self._clock() - reader.started >= RESTART_INTERVAL

    def _level_loop(self) -> None:
        """The loudness leveller: look at the chain output ten times a second and ride the gain."""
        while not self._stop.wait(LEVEL_INTERVAL):
            try:
                self._level_tick()
            except Exception:  # the leveller must outlive any one bad round
                log.exception("loudness leveller failed")

    def _level_tick(self) -> None:
        now = self._clock()
        dt, self._level_t = now - self._level_t, now
        with self._lock:
            voice, mic = self._readers["voice"], self._readers["mic"]
            muted = self._muted is True
        latest = voice.latest() if voice else None
        heard = mic.latest() if mic else None
        if heard is not None:
            self._mic_rms, self._mic_peak = heard[3], heard[2]
        else:
            self._mic_rms = self._mic_peak = SILENCE_DB
        self._chain.level_step(min(dt, 1.0), latest[0] if latest else None, muted, heard[0] if heard else None)
        self._host.poke()

    # -- state --------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        view = self._chain.view()
        status = self._host.status()
        params = self._chain.params
        ready = bool(status["running"])
        with self._lock:
            return {
                "mic": {
                    "name": self._mic or None,
                    "description": self._mic_desc or None,
                    "present": bool(self._mic),
                    "gain_db": self._gain,
                    "muted": self._muted,
                    "stalled": self._detector.stalled,
                    "stalled_since": self._detector.since,
                    "stall_kind": self._kind if self._detector.stalled else "",
                    "revive": self._revive_view(),
                },
                "gain_range": list(GAIN_RANGE),
                "chain": {**view, **status},
                "tone": _tone_state(params, True, view["custom"]),
                "tone_ranges": _ranges(),
                "denoise": _denoise_state(view, params, ready or status["available"]),
                "mic_check": {**self._check, "clips": self._clips.listing()},
                "outputs": self._outputs.state(),
            }

    def mic_muted(self) -> bool | None:
        """The Console's own mute of the microphone (None while no microphone is known)."""
        with self._lock:
            return self._muted

    def meters(self) -> dict[str, Any]:
        with self._lock:
            mic, voice = self._readers["mic"], self._readers["voice"]
        silent = meter_frame(latch=self._latches["mic"])
        mic_frame = mic.take() if mic else silent
        voice_frame = voice.take() if voice else None
        if voice_frame is not None:
            voice_frame["gr_db"] = chainlib.estimate_reduction(
                self._chain.params, mic_frame["rms_db"], mic_frame["peak_db"], self._chain.auto_db
            )
        return {"t": time.time(), "mic": mic_frame, "voice": voice_frame}

    def clip_path(self, clip_id: str) -> Path | None:
        return self._clips.path(clip_id)

    # -- actions ------------------------------------------------------------

    def act(self, action: str, params: dict[str, Any]) -> str | None:
        handlers: dict[str, Callable[[dict[str, Any]], str | None]] = {
            "audio.gain": self._act_gain,
            "audio.mute": self._act_mute,
            "audio.preset": self._act_preset,
            "audio.bypass": self._act_bypass,
            "audio.tone": self._act_tone,
            "audio.denoise": self._act_denoise,
            "audio.chain": self._act_chain,
            "audio.chain_reset": self._act_chain_reset,
            "audio.preset_save": self._act_preset_save,
            "audio.loudness": self._act_loudness,
            "audio.mic_check": self._act_mic_check,
            "audio.revive": self._act_revive,
            "audio.clip_clear": self._act_clip_clear,
        }
        handler = handlers.get(action)
        if handler is None and action.startswith("audio.output_"):
            return self._outputs.act(action, params)
        if handler is None:
            raise ActionError(f"unknown audio action {action!r}", 400)
        return handler(params)

    def _act_clip_clear(self, params: dict[str, Any]) -> str | None:
        for latch in self._latches.values():
            latch.clear()
        return None

    def _need_mic(self) -> str:
        with self._lock:
            mic = self._mic
        if not mic:
            raise ActionError("No microphone found - plug it in or set STREAMSTACK_MICROPHONE.", 409)
        return mic

    def _pactl(self, args: list[str]) -> None:
        if not system.have("pactl"):
            raise ActionError("pactl is missing (install pulseaudio-utils).", 409)
        result = system.run(["pactl", *args])
        if result is None or result.returncode != 0:
            raise ActionError("The audio server did not accept the change.", 409)

    def _act_gain(self, params: dict[str, Any]) -> str | None:
        db = round(number_param(params, "db", low=GAIN_RANGE[0], high=GAIN_RANGE[1], clamped=True), 2)
        mic = self._need_mic()
        self._pactl(["set-source-volume", mic, f"{db}dB"])
        with self._lock:
            self._gain = db
            self._gain_hold = self._clock() + 1.5  # the next probe may still report the old value
        return None

    def _act_mute(self, params: dict[str, Any]) -> str | None:
        on = bool_param(params)
        mic = self._need_mic()
        self._pactl(["set-source-mute", mic, "1" if on else "0"])
        with self._lock:
            self._muted = on
            self._gain_hold = self._clock() + 1.5
        return None

    def _act_preset(self, params: dict[str, Any]) -> str | None:
        name = params.get("name")
        if not isinstance(name, str):
            raise ActionError("name must be a preset name", 400)
        _chain_call(self._chain.load_preset, name)
        self._host.poke()
        return f"Voice preset: {self._chain.preset.removeprefix('StreamStack ') or self._chain.preset}"

    def _act_preset_save(self, params: dict[str, Any]) -> str | None:
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ActionError("name must be a preset name", 400)
        blurb = params.get("blurb", "")
        if not isinstance(blurb, str):
            raise ActionError("blurb must be text", 400)
        _chain_call(self._chain.save_as_preset, name.strip(), blurb)
        return f"Saved voice preset: {name.strip()}"

    def _act_bypass(self, params: dict[str, Any]) -> str | None:
        on = bool_param(params)
        self._chain.set_bypass(on)
        self._host.poke()
        return "Voice effects bypassed" if on else "Voice effects on"

    def _act_tone(self, params: dict[str, Any]) -> str | None:
        _chain_call(self._chain.set_params, _validate_tone(params))
        self._host.poke()
        return None

    def _act_denoise(self, params: dict[str, Any]) -> str | None:
        """Switch the noise removal engine (RNNoise or DeepFilterNet) or set its strength (the attenuation limit)."""
        changes = _validate_denoise(params)
        if "engine" in changes:
            _chain_call(self._chain.set_engine, changes["engine"])
        if "atten_limit_db" in changes:
            _chain_call(self._chain.set_params, {"denoise_atten_db": changes["atten_limit_db"]})
        self._host.poke()
        return f"Noise removal: {chaingraph.PLUGINS[changes['engine']]['name']}" if "engine" in changes else None

    def _act_chain(self, params: dict[str, Any]) -> str | None:
        """Set any chain stage parameters or switches live: ``{"params": {"comp_ratio": 4, "gate_on": false}}``."""
        _chain_call(self._chain.set_params, _chain_changes(params))
        self._host.poke()
        return None

    def _act_chain_reset(self, params: dict[str, Any]) -> str | None:
        self._chain.reset()
        self._host.poke()
        return "Voice chain reset to the preset"

    def _act_loudness(self, params: dict[str, Any]) -> str | None:
        on, target = _loudness_args(params)
        _chain_call(self._chain.set_loudness, on, target)
        self._host.poke()
        if on is None:
            return f"Loudness target: {self._chain.target:g} LUFS"
        return "Loudness leveller on" if on else "Loudness leveller off"

    # -- revive ---------------------------------------------------------------

    def _act_revive(self, params: dict[str, Any]) -> str | None:
        mic = self._need_mic()
        if not system.have("pactl"):
            raise ActionError("pactl is missing (install pulseaudio-utils).", 409)
        with self._lock:
            if self._revive["state"] == "reviving":
                raise ActionError("The microphone is already being revived.", 409)
            if not (self._detector.stalled or self._revive["state"] == "failed" or params.get("force") is True):
                raise ActionError("The microphone is delivering audio; there is nothing to revive.", 409)
            kind = self._kind if self._detector.stalled else micwatch.KIND_STALL
            first = (
                "Re-writing the device's capture controls..."
                if kind == micwatch.KIND_SILENCE
                else "Suspending and resuming the microphone..."
            )
            self._set_revive("reviving", first)
        thread = threading.Thread(target=self._run_revive, args=(mic, kind), name="mic-revive", daemon=True)
        self._revive_thread = thread
        thread.start()
        return "Reviving the microphone..."

    def _resync_for(self, mic: str) -> Callable[[], str] | None:
        """The capture-control resync of the mic's ALSA card: the hardware Plan's interface, else the card the
        source itself reports. None when no card can be named."""
        plan = self.plan_source() if self.plan_source is not None else None
        interface = plan.audio_interface() if plan is not None else None
        card = interface.alsa_card if interface is not None else None
        if card is None:
            listing = system.run(["pactl", "-f", "json", "list", "sources"], timeout=micwatch.PACTL_TIMEOUT)
            info = micwatch.parse_source(listing.stdout, mic) if listing and listing.returncode == 0 else None
            card = info.pcm.card if info is not None and info.pcm is not None else None
        return self._resync(card) if card is not None else None

    def _still_silent(self, mic: str) -> bool | None:
        """After a revive step: True when a short capture is still all zeros, False when audio arrives."""
        sample = self._signal(mic, micwatch.VERIFY_SIGNAL_S)
        return None if sample is None else sample.digital_silence(micwatch.VERIFY_MIN_S)

    def _run_revive(self, mic: str, kind: str = micwatch.KIND_STALL) -> None:
        """The worker: escalate until the node is scheduled again, report through state and a toast."""
        try:
            result = micwatch.revive_mic(
                mic,
                run=system.run,
                sleep=self._sleep,
                stalled=lambda: self._recheck(mic),
                say=lambda text: self._set_revive("reviving", text),
                kind=kind,
                resync=self._resync_for(mic) if kind == micwatch.KIND_SILENCE else None,
                silent=lambda: self._still_silent(mic),
            )
        except Exception as exc:  # the state must leave "reviving" whatever went wrong
            log.exception("reviving the microphone failed")
            result = micwatch.ReviveResult(False, f"Reviving failed: {type(exc).__name__}: {exc}")
        for step in result.steps:
            log.info("mic revive: %s", step)
        try:
            self._restart_readers()
        except Exception:
            log.exception("restarting the meter readers failed")
        with self._lock:
            if result.ok:
                self._detector.reset()
                self._graph = None
            self._set_revive("ok" if result.ok else "failed", result.message)
        _emit(self.notify, "ok" if result.ok else "error", result.message)

    # -- mic check ----------------------------------------------------------

    def _act_mic_check(self, params: dict[str, Any]) -> str | None:
        seconds = _check_seconds(params)
        if not system.have("parecord"):
            raise ActionError("parecord is missing (install pulseaudio-utils).", 409)
        with self._lock:
            if self._check["state"] in ("recording-raw", "recording-processed"):
                raise ActionError("A mic check is already running.", 409)
            takes = []
            if self._mic:
                takes.append(("recording-raw", "raw microphone", self._mic))
            if VOICE_SOURCE_NAME in self._sources:
                takes.append(("recording-processed", "processed voice", VOICE_SOURCE_NAME))
            if not takes:
                raise ActionError("No microphone found to record from.", 409)
            self._check = {"state": takes[0][0], "seconds": seconds, "error": None}
        self._clips.clear()
        thread = threading.Thread(target=self._run_check, args=(takes, seconds), name="mic-check", daemon=True)
        self._check_thread = thread
        thread.start()
        return f"Recording {seconds:g} s - talk now"

    def _run_check(self, takes: list[tuple[str, str, str]], seconds: float) -> None:
        for phase, label, source in takes:
            with self._lock:
                self._check["state"] = phase
            clip_id, path = self._clips.reserve()
            try:
                ok = system.record(source, seconds, str(path))
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("mic check recording from %s failed: %s", source, exc)
                ok = False
            if ok:
                self._clips.add(clip_id, label)
            else:
                with contextlib.suppress(OSError):
                    path.unlink()
        if self._clips.listing():
            with self._lock:
                self._check["state"] = "done"
            _emit(self.notify, "ok", "Mic check ready - play it back")
        else:
            with self._lock:
                self._check.update(state="error", error="Nothing was captured from the microphone.")
            _emit(self.notify, "error", "Mic check captured nothing")


# ---------------------------------------------------------------------------
# The simulated twin
# ---------------------------------------------------------------------------

DEMO_REVIVE_S = 1.5  # how long a simulated revive takes
SYLLABLE_S = 0.18
PHRASE_S = 4.5


def speech_peak_db(t: float) -> float | None:
    """A deterministic speech-like peak level at time t: syllables, pauses between phrases, rare loud peaks."""
    phrase = int(t // PHRASE_S)
    phrase_rng = random.Random(phrase * 104729 + 7)  # noqa: S311 - a deterministic simulation, not security
    if t - phrase * PHRASE_S > 1.8 + phrase_rng.random() * 1.8:
        return None  # a pause
    slot = int(t / SYLLABLE_S)
    rng = random.Random(slot * 7919 + 13)  # noqa: S311 - a deterministic simulation, not security
    if rng.random() < 0.12:
        return None  # a gap between words
    envelope = math.sin(math.pi * (t / SYLLABLE_S - slot)) ** 0.7
    level = -26 + rng.random() * 10 + 20 * math.log10(max(envelope, 1e-3))
    if rng.random() < 0.07:
        level += 10  # an emphasised syllable
    return level


def demo_levels(
    t: float, gain_db: float, muted: bool, bypass: bool, trim_db: float = 0.0
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(mic, voice) meter dicts: the voice is a few dB hotter than the mic and more compressed.

    Loudness is a pure function of time too: momentary averages the last 400 ms, short-term the last 3 s, of the
    same RMS the meter shows (less 3 dB: a K-weighted mono signal reads about that far under its RMS).
    `trim_db` is the loudness leveller's gain, added to the voice only.
    """
    if muted:
        return meter_frame(), meter_frame()

    def levels(at: float) -> tuple[tuple[float, float], tuple[float, float]]:
        speech = speech_peak_db(at)
        noise = -66 + 3 * math.sin(at * 7.3)
        mic_peak = max(noise, speech if speech is not None else noise) + gain_db
        if bypass:
            return (mic_peak, 10), (mic_peak, 10)
        if speech is None:
            return (mic_peak, 10), (noise - 12 + trim_db, 4)
        return (mic_peak, 10), (
            -13 + 0.45 * (speech + gain_db + 20) + 3 + trim_db,
            6,
        )  # compressed towards a steady level, then lifted

    def pack(index: int) -> dict[str, Any]:
        peak, crest = levels(t)[index]
        peak = round(max(SILENCE_DB, min(0.0, peak)), 1)
        rms = round(max(SILENCE_DB, peak - crest), 1)

        def lufs(span: float, count: int) -> float:
            powers = []
            for k in range(count):
                p, c = levels(max(0.0, t - span * k / count))[index]
                powers.append(10 ** (max(SILENCE_DB, min(0.0, p) - c) / 10))
            return round(max(SILENCE_DB, 10 * math.log10(sum(powers) / count) - 3.0), 1)

        # the sample peak sits a hair under the true peak, which can top it by up to ~3 dB
        sample = round(max(SILENCE_DB, peak - 0.3), 1)
        return meter_frame(peak, sample, rms, peak >= loudness.CLIP_TP_DB, lufs(0.4, 4), lufs(3.0, 6))

    return pack(0), pack(1)


def synth_wav(path: Path, seconds: float, processed: bool) -> None:
    """A small mono WAV of synthesised speech-like tone bursts (the processed take is cleaner and steadier)."""
    rate = 8000
    rng = random.Random(5 if processed else 3)  # noqa: S311 - a deterministic simulation, not security
    samples = array.array("h")
    for i in range(int(seconds * rate)):
        t = i / rate
        speech = speech_peak_db(t)
        amp = 0.0 if speech is None else 10 ** ((-16 if processed else speech + 6) / 20) * 0.5
        f0 = 120 + 6 * math.sin(t * 5)
        voice = sum(math.sin(2 * math.pi * f0 * k * t) / k for k in (1, 2, 3))
        hiss = 0.0 if processed else (rng.random() - 0.5) * 0.02
        samples.append(int(clamp((amp * voice / 1.8 + hiss) * 32767, -32768, 32767)))
    if sys.byteorder == "big":
        samples.byteswap()
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(samples.tobytes())


class DemoAudio:
    """The simulated audio rig: the same methods as AudioController, nothing touches the machine."""

    def __init__(self, notify: Notify | None = None, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.notify = notify
        self._clock = clock
        self._lock = threading.RLock()
        self._t0 = clock()
        self._gain = 0.0
        self._muted = False
        self._bypass = False
        self._df_installed = True  # the demo rig has the library; audio.demo_deepfilternet takes it away
        self._chain = chainlib.ChainModel(store=None, engines=self._engines)
        self._level_t = clock()
        self._last_lufs_m: float | None = None
        self._last_mic_lufs: float | None = None
        self._clips = ClipStore(clock)
        self._check = _empty_check()
        self._check_start = 0.0
        self._stalled = False
        self._stalled_since: str | None = None
        self._revive = {"state": "idle", "message": None, "at": 0.0}
        self._latches = {"mic": loudness.ClipLatch(), "voice": loudness.ClipLatch()}
        self._outputs = DemoOutputs(lambda level, text: _emit(self.notify, level, text), clock=clock)

    def _engines(self) -> list[str]:
        return list(stages.DENOISE_ENGINES) if self._df_installed else ["rnnoise"]

    def start(self) -> None:
        return None

    def stop(self) -> None:
        self._outputs.stop()
        self._clips.close()

    # The mic check and the revive are timelines from their start, advanced whenever they are looked at
    # (no timer thread needed).
    def _advance_revive(self) -> None:
        with self._lock:
            state, elapsed = self._revive["state"], self._clock() - self._revive["at"]
            if state == "reviving":
                if elapsed >= DEMO_REVIVE_S:
                    self._stalled, self._stalled_since = False, None
                    self._revive.update(
                        state="ok", message="Microphone is back (suspend and resume).", at=self._clock()
                    )
                    _emit(self.notify, "ok", self._revive["message"])
                elif elapsed >= 0.4:
                    self._revive["message"] = "Waiting for the microphone to come back..."
            elif state == "ok" and elapsed > REVIVE_SHOW_S:
                self._revive.update(state="idle", message=None)

    def _advance(self) -> None:
        self._advance_revive()
        with self._lock:
            state = self._check["state"]
            if state not in ("recording-raw", "recording-processed"):
                return
            seconds = self._check["seconds"]
            elapsed = self._clock() - self._check_start
            if state == "recording-raw" and elapsed >= seconds:
                clip_id, path = self._clips.reserve()
                synth_wav(path, min(seconds, 15.0), processed=False)
                self._clips.add(clip_id, "raw microphone")
                self._check["state"] = state = "recording-processed"
            if state == "recording-processed" and elapsed >= 2 * seconds:
                clip_id, path = self._clips.reserve()
                synth_wav(path, min(seconds, 15.0), processed=True)
                self._clips.add(clip_id, "processed voice")
                self._check["state"] = "done"
                _emit(self.notify, "ok", "Mic check ready - play it back")

    def state(self) -> dict[str, Any]:
        self._advance()
        with self._lock:
            return {
                "mic": {
                    "name": "alsa_input.demo-wave3",
                    "description": "Wave:3 (demo)",
                    "present": True,
                    "gain_db": self._gain,
                    "muted": self._muted,
                    "stalled": self._stalled,
                    "stalled_since": self._stalled_since,
                    "stall_kind": micwatch.KIND_STALL if self._stalled else "",
                    "revive": {"state": self._revive["state"], "message": self._revive["message"]},
                },
                "gain_range": list(GAIN_RANGE),
                "chain": {**self._chain.view(), **self._demo_status()},
                "tone": _tone_state(self._chain.params, True, self._chain.custom()),
                "tone_ranges": _ranges(),
                "denoise": _denoise_state(self._chain.view(), self._chain.params, True),
                "mic_check": {**self._check, "clips": self._clips.listing()},
                "outputs": self._outputs.state(),
            }

    @staticmethod
    def _demo_status() -> dict[str, Any]:
        return {
            "running": True,
            "available": True,
            "missing": [],
            "error": None,
            "source": chaingraph.NODE_NAME,
            "description": chaingraph.DESCRIPTION,
            "rate": chaingraph.RATE,
            "graph_rate": chaingraph.RATE,
            "graph_rate_reason": "the conferencing rate, which the interface offers",
            "quantum": 256,
            "target": {
                "state": "interface",
                "microphone": "alsa_input.usb-Elgato_Systems_Elgato_Wave_XLR_<serial>-00.mono-fallback",
                "monitor": "alsa_output.usb-Elgato_Systems_Elgato_Wave_XLR_<serial>-00.analog-stereo",
                "interface": "wave-xlr",
                "rate": chaingraph.RATE,
                "rate_reason": "the conferencing rate, which the interface offers",
                "native_rate": chaingraph.RATE,
                "format": "s24le",
                "quantum": 256,
                "activate": None,
                "reason": "Elgato Wave XLR (USB 0fd9:007d)",
            },
            "contract": {
                "name": chaingraph.NODE_NAME,
                "description": chaingraph.DESCRIPTION,
                "media_class": "Audio/Source",
                "rate": chaingraph.RATE,
                "channels": 1,
                "state": "running",
            },
        }

    def mic_muted(self) -> bool | None:
        with self._lock:
            return self._muted

    def meters(self) -> dict[str, Any]:
        self._advance_revive()
        with self._lock:
            now = self._clock()
            dt, self._level_t = min(1.0, now - self._level_t), now
            stalled = self._muted or self._stalled
            self._chain.level_step(dt, self._last_lufs_m, stalled, self._last_mic_lufs)
            # a stalled microphone delivers nothing, and neither does the voice chain behind it
            mic, voice = demo_levels(now - self._t0, self._gain, stalled, self._chain.bypass, self._chain.auto_db)
            self._last_lufs_m = voice["lufs_m"]
            self._last_mic_lufs = mic["lufs_m"]
            voice["gr_db"] = chainlib.estimate_reduction(
                self._chain.params, mic["rms_db"], mic["peak_db"], self._chain.auto_db
            )
            for frame, latch in ((mic, self._latches["mic"]), (voice, self._latches["voice"])):
                latch.trip(frame["sample_db"], frame["peak_db"])
                frame["latched"] = latch.latched
        return {"t": time.time(), "mic": mic, "voice": voice}

    def health_checks(self) -> list[dict[str, str]]:
        self._advance_revive()
        with self._lock:
            item = micwatch.check_item(self._stalled, "demo: simulated stall" if self._stalled else "")
        return [item] if item else []

    def clip_path(self, clip_id: str) -> Path | None:
        self._advance()
        return self._clips.path(clip_id)

    def _demo_stall(self, on: bool) -> str | None:
        with self._lock:
            was = self._stalled
            self._stalled = on
            self._stalled_since = (
                (time.strftime("%Y-%m-%dT%H:%M:%S") if not was else self._stalled_since) if on else None
            )
            if on != was:
                self._revive.update(state="idle", message=None)
        if on and not was:
            _emit(self.notify, "warn", f"{micwatch.STALL_TITLE}. Press Revive mic.")
        return "Demo: the microphone is stalled" if on else "Demo: the microphone is healthy"

    def act(self, action: str, params: dict[str, Any]) -> str | None:
        if action.startswith(("audio.output_", "audio.demo_output_")):
            return self._outputs.act(action, params)
        if action == "audio.gain":
            db = round(number_param(params, "db", low=GAIN_RANGE[0], high=GAIN_RANGE[1], clamped=True), 2)
            with self._lock:
                self._gain = db
            return None
        if action == "audio.mute":
            on = bool_param(params)
            with self._lock:
                self._muted = on
            return None
        if action == "audio.preset":
            name = params.get("name")
            if not isinstance(name, str):
                raise ActionError("name must be a preset name", 400)
            _chain_call(self._chain.load_preset, name)
            return f"Voice preset: {self._chain.preset.removeprefix('StreamStack ') or self._chain.preset}"
        if action == "audio.preset_save":
            name = params.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ActionError("name must be a preset name", 400)
            _chain_call(self._chain.save_as_preset, name.strip(), str(params.get("blurb", "")))
            return f"Saved voice preset: {name.strip()}"
        if action == "audio.bypass":
            self._chain.set_bypass(bool_param(params))
            return "Voice effects bypassed" if self._chain.bypass else "Voice effects on"
        if action == "audio.tone":
            _chain_call(self._chain.set_params, _validate_tone(params))
            return None
        if action == "audio.denoise":
            changes = _validate_denoise(params)
            if "engine" in changes:
                _chain_call(self._chain.set_engine, changes["engine"])
            if "atten_limit_db" in changes:
                _chain_call(self._chain.set_params, {"denoise_atten_db": changes["atten_limit_db"]})
            return f"Noise removal: {chaingraph.PLUGINS[changes['engine']]['name']}" if "engine" in changes else None
        if action == "audio.chain":
            _chain_call(self._chain.set_params, _chain_changes(params))
            return None
        if action == "audio.chain_reset":
            self._chain.reset()
            return "Voice chain reset to the preset"
        if action == "audio.loudness":
            on, target = _loudness_args(params)
            _chain_call(self._chain.set_loudness, on, target)
            if on is None:
                return f"Loudness target: {self._chain.target:g} LUFS"
            return "Loudness leveller on" if on else "Loudness leveller off"
        if action == "audio.demo_deepfilternet":  # demo only: take the DeepFilterNet library away, for the e2e suite
            with self._lock:
                self._df_installed = bool_param(params)
                if not self._df_installed and self._chain.engine == "deepfilternet":
                    self._chain.engine = "rnnoise"
                self._chain.revision += 1
            return None
        if action == "audio.clip_clear":
            for latch in self._latches.values():
                latch.clear()
            return None
        if action == "audio.demo_clip":  # demo only: a clip now, for the e2e suite
            for latch in self._latches.values():
                latch.trip(0.0, 0.0)
            return None
        if action == "audio.demo_stall":  # demo only: the deterministic trigger for the e2e suite
            self._advance_revive()
            return self._demo_stall(bool_param(params))
        if action == "audio.revive":
            self._advance_revive()
            with self._lock:
                if self._revive["state"] == "reviving":
                    raise ActionError("The microphone is already being revived.", 409)
                if not (self._stalled or self._revive["state"] == "failed"):
                    raise ActionError("The microphone is delivering audio; there is nothing to revive.", 409)
                self._revive.update(state="reviving", message="Suspending and resuming the microphone...")
                self._revive["at"] = self._clock()
            return "Reviving the microphone..."
        if action == "audio.mic_check":
            seconds = _check_seconds(params)
            self._advance()
            with self._lock:
                if self._check["state"] in ("recording-raw", "recording-processed"):
                    raise ActionError("A mic check is already running.", 409)
                self._clips.clear()
                self._check = {"state": "recording-raw", "seconds": seconds, "error": None}
                self._check_start = self._clock()
            return f"Recording {seconds:g} s - talk now"
        raise ActionError(f"unknown audio action {action!r}", 400)
