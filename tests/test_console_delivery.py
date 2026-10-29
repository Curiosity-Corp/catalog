"""Camera delivery: StreamStack Camera stays fed when the source goes away, and the engine comes back by itself."""

from __future__ import annotations

from pathlib import Path

import pytest

from streamstack import config
from streamstack.console.arcblur import ArcBlur
from streamstack.console.delivery import Delivery
from streamstack.console.native import NativeVideo
from streamstack.engine import source
from streamstack.engine.graph import Source, Spec
from streamstack.engine.segment import Arc
from streamstack.engine.slate import SlateSpec
from tests.test_console_native import CANON, CANON_SRC, LINK, LINK_SRC, FakeEngine, wait_for

CFG = {**config.DEFAULTS}
SPEC = Spec(CANON_SRC, "/dev/video43", 1280, 720)


class FakeSlate:
    def __init__(self) -> None:
        self.shown: list[tuple[SlateSpec, bytes | None]] = []
        self.cleared = 0
        self._active = False
        self.polled = 0

    @property
    def active(self) -> bool:
        return self._active

    def show(self, spec: SlateSpec, hold_frame: bytes | None = None) -> None:
        self._active = True
        self.shown.append((spec, hold_frame))

    def clear(self) -> None:
        self._active = False
        self.cleared += 1

    def poll(self) -> None:
        self.polled += 1

    def state(self) -> dict:
        return {"active": self._active, "writing": self._active, "reason": "", "mode": "card", "seconds": 0.0}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class World:
    """What the machine looks like: which nodes exist and which loopbacks have a producer."""

    def __init__(self) -> None:
        self.nodes = {"/dev/video42", "/dev/video0"}
        self.fed = {"/dev/video42": True}

    def probe(self, device: str) -> bool | None:
        return self.fed.get(device)

    def exists(self, device: str) -> bool:
        return device in self.nodes


@pytest.fixture
def world() -> World:
    return World()


def make(world: World, cfg: dict | None = None, **kw) -> tuple[Delivery, FakeSlate, Clock, list[tuple[str, str]]]:
    slate, clock, said = FakeSlate(), Clock(), []
    delivery = Delivery(
        {**CFG, **(cfg or {})},
        slate=slate,  # type: ignore[arg-type]
        notify=lambda level, text: said.append((level, text)),
        probe=world.probe,
        exists=world.exists,
        clock=clock,
        **kw,
    )
    return delivery, slate, clock, said


# -- is a camera deliverable ----------------------------------------------------------------------------------------


def test_a_fed_loopback_is_ready_and_an_unfed_one_says_why(world: World) -> None:
    delivery, *_ = make(world)
    assert delivery.camera_ready("/dev/video42", "Canon EOS 50D") is None
    world.fed["/dev/video42"] = False
    assert "no producer on /dev/video42" in (delivery.camera_ready("/dev/video42", "Canon EOS 50D") or "")


def test_a_missing_node_is_not_connected(world: World) -> None:
    delivery, *_ = make(world)
    world.nodes.discard("/dev/video0")
    assert "not connected" in (delivery.camera_ready("/dev/video0", "Cam Link 4K") or "")


def test_a_probe_that_cannot_tell_does_not_block_the_engine(world: World) -> None:
    delivery, *_ = make(world)
    world.fed.clear()  # v4l2-ctl missing: None
    assert delivery.camera_ready("/dev/video42", "Canon EOS 50D") is None


# -- the engine guard ---------------------------------------------------------------------------------------------


def test_guard_covers_the_node_while_the_source_is_gone_and_releases_it_when_back(world: World) -> None:
    delivery, slate, _, said = make(world)
    world.fed["/dev/video42"] = False
    reason = delivery.guard(SPEC)
    assert reason and "no producer" in reason and slate.active
    spec, _hold = slate.shown[-1]
    assert (spec.device, spec.width, spec.height) == ("/dev/video43", 1280, 720)  # the engine's own caps
    assert spec.fps == 20.0 and spec.reason == reason
    assert said[0][0] == "warn" and "camera reconnecting" in said[0][1]
    delivery.guard(SPEC)  # still gone: one message, not one per poll
    assert len([s for s in said if s[0] == "warn"]) == 1
    world.fed["/dev/video42"] = True
    assert delivery.guard(SPEC) is None and not slate.active
    assert said[-1][0] == "ok" and "back" in said[-1][1]


def test_the_slate_defaults_to_the_adr_contract_before_the_engine_ever_ran(world: World) -> None:
    delivery, slate, *_ = make(world)
    delivery.cover("Canon EOS 50D is not connected")
    spec, _ = slate.shown[-1]
    assert (spec.width, spec.height, spec.fps) == (1920, 1080, 30.0)


