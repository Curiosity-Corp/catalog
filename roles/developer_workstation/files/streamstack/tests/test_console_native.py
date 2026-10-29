"""NativeVideo: the Console's video controller on the engine, with a fake engine and fake cameras."""

from __future__ import annotations

import json
import stat
import time
from pathlib import Path

import pytest

from streamstack import config
from streamstack.console import video as console_video
from streamstack.console.arcblur import ArcBlur
from streamstack.console.errors import ActionError
from streamstack.console.native import NativeVideo
from streamstack.engine import graph, source
from streamstack.engine.cropper import EngineCropper
from streamstack.engine.graph import Rect, Source, Spec
from streamstack.engine.segment import Arc
from streamstack.framing.crop import Crop

CANON = source.Camera("Canon EOS 50D", "/dev/video42", "canon")
LINK = source.Camera("Cam Link 4K", "/dev/video0", "camlink")
CANON_SRC = Source("Canon EOS 50D", "/dev/video42", "YUYV", 1280, 720, 20.0)
LINK_SRC = Source("Cam Link 4K", "/dev/video0", "YUYV", 1920, 1080, 30.0)


class FakeEngine:
    def __init__(self, spec: Spec, look: dict | None = None, **kwargs) -> None:
        self.spec = spec
        self.look = dict(look or {})
        self.looks: list[dict] = []
        self.windows: list[Rect | None] = []
        self.blackout = False
        self.running = False
        self.specs = [spec]
        self.frame: bytes | None = b"\xff\xd8\xff\xe0det\xff\xd9"
        self.notify = kwargs.get("notify")

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    def set_spec(self, spec: Spec) -> None:
        self.spec = spec
        self.specs.append(spec)

    def apply_look(self, look: dict) -> float:
        self.look = dict(look)
        self.looks.append(dict(look))
        return 0.05

    def set_blackout(self, on: bool) -> None:
        self.blackout = on

    def set_window(self, rect: Rect | None) -> None:
        self.windows.append(rect)

    def latest_frame(self, max_age: float = 3.0) -> bytes | None:
        return self.frame

    def state(self) -> dict:
        crop = self.windows[-1] if self.windows else None
        return {
            "state": "running" if self.running else "stopped",
            "error": None,
            "backend": "vulkan",
            "decode": "none (raw)",
            "source": self.spec.source.label,
            "source_mode": "1280x720 YUYV",
            "output": "/dev/video43 1280x720 yuyv422",
            "fps": 20.0,
            "dropped": 0,
            "restarts": 0,
            "look_apply_ms": 0.05,
            "crop": None if crop is None else vars(crop),
            "blackout": self.blackout,
        }


