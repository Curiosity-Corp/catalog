from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from streamstack.stream import proc
from streamstack.stream.proc import Child
from streamstack.stream.sinks import FileSink, PipeSink


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture(autouse=True)
def no_leftover_children():
    yield
    proc.kill_all()  # the net under the tests: nothing they start may outlive them


def script(code: str):
    def spawn(on_exit: Callable[[int], None], on_line: Callable[[str], None]) -> Child:
        return Child("test", [sys.executable, "-c", code], stdin=True, on_exit=on_exit, on_line=on_line)

    return spawn


def test_pipe_sink_delivers_bytes_to_the_child(tmp_path: Path) -> None:
    out = tmp_path / "out.bin"
    code = (
        f"import sys; f=open({str(out)!r},'wb',buffering=0)\n"
        "while True:\n d=sys.stdin.buffer.read1(65536)\n if not d: break\n f.write(d)"
    )
    sink = PipeSink("t", script(code))
    sink.open(b"HEAD")
    assert sink.state == "connecting"
    assert sink.write(b"-one", 1, True) and sink.write(b"-two", 1, False)
    assert wait_for(lambda: out.exists() and out.read_bytes() == b"HEAD-one-two")
    sink.shutdown()
    assert sink.state == "stopped"


def test_progress_lines_make_the_sink_live(tmp_path: Path) -> None:
    code = (
        "import sys,time\n"
        "print('total_size=2048\\nspeed=1.00x\\nprogress=continue',file=sys.stderr,flush=True)\n"
        "sys.stdin.buffer.read()"
    )
    events: list[tuple[str, str]] = []
    sink = PipeSink("t", script(code), on_event=lambda k, t: events.append((k, t)))
    sink.open(b"H")
    assert wait_for(lambda: sink.state == "live")
    status = sink.status()
    assert status["net_bytes"] == 2048 and status["speed"] == 1.0
    assert ("ok", "t is connected") in events
    sink.shutdown()


def test_child_exit_backs_off_and_refuses_to_reopen_early() -> None:
    clock = Clock()
    code = "import sys; print('Connection refused', file=sys.stderr); sys.exit(1)"
    events: list[tuple[str, str]] = []
    sink = PipeSink("t", script(code), clock=clock, on_event=lambda k, t: events.append((k, t)))
    sink.open(b"H")
    assert wait_for(lambda: sink.state == "backoff")
    assert sink.last_error == "Connection refused"
    assert sink.restarts == 1 and sink.status()["retry_in_s"] == 1.0
    with pytest.raises(OSError, match="waiting"):
        sink.open(b"H")
    clock.now += 1.5
    sink.open(b"H")  # the wait is over
    assert wait_for(lambda: sink.restarts == 2)
    clock.now += 1.0
    with pytest.raises(OSError):  # the second failure waits 2 s
        sink.open(b"H")
    assert any("Retrying in 2s" in text for _, text in events)
    sink.shutdown()


def test_a_missing_executable_is_an_oserror_and_a_failure() -> None:
    def spawn(on_exit: Callable[[int], None], on_line: Callable[[str], None]) -> Child:
        return Child("t", ["/nonexistent/ffmpeg"], stdin=True, on_exit=on_exit, on_line=on_line)

    sink = PipeSink("t", spawn)
    with pytest.raises(OSError, match="cannot start ffmpeg"):
        sink.open(b"H")
    assert sink.state == "backoff"


def test_a_full_queue_drops_to_the_next_keyframe() -> None:
    code = "import sys,time; time.sleep(30)"  # never reads, so the pipe and the queue fill
    sink = PipeSink("t", script(code), queue_bytes=1000)
    sink.open(b"H")
    # The writer thread moves queued frames into the OS pipe (64 KiB or more) until the child's side is full, so
    # only then can the 1000-byte queue overflow. Write until it does instead of racing the thread.
    for _ in range(10_000):
        assert sink.write(b"x" * 600, 1, True)
        if sink.dropped_frames:
            break
    assert sink.dropped_frames == 1  # overflowed: queue cleared, waiting for a keyframe
    assert sink.write(b"x" * 100, 1, False)  # discarded while resyncing
    assert sink.dropped_frames == 2
    assert sink.write(b"x" * 100, 1, True)  # keyframe: accepted again
    assert sink.dropped_frames == 2
    sink.shutdown()


def test_write_without_open_returns_false_and_close_is_not_shutdown() -> None:
    sink = PipeSink("t", script("import sys; sys.stdin.buffer.read()"))
    assert sink.write(b"x", 1, True) is False
    sink.open(b"H")
    sink.close()
    assert sink.state == "idle"
    sink.open(b"H")  # reopening after a geometry close is allowed
    sink.shutdown()
    with pytest.raises(OSError):
        sink.open(b"H")


def test_file_sink_writes_flushes_and_rolls_segments(tmp_path: Path) -> None:
    clock = Clock()
    sink = FileSink("rec", tmp_path / "sub" / "show.mkv", clock=clock)
    sink.open(b"HEAD")
    assert sink.write(b"-a", 1, True)
    clock.now += 2  # triggers an fsync, which must not fail
    assert sink.write(b"-b", 1, False)
    assert (tmp_path / "sub" / "show.mkv").read_bytes() == b"HEAD-a-b"
    sink.close()  # a geometry change closes the file
    assert sink.write(b"x", 1, False) is False
    sink.open(b"HEAD2")
    sink.write(b"-c", 1, True)
    sink.shutdown()
    assert (tmp_path / "sub" / "show-002.mkv").read_bytes() == b"HEAD2-c"
    assert [p.name for p in sink.paths] == ["show.mkv", "show-002.mkv"]
    assert sink.status()["state"] == "stopped" and sink.status()["file"].endswith("show-002.mkv")


def test_file_sink_reports_an_unwritable_path(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    sink = FileSink("rec", blocker / "show.mkv")
    with pytest.raises(OSError, match="cannot write"):
        sink.open(b"H")
    assert sink.status()["state"] == "failed" and "show.mkv" in sink.status()["last_error"]
