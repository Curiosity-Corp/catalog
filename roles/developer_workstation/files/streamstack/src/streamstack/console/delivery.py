"""Keeps "StreamStack Camera" listed and fed when the real camera goes away (work package C, ADR 0004 addendum).

The failure this exists for (dm313red, 2026-10-27): the external PTP bridge that feeds /dev/video42 died after a
reboot, the engine had nothing to read, nothing wrote /dev/video43, and Teams in Edge listed only the Cam Link: with
`exclusive_caps=1` an unfed node is not a camera. The Console now treats "the program camera is always fed" as its
own job, in three steps that follow the source's health:

1. **Fail over** (STREAMSTACK_CAMERA_FALLBACK=auto, the default): when the camera the person picked has had no
   producer for STREAMSTACK_CAMERA_FALLBACK_AFTER seconds (6) and another camera is delivering (the Cam Link), the
   engine is re-pointed at it, and goes back after the first one has been steady for FAILBACK_AFTER seconds.
   `off` keeps the engine on the camera that was picked.
2. **Cover the gap** (STREAMSTACK_CAMERA_LOSS=slate, the default; `hold` darkens and blurs the last frame instead,
   `off` writes nothing): while no engine child writes the node, the slate (engine/slate.py) does, at the size and rate
   the engine writes, so the node never loses its producer and the call app never loses the device.
3. **Reconnect on its own**: the engine supervisor asks `guard` before every child start; when the camera is back the
   slate is released and the engine child takes the node, no click needed.
4. **Prove the picture before handing over** (#177): a camera that is connected but delivers nothing (the Cam Link with
   no HDMI signal) passes step 3's "is it there" test, so the engine child used to start, stall, die and start again,
   and the node changed writer every few seconds. After a fault the guard now reads one frame from the source *without
   touching the program node* (`read_one_frame`) and keeps the slate as the single writer until that read succeeds,
   asking again with a growing pause (`PROBE_BACKOFF`).
5. **Say who holds a busy camera** (#193): v4l2loopback 0.15 lets one reader stream a loopback at a time (a second
   one gets EBUSY from VIDIOC_REQBUFS or VIDIOC_S_FMT), so an app that opens the raw camera node takes it from the
   engine. That is not "no signal": the card, the log and `streamstack doctor` name the program holding the node
   (`device_holders`) and the fix, and the engine takes the camera back on its own once that program lets go.

Everything here is read-only towards cameras (`v4l2-ctl --info` on a node, `os.path.exists`); only the slate and the
engine child ever write the program node.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable

from streamstack import config, system
from streamstack.engine import source
from streamstack.engine.graph import Source, Spec
from streamstack.engine.slate import DEFAULT_FPS, DEFAULT_SIZE, SlateFeeder, SlateSpec

log = logging.getLogger(__name__)
Notify = Callable[[str, str], None]

FALLBACK_AFTER = 6.0  # seconds without a producer before the engine moves to another camera
FAILBACK_AFTER = 5.0  # seconds the first camera must be steady before the engine moves back
IDLE_FPS = 5.0  # the idle placeholder's rate: enough to keep the node listed, a few percent of the engine's cost
IDLE_CAPTION = "Camera idle"
IDLE_REASON = "It starts when an app opens StreamStack Camera"
LOSS_MODES = ("slate", "hold", "off")
FALLBACK_MODES = ("auto", "off")
PROBE_BACKOFF = (1.0, 2.0, 4.0, 8.0, 15.0)  # pause before asking a silent source for a frame again; stays at the last
PROBE_TIMEOUT = 4.0  # seconds a source gets to deliver one frame
HEALTHY_AFTER = 30.0  # a child that ran this long after a good probe was healthy: the pauses start over
NO_PICTURE = "{label} is connected but delivers no picture (no signal?)"
BUSY = "{label} is in use by {holders}: only one app at a time can read {device}"
BUSY_HINT = (
    "close {holders_short}, or switch it to StreamStack Camera: "
    "StreamStack takes {label} back on its own once it is free"
)
EBUSY_TEXT = "Device or resource busy"
UNKNOWN_HOLDER = "another program (one of another account's, or one this account may not inspect)"


def _is_producer(argv: list[str], device: str) -> bool:
    """Whether a process holding `device` writes it (the bridge feeding a loopback) rather than reads it.

    ffmpeg reads the device it names after `-i` and writes one it names anywhere else (its output); GStreamer writes
    a `device=` that follows `v4l2sink` and reads one that follows `v4l2src`. Anything else is taken for a reader."""
    for i, arg in enumerate(argv):
        if arg == device:
            return not (i > 0 and argv[i - 1] == "-i")
        if arg == f"device={device}":
            element = next((a for a in reversed(argv[:i]) if a.startswith("v4l2")), "")
            return element == "v4l2sink"
    return False


def device_holders(device: str, *, proc_root: str = "/proc", self_pid: int | None = None) -> list[str]:
    """`name (pid N)` for each process this account can see that has `device` open and is not its producer.

    Read-only: /proc/<pid>/fd links and cmdline. Another account's processes are not visible (the caller says so)."""
    me = os.getpid() if self_pid is None else self_pid
    try:
        target = os.path.realpath(device)
        pids = [entry for entry in os.listdir(proc_root) if entry.isdigit()]
    except OSError:
        return []
    found: list[str] = []
    for pid in sorted(pids, key=int):
        if int(pid) == me:
            continue
        base = os.path.join(proc_root, pid)
        try:
            fds = os.listdir(os.path.join(base, "fd"))
        except OSError:
            continue  # gone, or another account's
        holds = False
        for fd in fds:
            try:
                if os.path.realpath(os.readlink(os.path.join(base, "fd", fd))) == target:
                    holds = True
                    break
            except OSError:
                continue
        if not holds:
            continue
        try:
            with open(os.path.join(base, "cmdline"), "rb") as handle:
                argv = [a.decode(errors="replace") for a in handle.read().split(b"\0") if a]
        except OSError:
            argv = []
        if argv and _is_producer(argv, device):
            continue
        try:
            with open(os.path.join(base, "comm"), encoding="utf-8", errors="replace") as handle:
                name = handle.read().strip()
        except OSError:
            name = os.path.basename(argv[0]) if argv else "a program"
        found.append(f"{name} (pid {pid})")
    return found


