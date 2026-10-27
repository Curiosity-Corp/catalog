"""Background blur in the Console: the Arc controller, its state block, NativeVideo's wiring and the demo rig."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from streamstack import config
from streamstack.console import arcblur
from streamstack.console.arcblur import ArcBlur, DemoArc
from streamstack.console.errors import ActionError
from streamstack.console.native import NativeVideo
from streamstack.engine import source
from streamstack.engine.blurfeed import BlurFeed
from streamstack.engine.graph import BlurGraph, Source, Spec
from streamstack.engine.segment import Arc, SegmentError

ARC = Arc(True, device="GPU.1", label="Intel Arc A380")
NO_OPENVINO = Arc(False, "the openvino package is not installed (pip install 'streamstack[blur]')")
CANON = source.Camera("Canon EOS 50D", "/dev/video42", "canon")
CANON_SRC = Source("Canon EOS 50D", "/dev/video42", "YUYV", 1280, 720, 20.0)


def wait_for(check, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.01)
    return False


class FakeSegmenter:
    label = "Intel Arc A380"

    def infer(self, rgb: bytes):
        raise AssertionError("no camera frames in these tests")

    def close(self) -> None:
        pass


def make(probe_result: Arc = ARC, cfg: dict | None = None, **kw) -> ArcBlur:
    arc = ArcBlur({**config.DEFAULTS, **(cfg or {})}, probe=lambda wanted: probe_result, **kw)
    arc.start()
    assert arc.wait_probed(2)
    return arc


# -- availability: every missing piece is a reason, never an exception ------------------------------


def test_without_openvino_blur_is_unavailable_with_the_reason() -> None:
    arc = make(NO_OPENVINO)
    block = arc.arc_block()
    assert block["state"] == "unavailable" and "streamstack[blur]" in block["reason"]
    assert arc.capable is False and arc.feed() is None
    state = arc.state()
    assert state["native"] is True and state["available"] is False and state["plugin"]["installed"] is False


def test_the_state_before_the_probe_finishes_is_checking() -> None:
    release = threading.Event()
    arc = ArcBlur(dict(config.DEFAULTS), probe=lambda wanted: release.wait(5) and ARC)
    arc.start()
    assert arc.arc_block()["state"] == "checking" and arc.state()["available"] is False
    assert arc.reason is None  # not a failure: the answer is still coming
    release.set()
    assert arc.wait_probed(2) and arc.arc_block()["state"] == "idle"


def test_the_config_kill_switch_removes_blur_without_touching_openvino() -> None:
    called: list[str] = []
    arc = ArcBlur({**config.DEFAULTS, "STREAMSTACK_ENGINE_BLUR": "off"}, probe=lambda w: called.append(w) or ARC)
    assert arc.wait_probed(1)
    assert called == [] and arc.arc_block()["state"] == "unavailable" and "STREAMSTACK_ENGINE_BLUR=off" in arc.reason


def test_the_device_setting_reaches_the_probe() -> None:
    seen: list[str] = []
    arc = ArcBlur({**config.DEFAULTS, "STREAMSTACK_ENGINE_BLUR_DEVICE": "GPU.0"}, probe=lambda w: seen.append(w) or ARC)
    assert arc.wait_probed(2) and seen == ["GPU.0"]


def test_a_probe_that_raises_anyway_is_contained() -> None:
    def broken(wanted: str) -> Arc:
        raise RuntimeError("segfault in the level zero loader")

    arc = ArcBlur(dict(config.DEFAULTS), probe=broken)
    assert arc.wait_probed(2)
    assert arc.arc_block()["state"] == "unavailable" and "level zero loader" in arc.arc_block()["reason"]


def test_a_capable_arc_is_idle_until_blur_is_switched_on() -> None:
    arc = make()
    block = arc.arc_block()
    assert block["state"] == "idle" and block["device"] == "Intel Arc A380" and block["fps"] is None
    state = arc.state()
    assert state["available"] is True and state["on"] is False and state["cost"]["state"] == "off"
    assert isinstance(arc.feed(), BlurFeed) and arc.feed() is arc.feed()  # one feed, kept for the engine's life


def test_a_missing_model_is_downloaded_on_first_use_and_the_state_says_so() -> None:
    gate, started = threading.Event(), threading.Event()

    def opener(arc: Arc) -> FakeSegmenter:
        started.set()
        gate.wait(5)
        return FakeSegmenter()

    arc = make(open_segmenter=opener, have_model=lambda: False)
    feed = arc.feed()
    assert feed is not None and arc.arc_block()["state"] == "idle"  # nothing is fetched until blur is on
    arc.configure(on=True)
    feed.set_enabled(True)
    assert started.wait(2)
    assert arc.arc_block()["state"] == "downloading"
    assert arc.state()["cost"]["state"] == "measuring"
    gate.set()
    assert wait_for(lambda: arc.arc_block()["state"] == "running")
    feed.stop()


def test_a_model_already_on_disk_is_loaded_not_downloaded() -> None:
    gate = threading.Event()

    def opener(arc: Arc) -> FakeSegmenter:
        gate.wait(5)
        return FakeSegmenter()

    arc = make(open_segmenter=opener, have_model=lambda: True)
    feed = arc.feed()
    feed.set_enabled(True)
    assert wait_for(lambda: arc.arc_block()["state"] == "loading")
    gate.set()
    feed.stop()


def test_a_failed_download_is_unavailable_with_its_reason_and_a_health_warning() -> None:
    def opener(arc: Arc) -> FakeSegmenter:
        raise SegmentError("could not download the PP-HumanSeg model: Temporary failure in name resolution")

    arc = make(open_segmenter=opener)
    arc.configure(on=True)
    arc.feed().set_enabled(True)
    assert wait_for(lambda: arc.arc_block()["state"] == "failed")
    assert "name resolution" in arc.arc_block()["reason"] and "name resolution" in arc.reason
    state = arc.state()
    assert state["available"] is False and state["on"] is False
    check = arc.health()[0]
    assert check["id"] == "video-blur" and check["state"] == "warn" and "name resolution" in check["detail"]


def test_health_is_quiet_when_blur_works_or_was_never_asked_for() -> None:
    assert make().health() == []
    assert make(NO_OPENVINO).health() == []  # a machine without the extra and without blur on is not a problem
    asked = make(NO_OPENVINO)
    asked.configure(on=True)
    assert asked.health()[0]["detail"].startswith("the openvino package")


# -- the state block --------------------------------------------------------------------------------


def running_block(**kw) -> dict:
    return {
        "state": "running",
        "reason": None,
        "device": "Intel Arc A380",
        "fps": 30.0,
        "infer_ms": 9.2,
        "infer_p50_ms": 8.3,
        "age_ms": 0.0,
        **kw,
    }


def test_the_native_block_keeps_the_shape_the_page_reads() -> None:
    state = arcblur.native_state(running_block(), {"on": True, "strength": 12, "rate": 1})
    base = arcblur.blurctl.empty_state()
    assert set(base) <= set(state)  # everything the earlier page reads is still there
    assert state["native"] is True and state["available"] is True and state["on"] is True
    assert state["strength"] == 12 and state["rate"] == 1 and state["limits"]["strength"] == [1, 20]
    assert state["arc"]["fps"] == 30.0 and state["plugin"]["device_labels"] == ["Intel Arc A380"]


def test_the_cost_pill_is_about_keeping_up_not_milliseconds_added() -> None:
    ok = arcblur.cost_for(running_block(), True, 1)
    assert ok["state"] == "ok" and "30 masks per second" in ok["detail"] and ok["limits"]["p95_ms"] == 33.3
    assert arcblur.cost_for(running_block(infer_ms=50.0), True, 1)["state"] == "over"
    assert arcblur.cost_for(running_block(infer_ms=50.0), True, 3)["state"] == "ok"  # 100 ms between masks
    assert arcblur.cost_for(running_block(state="stalled"), True, 1)["state"] == "over"
    assert arcblur.cost_for(running_block(infer_ms=None), True, 1)["state"] == "measuring"
    assert arcblur.cost_for(running_block(), False, 1)["state"] == "off"


def test_settings_are_restored_and_a_hand_edited_file_is_ignored() -> None:
    arc = make()
    arc.restore({"on": True, "strength": 14, "rate": 3})
    assert arc.settings() == {"on": True, "strength": 14, "rate": 3}
    arc.restore({"on": "yes", "strength": 99, "rate": "fast"})  # out of range or not numbers: kept as they were
    assert arc.settings() == {"on": True, "strength": 14, "rate": 3}
    arc.restore("nonsense")
    assert arc.settings()["strength"] == 14


# -- NativeVideo ------------------------------------------------------------------------------------


class FakeEngine:
    def __init__(self, spec: Spec, look: dict | None = None, **kwargs) -> None:
        self.spec, self.running = spec, False
        self.specs = [spec]
        self.feed: BlurFeed | None = None
        self.blur_calls: list[tuple] = []

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    def set_spec(self, spec: Spec) -> None:
        self.spec = spec
        self.specs.append(spec)

    def apply_look(self, look: dict) -> float:
        return 0.0

    def set_blackout(self, on: bool) -> None:
        pass

    def attach_blur(self, feed: BlurFeed) -> None:
        self.feed = feed

    def set_blur(self, on=None, strength=None, rate=None) -> None:
        self.blur_calls.append((on, strength, rate))
        if self.feed is not None and on is not None:
            self.feed.set_enabled(on)  # what the real engine does

    def state(self) -> dict:
        return {"state": "running" if self.running else "stopped", "error": None, "backend": "vulkan"}


def native(tmp_path: Path, arc: ArcBlur, saved: dict | None = None) -> tuple[NativeVideo, list[FakeEngine]]:
    made: list[FakeEngine] = []

    def factory(spec: Spec, look: dict | None = None, **kwargs) -> FakeEngine:
        made.append(FakeEngine(spec, look, **kwargs))
        return made[-1]

    path = tmp_path / "video.json"
    if saved is not None:
        path.write_text(json.dumps(saved))
    video = NativeVideo(
        dict(config.DEFAULTS),
        state_file=path,
        discover=lambda cfg: [CANON],
        resolve=lambda camera, cfg: CANON_SRC,
        engine_factory=factory,
        backend=lambda node: ("vulkan", ""),
        arc=arc,
    )
    video.start()
    assert wait_for(lambda: made and made[0].running)
    return video, made


@pytest.fixture
def capable(tmp_path: Path):
    video, made = native(tmp_path, make())
    yield video, made
    video.stop()


def test_a_capable_arc_gives_the_engine_a_blur_branch_and_its_feed(capable) -> None:
    video, made = capable
    assert isinstance(made[0].spec.blur, BlurGraph) and made[0].feed is video.arc.feed()
    assert made[0].blur_calls == [(False, 8, 2)]  # the saved settings, applied before the child starts


def test_unavailable_arc_leaves_the_engine_without_a_blur_branch(tmp_path: Path) -> None:
    video, made = native(tmp_path, make(NO_OPENVINO))
    try:
        assert made[0].spec.blur is None and made[0].feed is None
        state = video.state()["blur"]
        assert state["native"] is True and state["available"] is False and state["arc"]["state"] == "unavailable"
    finally:
        video.stop()


def test_the_blur_action_is_a_live_command_not_an_engine_restart(capable) -> None:
    video, made = capable
    specs = len(made[0].specs)
    assert video.act("video.blur", {"on": True}) == "Background blur on"
    assert video.act("video.blur", {"strength": 12}) == "Blur strength 12"
    assert video.act("video.blur", {"rate": 1}) == "Blur mask rate 1"
    assert made[0].blur_calls[1:] == [(True, None, None), (None, 12, None), (None, None, 1)]
    assert len(made[0].specs) == specs and len(made) == 1  # the same child, the same graph
    state = video.state()["blur"]
    assert state["on"] is True and state["strength"] == 12 and state["rate"] == 1


def test_blur_settings_survive_a_restart_of_the_console(tmp_path: Path) -> None:
    first, _ = native(tmp_path, make())
    first.act("video.blur", {"on": True, "strength": 15, "rate": 3})
    first.stop()
    saved = json.loads((tmp_path / "video.json").read_text())["blur"]
    assert saved == {"on": True, "strength": 15, "rate": 3}
    second, made = native(tmp_path, make())
    try:
        assert made[0].blur_calls == [(True, 15, 3)]  # on at startup: the engine begins with blur on
    finally:
        second.stop()


def test_blur_on_without_an_arc_is_a_409_naming_the_reason(tmp_path: Path) -> None:
    video, _ = native(tmp_path, make(NO_OPENVINO))
    try:
        with pytest.raises(ActionError) as caught:
            video.act("video.blur", {"on": True})
        assert caught.value.status == 409
        assert caught.value.message.startswith("Background blur is unavailable: the openvino package")
        assert video.act("video.blur", {"on": False}) == "Background blur off"  # switching off is always allowed
    finally:
        video.stop()


def test_a_malformed_blur_action_is_a_400(capable) -> None:
    video, _ = capable
    for params in ({}, {"on": "yes"}, {"strength": 0}, {"strength": 21}, {"rate": 99}):
        with pytest.raises(ActionError) as caught:
            video.act("video.blur", params)
        assert caught.value.status == 400


def test_the_source_parameter_is_accepted_for_the_pages_sake(capable) -> None:
    video, _ = capable
    assert video.act("video.blur", {"on": True, "source": "Canon EOS 50D"}) == "Background blur on"


def test_a_failed_blur_shows_in_the_health_checks(tmp_path: Path) -> None:
    def opener(arc: Arc) -> FakeSegmenter:
        raise SegmentError("OpenVINO could not load the model on Intel Arc A380: out of memory")

    video, _ = native(tmp_path, make(open_segmenter=opener))
    try:
        video.act("video.blur", {"on": True})
        assert wait_for(lambda: video.state()["blur"]["arc"]["state"] == "failed")
        checks = video.health_checks()
        assert [c["id"] for c in checks] == ["video-engine", "video-blur"]
        assert "out of memory" in checks[1]["detail"]
        assert video.state()["blur"]["on"] is False
    finally:
        video.stop()


def test_a_probe_that_finishes_late_still_gives_the_running_engine_its_blur(tmp_path: Path, monkeypatch) -> None:
    release = threading.Event()
    arc = ArcBlur(dict(config.DEFAULTS), probe=lambda wanted: release.wait(10) and ARC)
    monkeypatch.setattr(arcblur, "PROBE_WAIT", 0.05)
    video, made = native(tmp_path, arc)
    try:
        assert made[0].spec.blur is None  # the probe is held back: the engine started without the branch
        release.set()
        assert wait_for(lambda: made[0].spec.blur is not None)  # the monitor re-points the engine at the full graph
    finally:
        release.set()
        video.stop()


# -- the demo rig -----------------------------------------------------------------------------------


def shown(**kw) -> dict:
    return {**arcblur.blurctl.empty_state(), "on": True, "strength": 9, "rate": 2, **kw}


def test_demo_mode_off_leaves_the_obs_style_demo_alone() -> None:
    demo = DemoArc()
    base = shown()
    assert demo.overlay(base) is base and demo.active is False


@pytest.mark.parametrize("mode", ["running", "downloading", "unavailable", "failed", "stalled"])
def test_demo_modes_give_the_native_shape(mode: str) -> None:
    demo = DemoArc()
    demo.set_mode(mode)
    state = demo.overlay(shown())
    assert state["native"] is True and state["arc"]["state"] == mode
    assert state["available"] is (mode not in ("unavailable", "failed"))
    assert (state["arc"]["reason"] is not None) is (mode in ("unavailable", "failed"))
    assert state["strength"] == 9 and state["rate"] == 2


def test_demo_running_reports_measured_looking_numbers() -> None:
    demo = DemoArc()
    demo.set_mode("running")
    arc = demo.overlay(shown(rate=3))["arc"]
    assert arc["fps"] == 10.0 and arc["infer_ms"] == 2.4 and arc["device"] == "Intel Arc A380"


def test_demo_rejects_an_unknown_mode() -> None:
    with pytest.raises(ValueError):
        DemoArc().set_mode("sideways")
