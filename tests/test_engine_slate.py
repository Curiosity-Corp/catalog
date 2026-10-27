"""The "camera reconnecting" slate: its ffmpeg command line, and the process that runs it."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from streamstack.engine import slate
from streamstack.engine.slate import SlateFeeder, SlateSpec

SPEC = SlateSpec("/dev/video43", 1280, 720, 30.0, reason="Canon EOS 50D has no picture")


def test_the_card_writes_the_engines_caps_to_the_program_node() -> None:
    argv = slate.slate_argv(SPEC, textfile=Path("/tmp/s"))
    assert argv[argv.index("-i") + 1] == "color=c=0x0e1a24:s=1280x720:r=30"
    assert argv[-5:] == ["-pix_fmt", "yuyv422", "-f", "v4l2", "/dev/video43"]
    assert argv[argv.index("-r") + 1] == "30"
    assert "-re" in argv  # real time: a slate that runs faster than the frame rate would flood the node
    chain = argv[argv.index("-vf") + 1]
    assert chain.count("drawtext=") == 3 and chain.endswith("format=yuyv422")


def test_the_hold_variant_blurs_and_darkens_the_last_frame() -> None:
    argv = slate.slate_argv(SlateSpec("/dev/video43", 1920, 1080, 25.0, hold=True), hold_image=Path("/tmp/h.jpg"))
    assert argv[argv.index("-i") + 1] == "/tmp/h.jpg" and "-loop" in argv
    chain = argv[argv.index("-vf") + 1]
    assert chain.startswith("scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,gblur")
    assert argv[argv.index("-framerate") + 1] == "25"


def test_without_text_rendering_the_card_has_no_drawtext() -> None:
    chain = slate.slate_argv(SPEC, textfile=Path("/tmp/s"), text=False)
    assert "drawtext" not in " ".join(chain)


def test_the_message_says_what_is_going_on() -> None:
    assert slate.message_text(SPEC).splitlines() == [
        "StreamStack",
        "Camera reconnecting",
        "Canon EOS 50D has no picture",
    ]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="no ffmpeg")
@pytest.mark.parametrize("hold", [False, True])
def test_ffmpeg_accepts_the_graph_and_renders_a_yuyv_frame(tmp_path: Path, hold: bool) -> None:
    """The filter chain parses and runs (to a file, never to a device)."""
    feeder = SlateFeeder(work_dir=tmp_path)
    spec = SlateSpec("/dev/null", 640, 360, 30.0, reason="Canon EOS 50D has no picture", hold=hold)
    if hold:
        jpeg = tmp_path / "src.jpg"
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180", "-frames:v", "1", str(jpeg)],
            check=True,
        )
        feeder._hold_bytes = jpeg.read_bytes()
    base, hold_image = feeder._prepare(spec)
    argv = slate.slate_argv(spec, textfile=base, hold_image=hold_image)
    cut = argv.index("-f", argv.index("-pix_fmt"))
    out = tmp_path / "frame.yuv"
    run = subprocess.run([*argv[:cut], "-frames:v", "3", "-f", "rawvideo", str(out)], capture_output=True, text=True)
    if "drawtext" in run.stderr and "No such filter" in run.stderr:
        pytest.skip("this ffmpeg has no drawtext")
    assert run.returncode == 0, run.stderr
    assert out.stat().st_size == 3 * 640 * 360 * 2  # three YUYV frames


class FakeProc:
    def __init__(self, stderr: bytes = b"") -> None:
        import io

        self.pid = 2_000_000_000  # no such process
        self.returncode: int | None = None
        self.stderr = io.BytesIO(stderr)
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int | None:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("ffmpeg", timeout or 0)
        return self.returncode


@pytest.fixture
def feeder(tmp_path: Path):
    procs: list[FakeProc] = []
    argvs: list[list[str]] = []

    def popen(argv: list[str], **kwargs) -> FakeProc:
        argvs.append(argv)
        proc = FakeProc(kwargs.get("fake_stderr", b""))
        procs.append(proc)
        return proc

    feeder = SlateFeeder(popen=popen, work_dir=tmp_path)
    feeder.procs, feeder.argvs = procs, argvs  # type: ignore[attr-defined]
    return feeder


def test_show_starts_one_writer_and_is_idempotent(feeder) -> None:
    feeder.show(SPEC)
    feeder.show(SPEC)
    assert len(feeder.procs) == 1 and feeder.active
    state = feeder.state()
    assert state["active"] and state["writing"] and state["mode"] == "card" and state["reason"] == SPEC.reason


def test_a_new_reason_replaces_the_writer_one_at_a_time(feeder) -> None:
    feeder.show(SPEC)
    feeder.show(SlateSpec("/dev/video43", 1280, 720, 30.0, reason="the Cam Link has no signal"))
    assert len(feeder.procs) == 2 and feeder.procs[0].terminated and not feeder.procs[1].terminated
    assert (feeder._work_dir() / "slate-2.txt").read_text() == "the Cam Link has no signal"


def test_clear_stops_the_writer_and_waits_for_it(feeder) -> None:
    feeder.show(SPEC)
    feeder.clear()
    assert feeder.procs[0].terminated and feeder.procs[0].returncode is not None
    assert not feeder.active and feeder.state()["writing"] is False
    feeder.clear()  # idempotent


def test_a_dead_writer_is_restarted_by_poll(feeder) -> None:
    feeder.show(SPEC)
    feeder.procs[0].returncode = 1
    feeder.poll()
    assert len(feeder.procs) == 2 and feeder.state()["writing"]


def test_poll_does_nothing_when_no_slate_is_wanted(feeder) -> None:
    feeder.poll()
    assert feeder.procs == []


def test_no_drawtext_falls_back_to_the_card_without_words(feeder) -> None:
    feeder.show(SPEC)
    feeder.procs[0].stderr.close()
    feeder._stderr.append("No such filter: 'drawtext'")
    feeder.procs[0].returncode = 1
    feeder.poll()
    assert feeder.state()["words"] is False
    assert "drawtext" not in " ".join(feeder.argvs[1])


def test_a_late_crash_is_an_error_not_a_downgrade(feeder) -> None:
    feeder._clock = lambda: 100.0
    feeder.show(SPEC)
    feeder._started = 0.0  # it ran for a long time
    feeder._stderr.append("Device or resource busy")
    feeder.procs[0].returncode = 1
    feeder.poll()
    assert feeder.state()["words"] is True and len(feeder.procs) == 2


def test_cannot_start_ffmpeg_is_reported(tmp_path: Path) -> None:
    def broken(argv: list[str], **kwargs):
        raise FileNotFoundError(2, "No such file or directory")

    feeder = SlateFeeder(popen=broken, work_dir=tmp_path)
    feeder.show(SPEC)
    state = feeder.state()
    assert state["writing"] is False and "cannot start the camera slate" in state["error"]


def test_hold_writes_the_last_frame_next_to_the_message(feeder) -> None:
    feeder.show(SlateSpec("/dev/video43", 1280, 720, 30.0, hold=True), hold_frame=b"\xff\xd8jpeg\xff\xd9")
    assert (feeder._work_dir() / "hold.jpg").read_bytes() == b"\xff\xd8jpeg\xff\xd9"
    assert feeder.state()["mode"] == "hold"


def test_hold_without_a_frame_is_the_card(feeder) -> None:
    feeder.show(SlateSpec("/dev/video43", 1280, 720, 30.0, hold=True), hold_frame=None)
    assert feeder.state()["mode"] == "card"


def test_a_real_child_is_terminated(tmp_path: Path) -> None:
    """The stop path against a real process (a sleeping Python stands in for ffmpeg)."""

    def popen(argv: list[str], **kwargs):
        return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)

    feeder = SlateFeeder(popen=popen, work_dir=tmp_path)
    feeder.show(SPEC)
    proc = feeder._proc
    assert proc is not None and proc.poll() is None
    feeder.clear()
    assert proc.poll() is not None


def test_the_default_spawner_is_refused_in_tests() -> None:
    """The suite must never write a video node of the machine it runs on (a call may be on it)."""
    feeder = SlateFeeder()
    feeder.show(SPEC)
    assert feeder.state()["writing"] is False and "cannot start the camera slate" in feeder.state()["error"]


def test_a_leftover_slate_of_a_crashed_console_is_stopped_before_the_next_one_starts(tmp_path: Path, feeder) -> None:
    sleeper = subprocess.Popen(["sleep", "60"])
    try:
        # the pid file names a process that is not a slate ffmpeg: it must be left alone
        (feeder._work_dir() / slate.PIDFILE).write_text(f"{sleeper.pid}\n")
        feeder.show(SPEC)
        assert sleeper.poll() is None
    finally:
        sleeper.kill()
        sleeper.wait()


def test_a_dead_pid_in_the_pid_file_is_ignored(feeder) -> None:
    (feeder._work_dir() / slate.PIDFILE).write_text("2147483000\n")
    feeder.show(SPEC)
    assert feeder.state()["writing"]
