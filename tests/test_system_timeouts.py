"""Hard deadlines for helpers that talk to the audio server: nothing may hang, and nothing may be left running."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from streamstack import system

DEAF = (
    "import signal, time\n"
    "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "print('up', flush=True)\n"
    "time.sleep(120)\n"
)
TIGHT = (0.3, 0.3, 2.0)  # short grace so the tests are quick


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"  # a zombie is dead, just not reaped
    except OSError:
        return False


def spawn(code: str) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE)
    assert proc.stdout is not None
    proc.stdout.readline()  # wait until the child installed its handlers
    return proc


def test_reap_stops_a_polite_child_with_sigint() -> None:
    proc = spawn("import time; print('up', flush=True)\ntime.sleep(120)")
    assert system.reap(proc, TIGHT)
    assert proc.returncode is not None and not alive(proc.pid)


def test_reap_escalates_to_sigkill_for_a_child_that_ignores_everything() -> None:
    proc = spawn(DEAF)
    started = time.monotonic()
    assert system.reap(proc, TIGHT)
    assert time.monotonic() - started < 3
    assert proc.returncode == -9  # SIGKILL
    assert not alive(proc.pid)


def test_reap_of_a_finished_child_is_a_no_op() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert system.reap(proc, TIGHT)


class Stubborn:
    """A process that survives everything, like one stuck in an uninterruptible read."""

    pid = 424242

    def __init__(self) -> None:
        self.signals: list[int] = []

    def poll(self) -> None:
        return None

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)

    def wait(self, timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired("stubborn", timeout or 0)


def test_reap_never_waits_forever_on_an_unkillable_process() -> None:
    proc = Stubborn()
    assert system.reap(proc, (0.0, 0.0, 0.0)) is False
    assert [s.name for s in map(__import__("signal").Signals, proc.signals)] == ["SIGINT", "SIGTERM", "SIGKILL"]


def test_run_kills_and_reaps_a_hung_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    pids: list[int] = []
    real_popen = subprocess.Popen

    def popen(argv: list[str], **kwargs: Any) -> subprocess.Popen[str]:
        proc = real_popen([sys.executable, "-c", DEAF], **kwargs)
        pids.append(proc.pid)
        return proc

    monkeypatch.setattr(subprocess, "Popen", popen)
    started = time.monotonic()
    assert system.run(["pactl", "info"], timeout=0.5) is None
    assert time.monotonic() - started < 5
    assert pids and not alive(pids[0])


def test_run_returns_output_and_code() -> None:
    result = system.run([sys.executable, "-c", "import sys; print('hi'); sys.exit(3)"])
    assert result is not None
    assert (result.returncode, result.stdout.strip()) == (3, "hi")
    assert system.run(["/nonexistent/streamstack-tool"]) is None


def test_record_has_a_deadline_and_reaps_a_capture_that_never_ends(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """parecord on a wedged server: no data, ignores SIGINT. record() must still return, and kill it."""
    pids: list[int] = []
    real_popen = subprocess.Popen
    target = tmp_path / "take.wav"

    def popen(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        assert argv[0] == "parecord" and "--device=mic" in argv
        proc = real_popen([sys.executable, "-c", DEAF], **kwargs)
        pids.append(proc.pid)
        return proc

    monkeypatch.setattr(subprocess, "Popen", popen)
    started = time.monotonic()
    assert system.record("mic", 0.5, str(target), grace=TIGHT) is False  # nothing was captured
    assert time.monotonic() - started < 5
    assert pids and not alive(pids[0])


def test_record_succeeds_when_audio_arrives(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "take.wav"
    real_popen = subprocess.Popen
    code = f"import time; open({str(target)!r}, 'wb').write(b'RIFF' + bytes(4000)); time.sleep(120)"

    def popen(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        return real_popen([sys.executable, "-c", code], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    assert system.record("mic", 0.6, str(target), grace=TIGHT) is True


def test_record_without_parecord_is_a_clean_false(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def popen(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        raise FileNotFoundError("parecord")

    monkeypatch.setattr(subprocess, "Popen", popen)
    assert system.record("mic", 1, str(tmp_path / "x.wav")) is False