def test_a_configured_engine_size_is_what_the_slate_writes(world: World) -> None:
    delivery, slate, *_ = make(world, {"STREAMSTACK_ENGINE_SIZE": "1280x720"})
    delivery.cover("x")
    assert slate.shown[-1][0].width == 1280 and slate.shown[-1][0].height == 720


def test_the_slate_follows_the_node_format_not_the_slow_camera(world: World) -> None:
    from streamstack.engine.graph import Source, Spec

    card = Source("Cam Link 4K", "/dev/video0", "YUYV", 720, 480, 21.8)
    delivery, slate, *_ = make(world)
    delivery.note_output(Spec(card, "/dev/video43", 1280, 720, out_fps=30.0, letterbox=True))
    delivery.cover("x")
    shown = slate.shown[-1][0]
    assert (shown.width, shown.height, shown.fps) == (1280, 720, 30.0)


def test_loss_off_writes_nothing(world: World) -> None:
    delivery, slate, *_ = make(world, {"STREAMSTACK_CAMERA_LOSS": "off"})
    world.fed["/dev/video42"] = False
    assert delivery.guard(SPEC)  # the engine still waits for the camera
    assert slate.shown == []


def test_hold_passes_the_last_frame_to_the_slate(world: World) -> None:
    delivery, slate, *_ = make(world, {"STREAMSTACK_CAMERA_LOSS": "hold"}, last_frame=lambda: b"jpeg")
    delivery.cover("x")
    spec, frame = slate.shown[-1]
    assert spec.hold and frame == b"jpeg"


def test_an_unknown_loss_mode_is_the_slate(world: World) -> None:
    delivery, slate, *_ = make(world, {"STREAMSTACK_CAMERA_LOSS": "banana"})
    delivery.cover("x")
    assert slate.shown and not slate.shown[-1][0].hold


def test_a_child_that_keeps_dying_is_covered_at_once(world: World) -> None:
    delivery, slate, *_ = make(world)
    delivery.on_exit("the video engine stopped: Device or resource busy", 0)
    assert not slate.active
    delivery.on_exit("the video engine stopped: Device or resource busy", 1)
    assert slate.active


# -- fail over and back -----------------------------------------------------------------------------------------


def test_the_picked_camera_is_used_while_it_delivers(world: World) -> None:
    delivery, *_ = make(world)
    assert delivery.choose([CANON, LINK], "Canon EOS 50D") is CANON
    assert delivery.choose([CANON, LINK], "Cam Link 4K") is LINK


def test_the_engine_moves_to_the_cam_link_after_the_delay_and_back_when_steady(world: World) -> None:
    delivery, _, clock, said = make(world)
    cameras = [CANON, LINK]
    world.fed["/dev/video42"] = False
    assert delivery.choose(cameras, "Canon EOS 50D") is CANON  # lost just now: the slate covers, no flapping
    clock.now += 5
    assert delivery.choose(cameras, "Canon EOS 50D") is CANON
    clock.now += 2  # 7 s without a producer
    assert delivery.choose(cameras, "Canon EOS 50D") is LINK
    assert any("is on Cam Link 4K" in text for _, text in said)
    world.fed["/dev/video42"] = True  # it is back, but not yet trusted
    assert delivery.choose(cameras, "Canon EOS 50D") is LINK
    clock.now += 3
    assert delivery.choose(cameras, "Canon EOS 50D") is LINK
    clock.now += 3
    assert delivery.choose(cameras, "Canon EOS 50D") is CANON
    assert said[-1][0] == "ok" and "moved back" in said[-1][1]


def test_a_blip_does_not_move_the_engine(world: World) -> None:
    delivery, _, clock, _ = make(world)
    world.fed["/dev/video42"] = False
    delivery.choose([CANON, LINK], "Canon EOS 50D")
    clock.now += 3
    world.fed["/dev/video42"] = True
    delivery.choose([CANON, LINK], "Canon EOS 50D")
    clock.now += 10
    world.fed["/dev/video42"] = False
    assert delivery.choose([CANON, LINK], "Canon EOS 50D") is CANON  # the timer started over


def test_fallback_off_keeps_the_picked_camera(world: World) -> None:
    delivery, _, clock, _ = make(world, {"STREAMSTACK_CAMERA_FALLBACK": "off"})
    world.fed["/dev/video42"] = False
    clock.now += 60
    assert delivery.choose([CANON, LINK], "Canon EOS 50D") is CANON


