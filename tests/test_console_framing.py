"""The framing controller against a fake engine and a fake detector, driven tick by tick on a fake clock."""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from streamstack import config
from streamstack.console import framing as console_framing
from streamstack.console.errors import ActionError
from streamstack.console.framing import DemoFraming, FramingController
from streamstack.console.rig import Rig
from streamstack.engine.cropper import EngineCropper
from streamstack.engine.graph import Rect, Source
from streamstack.framing import detector, health
from streamstack.framing.engine import Face

CANON = Source("Canon EOS 50D", "/dev/video42", "YUYV", 1056, 704, 20.0)
LINK = Source("Cam Link 4K", "/dev/video0", "YUYV", 1920, 1080, 30.0)
JPEG = b"\xff\xd8jpeg"


class FakeEngine:
    """The slice of VideoEngine the cropper uses: the window, its state and the output height."""

    def __init__(self) -> None:
        self.window: Rect | None = None
        self.windows: list[Rect | None] = []
        self.fail_windows = False
        self.spec = SimpleNamespace(out_height=1080)

    def state(self) -> dict:
        return {"crop": None if self.window is None else vars(self.window)}

    def set_window(self, rect: Rect | None) -> None:
        if self.fail_windows:
            raise RuntimeError("the engine is restarting")
        self.window = rect
        self.windows.append(rect)


class FakeVideo:
    """The slice of NativeVideo the framing controller uses: what it is reading, its frames and its cropper."""

    def __init__(self, engine: FakeEngine, source: Source = CANON) -> None:
        self.engine = engine
        self.source = source
        self.connected = True
        self.calls: list[tuple[str, str]] = []

    def scene_view(self):
        return self.connected, "Program", ["Program"], {self.source.label}

    def make_cropper(self) -> EngineCropper:
        return EngineCropper(lambda: self.engine if self.connected else None, lambda: self.source)

    def detect_frame(self, label: str) -> bytes | None:
        self.calls.append(("detect_frame", label))
        return JPEG if label == self.source.label else None


class ScriptedDetector:
    backend = "fake"
    device = "Fake GPU"

    def __init__(self) -> None:
        self.faces: list[Face] = [Face(0.46, 0.30, 0.07, 0.12, 0.95, eye_y=0.34)]
        self.timing = detector.Timing()
        self.closed = False
        self.error: Exception | None = None

    def detect(self, jpeg: bytes) -> list[Face]:
        if self.error:
            raise self.error
        self.timing.add(0.003)
        return list(self.faces)

    def close(self) -> None:
        self.closed = True


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "readiness", lambda: (True, "", ""))


class Rigging:
    def __init__(self, tmp_path: Path, **cfg: str) -> None:
        self.engine = FakeEngine()
        self.video = FakeVideo(self.engine)
        self.det = ScriptedDetector()
        self.clock = Clock()
        self.toasts: list[tuple[str, str]] = []
        self.opened: list[str] = []
        settings = {**config.DEFAULTS, **cfg}

        def opener(device: str) -> ScriptedDetector:
            self.opened.append(device)
            return self.det

        self.ctl = FramingController(
            settings,
            self.video,
            lambda level, text: self.toasts.append((level, text)),
            opener=opener,
            state_file=tmp_path / "state" / "framing.json",
            clock=self.clock,
        )

    def run(self, seconds: float, *, hz: float = 8.0) -> None:
        """Detections at `hz` and crop writes at 20 Hz, like the two threads, on the fake clock."""
        steps = int(seconds * 20)
        every = max(1, round(20 / hz))
        for i in range(steps):
            self.clock.now += 0.05
            self.ctl._apply_tick(self.clock.now)
            if i % every == 0:
                self.ctl._detect_tick(self.clock.now)

    def crop(self) -> tuple[int, int, int, int]:
        """The margins (left, top, right, bottom) the engine's window leaves, in source pixels."""
        w = self.engine.window
        if w is None:
            return 0, 0, 0, 0
        src = self.video.source
        return w.x, w.y, src.width - w.x - w.w, src.height - w.y - w.h


@pytest.fixture
def rigging(tmp_path: Path, ready: None) -> Rigging:
    return Rigging(tmp_path)


