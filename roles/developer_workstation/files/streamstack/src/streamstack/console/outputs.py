"""Console audio outputs: every PipeWire sink with volume, mute and default, and a pre-meeting sound test per sink.

`Outputs` drives the real rig through PipeWire's own tools (`pw-dump`, `wpctl`, `pw-play`); `DemoOutputs` is a
simulated twin with four outputs for ``--demo``, tests and e2e runs. Both sit behind `AudioController` / `DemoAudio`
as the ``audio.output_*`` actions and the ``audio.outputs`` object of the state.

Design notes
------------
* The list is one `pw-dump`: the Audio/Sink nodes, their volume and mute (the node's Props), and the default sink
  from the "default" metadata. Nothing here is specific to a model. What an output *is* (headphones, HDMI, speaker,
  a USB interface, a headset) is read from PipeWire's own hints (form factor, ALSA path, bus, name), so an Elgato
  Wave XLR, a Focusrite Scarlett 2i2, a monitor's HDMI sound and a Bluetooth headset all show up the same way.
  Virtual sinks (an effects sink) are routes, not devices: they are listed apart and cannot be tested.
* Volume, mute and the default go through `wpctl` (WirePlumber's own CLI) by node id. Volume is the percentage
  `wpctl` shows: the cube root of the linear gain PipeWire stores.
* The test is a generated stereo WAV (stdlib `wave` and `struct`): one low beep on the left, two high beeps on the
  right, then a two-note chime on both. It is played with `pw-play --target <sink>`, so the default output and every
  other stream stay where they are. One test runs at a time, in a thread, with a hard deadline; "Test all" steps through
  every output in turn. Whether the person heard it is theirs to say: the result is remembered per sink (when, and
  heard / not heard) in `<state>/console-outputs.json`, so the whole desk can be checked before a call.
* Hot-plug: a `pw-dump --monitor` thread wakes a refresher when a sink, a device or the default changes, so the list
  follows a plug within a fraction of a second; the audio probe still polls now and then in case the stream fails.
* The default output vanishing (unplugged) raises a warning that stays until it is dismissed, the device comes back
  or someone picks a default.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import logging
import math
import os
import re
import struct
import subprocess
import tempfile
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import store as filestore
from .. import system
from .errors import ActionError

log = logging.getLogger(__name__)

Notify = Callable[[str, str], None]

VOLUME_RANGE = (0, 100)
TEST_RATE = 48000
TEST_LEVEL = 0.32  # of full scale: clearly audible on a headset, not painful on a speaker left at 100 percent
# The test sound as (phase, start s, end s): a low beep on the left, two high beeps on the right, a chime on both.
TEST_PHASES = (("left", 0.05, 0.45), ("right", 0.65, 1.15), ("chime", 1.5, 2.7))
TEST_SECONDS = 2.8
PLAY_GRACE = 6.0  # a player still running this long after the sound ended is stuck: it is reaped
TEST_GAP_S = 0.6  # between two outputs of "Test all"
POLL_SECONDS = 5.0  # the safety-net refresh while the hot-plug watch is alive
WATCH_RETRY_S = 3.0
SETTLE_S = 0.2  # a plug makes several events: refresh once after they stop
MAX_RESULTS = 32
RESULTS = ("pending", "heard", "not-heard", "failed")
WATCH_BUFFER = 64 * 1024 * 1024  # a watch stream that never parses is dropped, not buffered forever
NODE = "PipeWire:Interface:Node"
DEVICE = "PipeWire:Interface:Device"
METADATA = "PipeWire:Interface:Metadata"
SINK_ID = re.compile(r"^[^\s-][^\s]{0,199}$")
PROFILE_SUFFIX = re.compile(
    r"\s+(?:(?:Analog|Digital|Multichannel|Pro|Headphones|Speakers?|Line|Headset)\s+)?"
    r"(?:Stereo|Mono|Surround\s*\d(?:\.\d)?|Output|Duplex|Sink)(?:\s+\((?:HDMI|DisplayPort|Digital|Analog)[^)]*\))?\s*$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def sink_percent(props: object) -> int | None:
    """The volume `wpctl` shows, in percent, from a node's Props (None when unknown).

    PipeWire stores the linear gain of each channel; `wpctl` (and every mixer) shows its cube root, so 0.064 reads 40.
    """
    if not isinstance(props, dict):
        return None
    channels = props.get("channelVolumes")
    gain = channels[0] if isinstance(channels, list) and channels else props.get("volume")
    if isinstance(gain, bool) or not isinstance(gain, (int, float)) or not math.isfinite(gain) or gain < 0:
        return None
    return round(gain ** (1 / 3) * 100)


def split_label(description: str) -> tuple[str, str]:
    """("Elgato Wave XLR", "Analog Stereo") from "Elgato Wave XLR Analog Stereo": the device and its profile."""
    text = description.strip()
    match = PROFILE_SUFFIX.search(text)
    if match is None or not text[: match.start()].strip():
        return text, ""
    return text[: match.start()].strip(), match.group(0).strip()


def output_kind(props: dict[str, Any], name: str, description: str, port: str) -> str:
    """What the output is, from PipeWire's hints: hdmi, headphones, headset, speaker, interface or other."""
    form = str(props.get("device.form-factor", props.get("device.form_factor", ""))).lower()
    bus = str(props.get("device.bus", "")).lower()
    where = f"{port} {props.get('api.alsa.path', '')} {name} {description}".lower()
    if re.search(r"hdmi|displayport|display port", where):
        return "hdmi"
    if form in ("headset", "handsfree", "hands-free", "hands_free"):
        return "headset"
    if form in ("headphone", "headphones"):
        return "headphones"
    if form in ("speaker", "speakers", "tv", "portable", "car", "hifi", "computer", "internal"):
        return "speaker"
    if "headset" in port.lower():
        return "headset"
    if "headphone" in port.lower():
        return "headphones"
    if "speaker" in port.lower():
        return "speaker"
    if bus == "usb":
        return "interface"
    return "other"