def test_no_stand_in_means_the_slate_on_the_picked_camera(world: World) -> None:
    delivery, _, clock, _ = make(world)
    world.fed["/dev/video42"] = False
    world.nodes.discard("/dev/video0")
    clock.now += 60
    assert delivery.choose([CANON], "Canon EOS 50D") is CANON


def test_a_stand_in_that_goes_too_is_dropped(world: World) -> None:
    delivery, _, clock, _ = make(world)
    world.fed["/dev/video42"] = False
    delivery.choose([CANON, LINK], "Canon EOS 50D")
    clock.now += 10
    assert delivery.choose([CANON, LINK], "Canon EOS 50D") is LINK
    world.nodes.discard("/dev/video0")  # the Cam Link was unplugged
    assert delivery.choose([CANON, LINK], "Canon EOS 50D") is CANON


def test_the_settings_delays_are_read_as_seconds(world: World) -> None:
    delivery, _, clock, _ = make(world, {"STREAMSTACK_CAMERA_FALLBACK_AFTER": "1"})
    world.fed["/dev/video42"] = False
    delivery.choose([CANON, LINK], "Canon EOS 50D")
    clock.now += 1.5
    assert delivery.choose([CANON, LINK], "Canon EOS 50D") is LINK


def test_nothing_to_choose_is_none(world: World) -> None:
    delivery, *_ = make(world)
    assert delivery.choose([], None) is None


# -- what the Console shows ---------------------------------------------------------------------------------------


def test_state_and_checks_name_the_mode_and_the_fix(world: World) -> None:
    delivery, slate, *_ = make(world)
    live = delivery.state("running", "Canon EOS 50D", "Canon EOS 50D")
    assert live["mode"] == "live"
    row = delivery.checks(live, external=True)[0]
    assert row["state"] == "ok" and row["id"] == "video-delivery" and row["group"] == "video"
    delivery.cover("Canon EOS 50D has no producer on /dev/video42")
    block = delivery.state("waiting", "Canon EOS 50D", "Canon EOS 50D")
    assert block["mode"] == "reconnecting" and block["slate"]["active"]
    row = delivery.checks(block, external=True)[0]
    assert row["state"] == "fail" and "camera reconnecting" in row["title"]
    assert "canonctl-ptp-webcam.service" in row["hint"] and "no producer" in row["detail"]
    own = delivery.checks(block, external=False)[0]
    assert "streamstack-canon.service" in own["hint"]
    slate.clear()
    fallback = delivery.state("running", "Cam Link 4K", "Canon EOS 50D")
    assert fallback["mode"] == "live"  # fallback is set by choose(); see the next test


def test_fallback_is_reported_as_a_warning(world: World) -> None:
    delivery, _, clock, _ = make(world)
    world.fed["/dev/video42"] = False
    delivery.choose([CANON, LINK], "Canon EOS 50D")
    clock.now += 10
    delivery.choose([CANON, LINK], "Canon EOS 50D")
    block = delivery.state("running", "Cam Link 4K", "Canon EOS 50D")
    assert block["mode"] == "fallback"
    row = delivery.checks(block, external=False)[0]
    assert row["state"] == "warn" and "Cam Link 4K, not Canon EOS 50D" in row["title"]


# -- NativeVideo: the incident ---------------------------------------------------------------------------------


@pytest.fixture
def rig(tmp_path: Path, world: World):
    """A controller whose Canon has no producer at start-up: the 2026-10-27 reboot."""
    present = [CANON]
    made: list[FakeEngine] = []
    state = {"canon_up": False}

    def factory(spec: Spec, look: dict | None = None, **kwargs) -> FakeEngine:
        engine = FakeEngine(spec, look, **kwargs)
        engine.guard = kwargs.get("before_child")  # type: ignore[attr-defined]
        made.append(engine)
        return engine

    def resolve(camera: source.Camera, cfg: dict) -> Source:
        if camera.kind == "camlink":
            return LINK_SRC
        if not state["canon_up"]:
            raise source.SourceError(f"{camera.label} offers no picture (no signal, or the device is busy).")
        return CANON_SRC

    cfg = {**config.DEFAULTS}
    slate = FakeSlate()
    delivery = Delivery(cfg, slate=slate, probe=lambda device: state["canon_up"], exists=world.exists)  # type: ignore[arg-type]
    video = NativeVideo(
        cfg,
        arc=ArcBlur(cfg, probe=lambda wanted: Arc(False, "no openvino")),
        state_file=tmp_path / "video.json",
        discover=lambda c: list(present),
        resolve=resolve,
        engine_factory=factory,
        backend=lambda node: ("vulkan", ""),
        delivery=delivery,
    )
    video.start()
    handles = type("Rig", (), {})()
    handles.video, handles.slate, handles.made, handles.state, handles.present = video, slate, made, state, present
    yield handles
    video.stop()


