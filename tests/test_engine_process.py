"""The engine supervisor against a stand-in child (a Python script that logs stdin and reports progress)."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from streamstack.engine import process
from streamstack.engine.graph import BlurGraph, GraphError, Rect, Source, Spec

NEUTRAL = {
    "brightness": 0.0,
    "contrast": 0.0,
    "gamma": 0.0,
    "saturation": 0.0,
    "warmth": 0.0,
    "sharpen_enabled": False,
    "sharpen_amount": 0.08,
    "lut_enabled": False,
    "lut_amount": 0.35,
}
SOURCE = Source("Canon", "/dev/video42", "YUYV", 1280, 720, 20.0)
SPEC = Spec(SOURCE, "/dev/video43", 1280, 720)

CHILD = """
import os, sys, time
log, fd, mode = sys.argv[1], int(sys.argv[2]), sys.argv[3]
if mode == "die":
    sys.stderr.write("Device or resource busy\\n"); sys.exit(1)
out = os.fdopen(fd, "w")
out.write("fps=20.0\\ndrop_frames=2\\nspeed=1.0x\\nprogress=continue\\n"); out.flush()
if mode == "stall":  # one frame, then ffmpeg blocks in the V4L2 read: alive, silent, counter stuck
    out.write("frame=1\\nprogress=continue\\n"); out.flush()
if mode == "flow":  # a healthy child: the counter keeps moving
    import threading
    def count():
        n = 0
        while True:
            n += 1; out.write(f"frame={n}\\nprogress=continue\\n"); out.flush(); time.sleep(0.05)
    threading.Thread(target=count, daemon=True).start()
sys.stdout.buffer.write(b"\\xff\\xd8\\xff\\xe0jpegbytes\\xff\\xd9"); sys.stdout.flush()
with open(log, "ab", buffering=0) as handle:
    while True:
        chunk = os.read(0, 4096)
        if not chunk or chunk.startswith(b"q"):
            break
        handle.write(chunk)