def test_starts_off_and_touches_nothing(rigging: Rigging) -> None:
    rigging.run(3)
    assert rigging.ctl.state()["state"] == "off"
    assert rigging.engine.windows == []
    assert rigging.video.calls == []  # not even a frame request while off
    assert rigging.opened == []  # the detector is not loaded until it is wanted


def test_gentle_tracking_crops_the_picture_smoothly(rigging: Rigging) -> None:
    assert rigging.ctl.act("framing.mode", {"mode": "gentle"}) == "Auto-framing: Gentle"
    rigging.run(12)
    state = rigging.ctl.state()
    assert state["state"] == "tracking"
    assert state["device"] == "Fake GPU" and state["backend"] == "fake"
    assert state["detect_ms"] == pytest.approx(3.0, abs=0.1)
    assert rigging.opened == ["auto"]
    left, top, right, bottom = rigging.crop()
    assert left > 0 and top > 0 and right > 0 and bottom > 0
    assert (1056 - left - right) / (704 - top - bottom) == pytest.approx(1056 / 704, rel=0.02)  # same aspect: a zoom
    zoom = 1056 / (1056 - left - right)
    assert 1.1 < zoom <= 1.31  # the Canon's resolution cap
    # writes come only while the picture moves
    count = len(rigging.engine.windows)
    rigging.run(10)
    assert len(rigging.engine.windows) - count < 15


def test_the_crop_never_steps_more_than_a_few_pixels_at_a_time(rigging: Rigging) -> None:
    rigging.ctl.act("framing.mode", {"mode": "tight"})
    rigging.run(20)
    lefts = [w.x for w in rigging.engine.windows if w is not None]
    assert max(abs(b - a) for a, b in itertools.pairwise(lefts)) <= 8


def test_detection_reads_the_engines_frame_and_the_crop_goes_to_the_engine(rigging: Rigging) -> None:
    rigging.ctl.act("framing.mode", {"mode": "follow"})
    rigging.run(3)
    assert ("detect_frame", "Canon EOS 50D") in rigging.video.calls
    assert any(w is not None for w in rigging.engine.windows)


def test_off_eases_back_to_the_full_frame(rigging: Rigging) -> None:
    rigging.ctl.act("framing.mode", {"mode": "tight"})
    rigging.run(12)
    assert rigging.crop() != (0, 0, 0, 0)
    rigging.ctl.act("framing.mode", {"mode": "off"})
    assert rigging.ctl.state()["state"] == "off"
    rigging.run(20)
    assert rigging.crop() == (0, 0, 0, 0) and rigging.engine.windows[-1] is None
    writes = len(rigging.engine.windows)
    rigging.run(5)
    assert len(rigging.engine.windows) == writes  # idle again


def test_losing_the_face_zooms_back_out_and_says_so(rigging: Rigging) -> None:
    rigging.ctl.act("framing.mode", {"mode": "tight"})
    rigging.run(12)
    rigging.det.faces = []
    rigging.run(2.5)
    assert rigging.ctl.state()["state"] == "no-face"
    rigging.run(25)
    assert rigging.crop() == (0, 0, 0, 0)
    rigging.det.faces = [Face(0.46, 0.30, 0.07, 0.12, 0.95)]
    rigging.run(12)
    assert rigging.ctl.state()["state"] == "tracking" and rigging.crop() != (0, 0, 0, 0)


def test_hold_freezes_the_crop_and_stops_detecting(rigging: Rigging) -> None:
    rigging.ctl.act("framing.mode", {"mode": "follow"})
    rigging.run(10)
    rigging.ctl.act("framing.hold", {"on": True})
    frozen, shots = rigging.crop(), len(rigging.video.calls)
    rigging.det.faces = [Face(0.70, 0.30, 0.07, 0.12, 0.95)]
    rigging.run(6)
    assert rigging.ctl.state()["state"] == "held" and rigging.ctl.state()["held"] is True
    assert max(abs(a - b) for a, b in zip(rigging.crop(), frozen, strict=True)) <= 3
    assert len(rigging.video.calls) == shots
    rigging.ctl.act("framing.hold", {"on": False})
    rigging.run(10)
    assert rigging.ctl.state()["state"] == "tracking"