def node_props(node: dict[str, Any]) -> dict[str, Any]:
    """The `Props` object (volume, mute) of a pw-dump node, an empty one when it has none."""
    info = node.get("info") if isinstance(node.get("info"), dict) else {}
    params = info.get("params") if isinstance(info.get("params"), dict) else {}
    for entry in params.get("Props") or ():
        if isinstance(entry, dict) and ("channelVolumes" in entry or "volume" in entry):
            return entry
    return {}


def default_sink_name(objects: list[Any]) -> str:
    """The default sink's node name from the "default" metadata of a pw-dump ('' when there is none)."""
    for obj in objects:
        if not isinstance(obj, dict) or obj.get("type") != METADATA:
            continue
        props = obj.get("props") if isinstance(obj.get("props"), dict) else {}
        if props.get("metadata.name") != "default":
            continue
        for entry in obj.get("metadata") or ():
            if isinstance(entry, dict) and entry.get("key") == "default.audio.sink":
                value = entry.get("value")
                if isinstance(value, str):
                    with contextlib.suppress(ValueError):
                        value = json.loads(value)
                if isinstance(value, dict) and isinstance(value.get("name"), str):
                    return value["name"]
    return ""


def parse_dump(text: str) -> tuple[list[dict[str, Any]], str]:
    """The sinks of a `pw-dump` as the Console describes them, and the default sink's name (empty for garbage)."""
    try:
        objects = json.loads(text)
    except ValueError:
        return [], ""
    if not isinstance(objects, list):
        return [], ""
    found = []
    for obj in objects:
        if not isinstance(obj, dict) or obj.get("type") != NODE or not isinstance(obj.get("info"), dict):
            continue
        props = obj["info"].get("props") if isinstance(obj["info"].get("props"), dict) else {}
        if props.get("media.class") != "Audio/Sink" or not props.get("node.name") or not isinstance(obj.get("id"), int):
            continue
        name = str(props["node.name"])
        description = str(props.get("node.description") or props.get("node.nick") or name)
        label, detail = split_label(description)
        virtual = str(props.get("node.virtual", "")).lower() == "true" or name.startswith("streamstack")
        bus = (
            "virtual"
            if virtual
            else str(props.get("device.bus", "") or ("bluetooth" if name.startswith("bluez") else ""))
        )
        levels = node_props(obj)
        found.append(
            {
                "id": obj["id"],
                "name": name,
                "label": label,
                "detail": detail,
                "kind": "route" if virtual else output_kind(props, name, description, ""),
                "bus": bus.lower(),
                "virtual": virtual,
                "state": str(obj["info"].get("state", "")).lower(),
                "volume": sink_percent(levels),
                "muted": bool(levels.get("mute", False)),
            }
        )
    return found, default_sink_name(objects)