"""


def fake_popen(log: Path, mode: str = "run", started: list | None = None) -> Callable[..., subprocess.Popen]:
    def popen(argv: list[str], **kwargs) -> subprocess.Popen:
        fd = int(argv[argv.index("-progress") + 1].split(":")[1])
        proc = subprocess.Popen([sys.executable, "-c", CHILD, str(log), str(fd), mode], **kwargs)
        if started is not None:
            started.append(argv)
        return proc

    return popen


def wait_for(check: Callable[[], bool], timeout: float = 30.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture(autouse=True)
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process, "BACKOFF", (0.05, 0.1))
    monkeypatch.setattr(process, "STOP_GRACE", (1.0, 1.0))


@pytest.fixture
def engine(tmp_path: Path):
    started: list[list[str]] = []
    eng = process.VideoEngine(SPEC, dict(NEUTRAL), popen=fake_popen(tmp_path / "stdin.log", started=started))
    eng.started = started  # type: ignore[attr-defined]
    eng.log = tmp_path / "stdin.log"  # type: ignore[attr-defined]
    yield eng
    eng.stop()


def commands_seen(engine) -> list[str]:
    try:
        return engine.log.read_text().splitlines()
    except OSError:
        return []


def test_engine_reports_running_progress_and_detection_frame(engine) -> None:
    engine.start()
    assert wait_for(lambda: engine.state()["state"] == "running")
    state = engine.state()
    assert state["fps"] == 20.0 and state["dropped"] == 2 and state["backend"] == "vulkan"
    assert engine.wait_frame(2) == b"\xff\xd8\xff\xe0jpegbytes\xff\xd9"


def test_look_changes_send_only_the_difference_without_restarting(engine) -> None:
    engine.start()
    assert wait_for(lambda: engine.state()["state"] == "running")
    pid = engine.state()["pid"]
    ms = engine.apply_look({**NEUTRAL, "brightness": 0.2})
    assert ms < 50
    assert wait_for(lambda: commands_seen(engine) == ["clibplacebo@grade -1 brightness 0.2"])
    engine.apply_look({**NEUTRAL, "brightness": 0.2})  # nothing changed: nothing sent
    engine.apply_look({**NEUTRAL, "brightness": 0.2, "sharpen_enabled": True, "sharpen_amount": 0.5})
    assert wait_for(lambda: commands_seen(engine)[-2:] == ["ccas@sharp -1 strength 0.5", "ccas@sharp -1 enable 1"])
    assert engine.state()["pid"] == pid and engine.state()["restarts"] == 0


@pytest.fixture
def gpu_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(process, "KEY_SETTLE", 0.15)
    started: list[list[str]] = []
    spec = Spec(SOURCE, "/dev/video43", 1280, 720, shaders=str(tmp_path / "shaders"))
    eng = process.VideoEngine(spec, dict(NEUTRAL), popen=fake_popen(tmp_path / "stdin.log", started=started))
    eng.started = started  # type: ignore[attr-defined]
    eng.log = tmp_path / "stdin.log"  # type: ignore[attr-defined]
    eng.shader_dir = tmp_path / "shaders"  # type: ignore[attr-defined]
    yield eng
    eng.stop()


def test_gpu_sharpen_is_baked_and_warmth_stays_a_live_command(gpu_engine) -> None:
    gpu_engine.start()
    assert wait_for(lambda: gpu_engine.state()["state"] == "running")
    assert gpu_engine.state()["sharpen"] == "gpu"
    gpu_engine.apply_look({**NEUTRAL, "warmth": 0.5})
    assert wait_for(lambda: commands_seen(gpu_engine) == ["clibplacebo@grade -1 extra_opts temperature=-0.5"])
    assert len(gpu_engine.started) == 1 and gpu_engine.state()["restarts"] == 0


def test_a_new_sharpen_step_rebuilds_the_child_once_the_look_rests(gpu_engine) -> None:
    gpu_engine.start()
    assert wait_for(lambda: gpu_engine.state()["state"] == "running")
    for amount in (0.1, 0.2, 0.3):  # a slider drag: three steps in quick succession
        gpu_engine.apply_look({**NEUTRAL, "sharpen_enabled": True, "sharpen_amount": amount})
    assert len(gpu_engine.started) == 1  # nothing rebuilt while the look is moving
    assert wait_for(lambda: len(gpu_engine.started) == 2)
    text = gpu_engine.started[1][gpu_engine.started[1].index("-filter_complex") + 1]
    assert "cas-0.30.hook" in text and "cas@" not in text
    assert (gpu_engine.shader_dir / "cas-0.30.hook").is_file()
    assert wait_for(lambda: gpu_engine.state()["state"] == "running")
    gpu_engine.apply_look({**NEUTRAL, "sharpen_enabled": True, "sharpen_amount": 0.31})  # same step: no rebuild
    time.sleep(0.4)
    assert len(gpu_engine.started) == 2


def test_a_look_lut_is_written_and_rebuilds_the_child_once(gpu_engine) -> None:
    gpu_engine.start()
    assert wait_for(lambda: gpu_engine.state()["state"] == "running")
    gpu_engine.apply_look({**NEUTRAL, "lut": "portra", "lut_enabled": True, "lut_amount": 0.7})
    assert wait_for(lambda: len(gpu_engine.started) == 2)
    text = gpu_engine.started[1][gpu_engine.started[1].index("-filter_complex") + 1]
    assert "lut-portra-14.cube:lut_type=normalized" in text and "colorcorrect" not in text
    assert (gpu_engine.shader_dir / "lut-portra-14.cube").is_file()
    assert wait_for(lambda: gpu_engine.state()["state"] == "running")
    gpu_engine.apply_look({**NEUTRAL, "lut": "portra", "lut_enabled": True, "lut_amount": 0.71, "contrast": 0.1})
    time.sleep(0.4)
    assert len(gpu_engine.started) == 2  # the same step and a live slider: no rebuild


def test_an_unwritable_lut_falls_back_to_the_split_tone_without_looping(tmp_path: Path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    spec = Spec(SOURCE, "/dev/video43", 1280, 720, shaders=str(blocked / "shaders"))
    started: list = []
    look = {**NEUTRAL, "lut": "portra", "lut_enabled": True, "lut_amount": 0.7}
    eng = process.VideoEngine(spec, look, popen=fake_popen(tmp_path / "x.log", started=started))
    try:
        eng.start()
        assert wait_for(lambda: eng.state()["state"] == "running")
        text = started[0][started[0].index("-filter_complex") + 1]
        assert ".cube" not in text and "colorcorrect@tone" in text
        eng.apply_look({**look, "contrast": 0.1})
        time.sleep(0.8)
        assert len(started) == 1  # no rebuild loop for a key that can never be built
    finally:
        eng.stop()


def test_an_unwritable_shader_directory_turns_sharpening_off_not_the_picture(tmp_path: Path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    spec = Spec(SOURCE, "/dev/video43", 1280, 720, shaders=str(blocked / "shaders"))
    started: list = []
    eng = process.VideoEngine(
        spec,
        {**NEUTRAL, "sharpen_enabled": True, "sharpen_amount": 0.2},
        popen=fake_popen(tmp_path / "x.log", started=started),
    )
    try:
        eng.start()
        assert wait_for(lambda: eng.state()["state"] == "running")
        text = started[0][started[0].index("-filter_complex") + 1]
        assert "custom_shader_path" not in text
    finally:
        eng.stop()


def test_window_commands_grow_size_first_and_shrink_origin_first(engine) -> None:
    engine.start()
    assert wait_for(lambda: engine.state()["state"] == "running")
    engine.set_window(Rect(320, 180, 640, 360))  # shrink from the full frame
    assert wait_for(lambda: len(commands_seen(engine)) == 4)
    assert [line.split()[2] for line in commands_seen(engine)] == ["crop_x", "crop_y", "crop_w", "crop_h"]
    engine.set_window(Rect(0, 0, 1280, 720))  # back to full: grow
    assert wait_for(lambda: len(commands_seen(engine)) == 8)
    assert [line.split()[2] for line in commands_seen(engine)[4:]] == ["crop_w", "crop_h", "crop_x", "crop_y"]
    assert engine.state()["crop"] is None


def test_blackout_is_a_command_not_a_restart(engine) -> None:
    engine.start()
    assert wait_for(lambda: engine.state()["state"] == "running")
    engine.set_blackout(True)
    assert wait_for(lambda: "clibplacebo@grade -1 brightness -1" in commands_seen(engine))
    assert engine.state()["blackout"] is True and engine.state()["restarts"] == 0


def test_a_restarted_child_starts_from_the_current_look_and_window(engine) -> None:
    engine.start()
    assert wait_for(lambda: engine.state()["state"] == "running")
    engine.apply_look({**NEUTRAL, "contrast": 0.3})
    engine.set_window(Rect(320, 180, 640, 360))
    engine.set_spec(Spec(Source("Cam Link", "/dev/video0", "YUYV", 1920, 1080, 30.0), "/dev/video43", 1920, 1080))
    assert wait_for(lambda: len(engine.started) == 2)
    second = engine.started[1]
    text = second[second.index("-filter_complex") + 1]
    assert "contrast=1.3" in text
    assert "crop_x" not in text  # a new source starts from the full frame
    assert "/dev/video0" in second


def test_a_child_that_dies_is_restarted_with_the_reason(tmp_path: Path) -> None:
    started: list = []
    eng = process.VideoEngine(SPEC, dict(NEUTRAL), popen=fake_popen(tmp_path / "x.log", "die", started))
    try:
        eng.start()
        assert wait_for(lambda: len(started) >= 3)
        assert wait_for(lambda: eng.state()["state"] in ("error", "waiting"))
        assert "Device or resource busy" in (eng.state()["error"] or "")
    finally:
        eng.stop()


def test_unstartable_ffmpeg_is_reported(tmp_path: Path) -> None:
    def broken(argv: list[str], **kwargs) -> subprocess.Popen:
        raise FileNotFoundError("ffmpeg")

    eng = process.VideoEngine(SPEC, dict(NEUTRAL), popen=broken)
    try:
        eng.start()
        assert wait_for(lambda: eng.state()["state"] == "error")
        assert "cannot start ffmpeg" in eng.state()["error"]
    finally:
        eng.stop()


def test_stop_terminates_the_child(engine) -> None:
    engine.start()
    assert wait_for(lambda: engine.state()["state"] == "running")
    proc = engine._proc
    engine.stop()
    assert proc.poll() is not None
    assert engine.state()["state"] == "stopped"


def test_set_window_is_ignored_on_the_cpu_backend(tmp_path: Path) -> None:
    eng = process.VideoEngine(graph_cpu(), dict(NEUTRAL), popen=fake_popen(tmp_path / "c.log"))
    eng.log = tmp_path / "c.log"  # type: ignore[attr-defined]
    try:
        eng.start()
        assert wait_for(lambda: eng.state()["state"] == "running")
        eng.set_window(Rect(0, 0, 640, 360))
        time.sleep(0.2)
        assert commands_seen(eng) == []
    finally:
        eng.stop()


def graph_cpu() -> Spec:
    return Spec(SOURCE, "/dev/video43", 1280, 720, backend="cpu")


# -- background blur: the two pipes between the child and the feed ----------------------------------------------------

BLUR_CHILD = """
import os, sys
log, progress, seg, mask = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
out = os.fdopen(progress, "w")
out.write("fps=20.0\\ndrop_frames=0\\nspeed=1.0x\\nprogress=continue\\n"); out.flush()
size = int(sys.argv[5])
def read_exact(fd, n):
    data = b""
    while len(data) < n:
        data += os.read(fd, n - len(data))
    return data
