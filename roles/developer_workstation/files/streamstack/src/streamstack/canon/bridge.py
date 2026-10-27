"""The Canon bridge: owns the camera, feeds the loopback device, and serves control requests.

One process owns the USB camera for its whole life, so the Console never has to fight it for the device:
 - frames: camera session -> (JPEG) -> ffmpeg (GPU decode when it works, CPU otherwise) -> v4l2loopback
 - controls: requests arrive on a unix socket and run on this thread between frames (shared libgphoto2 session)
   or by pausing live view for a moment (gphoto2 CLI), then the picture resumes by itself
 - health: a stalled camera or ffmpeg is restarted with backoff; a call stuck inside libgphoto2 ends the process
   so systemd starts a fresh one
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from streamstack import system
from streamstack.canon import controls as ctl
from streamstack.canon import encode
from streamstack.canon.gphoto import CameraError, Reading, Session
from streamstack.canon.jpeg import JpegInfo, jpeg_info
from streamstack.hardware import camera as hwcamera
from streamstack.hardware.plan import Plan

SOURCE_STALL = 4.0  # seconds without a camera frame before the source is restarted
ENCODER_STALL = 3.0  # seconds without ffmpeg output while frames go in
HANG_LIMIT = 45.0  # a loop iteration this long means libgphoto2 is stuck: exit and let systemd restart us
HOLD_FPS = 5.0  # while live view is paused the last frame is re-sent this often so the loopback never goes dark
REFRESH_EVERY = 0.5  # shared session: one control is re-read this often, so dial changes on the body show up
STATS_EVERY = 1.0
LABELS = {"qsv": "GPU (Intel Quick Sync)", "vaapi": "GPU (VA-API)", "cpu": "CPU", "legacy": "CPU (legacy command)"}


def log(text: str) -> None:
    print(f"streamstack canon-bridge: {text}", file=sys.stderr, flush=True)


def proc_ticks(pid: int | None) -> int | None:
    if not pid:
        return None
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            rest = fh.read().rsplit(")", 1)[1].split()
        return int(rest[11]) + int(rest[12])
    except (OSError, IndexError, ValueError):
        return None


@dataclass
class Command:
    op: str
    args: dict[str, Any]
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None


@dataclass
class Settings:
    device: str
    canvas: tuple[int, int]
    output: str = "native"  # native | fill
    decode: str = "auto"  # auto | qsv | vaapi | cpu
    render_node: str = ""

    @classmethod
    def from_config(cls, cfg: dict[str, str], plan: Plan | None = None) -> Settings:
        def number(key: str, fallback: int) -> int:
            try:
                return max(2, int(cfg.get(key, fallback)))
            except ValueError:
                return fallback

        output = cfg.get("STREAMSTACK_CANON_OUTPUT", "native").strip().lower()
        decode = cfg.get("STREAMSTACK_CANON_DECODE", "auto").strip().lower()
        return cls(
            device=hwcamera.device_for("camera", cfg, plan),
            canvas=(number("STREAMSTACK_WIDTH", 1920), number("STREAMSTACK_HEIGHT", 1080)),
            output=output if output in ("native", "fill") else "native",
            decode=decode if decode in ("auto", "qsv", "vaapi", "cpu") else "auto",
            render_node=cfg.get("STREAMSTACK_CANON_RENDER_NODE", "").strip(),
        )


class Bridge:
    def __init__(
        self,
        settings: Settings,
        make_session: Callable[[], Session],
        make_encoder: Callable[[encode.Plan], encode.Encoder] = encode.Encoder,
        probe: Callable[[encode.Plan, bytes], str | None] = encode.probe,
        on_busy: Callable[[], None] | None = None,
        exit_fn: Callable[[int], None] = os._exit,
        clock: Callable[[], float] = time.monotonic,
        nodes: Callable[[], list[tuple[str, str]]] = encode.render_nodes,
        version: Callable[[], tuple[int, int]] = encode.ffmpeg_version,
        usb: Callable[[], list[str]] | None = None,
        plan: Callable[[], Plan | None] | None = None,
    ) -> None:
        self.s = settings
        self.hardware_plan = plan or (lambda: None)  # the hardware Plan, read each time the decoder starts (hot-plug)
        self.make_session = make_session
        self.make_encoder = make_encoder
        self.probe = probe
        self.on_busy = on_busy
        self.exit_fn = exit_fn
        self.clock = clock
        self.nodes = nodes
        self.version = version
        self.usb = usb or (lambda: system.canon_products())
        self.stop_event = threading.Event()
        self.commands: queue.Queue[Command] = queue.Queue()
        self.lock = threading.Lock()
        self.session: Session | None = None
        self.encoder: encode.Encoder | None = None
        self.readings: dict[str, Reading] = {}
        self.demoted: dict[str, str] = {}
        self.tried: list[dict[str, Any]] = []
        self.restarts = {"source": 0, "encoder": 0}
        self.stalls = 0
        self.last_error: str | None = None
        self.state = "starting"
        self.message: str | None = None
        self.info: JpegInfo | None = None
        self.last_frame: bytes | None = None
        self.frames_in = 0
        self.heartbeat = clock()
        self.started = clock()
        self.fps_in = 0.0
        self.fps_out = 0.0
        self.grab_ms = 0.0
        self.pipe_ms = 0.0
        self.cpu: dict[str, float | None] = {}
        self._win = (clock(), 0, 0, {})  # time, frames_in, frames_out, ticks by name
        self._ffmpeg_version: tuple[int, int] | None = None
        self._enc_stalls = 0
        self._refresh_i = 0
        self._last_refresh = 0.0
        self._last_hold = 0.0
        self.plan: encode.Plan | None = None
        self.node = ""

    # ---------- public ----------
    def stop(self) -> None:
        self.stop_event.set()

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        """One IPC request. `status` is answered from the cache; everything else runs on the bridge thread."""
        op = str(request.get("op", ""))
        if op == "status":
            return {"ok": True, "status": self.status()}
        if op not in ("set", "action", "refresh"):
            return {"ok": False, "error": f"unknown request {op!r}"}
        if self.stop_event.is_set():
            return {"ok": False, "error": "the Canon bridge is stopping"}
        cmd = Command(op, dict(request))
        self.commands.put(cmd)
        if not cmd.done.wait(timeout=20.0):
            return {"ok": False, "error": "the camera did not answer in time"}
        return cmd.result or {"ok": False, "error": "no result"}

    def run(self) -> int:
        threading.Thread(target=self._watchdog, daemon=True, name="canon-watchdog").start()
        backoff = 1.0
        try:
            while not self.stop_event.is_set():
                try:
                    self._cycle()
                    backoff = 1.0
                except CameraError as exc:
                    self._source_failed(exc)
                    if exc.kind == "busy" and self.on_busy:
                        self.on_busy()
                    self._wait(backoff)
                    backoff = min(10.0, backoff * 2)
        finally:
            self._shutdown()
        return 0

    # ---------- the cycle ----------
    def _wait(self, seconds: float) -> None:
        end = self.clock() + seconds
        while not self.stop_event.is_set() and self.clock() < end:
            self.heartbeat = self.clock()
            self._drain(fail="the camera is not connected right now")
            self.stop_event.wait(0.1)

    def _cycle(self) -> None:
        session = self.make_session()
        self.session = session
        self.heartbeat = self.clock()
        self._set_state("starting", "Opening the camera")
        try:
            session.open()
            self._configure(session)
            self._loop(session)
        finally:
            self.session = None
            try:
                session.close()
            except CameraError as exc:
                log(f"closing the camera session: {exc}")

    def _configure(self, session: Session) -> None:
        keys = [c.key for c in ctl.CONTROLS]
        self.readings = session.read(keys)
        self.last_error = None
        size = self.readings.get("liveviewsize")
        if size and not size.readonly and size.choices:
            best = ctl.best_liveview(size.choices)
            if best and best != size.value:
                try:
                    session.write([("liveviewsize", best)])
                    self.readings.update(session.read(["liveviewsize"]))
                except CameraError as exc:
                    log(f"live view size not changed: {exc}")

    def _loop(self, session: Session) -> None:
        last_real = self.clock()
        while not self.stop_event.is_set():
            self.heartbeat = now = self.clock()
            self._drain()
            t0 = self.clock()
            frame = session.next_frame(0.25)
            now = self.clock()
            if frame is None:
                if getattr(session, "paused", False):
                    last_real = now  # a deliberate pause for a setting, not a stall
                self._idle(session, now, now - last_real)
                if now - last_real > SOURCE_STALL:
                    self.stalls += 1
                    raise CameraError("The camera stopped sending live view", "io")
                continue
            last_real = now
            self.grab_ms = self.grab_ms * 0.8 + (now - t0) * 1000.0 * 0.2
            self.frames_in += 1
            self._push(frame)
            if session.shared and now - self._last_refresh >= REFRESH_EVERY:
                self._refresh_one(session)
            self._stats(now)

    def _idle(self, session: Session, now: float, silent: float) -> None:
        paused = bool(getattr(session, "paused", False))
        if paused:
            self._set_state("paused", "Changing a camera setting (picture resumes in a moment)")
            if self.last_frame is not None and now - self._last_hold >= 1.0 / HOLD_FPS:
                self._last_hold = now
                self._push(self.last_frame, count=False)
        elif silent > 1.5:
            self._set_state("stalled", "Waiting for the camera")
        self._stats(now)

    # ---------- encoder ----------
    def _push(self, frame: bytes, count: bool = True) -> None:
        info = jpeg_info(frame)
        if info is None:
            return  # not a picture (a half-written frame); the next one will be
        self.last_frame = frame
        if self.encoder is None or not self.encoder.alive() or info != self.info:
            if self.encoder is not None and not self.encoder.alive():
                self._encoder_died()
            self.info = info
            self._start_encoder(frame, info)
        enc = self.encoder
        if enc is None:
            return
        if count and enc.fed > enc.out_frames and self.clock() - max(enc.last_out, enc.started) > ENCODER_STALL:
            self._encoder_stalled()
            return
        try:
            self.pipe_ms = self.pipe_ms * 0.8 + enc.feed(frame) * 0.2
        except (BrokenPipeError, OSError):
            self._encoder_died()
            return
        if count and self.state != "running":
            self._set_state("running", None)

    def _candidates(self) -> list[str]:
        order = list(encode.DECODE_ORDER) if self.s.decode == "auto" else [self.s.decode]
        if self.s.decode in ("qsv", "vaapi"):
            order.append("cpu")
        return [n for n in order if n not in self.demoted] + ["legacy"]

    def _render_node(self) -> tuple[str, str]:
        if self.s.render_node:
            vendor = next((v for n, v in self.nodes() if n == self.s.render_node), "")
            return self.s.render_node, vendor
        plan = self.hardware_plan()
        gpu = plan.gpu("decode") if plan is not None else None
        if gpu is not None:  # the profile's decode role, found by PCI id: the iGPU on deskmeet1, the Arc on dm313red
            return gpu.node_path, "0x" + gpu.pci_id.split(":")[0]
        nodes = self.nodes()
        return nodes[0] if nodes else ("", "")

    def _start_encoder(self, frame: bytes, info: JpegInfo) -> None:
        if self.encoder is not None:
            self.encoder.stop()
            self.encoder = None
        if self._ffmpeg_version is None:
            self._ffmpeg_version = self.version()
        node, vendor = self._render_node()
        self.node = node
        self.tried = []
        for name in self._candidates():
            if name in ("qsv", "vaapi") and not node:
                self.tried.append({"name": name, "ok": False, "reason": "no GPU render node (/dev/dri/renderD*)"})
                continue
            plan = encode.build_plan(
                name, info, self.s.device, self.s.output, self.s.canvas, node, vendor, self._ffmpeg_version
            )
            reason = self.probe(plan, frame)
            self.tried.append({"name": name, "ok": reason is None, "reason": reason})
            if reason is not None:
                self.demoted[name] = reason
                log(f"decode path {name} unavailable: {reason}")
                continue
            enc = self.make_encoder(plan)
            try:
                enc.start()
            except OSError as exc:
                self.tried[-1].update(ok=False, reason=f"cannot start ffmpeg: {exc}")
                self.demoted[name] = str(exc)
                continue
            self.encoder, self.plan = enc, plan
            log(
                f"decoding on {LABELS[name]}, output {plan.out_w}x{plan.out_h} "
                f"({info.width}x{info.height} {info.subsampling} in)"
            )
            return
        raise CameraError("No ffmpeg decode path works; see the bridge log", "other")

    def _encoder_died(self) -> None:
        enc, self.encoder = self.encoder, None
        self.restarts["encoder"] += 1
        tail = enc.stderr_tail() if enc else ""
        self.last_error = f"ffmpeg stopped: {tail}" if tail else "ffmpeg stopped"
        log(self.last_error)
        if enc is not None:
            young = self.clock() - enc.started < 5.0
            if young and enc.plan.name not in ("cpu", "legacy"):
                self.demoted[enc.plan.name] = tail or "ffmpeg exited right after starting"
            enc.stop()
        self.info = None  # forces a rebuild with the next frame

    def _encoder_stalled(self) -> None:
        enc = self.encoder
        self.stalls += 1
        self._enc_stalls += 1
        name = enc.plan.name if enc else ""
        log(f"ffmpeg produced no output for {ENCODER_STALL:.0f}s on {name}; restarting it")
        if enc is not None and self._enc_stalls >= 2 and name not in ("cpu", "legacy"):
            self.demoted[name] = "stalled twice"
        self.restarts["encoder"] += 1
        if enc is not None:
            enc.stop()
        self.encoder = None
        self.info = None

    # ---------- controls ----------
    def _refresh_one(self, session: Session) -> None:
        self._last_refresh = self.clock()
        keys = [c.key for c in ctl.CONTROLS]
        key = keys[self._refresh_i % len(keys)]
        self._refresh_i += 1
        try:
            self.readings.update(session.read([key]))
        except CameraError as exc:
            log(f"refresh {key}: {exc}")

    def _drain(self, fail: str | None = None) -> None:
        pending: list[Command] = []
        while True:
            try:
                pending.append(self.commands.get_nowait())
            except queue.Empty:
                break
        if not pending:
            return
        if fail is not None or self.session is None:
            for cmd in pending:
                cmd.result = {"ok": False, "error": fail or "the camera is not connected right now"}
                cmd.done.set()
            return
        # coalesce: of several `set`s for the same control (a slider drag) only the last one needs the camera
        last_set: dict[str, Command] = {}
        for cmd in pending:
            if cmd.op == "set":
                last_set[str(cmd.args.get("id"))] = cmd
        for cmd in pending:
            if cmd.op == "set" and last_set.get(str(cmd.args.get("id"))) is not cmd:
                cmd.result = {"ok": True, "superseded": True}
                cmd.done.set()
        batch = [c for c in pending if c.op == "set" and last_set.get(str(c.args.get("id"))) is c]
        if batch:
            self._run_sets(self.session, batch)
        for cmd in pending:
            if cmd.op != "set":
                self._run(self.session, cmd)

    def _run(self, session: Session, cmd: Command) -> None:
        try:
            cmd.result = self._action(session, cmd.args) if cmd.op == "action" else self._refresh_all(session)
        except (CameraError, ValueError) as exc:
            cmd.result = {"ok": False, "error": str(exc)}
        cmd.done.set()

    def _refresh_all(self, session: Session) -> dict[str, Any]:
        self.readings.update(session.read([c.key for c in ctl.CONTROLS]))
        return {"ok": True}

    def _plan_set(self, args: dict[str, Any]) -> tuple[ctl.Control, list[tuple[str, str]], str]:
        control = ctl.BY_ID.get(str(args.get("id")))
        if control is None or not control.writable:
            raise ValueError(f"{args.get('id')!r} is not a setting this camera control offers")
        reading = self.readings.get(control.key)
        if reading is None or reading.error:
            raise ValueError(f"The camera does not expose {control.label}")
        value = args.get("value")
        if control.prereq:
            wb = self.readings.get(control.prereq[0])
            if wb is None or control.prereq[1] not in wb.choices:
                raise ValueError("This camera has no Kelvin white balance")
        elif reading.readonly:
            raise ValueError(control.readonly_reason)
        if control.numeric:
            text = str(ctl.snap_kelvin(value))
        else:
            wanted = str(value)
            match = next((c for c in reading.choices if c.lower() == wanted.lower()), None)
            if match is None:
                raise ValueError(
                    f"{control.label}: the camera offers {', '.join(reading.choices[:12]) or 'no choices'}"
                )
            text = match
        items: list[tuple[str, str]] = []
        if control.prereq:
            wb = self.readings[control.prereq[0]]
            if wb.value != control.prereq[1]:
                items.append(control.prereq)
        items.append((control.key, text))
        return control, items, text

    def _run_sets(self, session: Session, batch: list[Command]) -> None:
        planned: list[tuple[Command, ctl.Control, str]] = []
        items: list[tuple[str, str]] = []
        for cmd in batch:
            try:
                control, one, text = self._plan_set(cmd.args)
            except ValueError as exc:
                cmd.result = {"ok": False, "error": str(exc)}
                cmd.done.set()
                continue
            planned.append((cmd, control, text))
            for item in one:
                if item not in items:
                    items.append(item)
        if not planned:
            return
        try:
            session.write(items)
            keys = list(dict.fromkeys(k for k, _ in items))
            self.readings.update(session.read(keys))
        except CameraError as exc:
            for cmd, _, _ in planned:
                cmd.result = {"ok": False, "error": str(exc)}
                cmd.done.set()
            return
        for cmd, control, text in planned:
            now = self.readings.get(control.key)
            kept = now.value if now else None
            applied = kept is not None and kept.strip().lower() == text.lower()
            cmd.result = {"ok": True, "applied": applied, "value": kept}
            if not applied:
                cmd.result["message"] = f"The camera kept {control.label} at {kept}"
            cmd.done.set()

    def _action(self, session: Session, args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name"))
        if name == "autofocus":
            session.write([(ctl.AUTOFOCUS_KEY, "1")])
        elif name == "af_cancel":
            session.write([(ctl.AUTOFOCUS_CANCEL_KEY, "1")])
        elif name == "focus":
            step = args.get("step")
            if isinstance(step, bool) or not isinstance(step, int):
                raise ValueError("focus needs a whole-number step")
            session.write([(ctl.FOCUS_DRIVE_KEY, ctl.focus_choice(step))])
        else:
            raise ValueError(f"unknown camera action {name!r}")
        return {"ok": True}

    # ---------- state ----------
    def _set_state(self, state: str, message: str | None) -> None:
        if state != self.state:
            log(f"{state}" + (f": {message}" if message else ""))
        self.state, self.message = state, message

    def _source_failed(self, exc: CameraError) -> None:
        self.restarts["source"] += 1
        self.last_error = str(exc)
        log(f"camera: {exc}")
        self._set_state("offline" if exc.kind in ("missing", "busy") else "stalled", str(exc))

    def _shutdown(self) -> None:
        enc, self.encoder = self.encoder, None
        if enc is not None:
            enc.stop()
        self._set_state("stopped", None)
        self._drain(fail="the Canon bridge is stopping")

    def _watchdog(self) -> None:
        while not self.stop_event.wait(max(0.01, min(2.0, HANG_LIMIT / 4))):
            idle = self.clock() - self.heartbeat
            if idle > HANG_LIMIT:
                log(f"no progress for {idle:.0f}s (stuck inside the camera library); exiting so systemd restarts it")
                self.exit_fn(70)
                return

    def _stats(self, now: float) -> None:
        t0, in0, out0, ticks0 = self._win
        dt = now - t0
        if dt < STATS_EVERY:
            return
        enc = self.encoder
        out = enc.out_frames if enc else 0
        self.fps_in = (self.frames_in - in0) / dt
        self.fps_out = max(0, out - out0) / dt if out >= out0 else 0.0
        pids = {
            "bridge": os.getpid(),
            "ffmpeg": enc.proc.pid if enc and enc.proc else None,
            "gphoto2": getattr(getattr(self.session, "proc", None), "pid", None),
        }
        ticks = {k: proc_ticks(v) for k, v in pids.items()}
        hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
        cpu: dict[str, float | None] = {}
        for k, v in ticks.items():
            prev = ticks0.get(k)
            cpu[k] = round((v - prev) / hz / dt * 100.0, 1) if v is not None and prev is not None else None
        cpu["total"] = round(sum(x for x in cpu.values() if x is not None), 1)
        self.cpu = cpu
        if enc is not None and now - enc.started > 60.0:
            self._enc_stalls = 0
        self._win = (now, self.frames_in, out, ticks)

    def view_controls(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for c in ctl.CONTROLS:
            r = self.readings.get(c.key)
            if r is None or r.error:
                continue
            readonly = r.readonly or not c.writable
            if c.prereq:  # Kelvin is writable whenever the body has the Color Temperature white balance
                wb = self.readings.get(c.prereq[0])
                readonly = wb is None or c.prereq[1] not in wb.choices or wb.readonly
            out[c.id] = {
                "id": c.id,
                "key": c.key,
                "label": c.label,
                "group": c.group,
                "value": r.value,
                "choices": r.choices,
                "range": list(ctl.KELVIN_RANGE) if c.numeric else (list(r.range) if r.range else None),
                "readonly": readonly,
                "reason": c.readonly_reason if readonly and c.writable else None,
                "hint": ctl.HINTS.get(c.key),
            }
        return out

    def status(self) -> dict[str, Any]:
        plan = self.plan
        enc = self.encoder
        return {
            "state": self.state,
            "message": self.message,
            "pid": os.getpid(),
            "uptime_s": int(self.clock() - self.started),
            "session": "shared" if (self.session is not None and self.session.shared) else "pause",
            "usb": self.usb(),
            "decode": {
                "path": plan.name if plan and enc else None,
                "label": LABELS.get(plan.name, plan.name) if plan and enc else None,
                "tried": list(self.tried),
                "render_node": self.node or None,
                "ffmpeg": ".".join(str(x) for x in self._ffmpeg_version) if self._ffmpeg_version else None,
            },
            "input": {"width": self.info.width, "height": self.info.height, "subsampling": self.info.subsampling}
            if self.info
            else None,
            "output": {"width": plan.out_w, "height": plan.out_h, "mode": self.s.output} if plan else None,
            "fps_in": round(self.fps_in, 1),
            "fps_out": round(self.fps_out, 1),
            "grab_ms": round(self.grab_ms, 1),
            "pipe_ms": round(self.pipe_ms, 2),
            "cpu": dict(self.cpu),
            "frames": self.frames_in,
            "restarts": dict(self.restarts),
            "stalls": self.stalls,
            "last_error": self.last_error,
            "controls": self.view_controls(),
            "time": time.time(),
        }
