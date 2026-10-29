"""The Console's echo check: does a speaker bleed into the microphone?

One press (`echo.check`) plays a low-level sweep on the chosen output, records the raw microphone and measures the
coupling gain and delay (`streamstack.echocheck`: the DSP). The answer names the output: "No bleed from Dell
Monitor", or "Bleed at -34 dB from Dell Monitor: use headphones or lower the volume", or a feedback loop (a monitor
mix or an effects chain sending the output back into the microphone).

The rules of the output test (#36) apply, and one more:

* it tests only the sink you chose (the default output when you name none) with `pw-play --target`; the default output,
  every volume and every route stay where they are;
* it **never plays during a call**: it refuses to start while `rig.call.active()` (call awareness, #45), and a call that
  starts mid-check stops the sound at once;
* it reads the network monitor and the SRT path stats (`link_context`) and never blames or clears a verdict on a bad
  link: a degraded network or a stalling stream is named in the answer (the sweep itself is local, so it still runs);
* it does not unmute anything: a muted or absent microphone, a muted or silent output and a running output test are
  refused with the reason, so the one thing it plays is the one thing it was asked to.

The last result per output is remembered in `console-echo.json` (the Pre-flight check reads it).
"""

from __future__ import annotations

import contextlib
import copy
import logging
import math
import os
import struct
import tempfile
import threading
import time
import wave
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from streamstack import config, echocheck, system
from streamstack import store as filestore
from streamstack.console.audio import number_param
from streamstack.console.errors import ActionError
from streamstack.saving import LatestWriter

log = logging.getLogger(__name__)

Notify = Callable[[str, str], None]
Snapshot = Callable[[], Mapping[str, Any]]

STORE_FILE = "console-echo.json"
PLAY_RATE = 48000
LEAD_S = 0.3  # the recorder runs this long before the sweep starts
TAIL_S = 0.7  # and this long after it ends (a feedback loop's second copy lands here)
RECORD_S = LEAD_S + echocheck.SWEEP_S + TAIL_S + 0.2
PLAY_TIMEOUT_S = echocheck.SWEEP_S + 6.0
MAX_RESULTS = 16
SILENCE = 1e-6  # RMS of a recording that is digital silence (a muted microphone)
QUIET_KINDS = ("headphones", "headset")
LINK_RATINGS = ("degraded", "poor", "offline")
STALLING_STATES = ("backoff", "failed")
SRT_LOSS_PERCENT = 10.0  # probe loss at which a path counts as lossy (the adaptive picture's "severe" is 8)
SRT_RTT_MS = 400.0  # and a round trip this long as slow (the network monitor's "poor")
LEVELS = ("none", "slight", "moderate", "strong", "feedback", "unmeasured")


class CaptureError(Exception):
    """The sweep could not be played or recorded; the message is for the person."""


def live_store_path() -> Path:
    return config.state_dir() / STORE_FILE


def empty_state() -> dict[str, Any]:
    """The `echo` object of a rig without the echo check."""
    return {
        "available": False,
        "state": "idle",
        "sink": None,
        "started_at": None,
        "blocked": None,
        "result": None,
        "results": {},
        "error": None,
        "demo": False,
    }


def _iso(wall: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(wall))


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _dig(state: Mapping[str, Any], *path: str) -> Any:
    node: Any = state
    for key in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def link_context(state: Mapping[str, Any]) -> dict[str, Any]:
    """Is the link clean right now? Reads the network monitor and every live destination (SRT path stats included).

    `level` is "ok" or "caveat"; `reasons` are sentences for the person. A monitor that is off or absent, a recording
    and a destination that is not running say nothing: a signal that is missing is not judged."""
    reasons: list[str] = []
    net = _dig(state, "network")
    if isinstance(net, Mapping) and net.get("available") and net.get("enabled"):
        rating = net.get("rating")
        if rating in LINK_RATINGS:
            cause = net.get("cause")
            reasons.append(f"the network is {rating}" + (f" ({cause})" if isinstance(cause, str) and cause else ""))
    destinations = _dig(state, "stream", "destinations")
    for dest in destinations if isinstance(destinations, list) else ():
        if not isinstance(dest, Mapping) or not dest.get("active") or dest.get("kind") == "record":
            continue
        name = str(dest.get("name") or dest.get("id") or "a destination")
        if dest.get("state") in STALLING_STATES:
            reasons.append(f"the stream to {name} is stalling ({dest['state']})")
            continue
        srt = dest.get("srt")
        if not isinstance(srt, Mapping):
            continue
        loss, rtt = _number(srt.get("probe_loss_percent")), _number(srt.get("rtt_ms"))
        if loss is not None and loss >= SRT_LOSS_PERCENT:
            reasons.append(f"the SRT path to {name} loses {loss:.0f} % of its probes")
        elif rtt is not None and rtt >= SRT_RTT_MS:
            reasons.append(f"the SRT path to {name} has a {rtt:.0f} ms round trip")
    return {"level": "caveat" if reasons else "ok", "reasons": reasons}