with open(log, "ab", buffering=0) as handle:
    first = read_exact(mask, size)  # ffmpeg starts no graph until the mask input has a frame
    handle.write(b"mask-primed %d\\n" % len(first))
    os.write(seg, b"\\x01" * 192 * 192 * 3)
    handle.write(b"answer %d\\n" % len(read_exact(mask, size)))
    while True:
        chunk = os.read(0, 4096)
        if not chunk or chunk.startswith(b"q"):
            break
        handle.write(chunk)
"""


class FakeFeed:
    """Answers a camera frame with a clear mask, like BlurFeed's pump with blur off."""

    def __init__(self) -> None:
        self.window = None
        self.enabled: list[bool] = []
        self.rates: list[int] = []
        self.pumped = threading.Event()

    def set_window(self, window) -> None:
        self.window = window

    def set_enabled(self, on: bool) -> None:
        self.enabled.append(on)

    def set_rate(self, rate: int) -> None:
        self.rates.append(rate)

    def stats(self) -> dict:
        return {"state": "idle"}

    def pump(self, seg_fd: int, mask_fd: int) -> None:
        try:
            os.write(mask_fd, bytes(320 * 180 * 3))
            os.read(seg_fd, 192 * 192 * 3)
            os.write(mask_fd, bytes(320 * 180 * 3))
            self.pumped.set()
            time.sleep(0.3)
        except OSError:
            pass  # the child was stopped under us (a restart): the real pump returns the same way
        finally:
            os.close(seg_fd)
            os.close(mask_fd)


