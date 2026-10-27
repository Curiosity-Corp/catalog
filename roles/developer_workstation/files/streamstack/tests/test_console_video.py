"""Console video without a controller: looks and their library, the frame splitter and camera reader, the simulated
rig and the packaged demo cards. The live controller (NativeVideo) is tested in test_console_native.py."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from streamstack import config
from streamstack.console import video
from streamstack.console.errors import ActionError

JPEG = b"\xff\xd8\xff\xe0fakejpeg\xff\xd9"


class Toasts:
    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []

    def __call__(self, level: str, text: str) -> None:
        self.items.append((level, text))

    def texts(self) -> list[str]:
        return [t for _, t in self.items]


@pytest.fixture
def toasts() -> Toasts:
    return Toasts()


def test_look_validation():
    assert video.validate_look({"brightness": 0.1, "sharpen_enabled": True}) == {
        "brightness": 0.1,
        "sharpen_enabled": True,
    }
    for bad in (
        None,
        {},
        {"brightness": 0.9},
        {"contrast": "high"},
        {"contrast": True},
        {"contrast": float("nan")},
        {"sharpen_enabled": 1},
        {"lut_amount": 1.5},
        {"colour": 0.1},
    ):
        with pytest.raises(ActionError) as err:
            video.validate_look(bad)
        assert err.value.status == 400


def test_builtin_looks_are_complete_and_in_range():
    original = ["Natural", "Warm Studio", "Cool Daylight", "Cinematic", "Punchy", "Soft Skin"]
    assert list(video.BUILTIN_LOOKS)[:6] == original
    assert len(video.BUILTIN_LOOKS) >= 20
    for look in video.BUILTIN_LOOKS.values():
        assert set(look) == set(video.NEUTRAL)
        assert video.validate_look(look) == look
    assert video.BUILTIN_LOOKS["Natural"] == video.NEUTRAL
    assert video.BUILTIN_LOOKS["Cinematic"]["lut_enabled"] is True


def test_corrupt_looks_file_is_ignored(isolated_home):
    path = isolated_home / ".config/streamstack/looks.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"Good": {"contrast": 0.1}, "Bad": {"contrast": 9}, "Worse": 3}))
    assert [lk["name"] for lk in video.LookLibrary().listing()][-1] == "Good"
    path.write_text("{not json")
    assert len(video.LookLibrary().listing()) == len(video.BUILTIN_LOOKS)


def test_width_clamp():
    assert [video.clamp_width(w) for w in (0, 100, 160, 640, 1280, 5000, "800", None, "x")] == [
        160,
        160,
        160,
        640,
        1280,
        1280,
        800,
        640,
        640,
    ]


def test_jpeg_splitter_handles_chunks_and_garbage():
    one, two = b"\xff\xd8\xff\xdbAAAA\xff\xd9", b"\xff\xd8\xff\xdbBB\xff\xd9"
    stream = b"junk" + one + two + b"\xff\xd8\xff\xdbpartial"
    splitter = video.JpegSplitter()
    frames: list[bytes] = []
    for i in range(0, len(stream), 3):  # arbitrary read sizes
        frames += splitter.feed(stream[i : i + 3])
    assert frames == [one, two]
    assert splitter.feed(b"\xff\xd9") == [b"\xff\xd8\xff\xdbpartial\xff\xd9"]
    assert splitter.feed(b"") == []


def test_jpeg_splitter_drops_runaway_stream(monkeypatch):
    monkeypatch.setattr(video.JpegSplitter, "MAX_BUFFER", 100)
    splitter = video.JpegSplitter()
    assert splitter.feed(b"\xff\xd8\xff" + b"x" * 500) == []
    assert len(splitter.buffer) == 0


def test_ffmpeg_argv():
    single = video.ffmpeg_camera_argv("/dev/video42", 640)
    assert single[:2] == ["ffmpeg", "-nostdin"] and "-frames:v" in single and "scale=640:-2" in single
    stream = video.ffmpeg_camera_argv("/dev/video42", 640, 5)
    assert "-frames:v" not in stream and "fps=5,scale=640:-2" in stream and stream[-1] == "-"


class FakeProc:
    def __init__(self, data: bytes) -> None:
        self.stdout = type("S", (), {})()
        self._chunks = [data[i : i + 7] for i in range(0, len(data), 7)]
        self.stdout.read = lambda n: self._chunks.pop(0) if self._chunks else b""
        self.killed = False

    def poll(self):
        return None if not self.killed and self._chunks else 0

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout=None) -> int:
        return 0


def test_camera_stream_shares_one_process():
    started: list[list[str]] = []

    def popen(argv, **kwargs):
        started.append(argv)
        return FakeProc(JPEG * 3)

    stream = video.CameraStream("/dev/video42", popen=popen)
    assert stream.latest(640) == JPEG
    assert len(started) == 1 and "fps=5" in started[0][started[0].index("-vf") + 1]
    stream.stop()


def test_camera_stream_missing_ffmpeg():
    def popen(argv, **kwargs):
        raise FileNotFoundError("ffmpeg")

    assert video.CameraStream("/dev/video42", popen=popen).latest(640) is None


@pytest.fixture
def demo(toasts) -> video.DemoVideo:
    return video.DemoVideo(toasts)


def test_demo_privacy_scene_and_frames(demo):
    cards = {demo.frame("program", 640)}
    assert all(c and c.startswith(b"\xff\xd8") for c in cards)
    demo.act("video.scene", {"name": "Cam Link"})
    cards.add(demo.frame("program", 640))
    demo.act("video.privacy", {"on": True})
    assert demo.state()["privacy"] is True and demo.state()["program_scene"] == "Be Right Back"
    cards.add(demo.frame("program", 640))
    demo.act("video.privacy", {"on": False})
    assert demo.state()["program_scene"] == "Cam Link" and demo.state()["privacy"] is False
    assert len(cards) == 3  # a different card per scene
    assert demo.frame("camera", 640) != demo.frame("Cam Link 4K", 640)
    assert demo.frame("nope", 640) is None


def test_demo_look_responds_and_validates(demo):
    demo.act("video.look", {"values": {"warmth": 0.4, "lut_enabled": True}})
    assert demo.state()["look"]["warmth"] == 0.4 and demo.state()["look"]["lut_enabled"] is True
    demo.act("video.source", {"name": "Cam Link 4K"})
    assert demo.state()["look"]["warmth"] == 0.0  # per-source looks
    with pytest.raises(ActionError):
        demo.act("video.look", {"values": {"warmth": 5}})
    demo.act("video.looks.apply", {"name": "Punchy"})
    assert demo.state()["look"]["sharpen_enabled"] is True
    demo.act("video.look.reset", {})
    assert demo.state()["look"] == video.NEUTRAL


def test_demo_library_and_outputs(demo, toasts):
    demo.act("video.looks.save", {"name": "Mine"})
    assert demo.state()["looks"][-1]["name"] == "Mine"
    with pytest.raises(ActionError):
        demo.act("video.looks.save", {"name": "Punchy"})
    demo.act("video.looks.delete", {"name": "Mine"})
    demo.act("video.record", {"on": True})
    assert demo.state()["recording"]["active"] and demo.state()["recording"]["timecode"] == "00:00:00"
    demo.act("video.stream", {"on": True})
    demo.act("video.virtualcam", {"on": False})
    assert demo.state()["virtualcam"] is False and demo.state()["streaming"]["active"]
    demo.act("video.record", {"on": False})
    assert demo.state()["recording"] == {"active": False, "paused": False, "timecode": None}
    assert "Recording started" in toasts.texts()
    with pytest.raises(ActionError):
        demo.act("video.unknown", {})


def test_demo_cards_ship_and_are_small():
    folder = config.data_dir() / "console/demo"
    files = sorted(folder.glob("*.jpg"))
    assert len(files) >= 3
    for file in files:
        data = file.read_bytes()
        assert data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9") and len(data) < 150_000
    assert {name for name in video.DEMO_CARDS.values()} <= {f.name for f in files}


def test_looks_file_atomic_write_leaves_no_partial(isolated_home, monkeypatch):
    library = video.LookLibrary()
    library.save("One", dict(video.NEUTRAL))
    before = library.file().read_text()

    def explode(*args, **kwargs):
        raise OSError(28, "No space left")

    monkeypatch.setattr(os, "fsync", explode)
    with pytest.raises(ActionError) as err:
        library.save("Two", dict(video.NEUTRAL))
    assert err.value.status == 409
    assert library.file().read_text() == before  # the old file is untouched


def test_data_files_are_packaged(tmp_path: Path):
    assert (config.data_dir() / "console/demo/card-brb.jpg").is_file()


def test_the_demo_rig_saves_looks_without_flushing_the_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    flushed: list[int] = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (flushed.append(fd), real(fd))[1])
    video.LookLibrary(tmp_path / "durable.json").save("Warm", dict(video.NEUTRAL))
    assert flushed
    flushed.clear()
    quick = video.LookLibrary(tmp_path / "quick.json", ephemeral=True)
    quick.save("Warm", dict(video.NEUTRAL))
    assert flushed == []
    assert not (tmp_path / "quick.json.bak").exists()
    assert video.DemoVideo().library.ephemeral is True
