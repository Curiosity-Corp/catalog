"""Console audio: meters, microphone gain and mute, voice presets, live tone and the in-app mic check.

`AudioController` drives the real rig (PipeWire through pactl/parec, Easy Effects through its local
socket); `DemoAudio` is a simulated twin with the same methods for ``--demo``, tests and e2e runs.

Easy Effects 8.1.2 research (source: wwmm/easyeffects tag v8.1.2, checked against the running 8.1.2 here)
-------------------------------------------------------------------------------------------------------
* Easy Effects 8 is a Qt app. In service mode it listens on a QLocalServer at
  ``$XDG_RUNTIME_DIR/EasyEffectsServer`` (an AF_UNIX stream socket; ``src/local_server.cpp``). The
  protocol is newline terminated text, several commands per connection, answers in order:

  - ``load_preset:<input|output>:<name>``   load a preset file (no answer)
  - ``global_bypass:<0|1>``                 1 = bypass on (no answer)
  - ``set_property:<input|output>:<plugin>:<instance>:<property>:<value>``   set one plugin parameter now
  - ``get_property:<input|output>:<plugin>:<instance>:<property>``           answers the value, or
    ``error_plugin_not_found`` / ``error_property_not_found``
  - ``get_last_loaded_preset:<input|output>``   answers the preset name (empty line when none)

  ``<plugin>`` is the bare plugin name ("pitch") and ``<instance>`` its number, so ``pitch:0`` is the
  ``pitch#0`` of the shipped presets; property names are the KConfig entry names (``semitones``,
  ``bypass``, ...). The ``-l`` / ``-b`` / ``-a`` command line options are thin clients of these same
  messages (a second process hands them to the running one), but they spawn a whole Qt process each time.
* Pitch is therefore live: ``set_property:input:pitch:0:semitones:<v>`` plus ``...:bypass:<true|false>``.
* Equalizer band gains are NOT reachable live. The bands live in separate per-channel settings objects
  (``equalizer#0#left`` / ``#right``) and the key the server looks up is ``<plugin>#<instance>`` with a
  digits-only instance, which can never name them. Only the equalizer's own gains and ``pitch-left`` /
  ``pitch-right`` can be set. There is no D-Bus interface either (D-Bus is only used for the global
  shortcuts portal), and plugin settings are not file-watched: they are written to
  ``~/.config/easyeffects/db`` only on shutdown, never read back while running.
* Bass, presence and treble therefore go through a managed preset ``StreamStack Live`` written next to
  the user's presets and loaded over the socket (``load_preset`` followed by ``get_last_loaded_preset`` on
  the same connection confirms it took), coalesced to at most one load per 400 ms with the newest
  tone winning. ``easyeffects -l`` is the fallback when the socket is missing.
* Loading a preset leaves "StreamStack Live" as the last loaded preset, so the base preset the user
  chose is remembered here and in ``<state>/console-live.json``.
* Global bypass cannot be read from a running Easy Effects (no ``get_property`` for it and the settings
  file is stale until shutdown). The state is tracked from our own changes, seeded from the settings
  file at start. ``-b 1`` enables bypass and ``-b 2`` disables it; the socket takes ``global_bypass:1|0``.
* The mapping to the shipped presets: equalizer band0 (Lo-shelf 120 Hz) is bass, band2 (Bell 3.5 kHz)
  is presence, band3 (Hi-shelf 9 kHz) is treble, both channels; pitch semitones, with the plugin
  bypassed while it is 0.
"""

from __future__ import annotations

import array
import contextlib
import copy
import json
import logging
import math
import os
import random
import re
import secrets
import shutil
import socket
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
from .errors import ActionError

log = logging.getLogger(__name__)

Notify = Callable[[str, str], None]

SILENCE_DB = -90.0
CLIP_DB = -0.5
RATE = 16000
FRAME_BYTES = RATE // 20 * 2  # 50 ms of 16 bit mono
STALE_S = 0.5  # a reader that sent nothing for this long is silent, whatever it last said