def blur_popen(log: Path, started: list) -> Callable[..., subprocess.Popen]:
    def popen(argv: list[str], **kwargs) -> subprocess.Popen:
        progress = argv[argv.index("-progress") + 1].split(":")[1]
        seg = argv[-1].split(":")[1]
        mask = next(a for a in argv if a.startswith("/dev/fd/")).split("/")[-1]
        started.append(argv)
        return subprocess.Popen(
            [sys.executable, "-c", BLUR_CHILD, str(log), progress, seg, mask, str(320 * 180 * 3)], **kwargs
        )

    return popen


@pytest.fixture
def blur_engine(tmp_path: Path):
    started: list[list[str]] = []
    spec = Spec(SOURCE, "/dev/video43", 1280, 720, blur=BlurGraph())
    eng = process.VideoEngine(spec, dict(NEUTRAL), popen=blur_popen(tmp_path / "stdin.log", started))
    eng.started, eng.log, eng.feed = started, tmp_path / "stdin.log", FakeFeed()  # type: ignore[attr-defined]
    eng.attach_blur(eng.feed)
    yield eng
    eng.stop()


def test_a_blur_child_gets_its_mask_and_segmenter_pipes_pumped(blur_engine) -> None:
    blur_engine.start()
    assert wait_for(lambda: blur_engine.feed.pumped.is_set())
    assert wait_for(lambda: blur_engine.state()["state"] == "running")
    assert wait_for(lambda: commands_seen(blur_engine)[:2] == ["mask-primed 172800", "answer 172800"])
    text = blur_engine.started[0][blur_engine.started[0].index("-filter_complex") + 1]
    assert "maskedmerge@blur=enable=0" in text
    assert blur_engine.state()["blur"] == {"state": "idle"}