def merge_context(*contexts: Mapping[str, Any]) -> dict[str, Any]:
    """The link as it was at the start and at the end of a check: a bad moment at either end counts."""
    reasons: list[str] = []
    for context in contexts:
        for reason in context.get("reasons") or ():
            if isinstance(reason, str) and reason not in reasons:
                reasons.append(reason)
    return {"level": "caveat" if reasons else "ok", "reasons": reasons}


def with_caveat(text: str, level: str, link: Mapping[str, Any]) -> str:
    """The verdict, with the link named when it is not clean, so a verdict is never blamed on or cleared by it."""
    reasons = link.get("reasons") or []
    if not reasons or level == "unmeasured":
        return text
    named = "; ".join(reasons)
    if level in ("none", "slight"):
        return (
            f"{text} The link is not clean ({named}), so what the other side hears may come from it, "
            "not from echo: this check only clears the speaker."
        )
    return (
        f"{text} The link is also not clean ({named}): delay or choppiness on the other side may come from it "
        "as well as from the bleed."
    )


def write_sweep(path: Path, rate: int = PLAY_RATE) -> None:
    """The sweep as a mono 16-bit WAV at the player's rate, at PLAY_LEVEL_DB of full scale."""
    samples = echocheck.chirp(rate, level_db=echocheck.PLAY_LEVEL_DB)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"".join(struct.pack("<h", round(max(-1.0, min(1.0, v)) * 32767)) for v in samples))


