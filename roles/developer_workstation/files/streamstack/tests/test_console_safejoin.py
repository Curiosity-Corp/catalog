"""Safe join: what happens when a call app takes the camera, and every way it must stay out of the person's way."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from streamstack.callstate import CallSnapshot, CallState
from streamstack.console.errors import ActionError
from streamstack.console.rig import Rig
from streamstack.console.safejoin import (
    SETTLE_S,
    DemoSafeJoin,
    SafeJoinController,
    empty_state,
    read_settings,
)


class FakeCall:
    """Stands in for rig.call: the test pushes snapshots through the listener, as the monitor thread does."""

    def __init__(self) -> None:
        self.listeners: list[Callable[[CallSnapshot, CallSnapshot], None]] = []
        self.current = CallSnapshot()

    def subscribe(self, listener: Callable[[CallSnapshot, CallSnapshot], None]) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def snapshot(self) -> CallSnapshot:
        return self.current

    def push(self, **fields: Any) -> None:
        before, self.current = self.current, CallSnapshot(**fields)
        for listener in list(self.listeners):
            listener(before, self.current)

    def join(self, camera: bool = True, service: str = "Teams") -> None:
        self.push(state=CallState.JOINING, service=service, camera=camera)

    def leave(self) -> None:
        self.push()


class FakeRig:
    """The slice of the rig safe join sees: privacy and mute, with actions that change them."""

    def __init__(self) -> None:
        self.privacy = False
        self.muted = False
        self.present = True
        self.actions: list[tuple[str, dict[str, Any]]] = []
        self.fail: dict[str, Exception] = {}

    def snapshot(self) -> dict[str, Any]:
        return {"video": {"privacy": self.privacy}, "audio": {"mic": {"muted": self.muted, "present": self.present}}}

    def run(self, action: str, params: dict[str, Any]) -> str | None:
        self.actions.append((action, params))
        if action in self.fail:
            raise self.fail[action]
        if action == "video.privacy":
            self.privacy = params["on"]
        elif action == "audio.mute":
            self.muted = params["on"]
        return None


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def setup() -> tuple[SafeJoinController, FakeCall, FakeRig, Clock, list[tuple[str, str]]]:
    clock, rig, call, toasts = Clock(), FakeRig(), FakeCall(), []
    ctl = SafeJoinController(lambda level, text: toasts.append((level, text)), clock=clock, wall=clock)
    ctl.bind(rig.snapshot, rig.run)
    ctl.attach(call)
    ctl._act_set({"mode": "safe"})
    return ctl, call, rig, clock, toasts


def tick(ctl: SafeJoinController, clock: Clock, seconds: float) -> None:
    clock.now += seconds
    ctl.on_meters({}, clock.now)


# -- the three modes -----------------------------------------------------------------------------------------------


def test_off_does_nothing(setup) -> None:
    ctl, call, rig, _, toasts = setup
    ctl._act_set({"mode": "off"})
    call.join()
    assert rig.actions == [] and toasts == [] and ctl.state()["events"] == []


def test_warn_is_the_default_and_only_tells_you() -> None:
    rig, call, toasts = FakeRig(), FakeCall(), []
    ctl = SafeJoinController(lambda level, text: toasts.append((level, text)))
    assert ctl.state()["mode"] == "warn"
    ctl.bind(rig.snapshot, rig.run)
    ctl.attach(call)
    call.join()
    assert rig.actions == [] and not ctl.state()["holding"]
    assert toasts == [("warn", "Teams has the camera. You are on air: check privacy and the microphone.")]
    assert ctl.state()["events"][0]["mode"] == "warn"


def test_safe_join_holds_privacy_and_mute_and_says_so(setup) -> None:
    ctl, call, rig, _, toasts = setup
    call.join()
    assert rig.privacy and rig.muted
    assert [a for a, _ in rig.actions] == ["video.privacy", "audio.mute"]
    state = ctl.state()
    assert state["holding"] and state["held"] == {"privacy": True, "mute": True} and state["service"] == "Teams"
    assert toasts == [("warn", "Teams has the camera. You are not on air: press Go live.")]
    event = state["events"][0]
    assert event["did"] == ["privacy on", "microphone muted"] and event["kept"] == [] and event["service"] == "Teams"


def test_a_call_that_has_no_camera_is_left_alone_until_it_does(setup) -> None:
    ctl, call, rig, _, _ = setup
    call.join(camera=False)
    assert rig.actions == [] and not ctl.state()["holding"]
    call.push(state=CallState.IN_CALL, service="Teams", camera=True)  # the camera comes on mid-call
    assert rig.privacy and rig.muted


def test_the_decision_is_made_once_per_call(setup) -> None:
    ctl, call, rig, _, _ = setup
    call.join()
    ctl._act_go_live()
    n = len(rig.actions)
    call.push(state=CallState.IN_CALL, service="Teams", camera=True)  # joining -> in call
    call.push(state=CallState.IN_CALL, service="Teams", camera=False)
    call.push(state=CallState.IN_CALL, service="Teams", camera=True)  # a camera flap
    assert len(rig.actions) == n and not rig.privacy and not rig.muted
    call.leave()
    call.join()  # a new call is a new decision
    assert rig.privacy and rig.muted and len(ctl.state()["events"]) == 2


# -- the person's own choices ----------------------------------------------------------------------------------------


def test_what_was_already_on_is_yours_and_go_live_leaves_it(setup) -> None:
    ctl, call, rig, _, _ = setup
    rig.muted = True
    call.join()
    assert [a for a, _ in rig.actions] == ["video.privacy"]
    assert ctl.state()["held"] == {"privacy": True, "mute": False}
    assert ctl.state()["events"][0]["kept"] == ["the microphone was already muted"]
    ctl._act_go_live()
    assert not rig.privacy and rig.muted  # still muted: that was your choice


def test_nothing_to_hold_says_so_and_holds_nothing(setup) -> None:
    ctl, call, rig, _, toasts = setup
    rig.muted = rig.privacy = True
    call.join()
    assert rig.actions == [] and not ctl.state()["holding"]
    assert toasts[0][0] == "info" and "already on" in toasts[0][1]


def test_no_microphone_is_not_muted(setup) -> None:
    ctl, call, rig, _, _ = setup
    rig.present = False
    call.join()
    assert [a for a, _ in rig.actions] == ["video.privacy"]
    assert "no microphone to mute" in ctl.state()["events"][0]["kept"]


def test_unmuting_yourself_ends_that_part_of_the_hold_and_it_is_never_taken_back(setup) -> None:
    ctl, call, rig, clock, toasts = setup
    call.join()
    tick(ctl, clock, SETTLE_S + 0.1)
    assert ctl.state()["holding"]
    rig.muted = False  # the person pressed M
    tick(ctl, clock, 0.5)
    assert ctl.state()["held"] == {"privacy": True, "mute": False}
    call.push(state=CallState.IN_CALL, service="Teams", camera=True)
    assert rig.muted is False  # never re-muted
    rig.privacy = False  # and privacy off by hand
    tick(ctl, clock, 0.5)
    assert not ctl.state()["holding"] and toasts[-1] == ("ok", "You are on air.")
    assert any(
        "released the microphone yourself" in n or "You released" in n for n in ctl.state()["events"][0]["notes"]
    )


def test_the_snapshot_lagging_right_after_the_action_is_not_a_manual_release(setup) -> None:
    ctl, call, rig, clock, _ = setup
    call.join()
    rig.muted = False  # the state has not caught up yet (inside the settle time)
    tick(ctl, clock, SETTLE_S - 1.0)
    assert ctl.state()["held"]["mute"] is True


def test_go_live_releases_both_together_and_is_idempotent(setup) -> None:
    ctl, call, rig, _, toasts = setup
    call.join()
    assert ctl._act_go_live() == "You are on air."
    assert not rig.privacy and not rig.muted and not ctl.state()["holding"]
    assert toasts[-1] == ("ok", "You are on air.")
    n = len(rig.actions)
    assert ctl._act_go_live() == "Nothing is held: you are on air."
    assert len(rig.actions) == n


def test_the_end_of_the_call_gives_everything_back(setup) -> None:
    ctl, call, rig, _, _ = setup
    call.join()
    call.leave()
    assert not rig.privacy and not rig.muted and not ctl.state()["holding"]
    assert any("the call ended" in n for n in ctl.state()["events"][0]["notes"])


def test_the_end_of_the_call_does_not_unmute_what_you_muted(setup) -> None:
    ctl, call, rig, _, _ = setup
    call.join()
    ctl._act_go_live()
    rig.muted = True  # you muted yourself after going live
    call.leave()
    assert rig.muted is True


# -- the timer -------------------------------------------------------------------------------------------------------


def test_the_optional_timer_goes_live_by_itself(setup) -> None:
    ctl, call, rig, clock, _ = setup
    ctl._act_set({"auto_release_s": 30})
    call.join()
    assert ctl.state()["release_in_s"] == 30.0
    tick(ctl, clock, 29)
    assert ctl.state()["holding"]
    tick(ctl, clock, 2)
    assert not ctl.state()["holding"] and not rig.privacy and not rig.muted
    assert any("the timer ran out" in n for n in ctl.state()["events"][0]["notes"])


def test_no_timer_means_it_waits_for_you(setup) -> None:
    ctl, call, _, clock, _ = setup
    call.join()
    tick(ctl, clock, 3600)
    assert ctl.state()["holding"] and ctl.state()["release_in_s"] is None


# -- failures --------------------------------------------------------------------------------------------------------


def test_one_actuator_failing_does_not_stop_the_other(setup) -> None:
    ctl, call, rig, _, _ = setup
    rig.fail["video.privacy"] = ActionError("OBS is not running", 409)
    call.join()
    assert rig.muted and not rig.privacy
    assert ctl.state()["held"] == {"privacy": False, "mute": True}
    assert "privacy on failed: OBS is not running" in ctl.state()["events"][0]["did"]


def test_an_unexpected_failure_is_recorded_not_raised(setup) -> None:
    ctl, call, rig, _, _ = setup
    rig.fail["audio.mute"] = RuntimeError("pactl died")
    call.join()
    assert rig.privacy and ctl.state()["held"] == {"privacy": True, "mute": False}
    assert any("pactl died" in d for d in ctl.state()["events"][0]["did"])


def test_a_rig_that_cannot_snapshot_does_not_break_the_call_callback(setup) -> None:
    ctl, call, rig, _, _ = setup

    def boom() -> dict[str, Any]:
        raise RuntimeError("snapshot failed")

    ctl.bind(boom, rig.run)
    call.join()  # no exception; with no state it takes both (the safe side)
    assert rig.privacy and rig.muted


def test_a_call_already_on_when_the_console_starts_counts() -> None:
    rig, call = FakeRig(), FakeCall()
    call.join()
    ctl = SafeJoinController()
    ctl._settings["mode"] = "safe"
    ctl.bind(rig.snapshot, rig.run)
    ctl.attach(call)
    assert rig.privacy and rig.muted


# -- settings --------------------------------------------------------------------------------------------------------


def test_settings_validate_and_persist(tmp_path: Path) -> None:
    path = tmp_path / "sj.json"
    ctl = SafeJoinController(settings_path=path)
    ctl.act("safejoin.set", {"mode": "safe", "auto_release_s": 45})
    again = SafeJoinController(settings_path=path)
    assert again.state()["mode"] == "safe" and again.state()["auto_release_s"] == 45.0
    for bad in (
        {"mode": "always"},
        {"auto_release_s": -1},
        {"auto_release_s": 9999},
        {"auto_release_s": "x"},
        {"x": 1},
        {},
    ):
        with pytest.raises(ActionError) as err:
            ctl.act("safejoin.set", bad)
        assert err.value.status == 400
    with pytest.raises(ActionError):
        ctl.act("safejoin.nope", {})


def test_a_damaged_settings_file_falls_back_to_the_defaults(tmp_path: Path) -> None:
    path = tmp_path / "sj.json"
    path.write_text('{"mode": "banana", "auto_release_s": true}')
    assert read_settings(path) == {"mode": "warn", "auto_release_s": 0.0}


def test_empty_and_live_state_have_the_same_keys() -> None:
    assert set(empty_state()) == set(SafeJoinController().state())


def test_the_event_list_keeps_the_newest_twenty(setup) -> None:
    ctl, call, _, _, _ = setup
    for _ in range(25):
        call.join()
        call.leave()
    events = ctl.state()["events"]
    assert len(events) == 20 and all(e["service"] == "Teams" for e in events)


# -- on the demo rig -------------------------------------------------------------------------------------------------


def wait(rig: Rig, probe: Callable[[dict[str, Any]], bool], timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = rig.snapshot()
        if probe(state):
            return state
        time.sleep(0.05)
    raise AssertionError("the demo rig never reached the state")


@pytest.fixture
def demo() -> Rig:
    rig = Rig.demo()
    rig.start()
    yield rig
    rig.stop()


def test_a_simulated_call_start_holds_privacy_and_mute_within_a_second(demo: Rig) -> None:
    demo.act("safejoin.set", {"mode": "safe"})
    started = time.monotonic()
    demo.act("call.demo", {"app": "Teams"})
    wait(demo, lambda s: s["safejoin"]["holding"], timeout=3.0)
    assert time.monotonic() - started < 1.0
    # The snapshot is composed controller by controller, so one taken just as the hold lands can show the video
    # section from before it and the safe-join section from after it. Read the effect from a fresh snapshot.
    state = wait(demo, lambda s: s["video"]["privacy"] and s["audio"]["mic"]["muted"], timeout=3.0)
    assert state["safejoin"]["holding"]
    demo.act("safejoin.go_live", {})
    state = demo.snapshot()
    assert not state["video"]["privacy"] and not state["audio"]["mic"]["muted"] and not state["safejoin"]["holding"]


def test_the_demo_rig_leaves_lights_and_the_director_alone(demo: Rig) -> None:
    before = demo.snapshot()
    demo.act("safejoin.set", {"mode": "safe"})
    demo.act("call.demo", {"app": "Teams"})
    after = wait(demo, lambda s: s["safejoin"]["holding"])
    assert after["lighting"]["master"] == before["lighting"]["master"]
    assert after["auto"]["running"] == before["auto"]["running"]


def test_a_manual_unmute_first_leaves_it_alone_on_the_demo_rig(demo: Rig) -> None:
    demo.act("safejoin.set", {"mode": "safe"})
    demo.act("audio.mute", {"on": True})
    demo.act("call.demo", {"app": "Teams"})
    wait(demo, lambda s: s["safejoin"]["holding"])
    assert demo.snapshot()["safejoin"]["held"] == {"privacy": True, "mute": False}


def test_demo_reset_forgets_everything(demo: Rig) -> None:
    demo.act("safejoin.set", {"mode": "safe"})
    demo.act("call.demo", {"app": "Teams"})
    wait(demo, lambda s: s["safejoin"]["holding"])
    demo.act("call.demo_reset", {})
    demo.act("safejoin.demo_reset", {})
    state = demo.snapshot()["safejoin"]
    assert state["mode"] == "warn" and state["events"] == [] and not state["holding"] and state["demo"]


def test_a_rig_without_it_says_so() -> None:
    rig = Rig.demo()
    rig.safejoin = None
    assert rig.snapshot()["safejoin"]["available"] is False
    with pytest.raises(ActionError) as err:
        rig.act("safejoin.go_live", {})
    assert err.value.status == 409


def test_the_demo_class_is_a_controller() -> None:
    assert isinstance(DemoSafeJoin(), SafeJoinController)
