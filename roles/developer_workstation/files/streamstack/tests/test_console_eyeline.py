"""The eye-line coach controller against a fake video link and a fake detector, driven tick by tick on a fake clock.

No test here opens a device: the autouse guard below fails any attempt to open a /dev/video node, a sound card or a
camera, so a regression cannot grab the desk's live cameras.
"""

from __future__ import annotations

import builtins
import json
import os
from pathlib import Path

import pytest

from streamstack.console import eyeline as console_eyeline
from streamstack.console.errors import ActionError
from streamstack.console.eyeline import DemoEyeLine, EyeLineController, empty_state
from streamstack.console.rig import Rig
from streamstack.framing import detector, eyeline, health
from streamstack.framing.engine import Face

JPEG = b"\xff\xd8jpeg"
STEP = 0.2


@pytest.fixture(autouse=True)
def no_live_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in this file may open /dev/video*, /dev/snd or any other live device."""
    real_open, real_os_open = builtins.open, os.open

    def guard(path: object) -> None:
        if str(path).startswith(("/dev/video", "/dev/snd", "/dev/media", "/dev/dri", "/dev/hidraw")):
            raise AssertionError(f"a test tried to open the live device {path}")

    def guarded_open(file: object, *args: object, **kwargs: object):
        guard(file)
        return real_open(file, *args, **kwargs)

    def guarded_os_open(path: object, *args: object, **kwargs: object) -> int:
        guard(path)
        return real_os_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(os, "open", guarded_os_open)


@pytest.fixture
def ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "readiness", lambda: (True, "", ""))


class FakeVideo:
    def __init__(self) -> None:
        self.connected = True
        self.cameras = {"Canon EOS 50D"}
        self.calls: list[str] = []

    def scene_view(self):
        return self.connected, "Program", ["Program"], set(self.cameras)

    def detect_frame(self, label: str) -> bytes | None:
        self.calls.append(label)
        return JPEG


class ScriptedDetector:
    backend = "fake"
    device = "Fake GPU"

    def __init__(self) -> None:
        self.pose: tuple[float, float] | None = (0.0, 0.0)  # yaw, pitch of the one face, None for no face
        self.error: Exception | None = None
        self.closed = False
        self.extra: list[Face] = []

    def detect(self, jpeg: bytes) -> list[Face]:
        if self.error:
            raise self.error
        faces = list(self.extra)
        if self.pose is not None:
            yaw, pitch = self.pose
            faces.append(Face(0.4, 0.2, 0.2, 0.3, 0.95, landmarks=eyeline.project(yaw, pitch)))
        return faces

    def close(self) -> None:
        self.closed = True


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Rigging:
    def __init__(self, tmp_path: Path, in_call: bool = False) -> None:
        self.video = FakeVideo()
        self.det = ScriptedDetector()
        self.clock = Clock()
        self.toasts: list[tuple[str, str]] = []
        self.call = in_call
        self.opened = 0
        self.file = tmp_path / "state" / "eyeline.json"

        def opener(device: str) -> detector.Detector:
            self.opened += 1
            return self.det

        self.ctl = EyeLineController(
            {}, self.video, lambda level, text: self.toasts.append((level, text)),
            opener=opener, state_file=self.file, clock=self.clock, in_call=lambda: self.call,
        )  # fmt: skip

    def run(self, ticks: int) -> None:
        for _ in range(ticks):
            self.clock.now += STEP
            self.ctl._tick(self.clock.now)


@pytest.fixture
def rigging(tmp_path: Path, ready: None) -> Rigging:
    return Rigging(tmp_path)


# -- off by default ------------------------------------------------------------------------------------------------


def test_it_is_off_by_default_and_touches_nothing(rigging: Rigging) -> None:
    state = rigging.ctl.state()
    assert state["enabled"] is False and state["state"] == "off" and state["gaze"] is None
    assert state["reminder"]["on"] is False and state["calibrated"] is False
    rigging.run(10)
    assert rigging.opened == 0 and rigging.video.calls == []  # no detector, no picture while it is off


def test_the_empty_state_has_every_key_the_live_state_has(rigging: Rigging) -> None:
    assert set(empty_state()) == set(rigging.ctl.state())
    assert set(empty_state()["advice"]) == set(rigging.ctl.state()["advice"])


# -- running ----------------------------------------------------------------------------------------------------------


def test_turning_it_on_opens_the_detector_and_reads_the_gaze(rigging: Rigging) -> None:
    assert rigging.ctl.act("eyeline.enable", {"on": True}) == "Eye-line coach on"
    rigging.run(8)
    state = rigging.ctl.state()
    assert rigging.opened == 1 and rigging.video.calls and set(rigging.video.calls) == {"Canon EOS 50D"}
    assert state["state"] == "running" and state["device"] == "Fake GPU" and state["gaze"] == "lens"
    assert state["advice"]["kind"] == "wait" or state["advice"]["kind"] == "ok"


def test_a_camera_above_the_eye_line_is_reported_in_degrees(rigging: Rigging) -> None:
    rigging.ctl.act("eyeline.enable", {"on": True})
    rigging.det.pose = (0.0, 14.0)
    rigging.run(40)
    state = rigging.ctl.state()
    assert state["gaze"] == "screen" and state["advice"]["kind"] == "lower"
    assert 11 <= state["advice"]["degrees"] <= 17 and "degrees off your eye line" in state["advice"]["text"]
    assert state["advice"]["approximate"] is True  # not calibrated yet
    assert state["eye_line"] == pytest.approx(0.4, abs=0.01) and "top third" not in state["eye_line_hint"]


def test_the_person_at_the_desk_is_the_biggest_face(rigging: Rigging) -> None:
    rigging.det.extra = [Face(0.8, 0.2, 0.05, 0.08, 0.9, landmarks=eyeline.project(0, 45, scale=0.0008))]
    rigging.ctl.act("eyeline.enable", {"on": True})
    rigging.run(10)
    assert rigging.ctl.state()["gaze"] == "lens"  # the small face behind them looks at the keyboard; theirs does not


def test_no_face_and_no_camera_are_said_so_and_nothing_breaks(rigging: Rigging) -> None:
    rigging.ctl.act("eyeline.enable", {"on": True})
    rigging.det.pose = None
    rigging.run(30)
    assert rigging.ctl.state()["state"] == "no-face" and rigging.ctl.state()["gaze"] is None
    rigging.video.cameras = set()
    rigging.run(3)
    rigging.video.connected = False
    rigging.run(3)
    assert rigging.ctl.state()["state"] == "no-face"


def test_a_detector_that_keeps_failing_turns_the_coach_off_with_a_toast(rigging: Rigging) -> None:
    rigging.ctl.act("eyeline.enable", {"on": True})
    rigging.det.error = RuntimeError("GPU hung")
    rigging.run(console_eyeline.MAX_ERRORS + 1)
    state = rigging.ctl.state()
    assert state["enabled"] is False and state["state"] == "unavailable" and "GPU hung" in state["reason"]
    assert rigging.det.closed and any("eye-line coach is off" in text for _, text in rigging.toasts)
    assert json.loads(rigging.file.read_text())["enabled"] is False


def test_a_detector_that_cannot_open_says_why(tmp_path: Path, ready: None) -> None:
    def broken(device: str) -> detector.Detector:
        raise detector.DetectorUnavailable("OpenVINO is not installed")

    toasts: list[tuple[str, str]] = []
    ctl = EyeLineController(
        {}, FakeVideo(), lambda *a: toasts.append(a), opener=broken, state_file=tmp_path / "e.json", clock=Clock()
    )
    ctl.act("eyeline.enable", {"on": True})
    ctl._tick(1.0)
    state = ctl.state()
    assert state["state"] == "unavailable" and state["reason"] == "OpenVINO is not installed" and toasts


def test_the_detector_is_released_a_minute_after_the_coach_goes_off(rigging: Rigging) -> None:
    rigging.ctl.act("eyeline.enable", {"on": True})
    rigging.run(3)
    rigging.ctl.act("eyeline.enable", {"on": False})
    rigging.run(2)
    assert not rigging.det.closed
    rigging.clock.now += console_eyeline.CLOSE_AFTER_OFF + 1
    rigging.run(1)
    assert rigging.det.closed


def test_it_cannot_be_turned_on_where_framing_is_not_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        health, "readiness", lambda: (False, "OpenVINO is not installed", "pip install 'streamstack[framing]'")
    )
    rigging = Rigging(tmp_path)
    with pytest.raises(ActionError, match="not available") as err:
        rigging.ctl.act("eyeline.enable", {"on": True})
    assert err.value.status == 409 and rigging.ctl.state()["enabled"] is False
    assert rigging.ctl.state()["available"] is False and "pip install" in rigging.ctl.state()["unavailable_hint"]


# -- calibration -------------------------------------------------------------------------------------------------------


def test_calibrating_measures_angles_from_the_lens_pose_and_is_remembered(tmp_path: Path, ready: None) -> None:
    first = Rigging(tmp_path)
    first.ctl.act("eyeline.enable", {"on": True})
    first.det.pose = (2.0, 6.0)  # this person rests their head six degrees down while looking at the lens
    first.run(3)
    assert first.ctl.act("eyeline.calibrate", {}) == "Look at the camera lens"
    assert first.ctl.state()["calibrating"] is True
    first.run(eyeline.CALIBRATE_SAMPLES + 2)
    state = first.ctl.state()
    assert state["calibrating"] is False and state["calibrated"] is True
    assert state["baseline"]["pitch"] == pytest.approx(6.0, abs=1.5) and state["baseline"]["yaw"] == pytest.approx(
        2.0, abs=1.5
    )
    assert any("calibrated" in text for _, text in first.toasts)
    first.run(10)
    assert first.ctl.state()["gaze"] == "lens"  # their own lens pose is now the zero
    again = Rigging(tmp_path)
    assert again.ctl.state()["calibrated"] is True and again.ctl.state()["baseline"] == state["baseline"]
    first.ctl.act("eyeline.calibration.clear", {})
    assert first.ctl.state()["calibrated"] is False
    assert json.loads(first.file.read_text())["baseline"] is None


def test_calibration_needs_the_coach_on_and_gives_up_when_no_face_is_seen(rigging: Rigging) -> None:
    with pytest.raises(ActionError, match="Turn the eye-line coach on") as err:
        rigging.ctl.act("eyeline.calibrate", {})
    assert err.value.status == 409
    rigging.ctl.act("eyeline.enable", {"on": True})
    rigging.det.pose = None
    rigging.run(2)
    rigging.ctl.act("eyeline.calibrate", {})
    rigging.run(int(console_eyeline.CALIBRATE_TIMEOUT / STEP) + 5)
    state = rigging.ctl.state()
    assert state["calibrating"] is False and state["calibrated"] is False
    assert any("could not see your face" in text for _, text in rigging.toasts)


# -- the reminder ------------------------------------------------------------------------------------------------------


def test_the_reminder_is_off_by_default_and_only_speaks_during_a_call(tmp_path: Path, ready: None) -> None:
    rigging = Rigging(tmp_path, in_call=True)
    rigging.ctl.act("eyeline.enable", {"on": True})
    rigging.det.pose = (0.0, 50.0)
    for _ in range(int(4 * 60 / STEP)):
        rigging.run(1)
    assert rigging.toasts == []  # in a call, away for four minutes, but the reminder is off
    rigging.ctl.act("eyeline.reminder", {"on": True, "minutes": 2})
    rigging.call = False
    for _ in range(int(60 / STEP)):
        rigging.run(1)
    assert rigging.toasts == []  # on, but no call
    rigging.call = True
    rigging.run(1)
    assert [text for _, text in rigging.toasts] == [
        "You have been looking away from the screen for 2 minutes. Back to the call?"
    ]
    rigging.run(100)
    assert len(rigging.toasts) == 1  # once per episode
    assert rigging.ctl.state()["reminder"]["away_s"] > 240


def test_the_reminder_settings_are_checked_and_saved(rigging: Rigging) -> None:
    assert rigging.ctl.act("eyeline.reminder", {"on": True, "minutes": 5}) == "Looking-away reminder on"
    assert json.loads(rigging.file.read_text()) == {"enabled": False, "reminder": True, "minutes": 5, "baseline": None}
    for bad in ({"on": "yes"}, {"on": True, "minutes": 0}, {"on": True, "minutes": 31}, {"on": True, "minutes": 2.5},
                {"on": True, "minutes": True}, {"on": True, "minutes": "5"}):  # fmt: skip
        with pytest.raises(ActionError) as err:
            rigging.ctl.act("eyeline.reminder", bad)
        assert err.value.status == 400
    assert rigging.ctl.state()["reminder"]["minutes"] == 5
    again = Rigging(rigging.file.parent.parent, in_call=False)
    assert again.ctl.state()["reminder"] == {"on": True, "minutes": 5, "range": [1, 30], "away_s": 0}


def test_a_damaged_or_odd_settings_file_loads_the_defaults(tmp_path: Path, ready: None) -> None:
    path = tmp_path / "state" / "eyeline.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert Rigging(tmp_path).ctl.state()["enabled"] is False
    path.write_text(json.dumps({"enabled": "yes", "minutes": 999, "baseline": {"pitch": "x"}, "reminder": 1}))
    state = Rigging(tmp_path).ctl.state()
    assert state["enabled"] is False and state["reminder"]["on"] is False and state["calibrated"] is False
    assert state["reminder"]["minutes"] == 30  # clamped, not trusted
    path.write_text("[1, 2]")
    assert Rigging(tmp_path).ctl.state()["enabled"] is False


def test_unknown_actions_and_bad_values_are_refused(rigging: Rigging) -> None:
    with pytest.raises(ActionError, match="unknown action"):
        rigging.ctl.act("eyeline.nope", {})
    with pytest.raises(ActionError, match="true or false"):
        rigging.ctl.act("eyeline.enable", {"on": 1})


# -- privacy -----------------------------------------------------------------------------------------------------------


def test_the_state_carries_numbers_only_never_a_picture(rigging: Rigging) -> None:
    rigging.ctl.act("eyeline.enable", {"on": True})
    rigging.det.pose = (0.0, 14.0)
    rigging.run(40)

    def walk(value: object) -> None:
        assert not isinstance(value, (bytes, bytearray)), "a picture in the state"
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(rigging.ctl.state())
    assert JPEG not in json.dumps(rigging.ctl.state(), default=repr).encode()
    assert not any(p.suffix in (".jpg", ".jpeg", ".png") for p in rigging.file.parent.rglob("*"))


# -- the demo rig and the Rig -----------------------------------------------------------------------------------------


def test_the_demo_simulates_a_person_and_has_the_same_state() -> None:
    demo = DemoEyeLine()
    assert set(demo.state()) == set(empty_state())
    demo.act("eyeline.enable", {"on": True})
    demo.act("eyeline.demo.gaze", {"gaze": "screen", "offset": 15})
    base = demo._clock()
    for i in range(60):
        demo._tick(base + i * 0.1)
    state = demo.state()
    assert state["enabled"] and state["gaze"] == "screen" and state["advice"]["kind"] == "lower"
    with pytest.raises(ActionError):
        demo.act("eyeline.demo.gaze", {"gaze": "ceiling"})
    with pytest.raises(ActionError):
        demo.act("eyeline.demo.gaze", {"gaze": "screen", "offset": "x"})
    with pytest.raises(ActionError, match="not looking away"):
        demo.act("eyeline.demo.away", {"seconds": 60})
    demo.act("eyeline.demo.reset", {})
    assert demo.state()["enabled"] is False


def test_the_demo_never_reads_or_writes_the_persons_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    demo = DemoEyeLine()
    demo.act("eyeline.enable", {"on": True})
    demo.act("eyeline.reminder", {"on": True, "minutes": 4})
    assert list(tmp_path.rglob("eyeline*.json")) == []


def test_the_rig_serves_the_coach_and_routes_its_actions() -> None:
    rig = Rig.demo()
    assert set(rig.snapshot()["eyeline"]) == set(empty_state())
    rig.act("eyeline.enable", {"on": True})
    assert rig.snapshot()["eyeline"]["enabled"] is True
    rig.act("eyeline.demo.reset", {})
    assert rig.eyeline.in_call == rig.call.active  # the reminder follows the call controller


def test_a_rig_without_the_coach_reports_an_empty_one() -> None:
    rig = Rig.demo()
    rig.eyeline = None
    assert rig.snapshot()["eyeline"] == empty_state()
    with pytest.raises(ActionError, match="not part of this rig"):
        rig.act("eyeline.enable", {"on": True})
