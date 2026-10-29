from __future__ import annotations

import subprocess

import pytest

from streamstack import latencyrig, system


def info(text: str, code: int = 0):
    return lambda argv, timeout=10: subprocess.CompletedProcess(argv, code, text, "")


LOOPBACK = "Driver Info:\n\tDriver name      : v4l2 loopback\n\tCard type        : Latency scratch\n"


def test_the_program_and_camera_nodes_are_never_written() -> None:
    for device in ("/dev/video42", "/dev/video43"):
        with pytest.raises(latencyrig.RigError, match="Console feeds"):
            latencyrig.refuse_unsafe_node(device, {})
    with pytest.raises(latencyrig.RigError, match="Console feeds"):
        latencyrig.refuse_unsafe_node("/dev/null", {"STREAMSTACK_PROGRAM_DEVICE": "/dev/null"})


def test_a_missing_node_is_refused(tmp_path) -> None:
    with pytest.raises(latencyrig.RigError, match="does not exist"):
        latencyrig.refuse_unsafe_node(str(tmp_path / "video99"), {})


def test_a_real_capture_device_is_never_written(monkeypatch, tmp_path) -> None:
    node = tmp_path / "video7"
    node.touch()
    monkeypatch.setattr(system, "run", info("Driver name : uvcvideo\n"))
    with pytest.raises(latencyrig.RigError, match="not a v4l2loopback"):
        latencyrig.refuse_unsafe_node(str(node), {})


def test_a_node_someone_holds_is_refused_and_names_them(monkeypatch, tmp_path) -> None:
    node = tmp_path / "video7"
    node.touch()
    monkeypatch.setattr(system, "run", info(LOOPBACK))
    monkeypatch.setattr(latencyrig, "holders", lambda device: ["obs (4242)"])
    with pytest.raises(latencyrig.RigError, match=r"held by obs \(4242\)"):
        latencyrig.refuse_unsafe_node(str(node), {})


def test_a_free_scratch_loopback_node_is_accepted(monkeypatch, tmp_path) -> None:
    node = tmp_path / "video7"
    node.touch()
    monkeypatch.setattr(system, "run", info(LOOPBACK))
    monkeypatch.setattr(latencyrig, "holders", lambda device: [])
    latencyrig.refuse_unsafe_node(str(node), {})


def test_v4l2_ctl_missing_is_a_clear_refusal(monkeypatch, tmp_path) -> None:
    node = tmp_path / "video7"
    node.touch()
    monkeypatch.setattr(system, "run", lambda argv, timeout=10: None)
    with pytest.raises(latencyrig.RigError, match="v4l-utils"):
        latencyrig.refuse_unsafe_node(str(node), {})


def test_feed_refuses_before_it_starts_ffmpeg(monkeypatch) -> None:
    def never(*args, **kwargs):
        raise AssertionError("ffmpeg must not start for a refused node")

    monkeypatch.setattr(subprocess, "Popen", never)
    with pytest.raises(latencyrig.RigError):
        latencyrig.feed("/dev/video43", 1.0)


def test_the_ffmpeg_command_writes_yuyv_at_the_asked_size_and_rate() -> None:
    argv = latencyrig.ffmpeg_argv("/dev/video50", (1280, 720), 30)
    assert argv[-1] == "/dev/video50" and argv[argv.index("-video_size") + 1] == "1280x720"
    assert argv.count("yuyv422") == 2 and argv[argv.index("-framerate") + 1] == "30"