def test_blur_on_off_and_strength_are_commands_never_a_restart(blur_engine) -> None:
    blur_engine.start()
    assert wait_for(lambda: blur_engine.state()["state"] == "running")
    pid = blur_engine.state()["pid"]
    blur_engine.set_blur(on=True, strength=10, rate=3)
    assert wait_for(lambda: "cmaskedmerge@blur -1 enable 1" in commands_seen(blur_engine))
    assert "cgblur@bg -1 sigma 6.5" in commands_seen(blur_engine)
    blur_engine.set_blur(strength=10)  # nothing changed: nothing sent
    blur_engine.set_blur(on=False)
    assert wait_for(lambda: commands_seen(blur_engine)[-1] == "cmaskedmerge@blur -1 enable 0")
    assert blur_engine.feed.enabled == [True, False] and blur_engine.feed.rates == [3]
    assert blur_engine.state()["pid"] == pid and blur_engine.state()["restarts"] == 0


def test_a_restarted_child_keeps_the_blur_settings(blur_engine) -> None:
    blur_engine.start()
    assert wait_for(lambda: blur_engine.state()["state"] == "running")
    blur_engine.set_blur(on=True, strength=4)
    blur_engine.set_spec(Spec(SOURCE, "/dev/video43", 1280, 720, blur=BlurGraph(mask_width=160, mask_height=90)))
    assert wait_for(lambda: len(blur_engine.started) == 2)
    second = blur_engine.started[1]
    text = second[second.index("-filter_complex") + 1]
    assert "maskedmerge@blur=enable=1" in text and "sigma=2.9" in text


def test_the_blur_window_is_the_crop_as_fractions_of_the_camera(blur_engine) -> None:
    assert blur_engine.blur_box() is None
    blur_engine.set_window(Rect(320, 180, 640, 360))
    assert blur_engine.blur_box() == (0.25, 0.25, 0.75, 0.75)
    assert blur_engine.feed.window() == (0.25, 0.25, 0.75, 0.75)  # the feed cuts its mask to the same box


def test_a_blur_spec_without_a_feed_is_an_error_not_a_hang(tmp_path: Path) -> None:
    eng = process.VideoEngine(
        Spec(SOURCE, "/dev/video43", 1280, 720, blur=BlurGraph()), dict(NEUTRAL), popen=fake_popen(tmp_path / "x.log")
    )
    with pytest.raises(GraphError, match="blur feed"):
        eng._open_blur_pipes(eng._spec)


# -- the delivery guard: before_child / on_exit / the stall watchdog ----------------------------------------------