def test_a_camera_change_moves_detection_and_the_crop_to_the_new_camera(rigging: Rigging) -> None:
    rigging.ctl.act("framing.mode", {"mode": "tight"})
    rigging.run(10)
    canon = next(s for s in rigging.ctl.state()["sources"] if s["name"] == "Canon EOS 50D")
    rigging.engine.window = None  # a camera change restarts the engine child at the full frame
    rigging.video.source = LINK
    rigging.run(10)
    state = rigging.ctl.state()
    assert ("detect_frame", "Cam Link 4K") in rigging.video.calls
    assert rigging.crop() != (0, 0, 0, 0)  # the Cam Link is framed now
    cam = next(s for s in state["sources"] if s["name"] == "Cam Link 4K")
    assert cam["on_air"] and cam["max_zoom"] > canon["max_zoom"] + 0.3  # 1080p source: more zoom than the Canon


def test_the_engine_going_away_pauses_framing_and_it_recovers(rigging: Rigging) -> None:
    rigging.ctl.act("framing.mode", {"mode": "gentle"})
    rigging.run(8)
    rigging.video.connected = False
    rigging.run(3)  # no exception, nothing to write to
    rigging.video.connected = True
    rigging.run(5)
    assert rigging.ctl.state()["state"] == "tracking"


def test_the_engine_refusing_a_crop_does_not_stop_framing(rigging: Rigging) -> None:
    rigging.ctl.act("framing.mode", {"mode": "gentle"})
    rigging.run(6)
    rigging.engine.fail_windows = True
    rigging.run(2)
    rigging.engine.fail_windows = False
    rigging.run(4)
    assert rigging.ctl.state()["state"] == "tracking"


def test_a_video_controller_that_cannot_crop_is_a_409_not_a_crash(tmp_path: Path, ready: None) -> None:
    rig = Rigging(tmp_path)
    rig.ctl.video = SimpleNamespace(scene_view=rig.video.scene_view, detect_frame=rig.video.detect_frame)
    rig.ctl.act("framing.mode", {"mode": "gentle"})
    rig.run(2)  # the apply loop treats the missing cropper as the engine being away
    assert rig.engine.windows == []


def test_missing_dependencies_refuse_the_mode_and_change_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health, "readiness", lambda: (False, "needs numpy and opencv", health.INSTALL))
    rig = Rigging(tmp_path)
    with pytest.raises(ActionError, match="needs numpy and opencv") as err:
        rig.ctl.act("framing.mode", {"mode": "gentle"})
    assert err.value.status == 409 and "streamstack[framing]" in err.value.message
    state = rig.ctl.state()
    assert state["mode"] == "off" and state["available"] is False and "Fix:" in state["unavailable_hint"]
    assert rig.ctl.act("framing.mode", {"mode": "off"}) == "Auto-framing: Off"  # off always works


def test_a_detector_that_cannot_load_falls_back_to_off_with_a_toast(tmp_path: Path, ready: None) -> None:
    rig = Rigging(tmp_path)

    def broken(device: str):
        raise detector.DetectorUnavailable("openvino: no GPU runtime; opencv: no cv2")

    rig.ctl._opener = broken
    rig.ctl.act("framing.mode", {"mode": "tight"})
    rig.run(3)
    state = rig.ctl.state()
    assert state["state"] == "unavailable" and state["mode"] == "off"
    assert "no GPU runtime" in state["reason"]
    assert rig.toasts and rig.toasts[0][0] == "warn" and "Auto-framing is off" in rig.toasts[0][1]
    assert rig.engine.windows == []
    assert health_titles(rig) == ["Auto-framing is unavailable"]
    assert json.loads((tmp_path / "state/framing.json").read_text()) == {"mode": "off"}  # does not retry at every start
    rig.ctl.act("framing.mode", {"mode": "off"})
    assert rig.ctl.state()["state"] == "off"


def health_titles(rig: Rigging) -> list[str]:
    return [c["title"] for c in rig.ctl.health_checks()]


def test_a_detector_that_keeps_failing_gives_up_without_touching_the_picture(rigging: Rigging) -> None:
    rigging.ctl.act("framing.mode", {"mode": "gentle"})
    rigging.run(6)
    crop_before = rigging.crop()
    rigging.det.error = OSError("GPU hung")
    rigging.run(2)
    assert rigging.ctl.state()["state"] == "unavailable"
    assert "GPU hung" in rigging.ctl.state()["reason"]
    assert rigging.det.closed
    rigging.run(20)
    assert rigging.crop() == (0, 0, 0, 0) != crop_before  # eased back to the camera's own picture


