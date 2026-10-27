"""The Canon bridge loop against a fake camera and a fake ffmpeg: decode fallback, controls, stalls, pauses."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from streamstack.canon import bridge as bridge_mod
from streamstack.canon import encode
from streamstack.canon.bridge import Bridge, Command, Settings
from streamstack.canon.gphoto import CameraError, Reading

FRAME = (Path(__file__).parent / "fixtures" / "canon_liveview_1056x704.jpg").read_bytes()


def radio(key: str, value: str, choices: list[str], readonly: bool = False) -> Reading:
    return Reading(key=key, value=value, type="RADIO", label=key, readonly=readonly, choices=choices)


def default_settings() -> dict[str, Reading]:
    return {
        "iso": radio("iso", "400", ["100", "400", "800"]),
        "shutterspeed": radio("shutterspeed", "1/60", ["1/30", "1/60"]),
        "aperture": radio("aperture", "2.8", ["2.8", "4"], readonly=True),
        "whitebalance": radio("whitebalance", "Daylight", ["Auto", "Daylight", "Color Temperature"]),
        "colortemperature": Reading(key="colortemperature", value="5200", type="TEXT"),
        "liveviewsize": radio("liveviewsize", "Small", ["Small", "Medium", "Large"]),
        "cameramodel": Reading(key="cameramodel", value="Canon EOS 50D", type="TEXT", readonly=True),
    }


class FakeSession:
    shared = True

    def __init__(self, settings: dict[str, Reading] | None = None) -> None:
        self.settings = settings if settings is not None else default_settings()
        self.writes: list[list[tuple[str, str]]] = []
        self.frames_left = 10**9
        self.stall = False
        self.paused = False
        self.open_error: CameraError | None = None
        self.closed = False
        self.keep: set[str] = set()  # settings the "camera" silently refuses to change
        self.rests: list[bool] = []
        self.rest_error: CameraError | None = None

    def open(self) -> None:
        if self.open_error:
            raise self.open_error

    def close(self) -> None:
        self.closed = True

    def next_frame(self, timeout: float) -> bytes | None:
        if self.stall or self.paused or self.frames_left <= 0:
            time.sleep(min(timeout, 0.02))
            return None
        self.frames_left -= 1
        time.sleep(0.005)
        return FRAME

    def read(self, keys: list[str]) -> dict[str, Reading]:
        return {k: self.settings[k] if k in self.settings else Reading(key=k, error="not supported") for k in keys}

    def write(self, items: list[tuple[str, str]]) -> None:
        self.writes.append(list(items))
        for key, value in items:
            if key in self.keep:
                continue
            if key in self.settings:
                self.settings[key].value = value

    def rest(self, on: bool) -> None:
        self.rests.append(on)
        if on:
            self.paused = True  # live view ended: no frames until it resumes
        else:
            if self.rest_error:
                raise self.rest_error
            self.paused = False

    def stderr_tail(self) -> str:
        return ""


class FakeEncoder:
    def __init__(self, plan: encode.Plan) -> None:
        self.plan = plan
        self.fed = 0
        self.out_frames = 0
        self.last_out = time.monotonic()
        self.started = time.monotonic()
        self.proc = None
        self.up = False
        self.die_after: int | None = None
        self.silent = False

    def start(self) -> None:
        self.up = True
        self.started = self.last_out = time.monotonic()

    def alive(self) -> bool:
        return self.up

    def feed(self, frame: bytes) -> float:
        if not self.up:
            raise BrokenPipeError("down")
        self.fed += 1
        if not self.silent:
            self.out_frames += 1
            self.last_out = time.monotonic()
        if self.die_after is not None and self.fed >= self.die_after:
            self.up = False
        return 0.1

    def stderr_tail(self) -> str:
        return "boom"

    def stop(self) -> None:
        self.up = False


class Rig:
    def __init__(self, **settings: Any) -> None:
        self.session = FakeSession()
        self.sessions: list[FakeSession] = [self.session]
        self.encoders: list[FakeEncoder] = []
        self.probed: list[str] = []
        self.fail_probe: dict[str, str] = {}
        self.exits: list[int] = []
        self.busy_calls = 0

        def make_session() -> FakeSession:
            if len(self.sessions) > 1 or self.session_used:
                new = FakeSession()
                self.sessions.append(new)
                self.session = new
            self.session_used = True
            return self.session

        self.session_used = False

        def make_encoder(plan: encode.Plan) -> FakeEncoder:
            enc = FakeEncoder(plan)
            self.encoders.append(enc)
            return enc

        def probe(plan: encode.Plan, frame: bytes) -> str | None:
            self.probed.append(plan.name)
            return self.fail_probe.get(plan.name)

        cfg = Settings(device="/dev/video42", canvas=(1920, 1080), **settings)
        self.bridge = Bridge(
            cfg,
            make_session,
            make_encoder,
            probe,
            on_busy=lambda: setattr(self, "busy_calls", self.busy_calls + 1),
            exit_fn=self.exits.append,
            nodes=lambda: [("/dev/dri/renderD128", "0x8086")],
            version=lambda: (8, 0),
            usb=lambda: ["04a9:3215 Canon EOS 50D"],
        )
        self.thread: threading.Thread | None = None

    def start(self) -> Rig:
        self.thread = threading.Thread(target=self.bridge.run, daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        self.bridge.stop()
        if self.thread:
            self.thread.join(timeout=5)
            assert not self.thread.is_alive()

    def call(self, **req: Any) -> dict[str, Any]:
        return self.bridge.handle(req)


def wait_for(cond: Callable[[], Any], timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


@pytest.fixture
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_mod, "SOURCE_STALL", 0.4)
    monkeypatch.setattr(bridge_mod, "ENCODER_STALL", 0.4)
    monkeypatch.setattr(bridge_mod, "REFRESH_EVERY", 0.01)
    monkeypatch.setattr(bridge_mod, "STATS_EVERY", 0.05)


@pytest.fixture
def rig(fast: None) -> Iterator[Rig]:
    r = Rig()
    yield r
    r.stop()


def test_first_working_decode_path_wins_and_failures_are_reported(rig: Rig) -> None:
    rig.fail_probe = {"qsv": "Device creation failed: -1"}
    rig.start()
    wait_for(lambda: rig.encoders and rig.encoders[-1].fed > 3)
    status = rig.call(op="status")["status"]
    assert status["decode"]["path"] == "vaapi" and status["decode"]["label"] == "GPU (VA-API)"
    assert [(t["name"], t["ok"]) for t in status["decode"]["tried"]] == [("qsv", False), ("vaapi", True)]
    assert status["decode"]["tried"][0]["reason"] == "Device creation failed: -1"
    assert status["input"] == {"width": 1056, "height": 704, "subsampling": "4:2:0"}
    assert status["output"] == {"width": 1056, "height": 704, "mode": "native"}
    assert status["state"] == "running" and status["session"] == "shared" and status["usb"]


def test_cpu_is_the_fallback_and_the_legacy_command_the_last_resort(rig: Rig) -> None:
    rig.fail_probe = {"qsv": "x", "vaapi": "y"}
    rig.start()
    wait_for(lambda: rig.encoders and rig.encoders[-1].fed > 1)
    assert rig.encoders[-1].plan.name == "cpu"
    rig.stop()

    rig2 = Rig()
    rig2.fail_probe = {"qsv": "x", "vaapi": "y", "cpu": "z"}
    rig2.start()
    wait_for(lambda: rig2.encoders and rig2.encoders[-1].fed > 1)
    assert rig2.encoders[-1].plan.name == "legacy" and rig2.probed == ["qsv", "vaapi", "cpu", "legacy"]
    rig2.stop()


def test_forced_decode_still_falls_back_to_cpu(fast: None) -> None:
    r = Rig(decode="vaapi")
    r.fail_probe = {"vaapi": "no driver"}
    r.start()
    wait_for(lambda: r.encoders and r.encoders[-1].fed > 1)
    assert r.probed == ["vaapi", "cpu"] and r.encoders[-1].plan.name == "cpu"
    r.stop()


def test_a_gpu_that_failed_is_not_tried_again_after_an_encoder_restart(fast: None) -> None:
    r = Rig()
    r.fail_probe = {"qsv": "broken"}
    r.start()
    wait_for(lambda: r.encoders and r.encoders[-1].fed > 3)
    r.encoders[-1].die_after = 1  # ffmpeg exits right after starting
    wait_for(lambda: len(r.encoders) >= 2)
    assert "qsv" not in r.probed[2:] and r.bridge.restarts["encoder"] >= 1
    assert "vaapi" in r.bridge.demoted  # it died young: do not trust it again this run
    wait_for(lambda: r.encoders[-1].plan.name == "cpu" and r.encoders[-1].fed > 2)
    r.stop()


def test_live_view_size_is_raised_to_the_largest(rig: Rig) -> None:
    rig.start()
    wait_for(lambda: rig.session.writes)
    assert rig.session.writes[0] == [("liveviewsize", "Large")]


def test_stalled_encoder_is_restarted(fast: None) -> None:
    r = Rig()
    r.start()
    wait_for(lambda: r.encoders and r.encoders[-1].fed > 3)
    r.encoders[-1].silent = True
    wait_for(lambda: len(r.encoders) >= 2, 5.0)
    assert r.bridge.stalls >= 1 and r.bridge.restarts["encoder"] >= 1
    r.stop()


def test_stalled_camera_restarts_the_session_with_backoff(fast: None) -> None:
    r = Rig()
    r.start()
    wait_for(lambda: r.encoders and r.encoders[-1].fed > 3)
    r.session.stall = True
    wait_for(lambda: len(r.sessions) >= 2, 8.0)
    assert r.bridge.restarts["source"] >= 1 and r.sessions[0].closed
    wait_for(lambda: r.bridge.state == "running")
    r.stop()


def test_camera_missing_is_offline_and_busy_triggers_gvfs_release(fast: None) -> None:
    r = Rig()
    r.session.open_error = CameraError("No Canon camera found on USB", "busy")
    r.start()
    wait_for(lambda: r.busy_calls >= 1)
    assert r.bridge.state == "offline" and "No Canon" in (r.bridge.message or "")
    assert r.call(op="set", id="iso", value="100")["ok"] is False
    r.stop()


def test_pausing_session_keeps_the_last_picture_on_the_loopback(fast: None) -> None:
    r = Rig()
    r.start()
    wait_for(lambda: r.encoders and r.encoders[-1].fed > 3)
    r.session.paused = True
    fed = r.encoders[-1].fed
    wait_for(lambda: r.encoders[-1].fed > fed + 3)  # held frames keep flowing
    assert r.bridge.state == "paused"
    r.session.paused = False
    wait_for(lambda: r.bridge.state == "running")
    r.stop()


def test_set_changes_a_setting_and_reads_it_back(rig: Rig) -> None:
    rig.start()
    wait_for(lambda: rig.bridge.readings)
    answer = rig.call(op="set", id="iso", value="800")
    assert answer == {"ok": True, "applied": True, "value": "800"}
    assert rig.call(op="status")["status"]["controls"]["iso"]["value"] == "800"


def test_set_matches_choices_ignoring_case_and_rejects_unknown_ones(rig: Rig) -> None:
    rig.start()
    wait_for(lambda: rig.bridge.readings)
    assert rig.call(op="set", id="whitebalance", value="daylight")["ok"] is True
    bad = rig.call(op="set", id="iso", value="64000")
    assert bad["ok"] is False and "100, 400, 800" in bad["error"]
    assert rig.call(op="set", id="nope", value="1")["ok"] is False
    assert rig.call(op="set", id="model", value="x")["ok"] is False  # information only


def test_a_readonly_setting_says_why(rig: Rig) -> None:
    rig.start()
    wait_for(lambda: rig.bridge.readings)
    out = rig.call(op="set", id="aperture", value="4")
    assert out["ok"] is False and "mode dial" in out["error"]
    view = rig.call(op="status")["status"]["controls"]["aperture"]
    assert view["readonly"] is True and "mode dial" in view["reason"]


def test_kelvin_switches_white_balance_first_and_snaps(rig: Rig) -> None:
    rig.start()
    wait_for(lambda: rig.bridge.readings)
    assert rig.call(op="set", id="kelvin", value=5234)["value"] == "5200"
    assert rig.session.writes[-1] == [("whitebalance", "Color Temperature"), ("colortemperature", "5200")]
    rig.session.writes.clear()
    assert rig.call(op="set", id="kelvin", value=6500)["ok"] is True
    assert rig.session.writes[-1] == [("colortemperature", "6500")]  # already on Color Temperature


def test_the_camera_keeping_its_old_value_is_reported_not_hidden(rig: Rig) -> None:
    rig.start()
    wait_for(lambda: rig.bridge.readings)
    rig.session.keep.add("iso")
    out = rig.call(op="set", id="iso", value="800")
    assert out["ok"] is True and out["applied"] is False and "kept ISO at 400" in out["message"]


def test_quick_slider_drags_reach_the_camera_once(fast: None) -> None:
    r = Rig()
    r.bridge.session = r.session
    r.bridge.readings = r.session.settings
    cmds = [Command("set", {"id": "iso", "value": v}) for v in ("100", "400", "800")]
    for c in cmds:
        r.bridge.commands.put(c)
    r.bridge._drain()
    assert [c.result.get("superseded") for c in cmds if c.result] == [True, True, None]
    assert r.session.writes == [[("iso", "800")]]


def test_actions_use_real_libgphoto2_keys(rig: Rig) -> None:
    rig.start()
    wait_for(lambda: rig.bridge.readings)
    assert rig.call(op="action", name="autofocus")["ok"]
    assert rig.call(op="action", name="af_cancel")["ok"]
    assert rig.call(op="action", name="focus", step=-2)["ok"]
    assert rig.session.writes[-3:] == [
        [("autofocusdrive", "1")],
        [("cancelautofocus", "1")],
        [("manualfocusdrive", "Near 2")],
    ]
    assert rig.call(op="action", name="focus", step=9)["ok"] is False
    assert rig.call(op="action", name="focus", step="1")["ok"] is False
    assert rig.call(op="action", name="explode")["ok"] is False


def test_unknown_requests_are_refused(rig: Rig) -> None:
    assert rig.call(op="format-card")["ok"] is False


def test_a_call_stuck_in_the_camera_library_ends_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_mod, "HANG_LIMIT", 0.2)
    r = Rig()
    r.bridge.heartbeat = time.monotonic() - 5
    t = threading.Thread(target=r.bridge._watchdog, daemon=True)
    t.start()
    wait_for(lambda: r.exits)
    assert r.exits == [70]
    r.bridge.stop()


def test_settings_read_the_config_with_safe_fallbacks() -> None:
    s = Settings.from_config(
        {
            "STREAMSTACK_CAMERA_DEVICE": "/dev/video42",
            "STREAMSTACK_WIDTH": "abc",
            "STREAMSTACK_HEIGHT": "720",
            "STREAMSTACK_CANON_OUTPUT": "FILL",
            "STREAMSTACK_CANON_DECODE": "magic",
        }
    )
    assert s.canvas == (1920, 720) and s.output == "fill" and s.decode == "auto"


# -- resting live view while the engine is idle (#128) ------------------------------------------------------------


def test_rest_ends_live_view_holds_the_picture_and_resumes_on_release(fast: None) -> None:
    r = Rig()
    r.start()
    wait_for(lambda: r.encoders and r.encoders[-1].fed > 3)
    assert r.call(op="rest", seconds=30) == {"ok": True, "resting": True, "seconds": 30.0}
    wait_for(lambda: r.bridge.state == "idle")
    assert r.session.rests == [True]
    assert "no app uses the camera" in r.bridge.message
    assert r.call(op="status")["status"]["resting"] is True
    fed = r.encoders[-1].fed
    wait_for(lambda: r.encoders[-1].fed > fed + 3)  # the last frame keeps /dev/video42 alive at HOLD_FPS
    assert r.call(op="rest", seconds=0)["resting"] is False
    wait_for(lambda: r.bridge.state == "running")
    assert r.session.rests == [True, False]
    assert r.call(op="status")["status"]["resting"] is False
    r.stop()


def test_a_rest_lease_that_is_not_renewed_resumes_by_itself(fast: None) -> None:
    """The Console died while the engine was idle: the body must not stay dark."""
    r = Rig()
    r.start()
    wait_for(lambda: r.encoders and r.encoders[-1].fed > 3)
    r.call(op="rest", seconds=0.3)
    wait_for(lambda: r.bridge.state == "idle")
    wait_for(lambda: r.bridge.state == "running", timeout=5)
    assert r.session.rests == [True, False]
    r.stop()


def test_a_renewed_lease_keeps_resting(fast: None) -> None:
    r = Rig()
    r.start()
    wait_for(lambda: r.encoders and r.encoders[-1].fed > 3)
    for _ in range(6):
        r.call(op="rest", seconds=0.4)
        time.sleep(0.1)
    assert r.bridge.state == "idle" and r.session.rests == [True]
    r.stop()


def test_rest_rejects_bad_durations_and_caps_the_lease(rig: Rig) -> None:
    assert rig.call(op="rest", seconds="soon")["ok"] is False
    assert rig.call(op="rest", seconds=-1)["ok"] is False
    assert rig.call(op="rest", seconds=float("nan"))["ok"] is False
    assert rig.call(op="rest", seconds=10**6)["seconds"] == bridge_mod.MAX_REST
    rig.call(op="rest", seconds=0)


def test_resting_is_never_mistaken_for_a_stall(fast: None) -> None:
    r = Rig()
    r.start()
    wait_for(lambda: r.encoders and r.encoders[-1].fed > 3)
    r.call(op="rest", seconds=30)
    wait_for(lambda: r.bridge.state == "idle")
    time.sleep(bridge_mod.SOURCE_STALL * 2)  # longer than the stall limit
    assert len(r.sessions) == 1 and not r.session.closed  # the session was not restarted
    r.stop()


def test_a_camera_that_cannot_resume_restarts_the_session(fast: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_mod, "RESTART_BACKOFF", 0.01, raising=False)
    r = Rig()
    r.start()
    wait_for(lambda: r.encoders and r.encoders[-1].fed > 3)
    r.session.rest_error = CameraError("usb went away", "io")
    r.call(op="rest", seconds=30)
    wait_for(lambda: r.bridge.state == "idle")
    r.call(op="rest", seconds=0)
    wait_for(lambda: len(r.sessions) > 1, timeout=10)  # a fresh session, in live view
    r.call(op="rest", seconds=0)
    r.stop()