def busy_reason(src: Source, holders: list[str] | None = None) -> str:
    """The sentence for a source another reader holds."""
    named = holders if holders is not None else device_holders(src.device)
    return BUSY.format(label=src.label, holders=", ".join(named) if named else UNKNOWN_HOLDER, device=src.device)


def read_one_frame(src: Source, timeout: float = PROBE_TIMEOUT) -> bool | str | None:
    """Whether `src` delivers a frame now: read one with ffmpeg into the null muxer. Never opens the program node.
    None when ffmpeg is not installed (the question cannot be asked; the caller proceeds as before); a sentence
    (`busy_reason`) when another reader holds the source, which is not the same as a camera without a picture."""
    if not system.have("ffmpeg"):
        return None
    argv = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "v4l2"]
    argv += ["-framerate", f"{src.fps:g}", "-video_size", f"{src.width}x{src.height}", "-input_format", src.pix_fmt]
    argv += ["-i", src.device, "-frames:v", "1", "-f", "null", "-"]
    result = system.run(argv, timeout=timeout)
    if result is None:
        return False
    if result.returncode == 0:
        return True
    if EBUSY_TEXT in (result.stderr or ""):
        return busy_reason(src)
    return False


def loss_mode(cfg: dict[str, str]) -> str:
    wanted = (cfg.get("STREAMSTACK_CAMERA_LOSS") or "slate").strip().lower()
    if wanted not in LOSS_MODES:
        log.warning("STREAMSTACK_CAMERA_LOSS=%r is not slate, hold or off; using slate", wanted)
        return "slate"
    return wanted


def fallback_mode(cfg: dict[str, str]) -> str:
    wanted = (cfg.get("STREAMSTACK_CAMERA_FALLBACK") or "auto").strip().lower()
    if wanted not in FALLBACK_MODES:
        log.warning("STREAMSTACK_CAMERA_FALLBACK=%r is not auto or off; using auto", wanted)
        return "auto"
    return wanted


def _pause(attempts: int) -> float:
    return PROBE_BACKOFF[min(max(attempts, 1), len(PROBE_BACKOFF)) - 1]


def _seconds(cfg: dict[str, str], key: str, default: float) -> float:
    try:
        return max(0.0, float(cfg.get(key) or default))
    except ValueError:
        log.warning("%s=%r is not a number of seconds; using %g", key, cfg.get(key), default)
        return default