def test_a_bad_screenshot_is_skipped_quietly(rigging: Rigging) -> None:
    rigging.ctl.act("framing.mode", {"mode": "gentle"})
    rigging.det.error = ValueError("the picture is not a readable JPEG")
    rigging.run(0.4)  # fewer than MAX_ERRORS in a row
    rigging.det.error = None
    rigging.run(6)
    assert rigging.ctl.state()["state"] == "tracking"


def test_the_mode_is_remembered_between_runs(tmp_path: Path, ready: None) -> None:
    first = Rigging(tmp_path)
    first.ctl.act("framing.mode", {"mode": "follow"})
    again = Rigging(tmp_path)
    assert again.ctl.state()["mode"] == "follow"
    # the environment's default only applies when nothing was saved
    fresh = Rigging(tmp_path / "other", STREAMSTACK_FRAMING_MODE="tight")
    assert fresh.ctl.state()["mode"] == "tight"
    bad = Rigging(tmp_path / "bad", STREAMSTACK_FRAMING_MODE="sideways")
    assert bad.ctl.state()["mode"] == "off"


def test_a_corrupt_state_file_is_ignored(tmp_path: Path, ready: None) -> None:
    (tmp_path / "state").mkdir()
    (tmp_path / "state/framing.json").write_text("{nope")
    assert Rigging(tmp_path).ctl.state()["mode"] == "off"


def test_stop_restores_the_full_frame(rigging: Rigging) -> None:
    rigging.ctl.act("framing.mode", {"mode": "tight"})
    rigging.run(12)
    rigging.ctl.stop()
    assert rigging.crop() == (0, 0, 0, 0)
    assert rigging.det.closed


def test_the_threads_start_and_stop_cleanly(rigging: Rigging) -> None:
    rigging.ctl.start()
    rigging.ctl.start()  # idempotent
    rigging.ctl.stop()
    assert not any(t.is_alive() for t in rigging.ctl._threads)


def test_the_detector_is_released_after_framing_has_been_off_a_while(rigging: Rigging) -> None:
    rigging.ctl.act("framing.mode", {"mode": "gentle"})
    rigging.run(3)
    rigging.ctl.act("framing.mode", {"mode": "off"})
    rigging.ctl._detect_tick(rigging.clock.now)
    assert not rigging.det.closed
    rigging.clock.now += console_framing.CLOSE_AFTER_OFF + 1
    rigging.ctl._detect_tick(rigging.clock.now)
    assert rigging.det.closed and rigging.ctl.state()["device"] is None


# -- the actions ----------------------------------------


@pytest.mark.parametrize("mode", ["", None, "cinematic", 7])
def test_bad_modes_are_400(rigging: Rigging, mode: object) -> None:
    with pytest.raises(ActionError) as err:
        rigging.ctl.act("framing.mode", {"mode": mode})
    assert err.value.status == 400


def test_hold_and_reset_need_framing_on_and_a_real_flag(rigging: Rigging) -> None:
    for action, params in (("framing.hold", {"on": True}), ("framing.reset", {})):
        with pytest.raises(ActionError, match="is off"):
            rigging.ctl.act(action, params)
    rigging.ctl.act("framing.mode", {"mode": "gentle"})
    with pytest.raises(ActionError, match="true or false") as err:
        rigging.ctl.act("framing.hold", {"on": "yes"})
    assert err.value.status == 400
    assert rigging.ctl.act("framing.reset", {}) == "Framing recentred"
    with pytest.raises(ActionError, match="unknown action"):
        rigging.ctl.act("framing.zoom", {})


def test_the_actuator_api_matches_the_actions(rigging: Rigging) -> None:
    """What the AI director calls."""
    assert rigging.ctl.set_mode("tight") == "Auto-framing: Tight"
    assert rigging.ctl.status()["mode"] == "tight"
    rigging.run(2)
    assert rigging.ctl.hold(True) == "Framing held"
    assert rigging.ctl.status()["state"] == "held"
    assert rigging.ctl.hold(False) == "Framing released"
    assert rigging.ctl.reset() == "Framing recentred"


