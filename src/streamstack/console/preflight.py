"""The Console's pre-flight: one press tests the whole rig and scores how ready it is for a meeting.

`PreflightController` runs `streamstack.preflight` (the pure checks) against the live rig:

1. **Listen** (a few seconds): the person says their name; the raw microphone meter's levels give the noise floor, the
   signal to noise ratio and the clip headroom. The levels come from the Console's own meter loop (`on_meters`), so
   nothing new is opened and nothing is recorded.
2. **Gather**: the state snapshot, whether a face is in the picture (the framing controller's own detector), the
   network monitor's numbers when one exists, the free disk. Each source has a hard timeout; one that fails simply
   leaves its facts out and the check says so.
3. **Judge**: the seven checks run in parallel, each with its own timeout (a check that cannot answer is a warning,
   never a hang). The result is a 0-100 score, a verdict and a plain-language reason per check.

Pre-flight never changes anything on its own. A finding that one existing action can repair carries that action; the
`preflight.fix` action runs it (the Fix button) and re-judges. It never opens the camera: a call app may hold it.

Actions: `preflight.run` (`listen` false skips the spoken prompt), `preflight.fix` (`check`), and, on the demo rig only,
`preflight.demo_fault` / `preflight.demo_reset` which inject the faults the end-to-end tests exercise. The last 30 runs
are kept in `console-preflight.json` (score, verdict and each check's state; no audio, no picture).
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime
from pathlib import Path
from typing import Any

from streamstack import config, preflight
from streamstack import store as filestore
from streamstack.console.audio import bool_param
from streamstack.console.errors import ActionError
from streamstack.preflight import Check, Facts, MicProbe, NetworkFacts

log = logging.getLogger(__name__)

Notify = Callable[[str, str], None]

HISTORY_FILE = "console-preflight.json"
HISTORY_MAX = 30
LISTEN_S = 6.0  # the spoken prompt
BUDGET_S = 20.0  # the whole run, on the real rig
SOURCE_TIMEOUT_S = 4.0  # each fact source and each check
PROMPT = "Say your name"
FAULTS = ("noise_floor", "no_voice", "clipping", "no_face", "jitter", "gpu_busy")
ACTIONS = ("preflight.run", "preflight.fix", "preflight.demo_fault", "preflight.demo_reset")
FIX_ACTIONS = frozenset(
    {
        "studio.start",
        "video.privacy",
        "hardware.refresh",
        "audio.revive",
        "audio.mute",
        "audio.clip_clear",
        "audio.output_default",
        "audio.output_mute",
        "audio.output_volume",
        "lighting.rescan",
        "lighting.power",
        "lighting.brightness",
        "lighting.temperature",
    }
)


def live_history_path() -> Path:
    return config.state_dir() / HISTORY_FILE


def _iso(wall: float | None) -> str | None:
    return None if wall is None else datetime.fromtimestamp(wall).isoformat(timespec="seconds")


def empty_state() -> dict[str, Any]:
    """The `preflight` object of a rig without pre-flight."""
    return {
        "available": False,
        "state": "idle",
        "phase": None,
        "prompt": PROMPT,
        "listen_s": LISTEN_S,
        "budget_s": BUDGET_S,
        "started_at": None,
        "started_ts": None,
        "result": None,
        "history": [],
        "faults": [],
        "demo": False,
    }


def face_found(framing: Mapping[str, Any] | None) -> bool | None:
    """Is a face in the on-air picture? None when auto-framing is off or cannot say (nothing is judged then)."""
    if not framing or not framing.get("available") or framing.get("mode") == "off":
        return None
    sources = [x for x in framing.get("sources") or [] if isinstance(x, Mapping) and x.get("on_air")]
    if not sources:
        return None
    states = {x.get("state") for x in sources}
    if "tracking" in states:
        return True
    return False if states == {"no-face"} else None


class PreflightController:
    """Owns one pre-flight run at a time, its last result and the history."""

    def __init__(
        self,
        *,
        history_path: Path | None = None,
        notify: Notify | None = None,
        listen_s: float = LISTEN_S,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        network: Callable[[], NetworkFacts | None] | None = None,
        face: Callable[[], bool | None] | None = None,
        disk: Callable[[], float | None] | None = None,
    ) -> None:
        self.notify = notify
        self.snapshot: Callable[[], dict[str, Any]] = dict  # the rig binds its own
        self.run_action: Callable[[str, dict[str, Any]], str | None] | None = None
        self.listen_s = listen_s
        self._clock, self._wall = clock, wall
        self._network, self._face, self._disk = network, face, disk
        self._path = history_path
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()  # one history write at a time, held without the state lock (#166)
        self._levels: list[tuple[float, float]] = []
        self._listening = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = "idle"
        self._phase: str | None = None
        self._started_wall: float | None = None
        self._result: dict[str, Any] | None = None
        self._facts: Facts | None = None
        self._history: list[dict[str, Any]] = self._load_history()

    # -- wiring ----------------------------------------------------------------------

    def bind(self, snapshot: Callable[[], dict[str, Any]], act: Callable[[str, dict[str, Any]], str | None]) -> None:
        self.snapshot, self.run_action = snapshot, act

    def start(self) -> None:
        self._stop.clear()  # a controller can be started again after a stop

    def stop(self) -> None:
        self._stop.set()
        self.join(SOURCE_TIMEOUT_S + 1.0)

    def join(self, timeout: float = SOURCE_TIMEOUT_S + 2.0) -> None:
        """Wait for a running check to end (the demo reset does, so a late result cannot land after it)."""
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def on_meters(self, frame: Mapping[str, Any]) -> None:
        """The raw microphone's levels, from the hub's meter loop; kept only while the prompt is listening."""
        if not self._listening:
            return
        mic = frame.get("mic")
        if not isinstance(mic, Mapping):
            return
        rms, peak = mic.get("rms_db"), mic.get("peak_db", mic.get("sample_db"))
        if isinstance(rms, (int, float)) and isinstance(peak, (int, float)):
            with self._lock:
                self._levels.append((float(rms), float(peak)))

    # -- history ---------------------------------------------------------------------

    def _load_history(self) -> list[dict[str, Any]]:
        if self._path is None:
            return []
        saved = filestore.read_json(self._path, {}, validate=filestore.is_object)
        runs = saved.get("runs")
        if not isinstance(runs, list):
            return []
        return [r for r in runs if isinstance(r, dict) and isinstance(r.get("score"), int)][-HISTORY_MAX:]

    def _save_history(self) -> None:
        if self._path is None:
            return
        # Snapshot inside the save lock so writes only go forward: a slow save can no longer land after a newer one.
        with self._save_lock:
            with self._lock:
                runs = list(self._history)
            try:
                filestore.write_json(self._path, {"runs": runs})
            except OSError as exc:
                log.warning("cannot save %s: %s", self._path, exc)

    # -- state -----------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "available": True,
                "state": self._state,
                "phase": self._phase,
                "prompt": PROMPT,
                "listen_s": self.listen_s,
                "budget_s": BUDGET_S,
                "started_at": _iso(self._started_wall),
                "started_ts": self._started_wall,
                "result": self._result,
                "history": list(reversed(self._history)),
                "faults": [],
                "demo": False,
            }

    # -- actions ---------------------------------------------------------------------

    def act(self, action: str, params: dict[str, Any]) -> str | None:
        if action == "preflight.run":
            listen = bool_param(params, "listen") if "listen" in params else True
            return self._start(listen)
        if action == "preflight.fix":
            return self._fix(params)
        raise ActionError(f"unknown pre-flight action {action!r}", 400)

    def _start(self, listen: bool) -> str:
        with self._lock:
            if self._state == "running":
                raise ActionError("A pre-flight check is already running.", 409)
            self._state, self._phase = "running", "listening" if listen else "checking"
            self._started_wall = self._wall()
            self._levels = []
        self._thread = threading.Thread(target=self._run, args=(listen,), name="preflight", daemon=True)
        self._thread.start()
        return "Pre-flight started. Say your name." if listen else "Pre-flight started."

    def _run(self, listen: bool) -> None:
        started = self._clock()
        try:
            probe = self._listen() if listen else None
            with self._lock:
                self._phase = "checking"
            facts = self._gather(probe, deadline=started + BUDGET_S)
            checks = self._judge(facts)
            self._finish(facts, checks, self._clock() - started)
        except Exception as exc:  # a bug in a check must end the run with a message, never leave it "running"
            log.exception("pre-flight failed")
            with self._lock:
                self._state, self._phase = "idle", None
            self._tell("error", f"Pre-flight could not finish: {type(exc).__name__}: {exc}")

    def _listen(self) -> MicProbe:
        with self._lock:
            self._levels = []
            self._listening = True
        self._stop.wait(self.listen_s)
        with self._lock:
            self._listening = False
            levels = list(self._levels)
        return self._probe(levels)

    def _probe(self, levels: list[tuple[float, float]]) -> MicProbe:
        return preflight.mic_probe(levels)

    # -- facts and checks ------------------------------------------------------------

    def _gather(self, probe: MicProbe | None, *, deadline: float) -> Facts:
        """Every fact source in parallel, each with a hard timeout; a source that fails leaves its facts unknown."""
        sources: dict[str, Callable[[], Any]] = {"state": self.snapshot}
        if self._network is not None:
            sources["network"] = self._network
        if self._face is not None:
            sources["face"] = self._face
        sources["disk"] = self._disk or self._free_disk
        got: dict[str, Any] = {}
        pool = ThreadPoolExecutor(max_workers=len(sources), thread_name_prefix="preflight-facts")
        try:
            futures = {name: pool.submit(fn) for name, fn in sources.items()}
            for name, future in futures.items():
                budget = max(0.1, min(SOURCE_TIMEOUT_S, deadline - self._clock()))
                try:
                    got[name] = future.result(timeout=budget)
                except FutureTimeout:
                    log.warning("pre-flight: %s took longer than %.1f s; left out", name, budget)
                except Exception:  # one failing source must not stop the others
                    log.exception("pre-flight: %s failed; left out", name)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return Facts(
            state=got.get("state") or {},
            mic=probe,
            network=got.get("network"),
            face=got.get("face"),
            gpu_percent=self._gpu(got.get("state") or {}),
            disk_free_gb=got.get("disk"),
        )

    def _gpu(self, state: Mapping[str, Any]) -> float | None:
        return None  # the stream backend reports it in the snapshot; the check reads that

    @staticmethod
    def _free_disk() -> float | None:
        try:
            return shutil.disk_usage(Path.home()).free / 1e9
        except OSError:
            return None

    def _judge(self, facts: Facts) -> list[Check]:
        """The checks in parallel; one that cannot answer in time is a warning that says so."""
        pool = ThreadPoolExecutor(max_workers=len(preflight.CHECKS), thread_name_prefix="preflight-check")
        checks: list[Check] = []
        try:
            futures = [(cid, pool.submit(fn, facts)) for cid, fn in preflight.CHECKS]
            for cid, future in futures:
                try:
                    checks.append(future.result(timeout=SOURCE_TIMEOUT_S))
                except FutureTimeout:
                    checks.append(Check(cid, preflight.WARN, "This check did not answer in time."))
                except Exception as exc:  # a broken check is reported, the others still count
                    log.exception("pre-flight check %s failed", cid)
                    checks.append(Check(cid, preflight.WARN, f"This check failed to run ({type(exc).__name__})."))
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return checks

    def _finish(self, facts: Facts, checks: list[Check], took: float) -> None:
        summary = preflight.summary(checks)
        result = {
            "at": _iso(self._wall()),
            "took_s": round(took, 1),
            **summary,
            "checks": [c.public() for c in checks],
        }
        entry = {
            "at": result["at"],
            "score": summary["score"],
            "verdict": summary["verdict"],
            "checks": {c.id: c.state for c in checks},
        }
        level = {"ready": "ok", "check": "warn", "not-ready": "error"}[summary["verdict"]]
        # Tell the person before the state flips to done: whoever sees "done" (a client, a test) can rely on the toast.
        self._tell(level, f"Pre-flight {summary['score']}/100. {summary['text']}")
        with self._lock:
            self._history = [*self._history, entry][-HISTORY_MAX:]
        # "done" means saved too: whoever sees it (the next run, a restart, a test) finds this run on disk (#166).
        self._save_history()
        with self._lock:
            self._facts, self._result = facts, result
            self._state, self._phase = "done", None

    def _tell(self, level: str, text: str) -> None:
        if self.notify is not None:
            self.notify(level, text)

    # -- fixes -----------------------------------------------------------------------

    def _fix(self, params: dict[str, Any]) -> str | None:
        check_id = params.get("check")
        if not isinstance(check_id, str) or check_id not in preflight.WEIGHTS:
            raise ActionError("check must be one of: " + ", ".join(preflight.WEIGHTS), 400)
        with self._lock:
            result, facts, busy = self._result, self._facts, self._state == "running"
        if busy:
            raise ActionError("Wait for the running pre-flight check to finish.", 409)
        entry = next((c for c in (result or {}).get("checks", []) if c["id"] == check_id), None)
        if entry is None:
            raise ActionError("Run a pre-flight check first.", 409)
        fix = entry.get("fix")
        if not fix:
            raise ActionError(f"There is no one-press fix for {entry['title']}.", 409)
        if fix["action"] not in FIX_ACTIONS or self.run_action is None:
            raise ActionError("That fix is not available here.", 409)
        message = self.run_action(fix["action"], dict(fix["params"]))
        self._rejudge(facts)
        return message or f"Done: {fix['label']}."

    def _rejudge(self, old: Facts | None) -> None:
        """After a fix: the same measurements (the voice is not asked for again), fresh state."""
        facts = self._gather(old.mic if old else None, deadline=self._clock() + BUDGET_S)
        if old is not None:
            facts = Facts(
                state=facts.state,
                mic=old.mic,
                network=facts.network if facts.network is not None else old.network,
                face=facts.face if facts.face is not None else old.face,
                exposure_clipped_pct=old.exposure_clipped_pct,
                gpu_percent=facts.gpu_percent,
                disk_free_gb=facts.disk_free_gb,
            )
        checks = self._judge(facts)
        summary = preflight.summary(checks)
        with self._lock:
            previous = self._result or {}
            self._facts = facts
            self._result = {
                **previous,
                **summary,
                "at": _iso(self._wall()),
                "checks": [c.public() for c in checks],
            }
            if self._history:
                self._history[-1] = {
                    **self._history[-1],
                    "score": summary["score"],
                    "verdict": summary["verdict"],
                    "checks": {c.id: c.state for c in checks},
                }
        self._save_history()


# ---------------------------------------------------------------------------
# The simulated twin
# ---------------------------------------------------------------------------

DEMO_LISTEN_S = 0.6


class DemoPreflight(PreflightController):
    """A pre-flight over the demo rig: the measurements are scripted, `preflight.demo_fault` injects each fault."""

    def __init__(self, notify: Notify | None = None, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.faults: set[str] = set()
        super().__init__(
            notify=notify,
            listen_s=DEMO_LISTEN_S,
            clock=clock,
            network=self._demo_network,
            face=self._demo_face,
            disk=lambda: 120.0,
        )

    def state(self) -> dict[str, Any]:
        return {**super().state(), "faults": sorted(self.faults), "demo": True}

    def _demo_network(self) -> NetworkFacts:
        if "jitter" in self.faults:
            return NetworkFacts(rtt_ms=42.0, jitter_ms=300.0, loss_pct=0.4, up_mbps=11.0, down_mbps=80.0)
        return NetworkFacts(rtt_ms=28.0, jitter_ms=4.0, loss_pct=0.0, up_mbps=11.0, down_mbps=80.0)

    def _demo_face(self) -> bool:
        return "no_face" not in self.faults

    def _gpu(self, state: Mapping[str, Any]) -> float | None:
        return 96.0 if "gpu_busy" in self.faults else 31.0

    def _probe(self, levels: list[tuple[float, float]]) -> MicProbe:
        faults = self.faults
        floor = -34.0 if "noise_floor" in faults else -62.0
        if "no_voice" in faults:
            return MicProbe(floor, floor + 3.0, floor + 6.0, False, 30)
        peak = -0.3 if "clipping" in faults else -7.0
        return MicProbe(floor, -21.0, peak, True, 30)

    def act(self, action: str, params: dict[str, Any]) -> str | None:
        if action == "preflight.demo_fault":
            fault = params.get("fault")
            if fault not in FAULTS:
                raise ActionError("fault must be one of: " + ", ".join(FAULTS), 400)
            if bool_param(params) if "on" in params else True:
                self.faults.add(fault)
            else:
                self.faults.discard(fault)
            return None
        if action == "preflight.demo_reset":
            self.join()
            self.faults.clear()
            with self._lock:
                self._state, self._phase, self._result, self._facts = "idle", None, None, None
                self._history = []
                self._started_wall = None
            return None
        return super().act(action, params)