class Delivery:
    """The guard between the cameras and the program node. All methods are safe to call from any thread."""

    def __init__(
        self,
        cfg: dict[str, str],
        *,
        slate: SlateFeeder | None = None,
        notify: Notify | None = None,
        probe: Callable[[str], bool | None] = system.loopback_has_producer,
        exists: Callable[[str], bool] = os.path.exists,
        clock: Callable[[], float] = time.monotonic,
        camera_device: Callable[[], str] | None = None,
        program_device: Callable[[], str] | None = None,
        last_frame: Callable[[], bytes | None] = lambda: None,
        delivers: Callable[[Source], bool | str | None] = read_one_frame,
    ) -> None:
        self.cfg = cfg
        self.slate = slate or SlateFeeder(clock=clock)
        self._notify = notify
        self._probe, self._exists, self._clock = probe, exists, clock
        self._camera_device = camera_device or (lambda: cfg.get("STREAMSTACK_CAMERA_DEVICE", ""))
        self._program_device = program_device or (lambda: cfg.get("STREAMSTACK_PROGRAM_DEVICE", ""))
        self._last_frame = last_frame
        self._delivers = delivers
        self._lock = threading.RLock()
        self._faulted = False  # the engine child died on this source: prove a frame before the next one starts
        self._attempts = 0  # failed proofs (and children that died soon after one) in a row: sets the pause
        self._next_probe = 0.0
        self._probed_ok_at: float | None = None
        self._out: tuple[int, int, float] | None = None  # what the engine last wrote: width, height, fps
        self._lost_since: float | None = None
        self._steady_since: float | None = None
        self._fallback: str | None = None  # label of the camera standing in for the picked one
        self._reason = ""
        self._announced: str | None = None
        self._parked = False  # the slate is the idle placeholder (nothing is wrong), not a "camera reconnecting" card
        self._source: Source | None = None  # what the engine reads: names a busy source when a child dies on EBUSY
        self._last_proof = ""  # the sentence of the last failed proof, repeated inside the pause
        self._busy = False  # the current cover is a source another reader holds, not a camera without a picture

    # -- is a camera deliverable? -------------------------------------------------------------------------------

    def camera_ready(self, device: str, label: str) -> str | None:
        """None when `device` can be read now, else a sentence saying why not. Read-only."""
        if not device or not self._exists(device):
            return f"{label} is not connected ({device or 'no device'} is absent)"
        if device == self._camera_device() and self._probe(device) is False:
            return f"{label} has no producer on {device}: the program that feeds it is not running"
        return None

    def source_ready(self, src: Source) -> str | None:
        return self.camera_ready(src.device, src.label)

    # -- which camera feeds the program node ---------------------------------------------------------------------

    def choose(self, cameras: list[source.Camera], picked: str | None) -> source.Camera | None:
        """The camera the engine should read now: the picked one, or the stand-in while the picked one is gone."""
        primary = next((c for c in cameras if c.label == picked), None) or (cameras[0] if cameras else None)
        if primary is None:
            return None
        others = [c for c in cameras if c is not primary]
        if fallback_mode(self.cfg) == "off" or not others:
            with self._lock:
                self._lost_since = self._steady_since = self._fallback = None
            return primary
        now = self._clock()
        reason = self.camera_ready(primary.device, primary.label)
        notice: tuple[str, str] | None = None
        with self._lock:
            if reason is None:
                self._lost_since = None
                if self._fallback is None:
                    return primary
                self._steady_since = self._steady_since if self._steady_since is not None else now
                if now - self._steady_since >= _seconds(self.cfg, "STREAMSTACK_CAMERA_FALLBACK_BACK", FAILBACK_AFTER):
                    notice = ("ok", f"{primary.label} is back: the program camera moved back to it")
                    self._fallback = self._steady_since = None
                    chosen = primary
                else:
                    chosen = next((c for c in others if c.label == self._fallback), primary)
            else:
                self._steady_since = None
                self._lost_since = self._lost_since if self._lost_since is not None else now
                chosen = primary
                if self._fallback is None and now - self._lost_since >= _seconds(
                    self.cfg, "STREAMSTACK_CAMERA_FALLBACK_AFTER", FALLBACK_AFTER
                ):
                    alt = next((c for c in others if self.camera_ready(c.device, c.label) is None), None)
                    if alt is not None:
                        self._fallback = alt.label
                        notice = ("warn", f"{primary.label} lost its picture: the program camera is on {alt.label}")
                if self._fallback is not None:
                    alt = next((c for c in others if c.label == self._fallback), None)
                    if alt is not None and self.camera_ready(alt.device, alt.label) is None:
                        chosen = alt
                    else:
                        self._fallback = None  # the stand-in went too: back to the slate on the picked camera
        if notice:
            self._say(*notice)
        return chosen

    # -- the engine's hooks ----------------------------------------------------------------------------------------

    def guard(self, spec: Spec) -> str | None:
        """Engine `before_child`: None lets the child start (the node is released first); a reason keeps it waiting
        while the slate holds the node."""
        self.note_output(spec)
        with self._lock:
            self._source = spec.source
        reason = self.source_ready(spec.source) or self._prove_picture(spec.source)
        if reason is None:
            self.release()
            return None
        self.cover(reason)
        return reason

    def on_exit(self, reason: str, failures: int) -> None:
        """Engine `on_exit`: a child ended. Cover the node at once when it keeps failing, so the gap is the restart."""
        if failures >= 1:
            with self._lock:
                src = self._source
            if src is not None and EBUSY_TEXT in reason:
                reason = busy_reason(src)  # ffmpeg's own line says "busy" but not who: name the holder
            now = self._clock()
            with self._lock:
                soon = self._probed_ok_at is not None and now - self._probed_ok_at < HEALTHY_AFTER
                self._attempts = self._attempts + 1 if soon else 1
                self._faulted = True
                self._next_probe = now + _pause(self._attempts)
                if " is in use by " in reason:
                    self._last_proof = reason  # the pause repeats who holds it, not "no picture"
            self.cover(reason)

    def _prove_picture(self, src: Source) -> str | None:
        """After a fault: None once `src` delivers a frame, else a sentence (the same one every time, so neither the
        log nor the card changes while the camera stays away). The slate keeps the node the whole time."""
        with self._lock:
            if not self._faulted:
                return None
            if self._clock() < self._next_probe:
                return self._last_proof or NO_PICTURE.format(label=src.label)
        try:
            ok = self._delivers(src)
        except Exception:
            log.exception("delivery: the picture check failed; letting the engine try")
            ok = None
        now = self._clock()
        with self._lock:
            if ok is False or isinstance(ok, str):
                self._attempts += 1
                self._next_probe = now + _pause(self._attempts)
                self._last_proof = ok if isinstance(ok, str) else NO_PICTURE.format(label=src.label)
                return self._last_proof
            self._faulted, self._probed_ok_at = False, now
        return None

    def note_output(self, spec: Spec) -> None:
        with self._lock:
            self._out = (spec.out_width, spec.out_height, spec.rate if spec.rate > 0 else DEFAULT_FPS)

    # -- the slate -------------------------------------------------------------------------------------------------

    def _slate_spec(self, reason: str) -> SlateSpec:
        with self._lock:
            width, height, fps = self._out or (*self._configured_size(), DEFAULT_FPS)
        return SlateSpec(
            self._program_device(),
            width,
            height,
            fps,
            reason=reason,
            hold=loss_mode(self.cfg) == "hold",
            preview_path=str(config.preview_file()),
        )

    def _configured_size(self) -> tuple[int, int]:
        wanted = (self.cfg.get("STREAMSTACK_ENGINE_SIZE") or "source").strip().lower()
        w, _, h = wanted.partition("x")
        if w.isdigit() and h.isdigit() and int(w) % 2 == 0 and int(h) % 2 == 0 and int(w) >= 160 and int(h) >= 90:
            return int(w), int(h)
        return DEFAULT_SIZE

    def cover(self, reason: str) -> None:
        """Write the slate on the program node because the source is gone (no-op with STREAMSTACK_CAMERA_LOSS=off)."""
        if loss_mode(self.cfg) == "off":
            return
        first = not self.slate.active or self._parked
        with self._lock:
            self._reason, self._parked = reason, False
            self._busy = " is in use by " in reason
        spec = self._slate_spec(reason)
        self.slate.show(spec, self._last_frame() if spec.hold else None)
        if first:
            self._say("warn", f"{reason}: StreamStack Camera shows 'camera reconnecting' until it returns")

    def park(self) -> bool:
        """The engine is resting (#128): hold the node with a quiet placeholder at IDLE_FPS so the camera stays listed.
        Unlike `cover` this is not a fault: no toast, no warning row. False with STREAMSTACK_CAMERA_LOSS=off, where
        the person chose that nothing but the engine ever writes the node (the camera then leaves the list while idle,
        so the Console does not idle in that mode)."""
        if loss_mode(self.cfg) == "off":
            return False
        with self._lock:
            self._reason, self._parked, self._busy = "", True, False
            width, height, _fps = self._out or (*self._configured_size(), DEFAULT_FPS)
        spec = SlateSpec(
            self._program_device(),
            width,
            height,
            IDLE_FPS,
            reason=IDLE_REASON,
            caption=IDLE_CAPTION,
            hold=loss_mode(self.cfg) == "hold",
            preview_path=str(config.preview_file()),
        )
        self.slate.show(spec, self._last_frame() if spec.hold else None)
        return True

    @property
    def parked(self) -> bool:
        with self._lock:
            return self._parked and self.slate.active

    def release(self) -> None:
        """The source is deliverable again: free the node for the engine child."""
        was, parked = self.slate.active, self._parked
        self.slate.clear()
        with self._lock:
            self._reason, self._parked, self._busy = "", False, False
            self._faulted, self._last_proof = False, ""
        if was and not parked:
            self._say("ok", "Camera is back: the program camera is live again")

    def tick(self) -> None:
        """Called by the monitor: restarts a dead slate child."""
        self.slate.poll()

    def stop(self) -> None:
        self.slate.clear()
        with self._lock:
            self._parked = False

    # -- state -------------------------------------------------------------------------------------------------------

    def _say(self, level: str, text: str) -> None:
        log.info("delivery: %s", text)
        if self._notify:
            try:
                self._notify(level, text)
            except Exception:
                log.exception("console toast hook failed")

    def state(self, engine_state: str | None, feeding: str | None, picked: str | None) -> dict:
        """`mode`: live | fallback | reconnecting | starting | off, with a sentence for the person."""
        slate = self.slate.state()
        with self._lock:
            reason, fallback, busy = self._reason, self._fallback, self._busy
        if slate["active"] and self._parked:
            mode = "idle"
        elif slate["active"]:
            mode = "reconnecting"
        elif engine_state == "running":
            mode = "fallback" if fallback else "live"
        elif engine_state in (None, "stopped"):
            mode = "off"
        else:
            mode = "starting"
        return {
            "mode": mode,
            "reason": reason,
            "busy": busy and bool(slate["active"]),
            "feeding": feeding,
            "picked": picked,
            "fallback": fallback,
            "slate": slate,
            "loss": loss_mode(self.cfg),
            "fallback_setting": fallback_mode(self.cfg),
        }

    def checks(self, block: dict, external: bool) -> list[dict[str, str]]:
        """The Console's `video-delivery` health row for `state()`'s block."""
        return delivery_checks(block, external)