GAIN_RANGE = (-30.0, 12.0)
TONE_RANGES: dict[str, tuple[float, float]] = {
    "bass_db": (-12.0, 12.0),
    "presence_db": (-12.0, 12.0),
    "treble_db": (-12.0, 12.0),
    "pitch_semitones": (-6.0, 6.0),
}
BAND_KEYS = ("bass_db", "presence_db", "treble_db")
BAND_INDEX = {"bass_db": 0, "presence_db": 2, "treble_db": 3}

LIVE_PRESET = "StreamStack Live"
LOAD_INTERVAL = 0.4
PROBE_INTERVAL = 1.0
RESTART_INTERVAL = 2.0
READER_SILENT_S = 4.0  # our own parec heard nothing this long: corroboration only, never a trigger
READER_IDLE_RESTART = 15.0  # a parec that has produced nothing for this long is killed and started afresh
REVIVE_SHOW_S = 8.0  # how long "the microphone is back" stays in the state
CLIP_TTL = 600.0
MAX_CLIPS = 4
CHECK_SECONDS = (2.0, 15.0)
CHECK_DEFAULT = 6.0
EE_PIPELINE = "input"
EE_SOURCE_NAME = system.EE_SOURCE

BLURBS = {
    "StreamStack Voice": "default broadcast voice",
    "StreamStack Voice Warm": "more low end for thin rooms or mics",
    "StreamStack Voice Bright": "more presence and air for dark mics",
    "StreamStack Voice Deep": "pitch down 2 semitones, extra body",
    "StreamStack Voice Minimal": "only high-pass, gentle compression and the limiter",
}
LABEL_PREFIX = "StreamStack "
CLIP_ID = re.compile(r"^[0-9a-f]{16}$")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def to_db(linear: float) -> float:
    """A 0..1 amplitude in dBFS, floored at silence."""
    if linear <= 0:
        return SILENCE_DB
    return max(SILENCE_DB, round(20 * math.log10(linear), 1))


def frame_levels(samples: array.array[int]) -> tuple[float, float, bool]:
    """(peak dBFS, RMS dBFS, clipped) of one frame of signed 16 bit samples."""
    if not samples:
        return SILENCE_DB, SILENCE_DB, False
    peak = max(max(samples), -min(samples)) / 32768
    rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768
    peak_db = to_db(peak)
    return peak_db, to_db(rms), peak_db >= CLIP_DB


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


def preset_label(name: str) -> str:
    return name.removeprefix(LABEL_PREFIX) or name


def preset_entry(name: str) -> dict[str, str]:
    return {"name": name, "label": preset_label(name), "blurb": BLURBS.get(name, "")}


def _plugin(doc: dict[str, Any], prefix: str) -> dict[str, Any] | None:
    section = doc.get("input")
    if not isinstance(section, dict):
        return None
    for key, value in section.items():
        if key.startswith(prefix + "#") and isinstance(value, dict):
            return value
    return None


def read_tone(doc: dict[str, Any]) -> dict[str, float] | None:
    """The four tone values of an Easy Effects preset document (None when it has no equalizer)."""
    eq = _plugin(doc, "equalizer")
    left = eq.get("left") if eq else None
    if not isinstance(left, dict):
        return None
    tone: dict[str, float] = {}
    for key, band in BAND_INDEX.items():
        gain = left.get(f"band{band}", {}).get("gain") if isinstance(left.get(f"band{band}"), dict) else None
        if not isinstance(gain, (int, float)):
            return None
        tone[key] = float(gain)
    pitch = _plugin(doc, "pitch")
    semitones = pitch.get("semitones", 0.0) if pitch else 0.0
    tone["pitch_semitones"] = float(semitones) if isinstance(semitones, (int, float)) else 0.0
    return tone


