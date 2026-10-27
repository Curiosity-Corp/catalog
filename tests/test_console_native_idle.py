"""NativeVideo idles its engine, the placeholder and the Canon live view when no app reads StreamStack Camera (#128)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_console_delivery import FakeSlate
from test_console_native import CANON, CANON_SRC, FakeEngine, wait_for

from streamstack import camerause, config
from streamstack.console.arcblur import ArcBlur
from streamstack.console.native import NativeVideo
from streamstack.engine import idle
from streamstack.engine.segment import Arc


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeWatch:
    """The reader sensor, driven by the test: `apps` are the processes holding the node, `ok` the scan's health."""

    def __init__(self) -> None:
        self.apps: list[camerause.Consumer] = []
        self.ok = True
        self.is_fresh = True
        self.started = self.stopped = False

    @property
    def latest(self) -> idle.Readers:
        return idle.Readers(0.0, self.ok, tuple(self.apps))

    def fresh(self) -> bool:
        return self.ok and self.is_fresh

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def scan(self) -> idle.Readers:
        return self.latest


class Rig:
    def __init__(self, controller: NativeVideo, watch: FakeWatch, clock: Clock, rests: list[bool]) -> None:
        self.video, self.watch, self.clock, self.rests = controller, watch, clock, rests

    @property
    def engine(self) -> FakeEngine:
        return self.video._engine  # type: ignore[return-value]

    @property
    def slate(self) -> FakeSlate:
        return self.video.delivery.slate  # type: ignore[return-value]

    def tick(self, seconds: float = 0.0) -> None:
        self.clock.advance(seconds)
        self.video._idle_tick()

    def go_idle(self) -> None:
        self.tick(0)  # starts the quiet timer if something had held it off
        self.tick(11)
        assert self.video._idle.state == idle.IDLE


def build(tmp_path: Path, cfg_extra: dict | None = None):
    clock, watch, rests = Clock(), FakeWatch(), []
    made: list[FakeEngine] = []

    def factory(spec, look=None, **kwargs) -> FakeEngine:
        engine = FakeEngine(spec, look, **kwargs)
        made.append(engine)
        return engine

    cfg = {**config.DEFAULTS, **(cfg_extra or {})}
    controller = NativeVideo(
        cfg,
        arc=ArcBlur(cfg, probe=lambda wanted: Arc(False, "no openvino")),
        state_file=tmp_path / "video.json",
        discover=lambda c: [CANON],
        resolve=lambda camera, c: CANON_SRC,
        engine_factory=factory,
        backend=lambda node: ("vulkan", ""),
        watch_factory=lambda *a, **k: watch,
        clock=clock,
    )
    controller.delivery.slate = FakeSlate()  # type: ignore[assignment]
    controller.canon.rest = rests.append  # type: ignore[method-assign]
    controller.start()
    assert wait_for(lambda: made and made[0].running)
    return Rig(controller, watch, clock, rests), made


@pytest.fixture
def rig(tmp_path: Path):
    made_rig, _ = build(tmp_path)
    yield made_rig
    made_rig.video.stop()


def test_a_quiet_camera_idles_the_engine_parks_the_slate_and_rests_the_canon(rig: Rig) -> None:
    rig.tick(5)
    assert rig.engine.running and not rig.slate.shown  # not yet: the debounce is 10 s
    rig.tick(6)
    assert not rig.engine.running
    assert rig.slate.shown and rig.slate.shown[-1][0].caption == "Camera idle"
    assert rig.rests[-1] is True
    state = rig.video.state()
    assert state["engine"]["state"] == "idle"
    assert state["engine"]["idle"]["state"] == "idle"
    assert state["virtualcam"] is True  # the camera is still listed, so the switch still reads on
    assert state["engine"]["delivery"]["mode"] == "idle"


def test_a_reader_wakes_the_engine_and_the_canon_after_the_confirm_window(rig: Rig) -> None:
    rig.go_idle()
    rig.watch.apps = [camerause.Consumer(99, "msedge", "/dev/video43")]
    rig.tick(0.1)
    assert not rig.engine.running  # a probe has not stayed long enough
    rig.tick(0.3)
    assert rig.engine.running
    assert rig.rests[-1] is False
    snap = rig.video.state()["engine"]["idle"]
    assert snap["wakes"] == 1
    assert snap["readers"][0]["app"]


def test_a_probe_that_closes_the_device_does_not_wake_the_engine(rig: Rig) -> None:
    rig.go_idle()
    rig.watch.apps = [camerause.Consumer(99, "msedge", "/dev/video43")]
    rig.tick(0.1)
    rig.watch.apps = []
    rig.tick(0.1)
    rig.watch.apps = [camerause.Consumer(99, "msedge", "/dev/video43")]
    rig.tick(0.1)
    assert not rig.engine.running and rig.video._idle.wakes == 0


