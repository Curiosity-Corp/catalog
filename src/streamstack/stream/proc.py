"""Supervised child processes: started in their own session, read by threads, and always killed on the way out.

An encode or capture process must never outlive the studio. Every `Child` registers itself, `kill_all()` ends the
survivors (it is also an `atexit` hook), and `stop()` escalates from closing stdin to SIGTERM to SIGKILL on the
whole process group, so an ffmpeg that is wedged in a network call cannot hold the device or the GPU.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import re
import signal
import subprocess
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable
from typing import IO

READ_SIZE = 65536
TAIL_LINES = 12
_live: weakref.WeakSet[Child] = weakref.WeakSet()
_live_lock = threading.Lock()


class Child:
    """One child process with a stdout byte handler, a stderr line handler and an exit handler."""

    def __init__(
        self,
        name: str,
        argv: list[str],
        *,
        stdin: bool = False,
        on_stdout: Callable[[bytes], None] | None = None,
        on_line: Callable[[str], None] | None = None,
        on_exit: Callable[[int], None] | None = None,
        redact: Callable[[str], str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self._argv = argv
        self._want_stdin = stdin
        self._on_stdout = on_stdout
        self._on_line = on_line
        self._on_exit = on_exit
        self._redact = redact or (lambda text: text)
        self._env = env
        self._proc: subprocess.Popen[bytes] | None = None
        self._threads: list[threading.Thread] = []
        self._stopping = threading.Event()
        self.tail: deque[str] = deque(maxlen=TAIL_LINES)
        self.started_at = 0.0
        self.returncode: int | None = None

    # -- lifecycle -----------------------------------------------------------------------------------------

    def start(self) -> None:
        env = dict(os.environ if self._env is None else self._env)
        env.setdefault("LIBVA_MESSAGING_LEVEL", "0")
        self._proc = subprocess.Popen(
            self._argv,
            stdin=subprocess.PIPE if self._want_stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE if self._on_stdout else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=True,
            env=env,
        )
        self.started_at = time.monotonic()
        with _live_lock:
            _live.add(self)
        if self._on_stdout and self._proc.stdout:
            self._spawn(self._read_stdout, self._proc.stdout)
        if self._proc.stderr:
            self._spawn(self._read_stderr, self._proc.stderr)
        self._spawn(self._wait)

    def _spawn(self, target: Callable[..., None], *args: object) -> None:
        thread = threading.Thread(target=target, args=args, name=f"stream-{self.name}", daemon=True)
        thread.start()
        self._threads.append(thread)

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def uptime(self) -> float:
        return time.monotonic() - self.started_at if self.alive else 0.0

    @property
    def stdin(self) -> IO[bytes] | None:
        return self._proc.stdin if self._proc else None

    def write(self, data: bytes) -> None:
        """Write to the child's stdin (blocking). Raises BrokenPipeError/OSError when the child is gone."""
        pipe = self.stdin
        if pipe is None:
            raise BrokenPipeError("the process has no stdin")
        pipe.write(data)

    def close_stdin(self) -> None:
        pipe = self.stdin
        if pipe is not None:
            with contextlib.suppress(OSError, ValueError):
                pipe.close()

    def stop(self, grace: float = 2.0) -> int | None:
        """End the process: close stdin, SIGTERM the group, SIGKILL after `grace` seconds. Returns the exit code."""
        proc = self._proc
        if proc is None:
            return None
        self._stopping.set()
        self.close_stdin()
        if proc.poll() is None:
            self._signal(signal.SIGTERM)
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                self._signal(signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=grace)
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=1.0)
        return proc.returncode

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    def _signal(self, sig: int) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(sig)

    # -- readers -------------------------------------------------------------------------------------------

    def _read_stdout(self, pipe: IO[bytes]) -> None:
        handler = self._on_stdout
        while handler is not None:
            try:
                data = os.read(pipe.fileno(), READ_SIZE)
            except (OSError, ValueError):
                return
            if not data:
                return
            try:
                handler(data)
            except Exception as exc:  # a handler bug must end this process, not silently stall the reader
                self.tail.append(f"output handler failed: {type(exc).__name__}: {exc}")
                self._signal(signal.SIGTERM)
                return

    def _read_stderr(self, pipe: IO[bytes]) -> None:
        buffer = b""
        while True:
            try:
                data = os.read(pipe.fileno(), 4096)
            except (OSError, ValueError):
                return
            if not data:
                break
            buffer += data
            *lines, buffer = buffer.split(b"\n")
            for raw in lines:
                self._line(raw)
            if len(buffer) > 65536:
                buffer = buffer[-4096:]
        if buffer:
            self._line(buffer)

    def _line(self, raw: bytes) -> None:
        line = self._redact(raw.decode("utf-8", "replace").rstrip("\r"))
        if not line:
            return
        if self._on_line:
            try:
                self._on_line(line)
            except Exception as exc:
                self.tail.append(f"line handler failed: {type(exc).__name__}: {exc}")
        if "=" not in line or " " in line.split("=", 1)[0]:  # progress key=value lines are not diagnostics
            self.tail.append(line)

    def _wait(self) -> None:
        proc = self._proc
        if proc is None:
            return
        code = proc.wait()
        self.returncode = code
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2.0)
        with _live_lock:
            _live.discard(self)
        if self._on_exit:
            try:
                self._on_exit(code)
            except Exception as exc:
                self.tail.append(f"exit handler failed: {type(exc).__name__}: {exc}")

    def last_error(self) -> str:
        """The line that says why the process ended: its last output, unless that is only ffmpeg's closing summary.

        ffmpeg ends a failed connection with "Error opening output ...: Input/output error", after the line that
        names the cause (`[tls @ 0x...] Peer certificate failed verification`). The cause is what the person needs.
        """
        for line in reversed(self.tail):
            if not _CLOSING_SUMMARY.match(line):
                return _LOG_PREFIX.sub("", line)
        return self.tail[-1] if self.tail else ""


_CLOSING_SUMMARY = re.compile(
    r"(\[out#\d+[^\]]*\] )?(Error opening (output|input)|Conversion failed|Error muxing)"
    r"|\[\w+ @ 0x[0-9a-f]+\] Cannot open connection",
    re.I,
)
_LOG_PREFIX = re.compile(r"^\[[a-z0-9_#/.-]+ @ 0x[0-9a-f]+\] ", re.I)


class ProgressParser:
    """Collects ffmpeg `-progress` key=value lines into one dict per report (the `progress=` line ends it)."""

    def __init__(self) -> None:
        self._current: dict[str, str] = {}

    def feed(self, line: str) -> dict[str, str] | None:
        key, sep, value = line.partition("=")
        if not sep or " " in key:
            return None
        self._current[key.strip()] = value.strip()
        if key.strip() == "progress":
            done, self._current = self._current, {}
            return done
        return None


def kill_all() -> int:
    """End every child that is still running (shutdown, tests). Returns how many were stopped."""
    with _live_lock:
        children = list(_live)
    for child in children:
        child.stop(grace=1.0)
    return len(children)


atexit.register(kill_all)