def apply_tone(doc: dict[str, Any], tone: dict[str, float]) -> dict[str, Any]:
    """A copy of a preset document with the tone written in; the pitch plugin is bypassed at 0."""
    out = copy.deepcopy(doc)
    eq = _plugin(out, "equalizer")
    if eq is None:
        raise ValueError("the preset has no equalizer")
    for channel in ("left", "right"):
        bands = eq.get(channel)
        if not isinstance(bands, dict):
            continue
        for key, band in BAND_INDEX.items():
            if isinstance(bands.get(f"band{band}"), dict):
                bands[f"band{band}"]["gain"] = float(tone[key])
    pitch = _plugin(out, "pitch")
    if pitch is not None:
        pitch["semitones"] = float(tone["pitch_semitones"])
        pitch["bypass"] = tone["pitch_semitones"] == 0
    return out


def ee_socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "EasyEffectsServer"


def ee_request(lines: list[str], replies: int = 0, timeout: float = 1.0) -> list[str] | None:
    """Send commands to the Easy Effects local server and read `replies` answer lines.

    None means the socket is unavailable (Easy Effects not running, or an older release without it).
    """
    if any("\n" in line for line in lines):
        raise ValueError("a socket command is a single line")
    out: list[str] = []
    barrier = replies == 0  # a harmless read after the commands proves the server got through them
    if barrier:
        lines = [*lines, f"get_last_loaded_preset:{EE_PIPELINE}"]
        replies = 1
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(timeout)
            conn.connect(str(ee_socket_path()))
            conn.sendall("".join(f"{line}\n" for line in lines).encode())
            buffer = b""
            while len(out) < replies:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buffer += chunk
                *done, buffer = buffer.split(b"\n")
                out.extend(part.decode(errors="replace") for part in done)
    except TimeoutError:
        return out  # connected, but it did not answer in time
    except OSError:
        return None
    return [] if barrier else out


def ee_user_dir() -> Path:
    return Path.home() / ".local/share/easyeffects" / EE_PIPELINE


def load_preset_json(name: str) -> dict[str, Any] | None:
    """A preset document: the user's copy first, else the one shipped with the package."""
    for folder in (ee_user_dir(), config.data_dir() / "easyeffects" / EE_PIPELINE):
        try:
            doc = json.loads((folder / f"{name}.json").read_text())
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict):
            return doc
    return None


def list_presets() -> list[str]:
    """Input presets in Easy Effects' folder, shipped ones first in README order; the managed one is hidden."""
    found = {p.stem for p in ee_user_dir().glob("*.json")} - {LIVE_PRESET}
    shipped = [name for name in BLURBS if name in found]
    return shipped + sorted(found - set(shipped))


def stored_bypass() -> bool:
    """Global bypass as last saved by Easy Effects (only a starting guess; it saves on shutdown)."""
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    path = base / "easyeffects/db/easyeffectsrc"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return False
    group = ""
    for line in lines:
        line = line.strip()
        if line.startswith("["):
            group = line
        elif group == "[EffectsPipelines]" and line.replace(" ", "").lower() == "bypass=true":
            return True
    return False