def test_a_reader_keeps_it_awake(rig: Rig) -> None:
    rig.watch.apps = [camerause.Consumer(99, "zoom", "/dev/video43")]
    rig.tick(300)
    assert rig.engine.running and rig.video._idle.state == idle.RUNNING


def test_a_sensor_that_cannot_tell_keeps_it_awake(rig: Rig) -> None:
    rig.watch.ok = False  # the /proc scan failed
    rig.tick(300)
    assert rig.engine.running
    rig.watch.ok, rig.watch.is_fresh = True, False  # an old answer is not proof either
    rig.tick(300)
    assert rig.engine.running


def test_a_preview_is_demand_and_wakes_an_idle_engine(rig: Rig) -> None:
    rig.go_idle()
    rig.video.touch("preview")
    rig.tick(0.0)
    assert rig.engine.running
    rig.tick(7)  # the preview stopped asking; it may rest again after the debounce
    rig.tick(11)
    assert not rig.engine.running


def test_a_running_stream_is_demand(rig: Rig) -> None:
    streaming = {"on": True}
    rig.video.add_demand("streaming", lambda: streaming["on"])
    rig.tick(300)
    assert rig.engine.running
    assert rig.video.state()["engine"]["idle"]["demand"] == ["streaming"]
    streaming["on"] = False
    rig.go_idle()
    streaming["on"] = True
    rig.tick(0.0)
    assert rig.engine.running


def test_a_broken_demand_probe_counts_as_demand(rig: Rig) -> None:
    def boom() -> bool:
        raise RuntimeError("stream service gone")

    rig.video.add_demand("streaming", boom)
    rig.tick(300)
    assert rig.engine.running


def test_keep_awake_prevents_idle_wakes_an_idle_engine_and_is_remembered(rig: Rig, tmp_path: Path) -> None:
    rig.go_idle()
    rig.video.act("video.keepalive", {"on": True})
    assert rig.engine.running
    rig.tick(300)
    assert rig.engine.running
    assert rig.video.state()["engine"]["idle"]["keep_alive"] is True
    assert json.loads((tmp_path / "video.json").read_text())["keep_alive"] is True
    rig.video.act("video.keepalive", {"on": False})
    rig.tick(11)
    assert not rig.engine.running


def test_keep_awake_survives_a_restart_of_the_console(tmp_path: Path) -> None:
    first, _ = build(tmp_path)
    first.video.act("video.keepalive", {"on": True})
    first.video.stop()
    second, _ = build(tmp_path)
    try:
        assert second.video.state()["engine"]["idle"]["keep_alive"] is True
    finally:
        second.video.stop()


def test_keepalive_needs_a_flag(rig: Rig) -> None:
    from streamstack.console.errors import ActionError

    with pytest.raises(ActionError):
        rig.video.act("video.keepalive", {})


def test_switching_the_program_camera_off_while_idle_clears_the_idle_state(rig: Rig) -> None:
    rig.go_idle()
    rig.video.act("video.virtualcam", {"on": False})
    assert rig.video._idle.state == idle.RUNNING
    assert rig.rests[-1] is False


def test_idle_off_never_idles(tmp_path: Path) -> None:
    rig, _ = build(tmp_path, {"STREAMSTACK_ENGINE_IDLE": "off"})
    try:
        rig.tick(500)
        assert rig.engine.running
        assert rig.video.state()["engine"]["idle"]["mode"] == "off"
    finally:
        rig.video.stop()


def test_camera_loss_off_refuses_to_idle_and_restarts_the_engine(tmp_path: Path) -> None:
    rig, _ = build(tmp_path, {"STREAMSTACK_CAMERA_LOSS": "off"})
    try:
        rig.tick(11)
        assert rig.engine.running  # restarted: nothing but the engine may write the node in this mode
        assert rig.video._idle.state == idle.RUNNING
        assert not rig.slate.shown
    finally:
        rig.video.stop()


def test_stopping_the_console_stops_the_watch_first(tmp_path: Path) -> None:
    rig, _ = build(tmp_path)
    engine = rig.engine
    rig.video.stop()
    assert rig.watch.stopped and not engine.running
    rig.tick(500)  # a late tick after stop does nothing
    assert not rig.slate.shown


def test_the_health_checks_carry_an_ok_idle_row(rig: Rig) -> None:
    rig.go_idle()
    rows = [c for c in rig.video.health_checks() if c["id"] == "video-engine"]
    assert rows and rows[0]["state"] == "ok" and "idle" in rows[0]["title"]