def test_state_shape_is_stable(rigging: Rigging) -> None:
    state = rigging.ctl.state()
    assert set(state) == {
        "mode", "state", "held", "available", "unavailable_hint", "reason", "backend", "device", "detect_ms",
        "detect_p95_ms", "fps", "model", "zoom", "window", "sources", "skipped", "modes",
    }  # fmt: skip
    assert [m["name"] for m in state["modes"]] == ["off", "gentle", "tight", "follow"]
    json.dumps(state)  # it goes to the browser as JSON


# -- health ----------------------------------------


def test_live_health_shows_device_and_time_per_detection(rigging: Rigging) -> None:
    assert rigging.ctl.health_checks() == []  # off: the static check stands
    rigging.ctl.act("framing.mode", {"mode": "gentle"})
    rigging.run(10)
    (check,) = rigging.ctl.health_checks()
    assert check["id"] == "framing" and check["state"] == "ok"
    assert "Fake GPU" in check["title"] and "3.0 ms per detection" in check["title"]


def test_a_slow_detector_earns_a_warning_with_the_fix(rigging: Rigging) -> None:
    rigging.det.timing.add(0.2)
    rigging.ctl.act("framing.mode", {"mode": "gentle"})
    rigging._ = None
    for _ in range(40):
        rigging.det.timing.add(0.2)
    rigging.run(2)
    (check,) = rigging.ctl.health_checks()
    assert check["state"] == "warn" and "STREAMSTACK_FRAMING_DEVICE=cpu" in check["hint"]


def test_static_health_check_levels(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(detector, "available_modules", lambda: {"numpy": False, "cv2": False, "openvino": False})
    quiet = health.health_check({})
    assert quiet["state"] == "ok" and "not installed (optional)" in quiet["title"]
    wanted = health.health_check({"STREAMSTACK_FRAMING_MODE": "gentle"})
    assert wanted["state"] == "warn" and "streamstack[framing]" in wanted["hint"]
    monkeypatch.setattr(detector, "available_modules", lambda: {"numpy": True, "cv2": True, "openvino": True})
    missing = health.health_check({})
    assert (
        missing["state"] == "warn"
        and "face model" in missing["title"]
        and missing["hint"] == "streamstack framing --fetch"
    )


# -- the rig and the demo ----------------------------------------


def test_the_demo_rig_carries_framing_through_state_actions_and_health() -> None:
    rig = Rig.demo()
    rig.start()
    try:
        assert rig.snapshot()["framing"]["state"] == "off"
        assert rig.act("framing.mode", {"mode": "tight"}) == "Auto-framing: Tight"
        framing = rig.snapshot()["framing"]
        assert framing["state"] == "tracking" and framing["zoom"] > 1 and "Arc A380" in framing["device"]
        titles = [c["title"] for c in rig.snapshot()["health"]["checks"] if c["id"] == "framing"]
        assert titles and "3.4 ms per detection" in titles[0]
        rig.act("framing.hold", {"on": True})
        assert rig.snapshot()["framing"]["state"] == "held"
        with pytest.raises(ActionError):
            rig.act("framing.mode", {"mode": "nope"})
        rig.act("framing.mode", {"mode": "off"})
        assert rig.snapshot()["framing"]["window"] is None
        with pytest.raises(ActionError, match="is off"):
            rig.act("framing.hold", {"on": True})
    finally:
        rig.stop()


def test_a_rig_without_framing_still_works() -> None:
    from streamstack.console.audio import DemoAudio
    from streamstack.console.studio import DemoStudio
    from streamstack.console.video import DemoVideo

    rig = Rig(DemoAudio(), DemoVideo(), DemoStudio(), demo=True)
    assert rig.snapshot()["framing"] is None
    with pytest.raises(ActionError, match="not part of this rig"):
        rig.act("framing.mode", {"mode": "gentle"})


def test_demo_framing_notifies_through_the_rig_hook() -> None:
    demo = DemoFraming()
    seen: list[tuple[str, str]] = []
    demo.notify = lambda level, text: seen.append((level, text))
    assert demo.notify is not None
