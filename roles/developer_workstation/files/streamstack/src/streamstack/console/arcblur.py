"""Background blur on the native video engine: the Console's view of the Arc segmenter.

`ArcBlur` owns what the engine's blur needs and the Console shows:

    probe     is there an Arc with OpenVINO? (once, off the state thread; `wait_probed` before the engine is built)
    feed      the `BlurFeed` the engine's child talks to; only exists when the probe found a GPU
    settings  on, strength, rate (persisted by `NativeVideo` in video.json)
    model     fetched the first time blur is switched on (streamstack.engine.segment.ensure_model)

and produces the `video.blur` state block, the former controller's shape plus `native: True` and an `arc` block:

    arc.state    checking | unavailable | idle | downloading | loading | running | stalled | failed
    arc.reason   why it is unavailable or failed, one sentence, never a traceback
    arc.device   "Intel Arc A380"; arc.fps / infer_ms / infer_p50_ms / age_ms are measured, not nominal

Nothing here may raise into the state thread or the engine: a missing package, a missing GPU, a failed download and a
crashed inference are all states the page shows as "Unavailable: <reason>".

`DemoArc` is the same state on the simulated rig, driven by the demo-only action `video.blur.arc`.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from streamstack import blur
from streamstack.console import blur as blurctl
from streamstack.engine import segment
from streamstack.engine.blurfeed import BlurFeed
from streamstack.engine.graph import BlurGraph
from streamstack.engine.segment import Arc, Segmenter
from streamstack.framing.model import ModelError
from streamstack.hardware import roles
from streamstack.hardware.plan import GpuRole

log = logging.getLogger(__name__)

PROBE_WAIT = 5.0  # seconds the engine start waits for the probe: OpenVINO takes about one to open
OFF_REASON = "background blur is switched off in the settings (STREAMSTACK_ENGINE_BLUR=off)"
STATES = ("checking", "unavailable", "idle", "downloading", "loading", "running", "stalled", "failed")

Opener = Callable[[Arc], Segmenter]


def _open_arc_segmenter(arc: Arc) -> Segmenter:
    return segment.ArcSegmenter(segment.ensure_model(), arc, cache=segment.models_dir() / "cache")


class ArcBlur:
    """The blur controller for one NativeVideo."""

    def __init__(
        self,
        cfg: dict[str, str],
        *,
        probe: Callable[[str], Arc] | None = None,
        open_segmenter: Opener = _open_arc_segmenter,
        have_model: Callable[[], bool] | None = None,
        plan: roles.PlanSource | None = None,
    ) -> None:
        self._cfg = cfg
        self._probe = probe  # None: the real OpenVINO probe, steered by the Plan's blur role
        self._plan = plan or (lambda: None)
        self._open = open_segmenter
        self._have_model = have_model or _model_present
        self._lock = threading.RLock()
        self._probed = threading.Event()
        self._arc: Arc | None = None
        self._thread: threading.Thread | None = None
        self._feed: BlurFeed | None = None
        self._phase = ""  # "downloading" while the model is fetched, then "loading" while it compiles
        self.on = False
        self.strength = blur.DEFAULT_STRENGTH
        self.rate = blur.DEFAULT_RATE
        self.graph = BlurGraph()

    # -- availability --------------------------------------------------------------------------------------------

    @property
    def switched_off(self) -> bool:
        return (self._cfg.get("STREAMSTACK_ENGINE_BLUR") or "auto").strip().lower() in ("off", "false", "0", "no")

    def start(self) -> None:
        """Begin the device probe in the background (idempotent)."""
        with self._lock:
            if self._thread is not None or self._probed.is_set():
                return
            if self.switched_off:
                self._arc = Arc(False, OFF_REASON)
                self._probed.set()
                return
            self._thread = threading.Thread(target=self._run_probe, daemon=True, name="streamstack-blur-probe")
            self._thread.start()

    def _run_probe(self) -> None:
        wanted = (self._cfg.get("STREAMSTACK_ENGINE_BLUR_DEVICE") or "auto").strip()
        try:
            if self._probe is not None:
                arc = self._probe(wanted)
            else:
                role = self._plan_role()
                arc = segment.probe(wanted=wanted, pci_id=role.pci_id if role else "")
        except Exception as exc:  # the probe promises not to raise; a bug in it must still not take the Console down
            log.exception("blur: the device probe failed")
            arc = Arc(False, f"the GPU check failed: {exc}")
        with self._lock:
            self._arc = arc
        if not arc.ok:
            log.info("background blur is unavailable: %s", arc.reason)
        self._probed.set()

    def _plan_role(self) -> GpuRole | None:
        """The GPU the hardware Plan gave the blur role (found by PCI id), unless a render node is set by hand."""
        if (self._cfg.get("STREAMSTACK_ENGINE_RENDER_NODE") or "").strip():
            return None
        plan = self._plan()
        return plan.gpu("blur") if plan is not None else None

    def wait_probed(self, timeout: float | None = None) -> bool:
        self.start()
        return self._probed.wait(PROBE_WAIT if timeout is None else timeout)

    @property
    def capable(self) -> bool:
        arc = self._arc
        return bool(arc and arc.ok)

    @property
    def reason(self) -> str | None:
        """Why blur cannot run, or None when it can (or the check is still running)."""
        arc = self._arc
        if arc is None:
            return None
        if not arc.ok:
            return arc.reason
        with self._lock:
            feed = self._feed
        stats = feed.stats() if feed is not None else None
        if stats and stats["state"] == "failed":
            return stats["error"] or "the segmenter stopped"
        return None

    def feed(self) -> BlurFeed | None:
        """The engine's feed, created on first use once the probe says there is an Arc."""
        with self._lock:
            arc = self._arc
            if arc is None or not arc.ok:
                return None
            if self._feed is None:
                self._feed = BlurFeed(self.graph, lambda: self._open_for(arc))
            return self._feed

    def _open_for(self, arc: Arc) -> Segmenter:
        """Runs on the inference worker, so a download or a slow compile never blocks the Console."""
        try:
            self._phase = "loading" if self._have_model() else "downloading"
            return self._open(arc)
        finally:
            self._phase = ""

    def stop(self) -> None:
        with self._lock:
            feed = self._feed
        if feed is not None:
            feed.stop()

    # -- settings ------------------------------------------------------------------------------------------------

    def configure(self, on: bool | None = None, strength: int | None = None, rate: int | None = None) -> None:
        with self._lock:
            if on is not None:
                self.on = on
            if strength is not None:
                self.strength = strength
            if rate is not None:
                self.rate = rate

    def settings(self) -> dict[str, Any]:
        return {"on": self.on, "strength": self.strength, "rate": self.rate}

    def restore(self, saved: object) -> None:
        """Settings from video.json; anything malformed is ignored (a hand-edited file must not stop the Console)."""
        if not isinstance(saved, dict):
            return
        try:
            on = saved.get("on")
            self.configure(
                on if isinstance(on, bool) else None,
                blur.validate_strength(saved["strength"]) if saved.get("strength") is not None else None,
                blur.validate_rate(saved["rate"]) if saved.get("rate") is not None else None,
            )
        except blur.BlurError as exc:
            log.warning("video state: ignoring the saved blur settings: %s", exc)

    # -- what the Console shows ----------------------------------------------------------------------------------

    def arc_block(self) -> dict[str, Any]:
        arc = self._arc
        base: dict[str, Any] = {
            "state": "checking",
            "reason": None,
            "device": arc.label if arc and arc.ok else "",
            "fps": None,
            "infer_ms": None,
            "infer_p50_ms": None,
            "age_ms": None,
        }
        if arc is None:
            return base
        if not arc.ok:
            return {**base, "state": "unavailable", "reason": arc.reason}
        with self._lock:
            feed, phase = self._feed, self._phase
        stats = feed.stats() if feed is not None else None
        if stats is None:
            return {**base, "state": "idle"}
        state = {"idle": "idle", "loading": phase or "loading"}.get(stats["state"], stats["state"])
        return {
            **base,
            "state": state,
            "reason": stats["error"] if stats["state"] == "failed" else None,
            "device": stats["device"] or base["device"],
            "fps": stats["mask_fps"] if stats["state"] in ("running", "stalled") else None,
            "infer_ms": stats["infer_p95_ms"],
            "infer_p50_ms": stats["infer_p50_ms"],
            "age_ms": stats["mask_age_ms"],
        }

    def state(self) -> dict[str, Any]:
        return native_state(self.arc_block(), self.settings())

    def health(self) -> list[dict[str, str]]:
        block = self.arc_block()
        if block["state"] != "failed" and not (block["state"] == "unavailable" and self.on):
            return []
        return [
            {
                "id": "video-blur",
                "group": "video",
                "state": "warn",
                "title": "Background blur is unavailable",
                "detail": block["reason"] or "",
                "hint": "",
            }
        ]