# ---------------------------------------------------------------------------
# Parts
# ---------------------------------------------------------------------------


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
    """One `parec` child turned into per-50 ms peak/RMS frames by a reader thread."""

    def __init__(
        self,
        source: str,
        clock: Callable[[], float] = time.monotonic,
        spawn: Callable[..., subprocess.Popen[bytes]] | None = None,
        idle_since: float | None = None,
    ) -> None:
        self.source = source
        self.started = clock()
        self._clock = clock
        self._origin = self.started if idle_since is None else idle_since
        self._last_bytes: float | None = None
        self._spawn = spawn or subprocess.Popen
        self._lock = threading.Lock()
        self._buffer = b""
        self._last = (SILENCE_DB, SILENCE_DB, False)
        self._acc_peak = SILENCE_DB
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
                    "--format=s16le",
                    f"--rate={RATE}",
                    "--channels=1",
                    "--latency-msec=50",
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
            while chunk := os.read(fd, 4096):
                self.feed(chunk)
        except (OSError, ValueError):
            pass  # the pipe closed under us: stop() or the source went away
        self.dead = True

    def feed(self, data: bytes) -> None:
        """Consume raw s16le bytes; every complete 50 ms frame updates the levels."""
        if data:
            self._last_bytes = self._clock()
        self._buffer += data
        while len(self._buffer) >= FRAME_BYTES:
            frame, self._buffer = self._buffer[:FRAME_BYTES], self._buffer[FRAME_BYTES:]
            samples = array.array("h")
            samples.frombytes(frame)
            if sys.byteorder == "big":
                samples.byteswap()
            peak, rms, clipped = frame_levels(samples)
            with self._lock:
                self._acc_peak = max(self._acc_peak, peak) if self._fresh else peak
                self._acc_clip = (self._acc_clip or clipped) if self._fresh else clipped
                self._last = (peak, rms, clipped)
                self._fresh = True
                self._stamp = self._clock()

    def idle_since(self) -> float:
        """The clock time the source was last heard from (or when it was first waited for)."""
        return self._origin if self._last_bytes is None else self._last_bytes

    def idle_for(self) -> float:
        """Seconds without a single byte from the source."""
        return max(0.0, self._clock() - self.idle_since())

    def take(self) -> dict[str, Any]:
        """Levels since the last call: the loudest peak (no frame is missed between polls) and the latest RMS."""
        with self._lock:
            if self._clock() - self._stamp > STALE_S:
                return {"peak_db": SILENCE_DB, "rms_db": SILENCE_DB, "clip": False}
            peak, rms, clipped = self._last
            if self._fresh:
                peak, clipped = self._acc_peak, self._acc_clip
                self._fresh = False
            return {"peak_db": peak, "rms_db": rms, "clip": clipped}

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


def _tone_state(tone: dict[str, float], supported: bool, custom: bool) -> dict[str, Any]:
    return {**{k: round(v, 2) for k, v in tone.items()}, "supported": supported, "custom": custom}


def _ranges() -> dict[str, list[float]]:
    return {key: list(rng) for key, rng in TONE_RANGES.items()}


def _empty_check(seconds: float = CHECK_DEFAULT) -> dict[str, Any]:
    return {"state": "idle", "seconds": seconds, "error": None}


def _check_seconds(params: dict[str, Any]) -> float:
    if "seconds" not in params or params["seconds"] is None:
        return CHECK_DEFAULT
    return number_param(params, "seconds", low=CHECK_SECONDS[0], high=CHECK_SECONDS[1])


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


# ---------------------------------------------------------------------------
# The real controller
# ---------------------------------------------------------------------------


