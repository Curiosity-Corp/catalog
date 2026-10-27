"""The "camera reconnecting" slate: keeps the program camera listed while the real camera is away.

With `exclusive_caps=1` a loopback node is only listed by Chromium, Edge, Teams and Zoom while a process feeds it
(ADR 0004). When the source camera loses its producer (the Canon bridge dies, the Cam Link is unplugged) the engine
has nothing to read and stops writing, the node loses its producer and Teams drops "StreamStack Camera" in the middle
of a call. The slate is a second, tiny ffmpeg writer for the same node, at the same caps the engine writes (size,
YUYV, frame rate), that runs *only* while the engine does not: either a branded card that says what is going on, or
the last frame the engine delivered, darkened and blurred, with the same line over it. Exactly one of the two writes
the node at any moment; the supervisor releases the slate before the engine child opens the node (`clear` waits until
the node is free), and the call app, which holds its own capture handle, sees a short freeze and then live video.

Nothing here opens a /dev/video node from Python: only the ffmpeg child writes it.
"""

from __future__ import annotations

import collections
import contextlib
import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from streamstack import config

log = logging.getLogger(__name__)

BRAND = "StreamStack"
BACKGROUND = "0x0e1a24"
ACCENT = "0x2fb5a8"
CAPTION = "Camera reconnecting"
DEFAULT_SIZE = (1920, 1080)  # the contract in ADR 0004: what the engine writes unless STREAMSTACK_ENGINE_SIZE says
DEFAULT_FPS = 30.0
EARLY_EXIT_SECONDS = 3.0  # a child that dies this fast failed to start (no drawtext or font), it did not crash later
STOP_WAIT = (2.0, 2.0)  # after `q`, after SIGTERM; then SIGKILL


@dataclass(frozen=True)
class SlateSpec:
    """What the slate writes and says. `width`/`height`/`fps` must equal what the engine writes to the node."""

    device: str
    width: int = DEFAULT_SIZE[0]
    height: int = DEFAULT_SIZE[1]
    fps: float = DEFAULT_FPS
    pix_fmt: str = "yuyv422"
    reason: str = ""
    caption: str = CAPTION  # the big line; the idle placeholder says something calmer than "reconnecting"
    hold: bool = False  # use the last delivered frame as the background when the caller has one