def _model_present() -> bool:
    try:
        segment.verify_model()
    except ModelError:
        return False
    return True


def cost_for(arc: dict[str, Any], on: bool, rate: int, fps: float = 30.0) -> dict[str, Any]:
    """The cost pill. The mask is made beside the picture, so cost is "can it keep up", not milliseconds added."""
    budget = round(1000.0 * rate / fps, 1)  # one inference may take as long as the frames between two masks
    limits = {"p95_ms": budget, "cpu_percent": 0.0}
    if not on or arc["state"] in ("unavailable", "failed", "checking", "idle"):
        return {"state": "off", "detail": "Blur is off." if not on else "", "limits": limits}
    if arc["state"] in ("downloading", "loading") or arc["infer_ms"] is None:
        return {"state": "measuring", "detail": "Measuring the mask.", "limits": limits}
    detail = f"{arc['fps'] or 0:g} masks per second, {arc['infer_ms']:g} ms to compute"
    over = arc["state"] == "stalled" or arc["infer_ms"] > budget
    return {"state": "over" if over else "ok", "detail": detail, "limits": limits}


def native_state(arc: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """The `video.blur` block for the native engine (docs/console.md)."""
    state = blurctl.empty_state()
    usable = arc["state"] not in ("unavailable", "checking")
    on = bool(settings["on"]) and arc["state"] not in ("unavailable", "failed")
    state.update(
        native=True,
        available=usable and arc["state"] != "failed",
        on=on,
        strength=settings["strength"],
        rate=settings["rate"],
        arc=arc,
        cost=cost_for(arc, on, settings["rate"]),
    )
    state["plugin"] = {**state["plugin"], "installed": usable, "device_labels": [arc["device"] or "Arc"]}
    return state


# ---------------------------------------------------------------------------
# The demo rig
# ---------------------------------------------------------------------------

DEMO_MODES = ("off", "running", "downloading", "unavailable", "failed", "stalled")
DEMO_REASON = {
    "unavailable": "the openvino package is not installed (pip install 'streamstack[blur]')",
    "failed": "the segmentation model could not be downloaded (no network)",
}


class DemoArc:
    """The native blur state on the simulated rig. Mode `off` (the default) leaves the plain demo blur untouched."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.mode = "off"

    def set_mode(self, mode: str) -> str:
        if mode not in DEMO_MODES:
            raise ValueError(f"mode must be one of {', '.join(DEMO_MODES)}")
        with self._lock:
            self.mode = mode
        return f"Demo blur path: {mode}"

    @property
    def active(self) -> bool:
        return self.mode != "off"

    def reason(self) -> str | None:
        return DEMO_REASON.get(self.mode)

    def blocked(self) -> bool:
        return self.mode in ("unavailable", "failed")

    def block(self, rate: int) -> dict[str, Any]:
        mode = self.mode
        measured = mode in ("running", "stalled")
        return {
            "state": mode,
            "reason": DEMO_REASON.get(mode),
            "device": "Intel Arc A380",
            "fps": (30.0 / rate if mode == "running" else 0.0) if measured else None,
            "infer_ms": 2.4 if measured else None,
            "infer_p50_ms": 1.9 if measured else None,
            "age_ms": 12.0 if measured else None,
        }

    def overlay(self, shown: dict[str, Any]) -> dict[str, Any]:
        """The demo's plain blur state, turned into the native shape for the current demo mode."""
        if not self.active:
            return shown
        settings = {"on": shown["on"], "strength": shown["strength"], "rate": shown["rate"]}
        return native_state(self.block(shown["rate"]), settings)