def wait_for(check, timeout: float = 15.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def video(tmp_path: Path):
    cameras = [CANON]
    made: list[FakeEngine] = []

    def factory(spec: Spec, look: dict | None = None, **kwargs) -> FakeEngine:
        engine = FakeEngine(spec, look, **kwargs)
        made.append(engine)
        return engine

    def resolve(camera: source.Camera, cfg: dict) -> Source:
        if camera.kind == "camlink":
            return LINK_SRC
        return CANON_SRC

    cfg = {**config.DEFAULTS}
    no_arc = ArcBlur(
        cfg, probe=lambda wanted: Arc(False, "the openvino package is not installed")
    )  # never the real GPU
    controller = NativeVideo(
        cfg,
        arc=no_arc,
        state_file=tmp_path / "video.json",
        discover=lambda cfg: list(cameras),
        resolve=resolve,
        engine_factory=factory,
        backend=lambda node: ("vulkan", ""),
    )
    controller.cameras, controller.made = cameras, made  # type: ignore[attr-defined]
    controller.start()
    assert wait_for(lambda: made and made[0].running)
    yield controller
    controller.stop()


def test_state_has_the_shape_the_pages_read(video: NativeVideo) -> None:
    state = video.state()
    assert not [key for key in state if key.startswith("ob")]  # the retired external video path left no keys behind
    assert state["sources"] == ["Canon EOS 50D"] and state["selected_source"] == "Canon EOS 50D"
    assert state["program_scene"] == "Program" and state["virtualcam"] is True
    assert state["engine"]["state"] == "running" and state["engine"]["backend"] == "vulkan"
    assert state["blur"]["native"] is True and state["blur"]["available"] is False
    assert state["blur"]["arc"]["state"] == "unavailable"
    assert state["streaming"]["active"] is False and state["recording"]["active"] is False


def test_the_engine_starts_on_the_programme_device_at_the_source_size(video: NativeVideo) -> None:
    spec = video.made[0].spec  # type: ignore[attr-defined]
    assert spec.out_device == "/dev/video43" and (spec.out_width, spec.out_height) == (1280, 720)
    assert spec.backend == "vulkan" and spec.source == CANON_SRC


def test_a_slider_reaches_the_engine_at_once_and_is_remembered(video: NativeVideo, tmp_path: Path) -> None:
    video.act("video.look", {"values": {"brightness": 0.2, "warmth": 0.4}})
    engine = video.made[0]  # type: ignore[attr-defined]
    assert engine.looks[-1]["brightness"] == 0.2 and engine.looks[-1]["warmth"] == 0.4
    assert video.state()["look"]["brightness"] == 0.2
    video.stop()
    saved = json.loads((tmp_path / "video.json").read_text())
    assert saved["looks"]["Canon EOS 50D"]["warmth"] == 0.4


def test_bad_look_values_are_refused(video: NativeVideo) -> None:
    with pytest.raises(ActionError) as caught:
        video.act("video.look", {"values": {"brightness": 5}})
    assert caught.value.status == 400
    with pytest.raises(ActionError):
        video.act("video.look", {"values": {"nonsense": 1}})


def test_applying_a_look_ramps_to_the_target(video: NativeVideo) -> None:
    video.act("video.looks.apply", {"name": "Warm Studio"})
    engine = video.made[0]  # type: ignore[attr-defined]
    assert wait_for(lambda: engine.look.get("warmth") == 0.25, timeout=5)
    assert len(engine.looks) > 4  # eased, not one jump
    mids = [look["warmth"] for look in engine.looks]
    assert mids == sorted(mids)


def test_a_look_with_its_own_lut_gives_the_engine_the_lut_name(video: NativeVideo) -> None:
    video.act("video.looks.apply", {"name": "Portra Portrait"})
    engine = video.made[0]  # type: ignore[attr-defined]
    assert wait_for(lambda: engine.look.get("lut_amount") == 0.7, timeout=5)
    assert all(look.get("lut") == "portra" for look in engine.looks)
    assert "lut" not in video.state()["look"]  # the sliders the UI shows stay numbers and flags only
    video.act("video.looks.apply", {"name": "Warm Studio"})  # a look with no LUT of its own drops it
    assert wait_for(lambda: engine.look.get("warmth") == 0.25, timeout=5)
    assert wait_for(lambda: "lut" not in engine.look)


def test_reset_drops_the_looks_lut(video: NativeVideo) -> None:
    video.act("video.looks.apply", {"name": "Portra Portrait"})
    engine = video.made[0]  # type: ignore[attr-defined]
    assert wait_for(lambda: engine.look.get("lut_amount") == 0.7, timeout=5)
    video.act("video.look.reset", {})
    assert wait_for(lambda: "lut" not in engine.look and engine.look.get("contrast") == 0.0)


def test_the_looks_lut_survives_a_restart_of_the_console(tmp_path: Path) -> None:
    path = tmp_path / "v.json"
    path.write_text(
        json.dumps(
            {
                "active": "Canon EOS 50D",
                "looks": {"Canon EOS 50D": {"lut_enabled": True, "lut_amount": 0.5}},
                "luts": {"Canon EOS 50D": "portra", "Cam Link 4K": "not-a-lut", "x": 3},
            }
        )
    )
    controller = NativeVideo(dict(config.DEFAULTS), state_file=path, discover=lambda cfg: [CANON])
    assert controller._engine_look("Canon EOS 50D")["lut"] == "portra"
    assert "lut" not in controller._engine_look("Cam Link 4K")  # an unknown name is dropped on load


def test_the_scaler_setting_accepts_only_a_benchmarked_name(video: NativeVideo) -> None:
    assert video._scaler() == graph.DEFAULT_SCALER
    video.cfg["STREAMSTACK_ENGINE_SCALER"] = "Spline36"
    assert video._scaler() == "spline36"
    video.cfg["STREAMSTACK_ENGINE_SCALER"] = "bogus"
    assert video._scaler() == graph.DEFAULT_SCALER  # a warning toast, never a refusal to start


def test_the_denoise_setting_is_off_unless_it_names_a_known_denoiser(video: NativeVideo) -> None:
    assert video._denoise() == ""
    video.cfg["STREAMSTACK_ENGINE_DENOISE"] = "NLMeans"
    assert video._denoise() == "nlmeans"
    video.cfg["STREAMSTACK_ENGINE_DENOISE"] = "bogus"
    assert video._denoise() == ""  # a warning toast, never a refusal to start


def test_reset_returns_to_natural(video: NativeVideo) -> None:
    video.act("video.look", {"values": {"contrast": 0.5}})
    video.act("video.look.reset", {})
    engine = video.made[0]  # type: ignore[attr-defined]
    assert wait_for(lambda: engine.look.get("contrast") == 0.0)


def test_privacy_blacks_the_picture_out_and_back(video: NativeVideo) -> None:
    video.act("video.privacy", {"on": True})
    assert video.made[0].blackout is True and video.state()["privacy"] is True  # type: ignore[attr-defined]
    video.act("video.privacy", {"on": False})
    assert video.made[0].blackout is False  # type: ignore[attr-defined]


def test_switching_camera_restarts_the_graph_with_that_cameras_look(video: NativeVideo) -> None:
    video.cameras.append(LINK)  # type: ignore[attr-defined]
    video.act("video.look", {"values": {"saturation": 0.3}})
    video._tick()
    video.act("video.source", {"name": "Cam Link 4K"})
    engine = video.made[0]  # type: ignore[attr-defined]
    assert engine.spec.source == LINK_SRC and (engine.spec.out_width, engine.spec.out_height) == (1920, 1080)
    assert engine.look["saturation"] == 0.0  # the Cam Link has its own look
    assert video.state()["selected_source"] == "Cam Link 4K"


def test_unknown_camera_and_scene_are_400(video: NativeVideo) -> None:
    for action, params in (("video.source", {"name": "Webcam"}), ("video.scene", {"name": "Wide"})):
        with pytest.raises(ActionError) as caught:
            video.act(action, params)
        assert caught.value.status == 400


def test_recording_streaming_and_blur_say_why_not(video: NativeVideo) -> None:
    for action, params in (
        ("video.record", {"on": True}),
        ("video.stream", {"on": True}),
        ("video.blur", {"on": True}),
    ):
        with pytest.raises(ActionError) as caught:
            video.act(action, params)
        assert caught.value.status == 409


def test_virtualcam_switch_stops_and_starts_the_engine(video: NativeVideo) -> None:
    video.act("video.virtualcam", {"on": False})
    assert video.made[0].running is False  # type: ignore[attr-defined]
    assert video.state()["virtualcam"] is False and video.state()["engine"]["state"] == "off"
    video.act("video.virtualcam", {"on": True})
    assert video.made[0].running is True  # type: ignore[attr-defined]


def test_unknown_action_is_400(video: NativeVideo) -> None:
    with pytest.raises(ActionError) as caught:
        video.act("video.nothing", {})
    assert caught.value.status == 400


def test_a_missing_camera_is_waiting_with_the_reason(tmp_path: Path) -> None:
    def resolve(camera: source.Camera, cfg: dict) -> Source:
        raise source.SourceError("Canon EOS 50D offers no picture (no signal, or the device is busy).")

    controller = NativeVideo(
        dict(config.DEFAULTS),
        state_file=tmp_path / "v.json",
        discover=lambda cfg: [CANON],
        resolve=resolve,
        engine_factory=FakeEngine,
        backend=lambda node: ("vulkan", ""),
    )
    controller._tick()
    block = controller.state()["engine"]
    assert block["state"] == "waiting" and "no picture" in block["error"]
    checks = controller.health_checks()
    assert checks[0]["id"] == "video-engine" and checks[0]["state"] == "warn"


def test_looks_survive_a_restart_of_the_console(tmp_path: Path) -> None:
    path = tmp_path / "v.json"
    path.write_text(json.dumps({"active": "Canon EOS 50D", "looks": {"Canon EOS 50D": {"contrast": 0.3}}}))
    controller = NativeVideo(dict(config.DEFAULTS), state_file=path, discover=lambda cfg: [CANON])
    assert controller.state()["look"]["contrast"] == 0.3


def test_a_corrupt_state_file_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "v.json"
    path.write_text("{nope")
    controller = NativeVideo(dict(config.DEFAULTS), state_file=path)
    assert controller.state()["look"]["contrast"] == 0.0


def test_framing_gets_the_engine_picture(video: NativeVideo) -> None:
    connected, program, scenes, cameras = video.scene_view()
    assert (connected, program, scenes, cameras) == (True, "Program", ["Program"], {"Canon EOS 50D"})
    assert video.detect_frame("Canon EOS 50D").startswith(b"\xff\xd8")  # type: ignore[union-attr]
    assert video.detect_frame("Cam Link 4K") is None


def test_cropper_turns_framing_margins_into_an_engine_window(video: NativeVideo) -> None:
    cropper = video.make_cropper()
    assert isinstance(cropper, EngineCropper)
    [item] = cropper.discover(["Program"], {"Canon EOS 50D"})
    assert (item.width, item.height, item.out_height) == (1280, 720, 720.0)
    cropper.apply(item, Crop(left=100, top=51, right=220, bottom=99))
    engine = video.made[0]  # type: ignore[attr-defined]
    assert engine.windows[-1] == Rect(100, 50, 960, 570)  # even origin, even size
    cropper.restore(item)
    assert engine.windows[-1] is None
    assert not hasattr(cropper, "recover")  # the engine always restarts at the full frame: nothing to put back


def test_cropper_sees_a_crop_already_applied(video: NativeVideo) -> None:
    video.made[0].set_window(Rect(100, 50, 640, 360))  # type: ignore[attr-defined]
    [item] = video.make_cropper().discover(["Program"], {"Canon EOS 50D"})
    assert item.applied == Crop(100, 50, 1280 - 740, 720 - 410)


def test_saved_looks_are_private_listed_and_deletable(video: NativeVideo, isolated_home: Path) -> None:
    video.act("video.look", {"values": {"contrast": 0.3, "warmth": 0.2}})
    video.act("video.looks.save", {"name": "Evening"})
    path = isolated_home / ".config/streamstack/looks.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["Evening"]["contrast"] == 0.3
    assert not list(path.parent.glob("*.tmp"))
    listing = video.state()["looks"]
    assert listing[-1]["name"] == "Evening" and listing[-1]["builtin"] is False
    video.act("video.looks.delete", {"name": "Evening"})
    assert "Evening" not in [look["name"] for look in video.state()["looks"]]


def test_look_names_are_validated(video: NativeVideo) -> None:
    for bad in ("Natural", "natural", "x" * 41, "   ", "bad\nname"):
        with pytest.raises(ActionError) as err:
            video.act("video.looks.save", {"name": bad})
        assert err.value.status == 400
    video.act("video.looks.save", {"name": "x" * 40})
    for bad in ("Natural", "Ghost"):
        with pytest.raises(ActionError) as err:
            video.act("video.looks.delete", {"name": bad})
        assert err.value.status == 400


def test_the_demo_rig_answers_with_the_same_state_keys_as_the_real_one(video: NativeVideo) -> None:
    demo = console_video.DemoVideo(lambda level, text: None)
    assert set(demo.state()) == set(video.state())
    assert set(demo.state()["look"]) == set(video.state()["look"])
    assert demo.state()["limits"] == video.state()["limits"]


def test_the_demo_engine_block_has_the_same_idle_keys_as_the_real_one(video: NativeVideo) -> None:
    demo = console_video.DemoVideo(lambda level, text: None)
    assert set(demo.state()["engine"]["idle"]) == set(video.state()["engine"]["idle"])


def test_the_demo_can_show_idle_and_keep_awake(video: NativeVideo) -> None:
    demo = console_video.DemoVideo(lambda level, text: None)
    demo.act("video.idle.demo", {"state": "idle"})
    assert demo.state()["engine"]["state"] == "idle" and demo.state()["engine"]["idle"]["state"] == "idle"
    demo.act("video.idle.demo", {"state": "running"})
    demo.act("video.keepalive", {"on": True})
    assert demo.state()["engine"]["idle"]["keep_alive"] is True


def test_frames_are_cached_briefly_and_shared_between_viewers(video: NativeVideo, monkeypatch) -> None:
    grabs: list[tuple[str, int]] = []
    monkeypatch.setattr(video, "_grab", lambda name, width: grabs.append((name, width)) or b"\xff\xd8jpeg")
    assert video.frame("program", 640) == b"\xff\xd8jpeg"
    assert video.frame("program", 640) == b"\xff\xd8jpeg"
    assert grabs == [("program", 640)]
    assert video.frame("program", 1280) and len(grabs) == 2  # another width is another frame


def test_a_cached_frame_expires(video: NativeVideo, monkeypatch) -> None:
    grabs: list[int] = []
    monkeypatch.setattr(video, "_grab", lambda name, width: grabs.append(width) or b"\xff\xd8jpeg")
    monkeypatch.setattr("streamstack.console.native.FRAME_TTL", 0.0)
    video.frame("camera", 640)
    video.frame("camera", 640)
    assert len(grabs) == 2


def test_a_failed_grab_is_none_and_is_not_cached(video: NativeVideo, monkeypatch) -> None:
    def broken(name: str, width: int) -> bytes:
        raise OSError("device busy")

    monkeypatch.setattr(video, "_grab", broken)
    assert video.frame("program", 640) is None
    monkeypatch.setattr(video, "_grab", lambda name, width: b"\xff\xd8jpeg")
    assert video.frame("program", 640) == b"\xff\xd8jpeg"


def test_an_unknown_camera_has_no_frame(video: NativeVideo) -> None:
    assert video.frame("No Such Camera", 640) is None


def test_a_saved_look_keeps_its_lut_and_renders_the_same(video: NativeVideo, isolated_home: Path) -> None:
    video.act("video.looks.apply", {"name": "Portra Portrait"})
    engine = video.made[0]  # type: ignore[attr-defined]
    assert wait_for(lambda: engine.look.get("lut_amount") == 0.7, timeout=5)
    video.act("video.looks.save", {"name": "My portra"})
    path = isolated_home / ".config/streamstack/looks.json"
    assert json.loads(path.read_text())["My portra"]["lut"] == "portra"
    assert video.state()["looks"][-1]["lut"] == "portra"
    video.act("video.looks.apply", {"name": "Warm Studio"})
    assert wait_for(lambda: "lut" not in engine.look)
    video.act("video.looks.apply", {"name": "My portra"})
    assert wait_for(lambda: engine.look.get("lut") == "portra", timeout=5)


def test_looks_saved_before_luts_were_kept_still_load(tmp_path: Path) -> None:
    from streamstack.console.video import LookLibrary

    path = tmp_path / "looks.json"
    path.write_text(json.dumps({"Old": {"contrast": 0.2}, "Odd": {"contrast": 0.1, "lut": "nope"}}))
    library = LookLibrary(path)
    assert library.get("Old")["contrast"] == 0.2 and library.lut_of("Old") is None
    assert library.lut_of("Odd") is None and library.lut_of("Portra Portrait") == "portra"
    library.save("New", {"contrast": 0.4}, "noir")
    assert library.lut_of("New") == "noir" and library.lut_of("Old") is None  # saving rewrites the others intact
    assert library.get("Old")["contrast"] == 0.2