def _fnum(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def message_text(spec: SlateSpec) -> str:
    """Three lines: the brand, the caption and (when known) why. Plain text, written to a file for drawtext."""
    lines = [BRAND, spec.caption]
    if spec.reason:
        lines.append(spec.reason)
    return "\n".join(lines) + "\n"


def drawtext_filters(spec: SlateSpec, textfile: Path) -> str:
    """One drawtext per line so each has its own size; `textfile` avoids every escaping rule of inline text."""
    h = spec.height
    brand, caption, reason = max(14, h // 22), max(20, h // 12), max(12, h // 36)
    parts = []
    lines = [(brand, ACCENT, h * 0.34), (caption, "white", h * 0.44), (reason, "0xb8c4cc", h * 0.58)]
    for index, (size, color, y) in enumerate(lines):
        # drawtext reads the whole file: select the line by an expansion-free split into three single-line files
        parts.append(
            f"drawtext=textfile={_quote(str(textfile.with_name(f'{textfile.stem}-{index}.txt')))}"
            f":fontcolor={color}:fontsize={size}:x=(w-text_w)/2:y={int(y)}"
        )
    return ",".join(parts)


def _quote(value: str) -> str:
    """Quote a filtergraph value: single quotes, with the characters the filter parser reads escaped."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:") + "'"


def pulse_filter(spec: SlateSpec) -> str:
    """A bar along the bottom that breathes once a second, so a viewer can tell the slate from a frozen frame."""
    h = spec.height
    bar = max(6, h // 90)
    return f"drawbox=x=0:y=ih-{bar}:w=iw:h={bar}:color={ACCENT}@0.9:t=fill:enable='lt(mod(t,2),1)'"


def slate_argv(
    spec: SlateSpec, *, textfile: Path | None = None, hold_image: Path | None = None, text: bool = True
) -> list[str]:
    """The ffmpeg command line for the slate. `text` False leaves the words out (no drawtext in this build)."""
    size = f"{spec.width}x{spec.height}"
    rate = _fnum(spec.fps)
    argv = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-nostats", "-y"]
    if hold_image is not None:
        argv += ["-re", "-loop", "1", "-framerate", rate, "-i", str(hold_image)]
        chain = [
            f"scale={spec.width}:{spec.height}:force_original_aspect_ratio=increase",
            f"crop={spec.width}:{spec.height}",
            "gblur=sigma=12",
            "eq=brightness=-0.22:saturation=0.6",
        ]
    else:
        argv += ["-re", "-f", "lavfi", "-i", f"color=c={BACKGROUND}:s={size}:r={rate}"]
        chain = []
    chain.append(pulse_filter(spec))
    if text and textfile is not None:
        chain.append(drawtext_filters(spec, textfile))
    chain.append(f"format={spec.pix_fmt}")
    argv += ["-vf", ",".join(chain), "-r", rate, "-pix_fmt", spec.pix_fmt, "-f", "v4l2", spec.device]
    return argv


def _spawn(argv: list[str], **kwargs) -> subprocess.Popen:
    """The one place the slate starts a process. The test suite replaces it: no test may write a video node."""
    return subprocess.Popen(argv, **kwargs)


PIDFILE = "streamstack-slate.pid"
WORK_DIR = "streamstack-slate"


class SlateFeeder:
    """Runs and stops the slate child. Thread-safe; `show` and `clear` are idempotent."""

    def __init__(
        self,
        *,
        popen: Callable[..., subprocess.Popen] | None = None,
        clock: Callable[[], float] = time.monotonic,
        work_dir: Path | None = None,
    ) -> None:
        self._popen = popen or _spawn
        self._clock = clock
        self._dir = work_dir
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._spec: SlateSpec | None = None
        self._started = 0.0
        self._since: float | None = None
        self._plain = False  # drawtext failed on this host: show the colour card without words
        self._error: str | None = None
        self._starts = 0
        self._stderr: collections.deque[str] = collections.deque(maxlen=10)
        self._hold_bytes: bytes | None = None

    # -- control ---------------------------------------------------------------------------------------------------

    def show(self, spec: SlateSpec, hold_frame: bytes | None = None) -> None:
        """Make sure the slate for `spec` is being written. A running slate with the same spec is left alone; with a
        different reason or device it is restarted (the node keeps its caps, so readers only see a new message)."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None and spec == self._spec:
                return
            self._stop_locked()
            self._spec = spec
            self._hold_bytes = hold_frame if spec.hold else None
            self._since = self._since if self._since is not None else self._clock()
            self._start_locked()

    def clear(self) -> None:
        """Stop the slate and return only once the node is free (the engine child opens it next)."""
        with self._lock:
            self._stop_locked()
            self._spec = None
            self._since = None
            self._error = None

    def poll(self) -> None:
        """Restart a slate child that died while it should be showing (called from the monitor tick)."""
        with self._lock:
            proc, spec = self._proc, self._spec
            if spec is None or (proc is not None and proc.poll() is None):
                return
            if proc is not None:
                self._note_exit(proc)
            self._start_locked()

    @property
    def active(self) -> bool:
        with self._lock:
            return self._spec is not None

    def state(self) -> dict:
        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
            spec = self._spec
            return {
                "active": spec is not None,
                "writing": running,
                "reason": spec.reason if spec else "",
                "mode": ("hold" if self._hold_bytes else "card") if spec else "",
                "seconds": round(self._clock() - self._since, 1) if self._since is not None else 0.0,
                "starts": self._starts,
                "words": not self._plain,
                "error": self._error,
            }

    # -- internals -------------------------------------------------------------------------------------------------

    def _work_dir(self) -> Path:
        if self._dir is None:
            self._dir = _runtime_dir() / WORK_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        return self._dir

    def _prepare(self, spec: SlateSpec) -> tuple[Path, Path | None]:
        folder = self._work_dir()
        base = folder / "slate"
        lines = message_text(spec).splitlines()
        lines += [" "] * (3 - len(lines))  # drawtext refuses an empty file, a space draws nothing
        for index, line in enumerate(lines):
            base.with_name(f"slate-{index}.txt").write_text(line)
        hold = None
        if self._hold_bytes:
            hold = folder / "hold.jpg"
            hold.write_bytes(self._hold_bytes)
        return base, hold

    def _start_locked(self) -> None:
        spec = self._spec
        if spec is None:
            return
        try:
            self._reap_stale()
            base, hold = self._prepare(spec)
            argv = slate_argv(spec, textfile=base, hold_image=hold, text=not self._plain)
            self._proc = self._popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, bufsize=0
            )
        except OSError as exc:
            self._proc = None
            self._error = f"cannot start the camera slate: {exc.strerror or exc}"
            log.warning("slate: %s", self._error)
            return
        self._remember(self._proc.pid)
        self._started = self._clock()
        self._starts += 1
        self._error = None
        threading.Thread(target=self._drain, args=(self._proc,), daemon=True, name="streamstack-slate-log").start()

    def _pidfile(self) -> Path:
        return self._work_dir() / PIDFILE

    def _remember(self, pid: int) -> None:
        try:
            self._pidfile().write_text(f"{pid}\n")
        except OSError as exc:
            log.warning("slate: cannot record pid %d: %s", pid, exc)

    def _reap_stale(self) -> None:
        """A slate left behind by a Console that crashed still holds the node, and the engine could not open it.
        The pid file names it; it is stopped only when that pid is still a slate ffmpeg (never another program)."""
        try:
            pid = int(self._pidfile().read_text().strip())
        except (OSError, ValueError):
            return
        if self._proc is not None and self._proc.pid == pid:
            return
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return  # gone already
        if b"ffmpeg" in cmdline and WORK_DIR.encode() in cmdline:
            log.warning("slate: stopping a leftover slate writer (pid %d)", pid)
            with contextlib.suppress(OSError):
                os.kill(pid, 15)
            time.sleep(0.3)

    def _drain(self, proc: subprocess.Popen) -> None:
        try:
            while proc.stderr is not None:
                line = proc.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    self._stderr.append(text)
        except (OSError, ValueError):
            return

    def _note_exit(self, proc: subprocess.Popen) -> None:
        tail = " | ".join(list(self._stderr)[-2:]) or f"exit code {proc.returncode}"
        early = self._clock() - self._started < EARLY_EXIT_SECONDS
        if early and not self._plain and any(w in tail.lower() for w in ("drawtext", "font", "freetype", "textfile")):
            self._plain = True  # no text rendering here: the card still keeps the node listed
            log.warning("slate: drawtext is unavailable (%s); showing the card without words", tail)
        else:
            self._error = f"the camera slate stopped: {tail}"
            log.warning("slate: %s", self._error)

    def _stop_locked(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        with contextlib.suppress(OSError):
            proc.terminate()
        for wait in STOP_WAIT:
            try:
                proc.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=2)


def _runtime_dir() -> Path:
    base = Path(os.environ.get("XDG_RUNTIME_DIR") or config.state_dir())
    base.mkdir(parents=True, exist_ok=True)
    return base
