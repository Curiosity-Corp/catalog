"""Talking to the camera: one interface, two ways to do it.

LibSession   python-gphoto2 (libgphoto2 bindings). One USB session carries live-view frames and configuration
             reads/writes, so a control change never interrupts the picture. Used when the module imports.
CliSession   the `gphoto2` command. The CLI cannot share a session, so a control change pauses the live-view
             process, runs the command and restarts it (a short freeze, the encoder keeps the last frame up).

Both raise CameraError with a short owner-readable message and a `kind` the bridge acts on.
"""

from __future__ import annotations

import importlib
import queue
import re
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from streamstack import system
from streamstack.canon.jpeg import JpegSplitter

# libgphoto2 error numbers (gphoto2-result.h)
GP_ERROR_IO = -7
GP_ERROR_TIMEOUT = -10
GP_ERROR_IO_USB_CLAIM = -53
GP_ERROR_CAMERA_BUSY = -110
GP_ERROR_MODEL_NOT_FOUND = -105
GP_ERROR_NOT_SUPPORTED = -6
GP_ERROR_BAD_PARAMETERS = -2


class CameraError(Exception):
    """kind: busy (another program holds the USB interface), missing, unsupported, bad_value, io, other."""

    def __init__(self, message: str, kind: str = "other", code: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code


def kind_for_code(code: int | None) -> str:
    return {
        GP_ERROR_IO_USB_CLAIM: "busy",
        GP_ERROR_CAMERA_BUSY: "busy",
        GP_ERROR_MODEL_NOT_FOUND: "missing",
        GP_ERROR_NOT_SUPPORTED: "unsupported",
        GP_ERROR_BAD_PARAMETERS: "bad_value",
        GP_ERROR_IO: "io",
        GP_ERROR_TIMEOUT: "io",
    }.get(code if code is not None else 0, "other")


@dataclass
class Reading:
    """One configuration widget as the camera reports it."""

    key: str
    value: str | None = None
    type: str = ""  # RADIO MENU TEXT RANGE TOGGLE ...
    label: str = ""
    readonly: bool = False
    choices: list[str] = field(default_factory=list)
    range: tuple[float, float, float] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------- gphoto2 CLI output ----------
_BLOCK = re.compile(r"^(Label|Readonly|Type|Current|Printable|Choice|Bottom|Top|Step|END)\b:?\s*(.*)$")


def parse_get_config(text: str) -> list[dict[str, Any]]:
    """Split `gphoto2 --get-config` output into one dict per widget, in request order.

    Format (gphoto2 CLI): Label, Readonly, Type, Current, then `Choice: N value` lines for radio/menu or
    Bottom/Top/Step for ranges, and END. The widget's name is not printed, so callers map by order.
    """
    blocks: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        m = _BLOCK.match(line)
        if not m:
            continue
        tag, rest = m.group(1), m.group(2).strip()
        if tag == "Label":
            cur = {"label": rest, "readonly": False, "type": "", "value": None, "choices": [], "range": None}
            continue
        if tag == "END":
            if cur is not None:
                blocks.append(cur)
            cur = None
            continue
        if cur is None:
            continue
        if tag == "Readonly":
            cur["readonly"] = rest not in ("0", "")
        elif tag == "Type":
            cur["type"] = rest
        elif tag == "Current":
            cur["value"] = rest
        elif tag == "Choice":
            parts = rest.split(" ", 1)
            cur["choices"].append(parts[1] if len(parts) == 2 else parts[0])
        elif tag in ("Bottom", "Top", "Step"):
            r = cur.get("_range", {})
            try:
                r[tag] = float(rest)
            except ValueError:
                continue
            cur["_range"] = r
            if len(r) == 3:
                cur["range"] = (r["Bottom"], r["Top"], r["Step"])
    if cur is not None:  # output cut short: keep what was read
        blocks.append(cur)
    for b in blocks:
        b.pop("_range", None)
    return blocks


def reading_from_block(key: str, block: dict[str, Any]) -> Reading:
    return Reading(
        key=key,
        value=block.get("value"),
        type=block.get("type", ""),
        label=block.get("label", ""),
        readonly=bool(block.get("readonly")),
        choices=list(block.get("choices", [])),
        range=block.get("range"),
    )


def cli_error(rc: int | None, err: str) -> CameraError:
    text = " ".join(err.split())[-300:]
    low = text.lower()
    if "claim" in low or "could not lock" in low or "busy" in low:
        return CameraError("Another program is holding the camera (close gphoto/GVFS/EOS Utility)", "busy")
    if "no camera" in low or "model not found" in low or "not found" in low:
        return CameraError("No Canon camera found on USB", "missing")
    if "not supported" in low or ("unknown" in low and "config" in low):
        return CameraError(text or "The camera does not support that setting", "unsupported")
    if "bad parameters" in low or "choice" in low:
        return CameraError(text or "The camera rejected that value", "bad_value")
    return CameraError(text or f"gphoto2 failed (exit {rc})", "io")


class Session(Protocol):
    shared: bool

    def open(self) -> None: ...
    def close(self) -> None: ...
    def next_frame(self, timeout: float) -> bytes | None: ...
    def read(self, keys: list[str]) -> dict[str, Reading]: ...
    def write(self, items: list[tuple[str, str]]) -> None: ...
    def stderr_tail(self) -> str: ...


# ---------- libgphoto2 through python-gphoto2 ----------
def load_gphoto2() -> Any | None:
    """The python-gphoto2 module, or None when it is not installed."""
    try:
        return importlib.import_module("gphoto2")
    except ImportError:
        return None


class LibSession:
    """One libgphoto2 session: live-view frames and configuration share the USB connection."""

    shared = True

    def __init__(self, gp: Any) -> None:
        self.gp = gp
        self.camera: Any = None

    def _wrap(self, exc: Exception) -> CameraError:
        code = getattr(exc, "code", None)
        return CameraError(f"libgphoto2: {exc}", kind_for_code(code), code)

    def _errors(self) -> tuple[type[BaseException], ...]:
        return (self.gp.GPhoto2Error,)

    def open(self) -> None:
        try:
            self.camera = self.gp.Camera()
            self.camera.init()
        except self._errors() as exc:
            self.camera = None
            raise self._wrap(exc) from exc

    def close(self) -> None:
        camera, self.camera = self.camera, None
        if camera is None:
            return
        try:
            camera.exit()
        except self._errors():
            return  # the camera is already gone; nothing left to release

    def next_frame(self, timeout: float) -> bytes | None:
        if self.camera is None:
            raise CameraError("camera session is not open", "io")
        try:
            data = self.camera.capture_preview().get_data_and_size()
        except self._errors() as exc:
            raise self._wrap(exc) from exc
        return bytes(data)

    def read(self, keys: list[str]) -> dict[str, Reading]:
        if self.camera is None:
            raise CameraError("camera session is not open", "io")
        out: dict[str, Reading] = {}
        for key in keys:
            try:
                out[key] = self._read_one(key)
            except self._errors() as exc:
                err = self._wrap(exc)
                if err.kind == "io":
                    raise err from exc
                out[key] = Reading(key=key, error=str(err))
        return out

    def _read_one(self, key: str) -> Reading:
        gp = self.gp
        widget = self.camera.get_single_config(key)
        wtype = widget.get_type()
        names = {
            getattr(gp, "GP_WIDGET_RADIO", -1): "RADIO",
            getattr(gp, "GP_WIDGET_MENU", -2): "MENU",
            getattr(gp, "GP_WIDGET_RANGE", -3): "RANGE",
            getattr(gp, "GP_WIDGET_TOGGLE", -4): "TOGGLE",
            getattr(gp, "GP_WIDGET_TEXT", -5): "TEXT",
            getattr(gp, "GP_WIDGET_DATE", -6): "DATE",
        }
        tname = names.get(wtype, "OTHER")
        reading = Reading(key=key, type=tname, label=widget.get_label(), readonly=bool(widget.get_readonly()))
        value = widget.get_value()
        reading.value = None if value is None else str(value)
        if tname in ("RADIO", "MENU"):
            reading.choices = [str(widget.get_choice(i)) for i in range(widget.count_choices())]
        elif tname == "RANGE":
            lo, hi, step = widget.get_range()
            reading.range = (float(lo), float(hi), float(step))
        return reading

    def write(self, items: list[tuple[str, str]]) -> None:
        if self.camera is None:
            raise CameraError("camera session is not open", "io")
        for key, value in items:
            try:
                widget = self.camera.get_single_config(key)
                widget.set_value(value)
                self.camera.set_single_config(key, widget)
            except self._errors() as exc:
                raise self._wrap(exc) from exc

    def stderr_tail(self) -> str:
        return ""


# ---------- the gphoto2 command ----------
Runner = Callable[[list[str], float], "tuple[int, str, str]"]


def run_gphoto2(argv: list[str], timeout: float) -> tuple[int, str, str]:
    """Run the gphoto2 CLI with a hard deadline; (returncode, stdout, stderr). A hang is killed, never waited on."""
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError as exc:
        raise CameraError(f"cannot start {argv[0]}: {exc}", "missing") from exc
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        system.reap(proc)
        raise CameraError(f"{argv[0]} did not answer within {timeout:.0f}s", "io") from exc
    return proc.returncode, out, err


class CliSession:
    """Live view from `gphoto2 --stdout --capture-movie`; control changes pause it (the CLI cannot share a session)."""

    shared = False
    USB_SETTLE = 0.6  # the kernel needs a moment to hand the interface back after the capture process exits

    def __init__(
        self,
        gphoto2: str = "gphoto2",
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        runner: Runner = run_gphoto2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.gphoto2 = gphoto2
        self.popen = popen
        self.runner = runner
        self.sleep = sleep
        self.proc: subprocess.Popen | None = None
        self.frames: queue.Queue[bytes] = queue.Queue(maxsize=2)
        self.err: deque[str] = deque(maxlen=12)
        self.paused = False
        self._threads: list[threading.Thread] = []
        self.exit_code: int | None = None

    def open(self) -> None:
        self._start_capture()

    def _start_capture(self) -> None:
        try:
            self.proc = self.popen(
                [self.gphoto2, "--stdout", "--capture-movie"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
        except OSError as exc:
            raise CameraError(f"cannot start gphoto2: {exc}", "missing") from exc
        self.exit_code = None
        for target in (self._pump, self._stderr):
            t = threading.Thread(target=target, args=(self.proc,), daemon=True, name=f"canon-{target.__name__}")
            t.start()
            self._threads.append(t)

    def _pump(self, proc: subprocess.Popen) -> None:
        splitter = JpegSplitter()
        stream = proc.stdout
        if stream is None:
            return
        while True:
            chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
            if not chunk:
                break
            for frame in splitter.feed(chunk):
                if proc is not self.proc:
                    return
                try:
                    self.frames.put_nowait(frame)
                except queue.Full:
                    try:
                        self.frames.get_nowait()  # keep only the newest: latency over completeness
                    except queue.Empty:
                        continue
                    self.frames.put_nowait(frame)
        self.exit_code = proc.poll()

    def _stderr(self, proc: subprocess.Popen) -> None:
        stream = proc.stderr
        if stream is None:
            return
        for line in iter(stream.readline, b""):
            text = line.decode("utf-8", "replace").strip() if isinstance(line, bytes) else str(line).strip()
            if text:
                self.err.append(text)

    def _stop_capture(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None:
            system.reap(proc)
        while True:
            try:
                self.frames.get_nowait()
            except queue.Empty:
                break

    def close(self) -> None:
        self._stop_capture()

    def next_frame(self, timeout: float) -> bytes | None:
        if self.paused:
            self.sleep(min(timeout, 0.05))
            return None
        try:
            return self.frames.get(timeout=timeout)
        except queue.Empty:
            proc = self.proc
            if proc is not None and proc.poll() is not None:
                raise cli_error(proc.returncode, self.stderr_tail()) from None
            return None

    def stderr_tail(self) -> str:
        return " | ".join(self.err)

    def _exclusive(self, work: Callable[[], Any]) -> Any:
        """Pause live view, run `work` with the camera free, restart live view."""
        self.paused = True
        try:
            self._stop_capture()
            self.sleep(self.USB_SETTLE)
            return work()
        finally:
            self.paused = False
            self.sleep(self.USB_SETTLE / 2)
            self._start_capture()

    def read(self, keys: list[str]) -> dict[str, Reading]:
        argv = [self.gphoto2]
        for key in keys:
            argv += ["--get-config", key]
        _, out, err = self._exclusive(lambda: self.runner(argv, 15.0))
        blocks = parse_get_config(out)
        if len(blocks) == len(keys):
            return {k: reading_from_block(k, b) for k, b in zip(keys, blocks, strict=True)}
        # one unknown key aborts the whole batch; ask one by one so the rest still read
        result: dict[str, Reading] = {}
        for key in keys:
            rc, out, err = self._exclusive(lambda key=key: self.runner([self.gphoto2, "--get-config", key], 10.0))
            parsed = parse_get_config(out)
            if parsed:
                result[key] = reading_from_block(key, parsed[0])
            else:
                result[key] = Reading(key=key, error=str(cli_error(rc, err)))
        return result

    def write(self, items: list[tuple[str, str]]) -> None:
        argv = [self.gphoto2]
        for key, value in items:
            argv += ["--set-config-value", f"{key}={value}"]
        rc, _, err = self._exclusive(lambda: self.runner(argv, 15.0))
        if rc != 0:
            raise cli_error(rc, err)