def delivery_checks(block: dict, external: bool) -> list[dict[str, str]]:
    """The `video-delivery` health row for a `Delivery.state()` block: the Console's, and `streamstack doctor`'s through
    the Console's state (#193: doctor called a node showing the slate "fed and delivering")."""
    base = {"id": "video-delivery", "group": "video"}
    mode = block.get("mode")
    if mode == "idle":
        return [
            {
                **base,
                "state": "ok",
                "title": "StreamStack Camera is idle: a placeholder keeps it listed",
                "detail": "the engine and the camera start when an app opens it",
                "hint": "",
            }
        ]
    if mode == "live":
        return [
            {
                **base,
                "state": "ok",
                "title": f"StreamStack Camera is fed by {block.get('feeding')}",
                "detail": "",
                "hint": "",
            }
        ]
    fix = (
        "restart the tool that feeds the camera node (on a host with an external PTP bridge: "
        "systemctl --user restart canonctl-ptp-webcam.service), or plug the camera in again"
        if external
        else "systemctl --user restart streamstack-canon.service, or plug the camera in again"
    )
    if mode == "fallback":
        return [
            {
                **base,
                "state": "warn",
                "title": f"StreamStack Camera is on {block.get('feeding')}, not {block.get('picked')}",
                "detail": f"{block.get('picked')} lost its picture; the engine returns to it on its own",
                "hint": fix,
            }
        ]
    if mode == "reconnecting":
        reason = block.get("reason") or "the camera has no picture"
        if block.get("busy"):
            label = block.get("picked") or "the camera"
            holders = reason.split(" is in use by ", 1)[1].split(":", 1)[0] if " is in use by " in reason else ""
            return [
                {
                    **base,
                    "state": "fail",
                    "title": f"StreamStack Camera has no picture: another app is reading {label}",
                    "detail": reason,
                    "hint": BUSY_HINT.format(holders_short=holders or "the other app", label=label),
                }
            ]
        return [
            {
                **base,
                "state": "fail",
                "title": "StreamStack Camera is showing 'camera reconnecting'",
                "detail": reason,
                "hint": fix,
            }
        ]
    return []