def read_wav(path: Path) -> tuple[list[float], int]:
    """A recording as one channel of floats in [-1, 1] and its rate (16- and 32-bit integer PCM)."""
    try:
        with wave.open(str(path), "rb") as src:
            channels, width, rate, frames = src.getnchannels(), src.getsampwidth(), src.getframerate(), src.getnframes()
            raw = src.readframes(frames)
    except (wave.Error, EOFError, OSError) as exc:
        raise CaptureError(f"The recording could not be read: {exc}") from exc
    if width not in (2, 4):
        raise CaptureError(f"The recording has an unsupported sample size ({width * 8} bit).")
    fmt, scale = ("h", 32768.0) if width == 2 else ("i", 2147483648.0)
    values = struct.unpack(f"<{len(raw) // width}{fmt}", raw[: len(raw) // width * width])
    return [v / scale for v in values[::channels]], rate


def _read_link(raw: Any) -> dict[str, Any]:
    reasons = [r for r in raw.get("reasons", []) if isinstance(r, str)] if isinstance(raw, dict) else []
    return {"level": "caveat" if reasons else "ok", "reasons": reasons}


class EchoController:
    """The check, its refusals, the result and the remembered results."""

    def __init__(
        self,
        notify: Notify | None = None,
        *,
        call: Any = None,
        store_path: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.notify = notify
        self.call = call  # anything with active() and snapshot(): rig.call
        self._clock, self._wall = clock, wall
        self._lock = threading.RLock()
        self._store = store_path
        self._writer: LatestWriter[dict[str, Any]] = LatestWriter(lambda doc: filestore.write_json(self._store, doc))
        self._snapshot: Snapshot | None = None
        self._state = "idle"
        self._sink: str | None = None
        self._started_wall: float | None = None
        self._result: dict[str, Any] | None = None
        self._error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._results = self._read_results()

    def bind(self, snapshot: Snapshot) -> None:
        self._snapshot = snapshot

    def start(self) -> None:
        return None

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=PLAY_TIMEOUT_S + 5)

    # -- state -----------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        blocked = self._call_reason()
        with self._lock:
            return {
                "available": True,
                "state": self._state,
                "sink": self._sink,
                "started_at": None if self._started_wall is None else _iso(self._started_wall),
                "blocked": blocked,
                "result": self._result,
                "results": {k: dict(v) for k, v in self._results.items()},
                "error": self._error,
                "demo": False,
            }

    def last_result(self, sink: str | None = None) -> dict[str, Any] | None:
        """The remembered result for an output (or the newest of all): what Pre-flight reads."""
        with self._lock:
            if sink is not None:
                return self._results.get(sink)
            newest = sorted(self._results.values(), key=lambda r: r["at"])
            return dict(newest[-1]) if newest else None

    def _call_reason(self) -> str | None:
        if self.call is None or not self.call.active():
            return None
        service = getattr(self.call.snapshot(), "service", None)
        return f"{service or 'A call'} is on. The echo check plays a sound, so it never runs during a call."

    # -- the action ------------------------------------------------------------------

    def act(self, action: str, params: dict[str, Any]) -> str | None:
        if action == "echo.check":
            return self._act_check(params)
        if action == "echo.stop":
            self._stop.set()
            return None
        raise ActionError(f"unknown echo action {action!r}", 400)

    def _state_now(self) -> Mapping[str, Any]:
        if self._snapshot is None:
            return {}
        try:
            return self._snapshot()
        except Exception:  # the snapshot is the whole rig; one controller failing must not break this check
            log.exception("echo check: cannot read the rig state")
            return {}

    def _choose_sink(self, params: dict[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
        devices = [d for d in (_dig(state, "audio", "outputs", "devices") or []) if isinstance(d, Mapping)]
        name = params.get("name")
        if name is None:
            name = _dig(state, "audio", "outputs", "default")
        if not isinstance(name, str) or not name:
            raise ActionError("There is no default output to test. Name one with `name`.", 409)
        sink = next((d for d in devices if d.get("name") == name), None)
        if sink is None:
            raise ActionError("That output is not connected (or it is a route, which has no speaker to test).", 409)
        return dict(sink)

    def _refuse(self, state: Mapping[str, Any], sink: dict[str, Any]) -> None:
        if reason := self._call_reason():
            raise ActionError(reason, 409)
        if _dig(state, "audio", "outputs", "test", "state") == "playing":
            raise ActionError("An output test is playing. Wait for it to finish.", 409)
        label = sink.get("label") or sink.get("name")
        if sink.get("muted") or sink.get("volume") == 0:
            raise ActionError(f"{label} is muted or at zero volume, so nothing would play. Unmute it first.", 409)
        mic = _dig(state, "audio", "mic")
        if not isinstance(mic, Mapping) or not mic.get("present") or not mic.get("name"):
            raise ActionError("No microphone found to listen with.", 409)
        if mic.get("muted"):
            raise ActionError("The microphone is muted, so it cannot hear the sound. Unmute it first.", 409)
        if mic.get("stalled"):
            raise ActionError("The microphone is stalled. Revive it first.", 409)

    def _act_check(self, params: dict[str, Any]) -> str | None:
        unknown = sorted(set(params) - {"name"})
        if unknown:
            raise ActionError(f"unknown echo option {unknown[0]!r}", 400)
        state = self._state_now()
        sink = self._choose_sink(params, state)
        self._refuse(state, sink)
        self._need_tools()
        source = _dig(state, "audio", "mic", "name")
        began = link_context(state)
        with self._lock:
            if self._state == "running":
                raise ActionError("An echo check is already running.", 409)
            self._state, self._sink, self._error = "running", sink["name"], None
            self._started_wall = self._wall()
            self._stop = threading.Event()
        thread = threading.Thread(target=self._run, args=(sink, source, began), name="echo-check", daemon=True)
        self._thread = thread
        thread.start()
        label = sink.get("label") or sink["name"]
        return f"Playing a quiet sweep on {label} and listening: stay quiet for a few seconds"

    def _need_tools(self) -> None:
        for tool in ("pw-play", "parecord"):
            if not system.have(tool):
                raise ActionError(f"{tool} is not installed, so the echo check cannot play or record.", 409)

    # -- the run ---------------------------------------------------------------------

    def _run(self, sink: dict[str, Any], source: str, began: Mapping[str, Any] | None = None) -> None:
        label = str(sink.get("label") or sink["name"])
        unsubscribe = None
        stop = self._stop
        if self.call is not None:  # a call that starts mid-check ends the sound at once

            def on_call(before: Any, after: Any) -> None:
                if getattr(after, "active", False):
                    stop.set()

            unsubscribe = self.call.subscribe(on_call)
        try:
            samples, rate, lead_s = self._capture(sink, source, stop)
            if stop.is_set():
                self._finish_error(
                    "Stopped: a call started." if self._call_reason() else "The echo check was stopped.", warn=True
                )
                return
            link = merge_context(began or {}, link_context(self._state_now()))
            self._finish(self._measure(samples, rate, lead_s, sink, label, link), label)
        except CaptureError as exc:
            self._finish_error(str(exc))
        except (ValueError, OSError) as exc:
            log.warning("echo check failed: %s", exc)
            self._finish_error(f"The echo check failed: {exc}")
        finally:
            if unsubscribe is not None:
                unsubscribe()

    def _capture(self, sink: dict[str, Any], source: str, stop: threading.Event) -> tuple[list[float], int, float]:
        """Play the sweep on `sink` while recording `source`: (samples, rate, seconds of recording before the sweep)."""
        fd, name = tempfile.mkstemp(prefix="streamstack-echo-", suffix=".wav")
        os.close(fd)
        out_fd, out_name = tempfile.mkstemp(prefix="streamstack-echo-rec-", suffix=".wav")
        os.close(out_fd)
        sweep, recorded = Path(name), Path(out_name)
        outcome: dict[str, bool] = {}
        try:
            write_sweep(sweep)

            def record() -> None:
                outcome["ok"] = system.record(source, RECORD_S, str(recorded))

            recorder = threading.Thread(target=record, name="echo-record", daemon=True)
            recorder.start()
            if not stop.wait(LEAD_S):
                if reason := self._call_reason():
                    raise CaptureError(reason)
                problem = system.play(sink["name"], str(sweep), PLAY_TIMEOUT_S, stop)
                if problem:
                    raise CaptureError(problem)
            recorder.join(timeout=RECORD_S + 8)
            if not outcome.get("ok"):
                raise CaptureError("Nothing was captured from the microphone.")
            samples, rate = read_wav(recorded)
            return samples, rate, LEAD_S
        finally:
            for path in (sweep, recorded):
                with contextlib.suppress(OSError):
                    path.unlink()

    def _measure(
        self,
        samples: list[float],
        rate: int,
        lead_s: float,
        sink: dict[str, Any],
        label: str,
        link: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        rms = math.sqrt(sum(v * v for v in samples) / len(samples)) if samples else 0.0
        if rms < SILENCE:
            result = echocheck.EchoResult(False, None, 0.0, None, 0.0, False, None, "unmeasured")
            text = f"The microphone delivered digital silence, so {label} could not be measured. Is it muted?"
        else:
            result = echocheck.analyze(samples, rate, lead_s=lead_s, play_db=echocheck.PLAY_LEVEL_DB)
            text = echocheck.describe(result, label)
        link = dict(link) if link is not None else {"level": "ok", "reasons": []}
        text = with_caveat(text, result.level, link)
        return {
            "at": _iso(self._wall()),
            "sink": sink["name"],
            "label": label,
            "kind": sink.get("kind"),
            "text": text,
            "link": link,
            **result.public(),
        }

    def _finish(self, result: dict[str, Any], label: str) -> None:
        with self._lock:
            self._result, self._state = result, "done"
            self._results[result["sink"]] = {
                k: result[k] for k in ("at", "label", "level", "coupling_db", "delay_ms", "loop", "text", "link")
            }
            for old in sorted(self._results, key=lambda n: self._results[n]["at"])[
                : max(0, len(self._results) - MAX_RESULTS)
            ]:
                del self._results[old]
            if self._store is not None:
                self._writer.publish({"results": copy.deepcopy(self._results)})
        clean = result["link"]["level"] == "ok"
        quiet = "ok" if clean else "warn"  # a clear verdict on a bad link is not good news to the person
        level = {"none": quiet, "slight": quiet, "moderate": "warn", "strong": "warn", "feedback": "error"}.get(
            result["level"], "warn"
        )
        self._tell(level, result["text"])
        self._save_results()  # last, and outside `_lock`: the result and its toast never wait for the disk (issue #168)

    def _finish_error(self, message: str, *, warn: bool = False) -> None:
        with self._lock:
            self._state, self._error = "error", message
        self._tell("warn" if warn else "error", message)

    def _tell(self, level: str, text: str) -> None:
        if self.notify is not None:
            self.notify(level, text)

    # -- remembered results ----------------------------------------------------------

    def _read_results(self) -> dict[str, dict[str, Any]]:
        if self._store is None:
            return {}
        data = filestore.read_json(self._store, None, validate=filestore.is_object)
        raw = data.get("results") if isinstance(data, dict) else None
        found: dict[str, dict[str, Any]] = {}
        for name, entry in raw.items() if isinstance(raw, dict) else ():
            if (
                isinstance(entry, dict)
                and isinstance(entry.get("at"), str)
                and entry.get("level") in LEVELS
                and isinstance(entry.get("text"), str)
            ):
                found[str(name)] = {
                    "at": entry["at"],
                    "label": str(entry.get("label", name)),
                    "level": entry["level"],
                    "coupling_db": _number(entry.get("coupling_db")),
                    "delay_ms": _number(entry.get("delay_ms")),
                    "loop": entry.get("loop") is True,
                    "text": entry["text"],
                    "link": _read_link(entry.get("link")),
                }
        return found

    def _save_results(self) -> None:
        if self._store is None:
            return
        try:
            self._writer.flush()
        except OSError as exc:
            log.warning("cannot save %s: %s", self._store, exc)
            self._tell("warn", f"The echo check result is shown but could not be saved: {exc.strerror or exc}")


# ---------------------------------------------------------------------------
# The simulated twin
# ---------------------------------------------------------------------------

DEMO_BLEED_DB = -34.0  # a speaker or a monitor on the desk
DEMO_DELAY_MS = 24.0


class DemoEcho(EchoController):
    """The demo rig's twin: the sweep is simulated through a loopback of known coupling and measured by the real DSP.

    Speakers and HDMI monitors bleed at DEMO_BLEED_DB, headphones and headsets do not; `echo.demo` scripts any other
    coupling, delay or feedback loop (tests and the e2e suite)."""

    def __init__(self, notify: Notify | None = None, **kwargs: Any) -> None:
        super().__init__(notify, **kwargs)
        self.script: dict[str, Any] = {}

    def state(self) -> dict[str, Any]:
        return {**super().state(), "demo": True}

    def _need_tools(self) -> None:
        return None  # the twin plays nothing

    def _capture(self, sink: dict[str, Any], source: str, stop: threading.Event) -> tuple[list[float], int, float]:
        if stop.wait(0.3):
            return [], echocheck.RATE, LEAD_S
        if reason := self._call_reason():
            raise CaptureError(reason)
        script = dict(self.script)
        if "coupling_db" in script:
            coupling = script["coupling_db"]
        else:
            coupling = None if sink.get("kind") in QUIET_KINDS else DEMO_BLEED_DB
        samples = echocheck.simulate(
            coupling_db=coupling,
            delay_ms=script.get("delay_ms", DEMO_DELAY_MS),
            lead_s=LEAD_S,
            tail_s=TAIL_S,
            loop_gap_ms=script.get("loop_gap_ms"),
            noise_db=script.get("noise_db", -70.0) - echocheck.PLAY_LEVEL_DB,
        )
        played = 10 ** (echocheck.PLAY_LEVEL_DB / 20)  # the simulation is of a full-scale sweep: scale it as played
        samples = [v * played for v in samples]
        return samples, echocheck.RATE, LEAD_S

    def act(self, action: str, params: dict[str, Any]) -> str | None:
        if action == "echo.demo":
            self._act_demo(params)
            return None
        if action == "echo.demo_reset":
            self._stop.set()
            with self._lock:
                self.script = {}
                self._state, self._sink, self._result, self._error = "idle", None, None, None
                self._results = {}
            return None
        return super().act(action, params)

    def _act_demo(self, params: dict[str, Any]) -> None:
        script: dict[str, Any] = {}
        if "coupling_db" in params:
            if params["coupling_db"] is not None:
                script["coupling_db"] = number_param(params, "coupling_db", low=-120.0, high=0.0)
            else:
                script["coupling_db"] = None
        for key, low, high in (("delay_ms", 0.0, 500.0), ("loop_gap_ms", 80.0, 800.0), ("noise_db", -120.0, -20.0)):
            if key in params and params[key] is not None:
                script[key] = number_param(params, key, low=low, high=high)
        with self._lock:
            self.script = script