def test_a_camera_that_is_absent_at_start_leaves_the_slate_on_the_program_node(rig) -> None:
    assert wait_for(lambda: rig.slate.active)
    assert rig.made == []  # no engine yet: nothing to read
    block = rig.video.state()["engine"]
    assert block["delivery"]["mode"] == "reconnecting" and block["state"] == "waiting"
    row = next(c for c in rig.video.health_checks() if c["id"] == "video-delivery")
    assert row["state"] == "fail" and "camera reconnecting" in row["title"]


def test_the_engine_takes_over_by_itself_when_the_camera_returns(rig) -> None:
    assert wait_for(lambda: rig.slate.active)
    rig.state["canon_up"] = True
    assert wait_for(lambda: rig.made and rig.made[0].running)
    assert rig.made[0].guard is not None  # the supervisor asks the guard before every child start
    assert rig.made[0].guard(rig.made[0].spec) is None and not rig.slate.active  # and the guard frees the node


def test_with_the_cam_link_plugged_in_the_engine_fails_over_after_the_delay(rig, monkeypatch) -> None:
    rig.video.cfg["STREAMSTACK_CAMERA_FALLBACK_AFTER"] = "0"
    rig.present.append(LINK)
    assert wait_for(lambda: rig.made and rig.made[0].running)
    assert rig.made[0].spec.source.label == "Cam Link 4K"
    assert rig.made[0].guard(rig.made[0].spec) is None  # the supervisor's check before the child starts
    assert rig.video.state()["engine"]["delivery"]["mode"] == "fallback"
    rig.state["canon_up"] = True
    rig.video.cfg["STREAMSTACK_CAMERA_FALLBACK_BACK"] = "0"
    assert wait_for(lambda: rig.made[0].spec.source.label == "Canon EOS 50D")
    assert wait_for(lambda: rig.video.state()["engine"]["delivery"]["mode"] == "live")


def test_turning_the_program_camera_off_releases_the_node(rig) -> None:
    assert wait_for(lambda: rig.slate.active)
    rig.video.act("video.virtualcam", {"on": False})
    assert wait_for(lambda: not rig.slate.active)


# -- parking the node while the engine idles (#128) ---------------------------------------------------------------


def test_park_holds_the_node_with_a_quiet_placeholder_and_no_toast(world: World) -> None:
    delivery, slate, _clock, said = make(world)
    assert delivery.park() is True
    spec, _frame = slate.shown[-1]
    assert spec.caption == "Camera idle" and spec.fps == 5.0 and spec.device == "/dev/video43"
    assert delivery.parked
    assert delivery.state("stopped", None, None)["mode"] == "idle"
    assert said == []  # resting is not a fault
    (row,) = delivery.checks(delivery.state("stopped", None, None), False)
    assert row["state"] == "ok" and "idle" in row["title"]


def test_park_keeps_the_last_picture_when_the_loss_mode_is_hold(world: World) -> None:
    delivery, slate, *_ = make(world, {"STREAMSTACK_CAMERA_LOSS": "hold"})
    delivery._last_frame = lambda: b"\xff\xd8last"  # type: ignore[method-assign]
    delivery.park()
    spec, frame = slate.shown[-1]
    assert spec.hold is True and frame == b"\xff\xd8last"


def test_park_is_refused_when_nothing_but_the_engine_may_write_the_node(world: World) -> None:
    delivery, slate, *_ = make(world, {"STREAMSTACK_CAMERA_LOSS": "off"})
    assert delivery.park() is False
    assert not slate.shown and not delivery.parked


def test_releasing_a_parked_node_says_nothing_but_releasing_a_covered_one_does(world: World) -> None:
    delivery, _slate, _clock, said = make(world)
    delivery.park()
    delivery.release()
    assert not delivery.parked and said == []
    delivery.cover("camera gone")
    delivery.release()
    assert any("Camera is back" in text for _level, text in said)


def test_a_cover_over_a_parked_node_is_a_fault_again(world: World) -> None:
    delivery, *_ = make(world)
    delivery.park()
    delivery.cover("the camera was unplugged")
    assert delivery.state("stopped", None, None)["mode"] == "reconnecting"


def test_stop_forgets_the_park(world: World) -> None:
    delivery, *_ = make(world)
    delivery.park()
    delivery.stop()
    assert not delivery.parked