class AudioController:
    """Meters, gain/mute, voice presets, tone and mic check for the real rig."""

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
        self._threads: list[threading.Thread] = []
        self._readers: dict[str, LevelReader | None] = {"mic": None, "voice": None}
        self._parec = True

        self._sources: dict[str, str] = {}
        self._mic = ""
        self._mic_desc = ""
        self._gain: float | None = None
        self._muted: bool | None = None
        self._ee_running = False
        self._ee_available = system.have("easyeffects")
        self._presets: list[str] = []
        self._base: str | None = None
        self._base_tone: dict[str, float] = {k: 0.0 for k in TONE_RANGES}
        self._tone = dict(self._base_tone)
        self._tone_ok = False
        self._custom = False
        self._ee_current: str | None = None
        self._hold_until = -math.inf
        self._gain_hold = -math.inf
        self._cli_poll = -math.inf
        self._bypass = stored_bypass()
        self._throttle = Throttle(LOAD_INTERVAL, clock)

        self._clips = ClipStore(clock)
        self._check = _empty_check()
        self._check_thread: threading.Thread | None = None

        self._detector = micwatch.StallDetector(clock)
        self._graph: micwatch.StallProbe | None = None
        self._revive = {"state": "idle", "message": None, "at": -math.inf}
        self._revive_thread: threading.Thread | None = None

        self._set_base(cfg.get("STREAMSTACK_VOICE_PRESET") or "StreamStack Voice")

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._threads:
            return
        self._parec = system.have("parec")
        self._stop.clear()
        self._probe()
        for target, name in ((self._supervise, "audio-supervisor"), (self._tone_loop, "audio-tone")):
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
        self._clips.close()

    # -- background ---------------------------------------------------------

    def _supervise(self) -> None:
        while not self._stop.wait(PROBE_INTERVAL):
            try:
                self._probe()
            except Exception:  # keep the supervisor alive whatever a helper does
                log.exception("audio probe failed")

    def _probe(self) -> None:
        """Refresh everything slow (pactl, pgrep, the Easy Effects socket) and (re)start meter readers."""
        listing = system.run(["pactl", "-f", "json", "list", "sources"])
        infos = parse_sources(listing.stdout) if listing and listing.returncode == 0 else []
        sources = {i["name"]: i["description"] for i in infos}
        wanted = self._cfg.get("STREAMSTACK_MICROPHONE", "")
        mic = system.pick_microphone(sources, wanted, system.default_device("source")) if sources else ""
        info = next((i for i in infos if i["name"] == mic), None)
        running = system.easyeffects_running()
        presets = list_presets()
        current = self._read_current_preset() if running else None
        with self._lock:
            self._sources = sources
            self._mic, self._mic_desc = mic, sources.get(mic, "")
            if info is not None and self._gain_settled():
                self._gain, self._muted = info["gain_db"], info["muted"]
            elif info is None:
                self._gain = self._muted = None
            self._ee_running = running
            self._presets = presets
            self._adopt_current(current)
            self._sync_readers(mic, EE_SOURCE_NAME in sources)
            muted = self._muted
        self._watch(mic, muted, listing.stdout if listing and listing.returncode == 0 else "")
        self._clips.expire()

    def _gain_settled(self) -> bool:
        return self._clock() >= self._gain_hold

    def _read_current_preset(self) -> str | None:
        answer = ee_request([f"get_last_loaded_preset:{EE_PIPELINE}"], replies=1)
        if answer:
            name = answer[0].strip()
            return None if name in ("", "None") else name
        if answer is None and self._clock() - self._cli_poll > 15:  # no socket: the slow CLI, rarely
            self._cli_poll = self._clock()
            name = system.easyeffects_preset()
            return None if name in ("", "None") else name
        return self._ee_current

    def _adopt_current(self, current: str | None) -> None:
        """Follow what Easy Effects reports, unless it is the echo of something we just did."""
        if current is None or self._clock() < self._hold_until or self._throttle.ready_in() is not None:
            return
        self._ee_current = current
        if current == LIVE_PRESET:
            stored = self._read_sidecar()
            if stored and self._base != stored["base"]:
                self._set_base(stored["base"])
            doc = load_preset_json(LIVE_PRESET)
            tone = read_tone(doc) if doc else None
            if tone and self._base is not None:
                self._tone, self._custom = tone, tone != self._base_tone
        elif current != self._base:
            self._set_base(current)  # changed from Easy Effects itself, or another tool

    def _sync_readers(self, mic: str, voice_present: bool) -> None:
        wants = {"mic": mic or None, "voice": EE_SOURCE_NAME if voice_present else None}
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
                fresh = LevelReader(source, self._clock, self._spawn, idle_since)
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
            self._sync_readers(self._mic, EE_SOURCE_NAME in self._sources)

    # -- the watchdog ---------------------------------------------------------

    def _watch(self, mic: str, muted: bool | None, listing: str) -> None:
        """Judge the microphone's ALSA PCM (a procfs read, once per probe). Never acts on its own."""
        with self._lock:
            if self._revive["state"] == "reviving":
                return  # the revive decides, and a half-resumed node must not count
            reader = self._readers["mic"]
            silent = reader is not None and reader.idle_for() >= READER_SILENT_S
        probe: micwatch.StallProbe | None = None
        if mic and listing:
            try:
                probe = self._stall_probe(mic, listing, silent)
            except Exception:  # a probe bug must not take the supervisor down
                log.exception("stall probe failed")
        with self._lock:
            self._graph = probe
            condition = bool(mic) and muted is not True and probe is not None and probe.stalled is True
            event = self._detector.update(condition)
            if event == "started" or (event == "cleared" and self._revive["state"] == "failed"):
                self._revive.update(state="idle", message=None)  # a new stall, or the old failure resolved itself
            cleared_quietly = event == "cleared" and self._revive["state"] != "ok"
        if event == "started":
            _emit(self.notify, "warn", f"{micwatch.STALL_TITLE}. Press Revive mic.")
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
        item = micwatch.check_item(stalled if (stalled or known) else None, graph.detail if graph else "")
        return [item] if item else []

    def _due_restart(self, reader: LevelReader) -> bool:
        return self._clock() - reader.started >= RESTART_INTERVAL

    def _tone_loop(self) -> None:
        while True:
            with self._cond:
                if self._stop.is_set():
                    return
                wait = self._throttle.ready_in()
                if wait is None:
                    self._cond.wait(1.0)
                    continue
                if wait > 0:
                    self._cond.wait(wait)
                    continue
                tone = self._throttle.take()
            if tone is not None:
                try:
                    self._load_live(tone)
                except Exception:
                    log.exception("loading the live tone failed")
                    _emit(self.notify, "error", "Could not apply the tone change")

    # -- sidecar and base preset -------------------------------------------

    def _sidecar(self) -> Path:
        return config.state_dir() / "console-live.json"

    def _read_sidecar(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self._sidecar().read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and isinstance(data.get("base"), str) else None

    def _write_sidecar(self, base: str) -> None:
        path = self._sidecar()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"base": base}))
        except OSError as exc:
            log.warning("cannot remember the base preset: %s", exc)

    def _set_base(self, name: str) -> None:
        """Make `name` the chosen voice preset and take its tone as the starting point (lock held or init)."""
        if name == LIVE_PRESET:
            return
        doc = load_preset_json(name)
        tone = read_tone(doc) if doc else None
        self._base = name
        self._tone_ok = tone is not None
        self._base_tone = tone or {k: 0.0 for k in TONE_RANGES}
        self._tone = dict(self._base_tone)
        self._custom = False

    # -- state --------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        with self._lock:
            running = self._ee_running
            return {
                "mic": {
                    "name": self._mic or None,
                    "description": self._mic_desc or None,
                    "present": bool(self._mic),
                    "gain_db": self._gain,
                    "muted": self._muted,
                    "stalled": self._detector.stalled,
                    "stalled_since": self._detector.since,
                    "revive": self._revive_view(),
                },
                "gain_range": list(GAIN_RANGE),
                "chain": {
                    "running": running,
                    "available": self._ee_available,
                    "preset": self._base if running else None,
                    "presets": [preset_entry(n) for n in self._presets],
                    "bypass": self._bypass,
                },
                "tone": _tone_state(self._tone, running and self._ee_available and self._tone_ok, self._custom),
                "tone_ranges": _ranges(),
                "mic_check": {**self._check, "clips": self._clips.listing()},
            }

    def meters(self) -> dict[str, Any]:
        with self._lock:
            mic, voice = self._readers["mic"], self._readers["voice"]
        silent = {"peak_db": SILENCE_DB, "rms_db": SILENCE_DB, "clip": False}
        return {"t": time.time(), "mic": mic.take() if mic else silent, "voice": voice.take() if voice else None}

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
            "audio.mic_check": self._act_mic_check,
            "audio.revive": self._act_revive,
        }
        handler = handlers.get(action)
        if handler is None:
            raise ActionError(f"unknown audio action {action!r}", 400)
        return handler(params)

    def _need_mic(self) -> str:
        with self._lock:
            mic = self._mic
        if not mic:
            raise ActionError("No microphone found - plug it in or set STREAMSTACK_MICROPHONE.", 409)
        return mic

    def _need_ee(self) -> None:
        with self._lock:
            ok = self._ee_running and self._ee_available
        if not ok:
            raise ActionError("Easy Effects is not running - start the studio first.", 409)

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
        with self._lock:
            known = name in self._presets
        if not known:
            raise ActionError(f"Unknown voice preset {name!r}.", 400)
        self._need_ee()
        if not self._ee_load(name):
            raise ActionError("Easy Effects did not accept the preset.", 409)
        with self._lock:
            self._set_base(name)
            self._ee_current = name
            self._hold_until = self._clock() + 1.5
            self._throttle.cancel()
        return f"Voice preset: {preset_label(name)}"

    def _act_bypass(self, params: dict[str, Any]) -> str | None:
        on = bool_param(params)
        self._need_ee()
        if ee_request([f"global_bypass:{1 if on else 0}"]) is None:
            result = system.run(["easyeffects", "-b", "1" if on else "2"], timeout=15)
            if result is None or result.returncode != 0:
                raise ActionError("Easy Effects did not accept the bypass change.", 409)
        with self._lock:
            self._bypass = on
        return "Voice effects bypassed" if on else "Voice effects on"

    def _act_tone(self, params: dict[str, Any]) -> str | None:
        changes = _validate_tone(params)
        with self._lock:
            supported = self._ee_running and self._ee_available and self._tone_ok
        if not supported:
            raise ActionError("Tone control needs Easy Effects running with a StreamStack voice preset.", 409)
        with self._cond:
            before = self._tone
            self._tone = {**before, **changes}
            self._custom = self._tone != self._base_tone
            bands = any(changes.get(k, before[k]) != before[k] for k in BAND_KEYS)
            pitch = changes.get("pitch_semitones", before["pitch_semitones"]) != before["pitch_semitones"]
            if bands:
                self._throttle.submit(dict(self._tone))
                self._cond.notify_all()
            tone = dict(self._tone)
        if pitch and not self._set_pitch_live(tone["pitch_semitones"]) and not bands:
            with self._cond:  # no socket: the preset path carries the pitch too
                self._throttle.submit(tone)
                self._cond.notify_all()
        return None

    def _set_pitch_live(self, semitones: float) -> bool:
        prefix = f"set_property:{EE_PIPELINE}:pitch:0"
        reply = ee_request(
            [f"{prefix}:semitones:{semitones}", f"{prefix}:bypass:{'true' if semitones == 0 else 'false'}"]
        )
        return reply is not None

    def _ee_load(self, name: str) -> bool:
        """Load an input preset: the socket (confirmed by reading the last loaded name back), else the CLI."""
        answer = ee_request([f"load_preset:{EE_PIPELINE}:{name}", f"get_last_loaded_preset:{EE_PIPELINE}"], replies=1)
        if answer is not None:
            return bool(answer) and answer[0].strip() == name
        result = system.run(["easyeffects", "-l", name], timeout=15)
        return bool(result and result.returncode == 0)

    def _load_live(self, tone: dict[str, float]) -> None:
        """Write the managed preset from the chosen base plus `tone`, and load it."""
        with self._lock:
            base = self._base
        doc = load_preset_json(base) if base else None
        if base is None or doc is None:
            raise ActionError("The base voice preset cannot be read.", 409)
        out = apply_tone(doc, tone)
        target = ee_user_dir() / f"{LIVE_PRESET}.json"
        tmp = target.with_suffix(".json.tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(out, indent=2))
            tmp.replace(target)
        except OSError as exc:
            raise ActionError(f"Cannot write the live preset: {exc}", 409) from exc
        self._write_sidecar(base)
        if not self._ee_load(LIVE_PRESET):
            _emit(self.notify, "error", "Easy Effects did not accept the tone change")
            return
        with self._lock:
            self._ee_current = LIVE_PRESET
            self._hold_until = self._clock() + 1.5

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
            self._set_revive("reviving", "Suspending and resuming the microphone...")
        thread = threading.Thread(target=self._run_revive, args=(mic,), name="mic-revive", daemon=True)
        self._revive_thread = thread
        thread.start()
        return "Reviving the microphone..."

    def _run_revive(self, mic: str) -> None:
        """The worker: escalate until the node is scheduled again, report through state and a toast."""
        try:
            result = micwatch.revive_mic(
                mic,
                run=system.run,
                sleep=self._sleep,
                stalled=lambda: self._recheck(mic),
                say=lambda text: self._set_revive("reviving", text),
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
            if EE_SOURCE_NAME in self._sources:
                takes.append(("recording-processed", "processed voice", EE_SOURCE_NAME))
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


def demo_levels(t: float, gain_db: float, muted: bool, bypass: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    """(mic, voice) meter dicts: the voice is a few dB hotter than the mic and more compressed."""
    if muted:
        silent = {"peak_db": SILENCE_DB, "rms_db": SILENCE_DB, "clip": False}
        return dict(silent), dict(silent)
    speech = speech_peak_db(t)
    noise = -66 + 3 * math.sin(t * 7.3)

    def pack(peak: float, crest: float) -> dict[str, Any]:
        peak = round(max(SILENCE_DB, min(0.0, peak)), 1)
        return {"peak_db": peak, "rms_db": round(max(SILENCE_DB, peak - crest), 1), "clip": peak >= CLIP_DB}

    mic_peak = max(noise, speech if speech is not None else noise) + gain_db
    if bypass:
        return pack(mic_peak, 10), pack(mic_peak, 10)
    if speech is None:
        return pack(mic_peak, 10), pack(noise - 12, 4)
    voice_peak = -13 + 0.45 * (speech + gain_db + 20) + 3  # compressed towards a steady level, then lifted
    return pack(mic_peak, 10), pack(voice_peak, 6)


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
        self._presets = [n for n in BLURBS]
        self._base = "StreamStack Voice"
        self._base_tone = self._shipped_tone(self._base)
        self._tone = dict(self._base_tone)
        self._clips = ClipStore(clock)
        self._check = _empty_check()
        self._check_start = 0.0
        self._stalled = False
        self._stalled_since: str | None = None
        self._revive = {"state": "idle", "message": None, "at": 0.0}

    @staticmethod
    def _shipped_tone(name: str) -> dict[str, float]:
        try:
            doc = json.loads((config.data_dir() / "easyeffects" / EE_PIPELINE / f"{name}.json").read_text())
        except (OSError, ValueError):
            return {k: 0.0 for k in TONE_RANGES}
        return read_tone(doc) or {k: 0.0 for k in TONE_RANGES}

    def start(self) -> None:
        return None

    def stop(self) -> None:
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
                    "revive": {"state": self._revive["state"], "message": self._revive["message"]},
                },
                "gain_range": list(GAIN_RANGE),
                "chain": {
                    "running": True,
                    "available": True,
                    "preset": self._base,
                    "presets": [preset_entry(n) for n in self._presets],
                    "bypass": self._bypass,
                },
                "tone": _tone_state(self._tone, True, self._tone != self._base_tone),
                "tone_ranges": _ranges(),
                "mic_check": {**self._check, "clips": self._clips.listing()},
            }

    def meters(self) -> dict[str, Any]:
        self._advance_revive()
        with self._lock:
            # a stalled microphone delivers nothing, and neither does the voice chain behind it
            mic, voice = demo_levels(self._clock() - self._t0, self._gain, self._muted or self._stalled, self._bypass)
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
            if not isinstance(name, str) or name not in self._presets:
                raise ActionError(f"Unknown voice preset {name!r}.", 400)
            with self._lock:
                self._base, self._base_tone = name, self._shipped_tone(name)
                self._tone = dict(self._base_tone)
            return f"Voice preset: {preset_label(name)}"
        if action == "audio.bypass":
            on = bool_param(params)
            with self._lock:
                self._bypass = on
            return "Voice effects bypassed" if on else "Voice effects on"
        if action == "audio.tone":
            changes = _validate_tone(params)
            with self._lock:
                self._tone.update(changes)
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