def parse_sinks(text: str) -> list[dict[str, Any]]:
    """Every sink of a `pw-dump` (empty for anything unreadable)."""
    return parse_dump(text)[0]


class JsonStream:
    """Incremental decoder for the back-to-back JSON values `pw-dump --monitor` writes."""

    def __init__(self) -> None:
        self._decoder = json.JSONDecoder()
        self._text = ""

    def feed(self, chunk: bytes) -> list[Any]:
        """The complete values this chunk finished; a partial one waits for the next chunk."""
        self._text += chunk.decode("utf-8", "replace")
        values: list[Any] = []
        while True:
            stripped = self._text.lstrip()
            if not stripped:
                self._text = ""
                break
            try:
                value, end = self._decoder.raw_decode(stripped)
            except ValueError:
                self._text = stripped
                if len(stripped) > WATCH_BUFFER:
                    self._text = ""
                    raise ValueError("the PipeWire stream does not parse") from None
                break
            values.append(value)
            self._text = stripped[end:]
        return values


def concerns_outputs(values: list[Any], sink_ids: set[int]) -> bool:
    """Whether a batch of `pw-dump --monitor` objects touches a sink, a device or the default (not a stream)."""
    for batch in values:
        for obj in batch if isinstance(batch, list) else [batch]:
            if not isinstance(obj, dict):
                continue
            info = obj.get("info")
            kind = obj.get("type")
            if kind in (DEVICE, METADATA) or obj.get("id") in sink_ids:
                return True
            props = info.get("props") if isinstance(info, dict) else None
            if kind == NODE and isinstance(props, dict) and props.get("media.class") == "Audio/Sink":
                return True
    return False


def test_phases() -> list[dict[str, Any]]:
    return [{"id": phase, "start": start, "end": end} for phase, start, end in TEST_PHASES]


def _beep(t: float, start: float, length: float, freq: float) -> float:
    """A sine beep with a 10 ms fade at both ends (no click on a speaker)."""
    if not start <= t < start + length:
        return 0.0
    fade = min(1.0, (t - start) / 0.01, (start + length - t) / 0.01)
    return math.sin(2 * math.pi * freq * (t - start)) * fade


def _chime(t: float, start: float) -> float:
    """Two notes (A5 then E6) that ring out: a bell-like decay, so a dead channel or a hum is easy to hear."""
    sound = 0.0
    for offset, freq in ((0.0, 880.0), (0.28, 1318.5)):
        age = t - start - offset
        if age >= 0:
            sound += math.sin(2 * math.pi * freq * age) * math.exp(-age * 3.2) * min(1.0, age / 0.005)
    return sound * 0.6