def test_a_source_that_is_not_ready_keeps_the_child_from_starting(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(process, "NOT_READY_POLL", 0.02)
    answers = ["Canon EOS 50D has no producer on /dev/video42"]
    asked: list[object] = []

    def guard(spec: Spec) -> str | None:
        asked.append(spec)
        return answers[0] if answers else None

    started: list[list[str]] = []
    eng = process.VideoEngine(
        SPEC, dict(NEUTRAL), popen=fake_popen(tmp_path / "in.log", started=started), before_child=guard
    )
    eng.start()
    try:
        assert wait_for(lambda: eng.state()["state"] == "waiting" and len(asked) >= 3)
        assert started == [] and eng.state()["error"] == answers[0] and eng.state()["not_ready"] == answers[0]
        answers.clear()  # the camera came back: no click, the child starts
        assert wait_for(lambda: eng.state()["state"] == "running")
        assert len(started) == 1 and eng.state()["not_ready"] is None
    finally:
        eng.stop()


def test_a_guard_that_raises_does_not_stop_the_engine(tmp_path: Path) -> None:
    def broken(spec: Spec) -> str | None:
        raise RuntimeError("probe blew up")

    eng = process.VideoEngine(SPEC, dict(NEUTRAL), popen=fake_popen(tmp_path / "in.log"), before_child=broken)
    eng.start()
    try:
        assert wait_for(lambda: eng.state()["state"] == "running")
    finally:
        eng.stop()


def test_on_exit_hears_about_a_child_that_dies(tmp_path: Path) -> None:
    exits: list[tuple[str, int]] = []
    eng = process.VideoEngine(
        SPEC, dict(NEUTRAL), popen=fake_popen(tmp_path / "in.log", "die"), on_exit=lambda why, n: exits.append((why, n))
    )
    eng.start()
    try:
        assert wait_for(lambda: len(exits) >= 2)
        assert "Device or resource busy" in exits[0][0] and exits[0][1] == 1 and exits[1][1] == 2
    finally:
        eng.stop()


def test_a_stalled_child_is_stopped_and_restarted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(process, "STALL_SECONDS", 0.3)
    exits: list[tuple[str, int]] = []
    started: list[list[str]] = []
    eng = process.VideoEngine(
        SPEC,
        dict(NEUTRAL),
        popen=fake_popen(tmp_path / "in.log", "stall", started),
        on_exit=lambda why, n: exits.append((why, n)),
    )
    eng.start()
    try:
        assert wait_for(lambda: exits and "stopped delivering frames" in exits[0][0], timeout=8)
        assert wait_for(lambda: len(started) >= 2, timeout=8)  # and it tries again by itself
    finally:
        eng.stop()


def test_a_child_that_never_delivers_a_frame_is_stopped_after_the_first_frame_window(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(process, "STALL_FIRST", 0.3)
    exits: list[tuple[str, int]] = []
    eng = process.VideoEngine(
        SPEC, dict(NEUTRAL), popen=fake_popen(tmp_path / "in.log"), on_exit=lambda why, n: exits.append((why, n))
    )
    eng.start()  # the stand-in reports progress but no frame counter at all
    try:
        assert wait_for(lambda: exits and "delivered no frame" in exits[0][0], timeout=8)
    finally:
        eng.stop()


def test_a_child_whose_counter_moves_is_left_alone(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(process, "STALL_SECONDS", 0.3)
    eng = process.VideoEngine(SPEC, dict(NEUTRAL), popen=fake_popen(tmp_path / "in.log", "flow"))
    eng.start()
    try:
        assert wait_for(lambda: eng.state()["state"] == "running")
        time.sleep(1.0)
        assert eng.state()["restarts"] == 0 and eng.state()["state"] == "running"
    finally:
        eng.stop()


def test_with_the_denoiser_on_a_window_never_reaches_the_frames_edge(tmp_path: Path) -> None:
    spec = Spec(SOURCE, "/dev/video43", 1280, 720, denoise="nlmeans")
    eng = process.VideoEngine(spec, dict(NEUTRAL), popen=fake_popen(tmp_path / "x.log"))
    eng.log = tmp_path / "x.log"  # type: ignore[attr-defined]
    try:
        eng.start()
        assert wait_for(lambda: eng.state()["state"] == "running")
        eng.set_window(Rect(0, 0, 1280, 720))  # "the whole frame" is the trimmed one: nothing to send
        eng.set_window(Rect(0, 0, 640, 360))
        assert wait_for(lambda: len(commands_seen(eng)) == 4)
        assert sorted(line.split()[2] for line in commands_seen(eng)) == ["crop_h", "crop_w", "crop_x", "crop_y"]
        assert eng.state()["crop"] == {"x": 2, "y": 2, "w": 638, "h": 358}
    finally:
        eng.stop()