def synth_test_wav(path: Path) -> None:
    """Write the stereo identification sound: left channel, then right channel, then a chime on both."""
    left_at, right_at, chime_at = (TEST_PHASES[0][1], TEST_PHASES[1][1], TEST_PHASES[2][1])
    frames = bytearray()
    for i in range(int(TEST_SECONDS * TEST_RATE)):
        t = i / TEST_RATE
        left = _beep(t, left_at, 0.4, 392.0)
        right = _beep(t, right_at, 0.22, 659.3) + _beep(t, right_at + 0.3, 0.22, 659.3)
        both = _chime(t, chime_at)
        frames += struct.pack(
            "<hh", *(round(max(-1.0, min(1.0, v)) * TEST_LEVEL * 32767) for v in (left + both, right + both))
        )
    with wave.open(str(path), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(TEST_RATE)
        out.writeframes(bytes(frames))


def _emit(notify: Notify | None, level: str, text: str) -> None:
    if notify is None:
        return
    try:
        notify(level, text)
    except Exception:
        log.exception("toast callback failed")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _idle_test() -> dict[str, Any]:
    return {"state": "idle", "name": None, "started": None, "index": 0, "total": 0, "all": False}


# ---------------------------------------------------------------------------
# The real outputs
# ---------------------------------------------------------------------------


class Outputs:
    """Output devices of the machine: list, volume, mute, default, test, remembered results and the hot-plug watch."""

    def __init__(
        self,
        notify: Notify | None = None,
        *,
        store: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.notify = notify
        self._store = store
        self._clock = clock
        self._lock = threading.RLock()
        self._closing = threading.Event()
        self._dirty = threading.Event()
        self._threads: list[threading.Thread] = []
        self._watch_proc: subprocess.Popen[bytes] | None = None
        self._watching = False
        self._last_refresh = -math.inf
        self._sinks: list[dict[str, Any]] = []
        self._default = ""
        self._problem: str | None = None
        self._labels: dict[str, str] = {}
        self._seen_default = ""
        self._lost: dict[str, str] | None = None
        self._results: dict[str, dict[str, Any]] = self._read_results()
        self._test = _idle_test()
        self._test_stop = threading.Event()
        self._test_thread: threading.Thread | None = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._threads:
            return
        self._closing.clear()
        self.refresh()
        if not self._can_watch():
            return
        for target, name in ((self._watch_loop, "outputs-watch"), (self._refresh_loop, "outputs-refresh")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._closing.set()
        self._dirty.set()
        self._test_stop.set()
        with self._lock:
            proc, self._watch_proc = self._watch_proc, None
        if proc is not None:
            system.reap(proc, (0.0, 1.0, 2.0))
        for thread in (*self._threads, self._test_thread):
            if thread is not None:
                thread.join(timeout=3)
        self._threads.clear()
        self._test_thread = None

    def _can_watch(self) -> bool:
        return system.have("pw-dump")

    # -- listing --------------------------------------------------------------

    def poll(self) -> None:
        """The supervisor's tick: refresh now, unless the hot-plug watch is alive and the list is recent."""
        with self._lock:
            fresh = self._watching and self._clock() - self._last_refresh < POLL_SECONDS
        if not fresh:
            self.refresh()

    def refresh(self) -> None:
        """Read the sinks and the default from PipeWire (an unreadable answer keeps the last list and says why)."""
        if not system.have("pw-dump"):
            self._apply(None, "", "pw-dump is missing (install pipewire-bin).")
            return
        listing = system.run(["pw-dump"])
        if listing is None or listing.returncode != 0:
            self._apply(None, "", "The audio server did not answer.")
            return
        sinks, default = parse_dump(listing.stdout)
        if not sinks and not listing.stdout.strip().startswith("["):
            self._apply(None, "", "The audio server gave an answer that could not be read.")
            return
        self._apply(sinks, default, None)

    def _apply(self, sinks: list[dict[str, Any]] | None, default: str, problem: str | None) -> None:
        """Adopt a fresh listing; a default output that is gone raises the warning (once)."""
        warn = ""
        with self._lock:
            self._problem = problem
            self._last_refresh = self._clock()
            if sinks is None:
                return
            names = {s["name"] for s in sinks}
            for sink in sinks:
                self._labels[sink["name"]] = sink["label"]
            self._sinks, self._default = sinks, default
            seen = self._seen_default
            if seen and seen not in names and (self._lost is None or self._lost["name"] != seen):
                self._lost = {"name": seen, "label": self._labels.get(seen, seen)}
                warn = self._lost_message()
            if self._lost is not None and self._lost["name"] in names:
                self._lost = None  # it is back
            if default in names:
                self._seen_default = default
        if warn:
            _emit(self.notify, "warn", warn)

    def _lost_message(self) -> str:
        """What happened to the default output, in words (lock held)."""
        lost = self._lost
        if lost is None:
            return ""
        now = self._labels.get(self._default) if self._default else None
        where = f"Sound now plays on {now}." if now else "No output is selected now."
        return f"{lost['label']} was the default output and is gone. {where}"

    # -- state ----------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        with self._lock:
            sinks, default = self._sinks, self._default
            testing = self._test["state"] == "playing"
            devices = [
                {
                    "name": s["name"],
                    "label": s["label"],
                    "detail": s["detail"],
                    "kind": s["kind"],
                    "bus": s["bus"],
                    "state": s["state"],
                    "volume": None if s["volume"] is None else min(VOLUME_RANGE[1], s["volume"]),
                    "muted": s["muted"],
                    "default": s["name"] == default,
                    "playing": testing and self._test["name"] == s["name"],
                    "result": self._results.get(s["name"]),
                }
                for s in sinks
                if not s["virtual"]
            ]
            routes = [
                {"name": s["name"], "label": s["label"], "detail": s["detail"], "default": s["name"] == default}
                for s in sinks
                if s["virtual"]
            ]
            test = {
                **{k: v for k, v in self._test.items() if k != "started"},
                "seconds": TEST_SECONDS,
                "phases": test_phases(),
                "elapsed": round(self._clock() - self._test["started"], 2) if testing else None,
            }
            default_label = self._labels.get(default) if default else None
            return {
                "available": bool(devices) or bool(routes),
                "problem": self._problem or (None if devices or routes else "No audio output found."),
                "default": default or None,
                "default_label": default_label,
                "volume_range": list(VOLUME_RANGE),
                "devices": devices,
                "routes": routes,
                "warning": None
                if self._lost is None
                else {"kind": "default-lost", **self._lost, "message": self._lost_message()},
                "test": test,
            }

    # -- actions --------------------------------------------------------------

    def act(self, action: str, params: dict[str, Any]) -> str | None:
        handlers: dict[str, Callable[[dict[str, Any]], str | None]] = {
            "audio.output_volume": self._act_volume,
            "audio.output_mute": self._act_mute,
            "audio.output_default": self._act_default,
            "audio.output_test": self._act_test,
            "audio.output_test_all": self._act_test_all,
            "audio.output_test_stop": self._act_test_stop,
            "audio.output_result": self._act_result,
            "audio.output_dismiss": self._act_dismiss,
        }
        handler = handlers.get(action)
        if handler is None:
            raise ActionError(f"unknown audio action {action!r}", 400)
        return handler(params)

    def _find(self, params: dict[str, Any], *, device: bool = False) -> dict[str, Any]:
        """The connected sink an action names (a route too, unless `device`): 400 for no name, 409 when it is gone."""
        name = params.get("name")
        if not isinstance(name, str) or not SINK_ID.match(name):
            raise ActionError("name must be the name of an output", 400)
        with self._lock:
            sink = next((s for s in self._sinks if s["name"] == name), None)
        if sink is None:
            raise ActionError("That output is not connected any more.", 409)
        if device and sink["virtual"]:
            raise ActionError(f"{sink['label']} is a route, not a device: there is nothing to test.", 409)
        return sink

    def _wpctl(self, args: list[str], what: str) -> None:
        if not system.have("wpctl"):
            raise ActionError("wpctl is missing (install wireplumber).", 409)
        result = system.run(["wpctl", *args])
        if result is None:
            raise ActionError(f"The audio server did not answer ({what}).", 409)
        if result.returncode != 0:
            detail = (result.stderr.strip().splitlines() or [""])[-1]
            raise ActionError(
                f"The audio server did not accept the change ({what}){': ' + detail if detail else ''}.", 409
            )

    def _act_volume(self, params: dict[str, Any]) -> str | None:
        percent = round(percent_param(params, "percent", *VOLUME_RANGE))
        sink = self._find(params)
        self._wpctl(["set-volume", str(sink["id"]), f"{percent}%"], f"volume of {sink['label']}")
        with self._lock:
            sink["volume"] = percent
        return None

    def _act_mute(self, params: dict[str, Any]) -> str | None:
        on = params.get("on")
        if not isinstance(on, bool):
            raise ActionError("on must be true or false", 400)
        sink = self._find(params)
        self._wpctl(["set-mute", str(sink["id"]), "1" if on else "0"], f"mute of {sink['label']}")
        with self._lock:
            sink["muted"] = on
        return None

    def _act_default(self, params: dict[str, Any]) -> str | None:
        sink = self._find(params)
        self._wpctl(["set-default", str(sink["id"])], f"default output {sink['label']}")
        with self._lock:
            self._default, self._seen_default, self._lost = sink["name"], sink["name"], None
        return f"Default output: {sink['label']}"

    def _act_dismiss(self, params: dict[str, Any]) -> str | None:
        with self._lock:
            self._lost = None
        return None

    def _act_result(self, params: dict[str, Any]) -> str | None:
        """The person's verdict on a played test: heard it or not."""
        heard = params.get("heard")
        if not isinstance(heard, bool):
            raise ActionError("heard must be true or false", 400)
        sink = self._find(params, device=True)
        with self._lock:
            entry = self._results.get(sink["name"])
            if entry is None or entry["result"] == "failed":
                raise ActionError(f"Play the test on {sink['label']} first.", 409)
            self._results[sink["name"]] = {**entry, "result": "heard" if heard else "not-heard"}
            self._save_results()
        return None

    # -- the test -------------------------------------------------------------

    def _need_player(self) -> None:
        if not system.have("pw-play"):
            raise ActionError("pw-play is missing (install pipewire-bin), so outputs cannot be tested.", 409)

    @staticmethod
    def _unhearable(sink: dict[str, Any]) -> str | None:
        """Why a test on this sink could not be heard (muted or all the way down), or None."""
        if sink["muted"]:
            return f"{sink['label']} is muted. Unmute it to test it."
        if sink["volume"] == 0:
            return f"{sink['label']} is turned all the way down. Raise its volume to test it."
        return None

    def _begin(self, names: list[str], whole: bool) -> None:
        with self._lock:
            if self._test["state"] == "playing":
                raise ActionError("A test is already playing. Stop it first.", 409)
            self._test = {
                "state": "playing",
                "name": names[0],
                "started": self._clock(),
                "index": 1,
                "total": len(names),
                "all": whole,
            }
            self._test_stop = stop = threading.Event()
        thread = threading.Thread(target=self._run_tests, args=(names, whole, stop), name="output-test", daemon=True)
        self._test_thread = thread
        thread.start()

    def _act_test(self, params: dict[str, Any]) -> str | None:
        self._need_player()
        sink = self._find(params, device=True)
        if reason := self._unhearable(sink):
            raise ActionError(reason, 409)
        self._begin([sink["name"]], False)
        return f"Playing the test on {sink['label']}: listen for left, right, then a chime"

    def _act_test_all(self, params: dict[str, Any]) -> str | None:
        self._need_player()
        with self._lock:
            names = [s["name"] for s in self._sinks if not s["virtual"]]
        if not names:
            raise ActionError("There are no outputs to test.", 409)
        self._begin(names, True)
        return f"Testing {len(names)} output{'s' if len(names) != 1 else ''} one after the other"

    def _act_test_stop(self, params: dict[str, Any]) -> str | None:
        self._test_stop.set()
        return None

    def _prepare(self) -> Path | None:
        """The test sound on disk for this run (a temp file), or None when this rig plays nothing."""
        fd, name = tempfile.mkstemp(prefix="streamstack-output-test-", suffix=".wav")
        os.close(fd)
        path = Path(name)
        try:
            synth_test_wav(path)
        except OSError:
            path.unlink(missing_ok=True)
            raise
        return path

    def _play(self, sink: str, path: Path | None, stop: threading.Event) -> str | None:
        """Play the test on one sink; None when it played, else why not."""
        if path is None:
            return "There is no test sound to play."
        return system.play(sink, str(path), TEST_SECONDS + PLAY_GRACE, stop)

    def _run_tests(self, names: list[str], whole: bool, stop: threading.Event) -> None:
        path: Path | None = None
        problem = ""
        try:
            path = self._prepare()
            for index, name in enumerate(names, start=1):
                if stop.is_set():
                    break
                with self._lock:
                    sink = next((s for s in self._sinks if s["name"] == name), None)
                    self._test.update(name=name, started=self._clock(), index=index)
                label = sink["label"] if sink else self._labels.get(name, name)
                error = "It is not connected any more." if sink is None else self._unhearable(sink)
                if error is None:
                    error = self._play(name, path, stop)
                if stop.is_set():
                    break
                self._record(name, error)
                if error:
                    _emit(self.notify, "warn", f"Test on {label} failed: {error}")
                if index < len(names):
                    stop.wait(TEST_GAP_S)
        except OSError as exc:
            problem = f"The test sound could not be written: {exc}"
            log.warning("output test: %s", problem)
        finally:
            if path is not None:
                with contextlib.suppress(OSError):
                    path.unlink()
            with self._lock:
                self._test = _idle_test()
        if problem:
            _emit(self.notify, "error", problem)
        elif whole and not stop.is_set():
            _emit(self.notify, "ok", "Output test finished: mark each output as heard or not heard")

    def _record(self, name: str, error: str | None) -> None:
        with self._lock:
            self._results[name] = {"at": _now(), "result": "failed" if error else "pending", "error": error}
            for old in sorted(self._results, key=lambda n: self._results[n]["at"])[
                : max(0, len(self._results) - MAX_RESULTS)
            ]:
                del self._results[old]
            self._save_results()

    # -- remembered results -----------------------------------------------------

    def _read_results(self) -> dict[str, dict[str, Any]]:
        if self._store is None:
            return {}
        data = filestore.read_json(self._store, None, validate=filestore.is_object)
        found: dict[str, dict[str, Any]] = {}
        raw = data.get("results") if isinstance(data, dict) else None
        for name, entry in raw.items() if isinstance(raw, dict) else ():
            if isinstance(entry, dict) and entry.get("result") in RESULTS and isinstance(entry.get("at"), str):
                found[str(name)] = {"at": entry["at"], "result": entry["result"], "error": entry.get("error")}
        return found

    def _save_results(self) -> None:
        """Write the results (lock held); a pending one is kept as pending, the person may still judge it."""
        if self._store is None:
            return
        try:
            filestore.write_json(self._store, {"results": self._results})
        except OSError as exc:
            log.warning("cannot remember the output test results: %s", exc)

    # -- hot-plug ---------------------------------------------------------------

    def _watch_loop(self) -> None:
        """`pw-dump --monitor` while we run: a change to a sink, a device or the default marks the list dirty."""
        while not self._closing.is_set():
            try:
                proc = subprocess.Popen(
                    ["pw-dump", "--monitor"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as exc:
                log.warning("pw-dump --monitor cannot start: %s", exc)
                if self._closing.wait(WATCH_RETRY_S):
                    return
                continue
            with self._lock:
                self._watch_proc, self._watching = proc, True
            stream = JsonStream()
            try:
                self._read_watch(proc, stream)
            except (OSError, ValueError) as exc:
                log.warning("pw-dump --monitor stopped reading: %s", exc)
            finally:
                with self._lock:
                    self._watching = False
                    self._watch_proc = None
                system.reap(proc, (0.0, 1.0, 2.0))
            self._dirty.set()  # whatever happened while we were blind
            if self._closing.wait(WATCH_RETRY_S):
                return

    def _read_watch(self, proc: subprocess.Popen[bytes], stream: JsonStream) -> None:
        reader = proc.stdout
        if reader is None:
            return
        read = getattr(reader, "read1", reader.read)  # whatever has arrived, not a full buffer
        while chunk := read(65536):  # the opening dump is a change too: it makes the first refresh after a (re)start
            values = stream.feed(chunk)
            with self._lock:
                known = {s["id"] for s in self._sinks if "id" in s}
            if concerns_outputs(values, known):
                self._dirty.set()

    def _refresh_loop(self) -> None:
        while not self._closing.is_set():
            self._dirty.wait()
            if self._closing.is_set():
                return
            self._closing.wait(SETTLE_S)  # a plug is several events
            self._dirty.clear()
            try:
                self.refresh()
            except Exception:  # keep the refresher alive whatever a helper does
                log.exception("output refresh failed")


def percent_param(params: dict[str, Any], key: str, low: float, high: float) -> float:
    """A required finite number, clamped into [low, high]; anything else is a 400."""
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ActionError(f"{key} must be a number", 400)
    return float(max(low, min(high, value)))


# ---------------------------------------------------------------------------
# The simulated twin
# ---------------------------------------------------------------------------

DEMO_PLAY_S = 1.4  # how long a simulated test "plays"
DEMO_ROUTE = "streamstack_route"
DEMO_IDS = itertools.count(100)  # stand-ins for PipeWire node ids


def _demo_sink(name: str, description: str, kind: str, bus: str, volume: int, state: str = "idle") -> dict[str, Any]:
    label, detail = split_label(description)
    return {
        "id": next(DEMO_IDS),
        "name": name,
        "label": label,
        "detail": detail,
        "kind": kind,
        "bus": bus,
        "virtual": False,
        "state": state,
        "volume": volume,
        "muted": False,
    }


def demo_world() -> list[dict[str, Any]]:
    """The four simulated outputs (two USB interfaces, an HDMI monitor, a Bluetooth headset) and the route."""
    route = _demo_sink(DEMO_ROUTE, "StreamStack Route Sink", "route", "virtual", 100)
    route["virtual"] = True
    return [
        _demo_sink(
            "alsa_output.demo-wave-xlr.analog-stereo",
            "Elgato Wave XLR (demo) Analog Stereo",
            "headphones",
            "usb",
            55,
            "running",
        ),
        _demo_sink(
            "alsa_output.demo-scarlett.analog-stereo",
            "Focusrite Scarlett 2i2 (demo) Analog Stereo",
            "interface",
            "usb",
            70,
        ),
        _demo_sink(
            "alsa_output.demo-monitor.hdmi-stereo", "Studio Monitor (demo) Digital Stereo (HDMI)", "hdmi", "pci", 40
        ),
        _demo_sink("bluez_output.demo-headset.1", "Evolve2 65 headset (demo)", "headset", "bluetooth", 60),
        route,
    ]


class DemoOutputs(Outputs):
    """The same actions as `Outputs` over four simulated sinks: nothing is probed, played or changed on the machine."""

    def __init__(self, notify: Notify | None = None, *, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(notify, store=None, clock=clock)
        self._world = demo_world()
        self._unplugged: set[str] = set()
        self._demo_default = self._world[0]["name"]
        self._publish()

    def start(self) -> None:
        return None

    def poll(self) -> None:
        return None

    def refresh(self) -> None:
        return None

    def _can_watch(self) -> bool:
        return False

    def _publish(self) -> None:
        """Present the plugged part of the simulated desk as a fresh listing (the default falls back when it goes)."""
        plugged = [s for s in self._world if s["name"] not in self._unplugged]
        default = self._demo_default
        if default not in {s["name"] for s in plugged}:
            default = self._demo_default = next((s["name"] for s in plugged if not s["virtual"]), "")
        self._apply(plugged, default, None)

    # -- no wpctl, no pw-play ------------------------------------------------------

    def _wpctl(self, args: list[str], what: str) -> None:
        if args[0] == "set-default":
            self._demo_default = next((s["name"] for s in self._world if str(s["id"]) == args[1]), self._demo_default)

    def _need_player(self) -> None:
        return None

    def _prepare(self) -> Path | None:
        return None

    def _play(self, sink: str, path: Path | None, stop: threading.Event) -> str | None:
        stop.wait(DEMO_PLAY_S)
        return None

    def _save_results(self) -> None:
        return None

    # -- demo only: plug and unplug, and the reset the e2e suite needs -------------

    def act(self, action: str, params: dict[str, Any]) -> str | None:
        if action == "audio.demo_output_plug":
            return self._demo_plug(params)
        if action == "audio.demo_output_reset":
            return self._demo_reset()
        return super().act(action, params)

    def _demo_plug(self, params: dict[str, Any]) -> str | None:
        name, plugged = params.get("name"), params.get("plugged")
        sink = next((s for s in self._world if s["name"] == name and not s["virtual"]), None)
        if sink is None or not isinstance(plugged, bool):
            raise ActionError("name must be a demo output and plugged true or false", 400)
        with self._lock:
            if plugged:
                self._unplugged.discard(sink["name"])
            else:
                self._unplugged.add(sink["name"])
        self._publish()
        return f"Demo: {sink['label']} {'plugged in' if plugged else 'unplugged'}"

    def _demo_reset(self) -> str | None:
        self._test_stop.set()
        thread = self._test_thread
        if thread is not None:
            thread.join(timeout=5)
        fresh = demo_world()
        with self._lock:
            self._world[:] = fresh
            self._unplugged.clear()
            self._results.clear()
            self._lost = None
            self._seen_default = ""
            self._demo_default = fresh[0]["name"]
            self._default = ""
        self._publish()
        return None
