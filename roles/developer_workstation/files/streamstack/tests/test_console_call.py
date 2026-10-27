"""The Console's call controller: the warning on a fake clock and fake evidence, settings, adapters, the demo twin."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from streamstack import callstate
from streamstack.callstate import AppEvidence, CallDetector, CallMonitor, CallState, CaptureStream, Gathered
from streamstack.console import call as callmod
from streamstack.console.call import CallController, DemoCall, empty_state, read_settings
from streamstack.console.errors import ActionError
from streamstack.console.rig import Rig


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Desk:
    """A call controller whose evidence, clock and mic are scripted."""

    def __init__(self, tmp_path: Path | None = None) -> None:
        self.clock = Clock()
        self.apps: list[AppEvidence] = []
        self.toasts: list[tuple[str, str]] = []
        self.own_mute: bool | None = False
        monitor = CallMonitor(
            {},
            source=lambda: Gathered(list(self.apps), True, True),
            detector=CallDetector(settle_s=1.0, leave_s=1.0, clock=self.clock),
            clock=self.clock,
        )
        self.call = CallController(
            {},
            lambda level, text: self.toasts.append((level, text)),
            monitor=monitor,
            own_muted=lambda: self.own_mute,
            clock=self.clock,
            settings_path=tmp_path / "console-call.json" if tmp_path else None,
        )

    def join(self, *, corked: bool = False, service: str = "Teams") -> None:
        stream = CaptureStream(service, service.lower(), 1, corked, False)
        self.apps = [AppEvidence(service, False, True, (stream,))]
        for _ in range(3):
            self.tick(0.5)

    def leave(self) -> None:
        self.apps = []
        for _ in range(4):
            self.hush(0.5)  # the meter loop keeps running while the call ends
            self.tick(0.0)
        self.hush(0.2)

    def tick(self, seconds: float) -> None:
        self.clock.now += seconds
        self.call.monitor.poll_once()

    def talk(self, seconds: float, level: float = -20.0) -> None:
        for _ in range(int(seconds * 50)):
            self.clock.now += 0.02
            self.call.on_meters({"mic": {"rms_db": level}})

    def hush(self, seconds: float) -> None:
        self.talk(seconds, level=-60.0)

    def warm_up(self) -> None:
        self.hush(2.0)  # a noise floor first, as in a real call


@pytest.fixture
def desk() -> Desk:
    return Desk()


def test_talking_into_a_muted_call_warns_once(desk: Desk) -> None:
    desk.join(corked=True)
    assert desk.call.snapshot().state is CallState.IN_CALL and desk.call.snapshot().muted
    desk.warm_up()
    desk.talk(6.0)
    assert desk.toasts == [("warn", "You are muted. Teams cannot hear you.")]
    state = desk.call.state()["muted_warning"]
    assert state["count"] == 1 and state["talking"] is True and state["last_at"]


def test_talking_into_an_open_call_never_warns(desk: Desk) -> None:
    desk.join()
    assert desk.call.snapshot().in_call and not desk.call.snapshot().muted
    desk.warm_up()
    desk.talk(10.0)
    assert desk.toasts == []


def test_room_noise_in_a_muted_call_never_warns(desk: Desk) -> None:
    desk.join(corked=True)
    desk.hush(30.0)
    assert desk.toasts == []


def test_no_warning_while_only_joining_or_idle(desk: Desk) -> None:
    desk.warm_up()
    desk.talk(5.0)  # idle
    desk.apps = [AppEvidence("Teams", False, True, ())]
    desk.tick(0.5)
    assert desk.call.snapshot().state is CallState.JOINING
    desk.talk(5.0)
    assert desk.toasts == []


def test_a_second_episode_after_the_cooldown_warns_again(desk: Desk) -> None:
    desk.join(corked=True)
    desk.warm_up()
    desk.talk(3.0)
    desk.hush(25.0)
    desk.talk(3.0)
    assert len(desk.toasts) == 2 and desk.call.state()["muted_warning"]["count"] == 2


def test_the_warning_can_be_switched_off(desk: Desk) -> None:
    desk.call.act("call.set", {"enabled": False})
    desk.join(corked=True)
    desk.warm_up()
    desk.talk(6.0)
    assert desk.toasts == []
    desk.call.act("call.set", {"enabled": True})
    desk.talk(6.0)
    assert len(desk.toasts) == 1


def test_leaving_the_call_resets_the_noise_floor_and_the_episode(desk: Desk) -> None:
    desk.join(corked=True)
    desk.warm_up()
    desk.talk(3.0)
    desk.leave()
    assert desk.call.snapshot().state is CallState.IDLE
    desk.join(corked=True)
    desk.warm_up()
    desk.talk(3.0)
    assert len(desk.toasts) == 2


def test_the_consoles_own_mute_is_silence_so_nothing_is_heard(desk: Desk) -> None:
    desk.own_mute = True
    desk.join()
    desk.warm_up()
    desk.hush(5.0)  # a muted source delivers silence
    assert desk.toasts == []


def test_frames_without_a_level_are_ignored(desk: Desk) -> None:
    desk.join(corked=True)
    for frame in ({}, {"mic": None}, {"mic": {"rms_db": None}}, {"mic": {"rms_db": "loud"}}):
        desk.call.on_meters(frame)
    assert desk.toasts == []


def test_state_shape_and_since(desk: Desk) -> None:
    idle = desk.call.state()
    assert idle["state"] == "idle" and idle["active"] is False and idle["since"] is None
    assert set(idle) == set(empty_state()) and idle["available"] is True
    desk.join()
    state = desk.call.state()
    assert state["state"] == "in_call" and state["service"] == "Teams" and state["camera"] is True
    assert state["mic"] == "open" and state["since"] and state["sources"] == {"audio": True, "video": True}
    json.dumps(state)  # the hub encodes it


def test_public_primitive_for_other_features(desk: Desk) -> None:
    seen: list[tuple[str, str]] = []
    desk.call.subscribe(lambda before, after: seen.append((before.state.value, after.state.value)))
    assert desk.call.active() is False
    desk.join()
    assert desk.call.active() is True and desk.call.snapshot().in_call
    desk.leave()
    assert seen == [("idle", "joining"), ("joining", "in_call"), ("in_call", "idle")]


# ---------------------------------------------------------------------------
# settings and adapters
# ---------------------------------------------------------------------------


def test_settings_validate_and_persist(tmp_path: Path) -> None:
    desk = Desk(tmp_path)
    desk.call.act("call.set", {"sensitivity": "high", "cooldown_s": 45, "sound": True})
    mw = desk.call.state()["muted_warning"]
    assert (mw["sensitivity"], mw["cooldown_s"], mw["sound"], mw["enabled"]) == ("high", 45.0, True, True)
    assert desk.call._watcher.cooldown_s == 45.0
    again = Desk(tmp_path)
    assert again.call.state()["muted_warning"]["sensitivity"] == "high"
    saved = json.loads((tmp_path / "console-call.json").read_text())
    assert saved["muted_warning"]["cooldown_s"] == 45.0


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"sensitivity": "extreme"},
        {"cooldown_s": 1},
        {"cooldown_s": 99999},
        {"cooldown_s": True},
        {"cooldown_s": "20"},
        {"enabled": "yes"},
        {"sound": 1},
        {"volume": 3},
    ],
)
def test_bad_settings_are_refused_and_change_nothing(desk: Desk, params: dict[str, Any]) -> None:
    before = desk.call.state()["muted_warning"]
    with pytest.raises(ActionError) as err:
        desk.call.act("call.set", params)
    assert err.value.status == 400
    assert desk.call.state()["muted_warning"] == before


def test_unknown_call_action_is_refused(desk: Desk) -> None:
    with pytest.raises(ActionError):
        desk.call.act("call.nope", {})


def test_a_damaged_or_odd_settings_file_falls_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text(
        json.dumps({"muted_warning": {"sensitivity": "x", "cooldown_s": 9999, "enabled": "no"}, "adapters": 3})
    )
    s = read_settings(path)
    assert s["muted_warning"]["sensitivity"] == "medium" and s["muted_warning"]["cooldown_s"] == 300.0
    assert s["muted_warning"]["enabled"] is True and s["adapters"] is False
    path.write_text("{not json")
    assert read_settings(path) == callmod.default_settings()


def test_adapter_mute_reports_are_refused_until_enabled(desk: Desk) -> None:
    desk.join()
    with pytest.raises(ActionError) as err:
        desk.call.act("call.report_mute", {"muted": True, "source": "extension"})
    assert err.value.status == 409


def test_an_adapter_that_says_muted_makes_a_live_stream_warn(desk: Desk) -> None:
    desk.call.act("call.set", {"adapters": True})
    desk.join()  # the stream is live, so inferred open
    desk.warm_up()
    desk.talk(4.0)
    assert desk.toasts == []
    desk.call.act("call.report_mute", {"muted": True, "source": "extension"})
    desk.tick(0.1)
    assert desk.call.state()["external"] == {"muted": True, "source": "extension"}
    desk.talk(4.0)
    assert len(desk.toasts) == 1
    desk.call.act("call.report_mute", {"muted": False, "source": "extension"})
    desk.tick(0.1)
    assert desk.call.snapshot().mic is callstate.MicState.OPEN
    desk.call.act("call.set", {"adapters": False})
    assert desk.call.state()["external"] is None


@pytest.mark.parametrize(
    "params",
    [
        {"muted": "yes", "source": "x"},
        {"muted": True},
        {"muted": True, "source": ""},
        {"muted": True, "source": "x" * 41},
    ],
)
def test_bad_adapter_reports_are_refused(desk: Desk, params: dict[str, Any]) -> None:
    desk.call.act("call.set", {"adapters": True})
    with pytest.raises(ActionError) as err:
        desk.call.act("call.report_mute", params)
    assert err.value.status == 400


# ---------------------------------------------------------------------------
# the demo twin and the rig
# ---------------------------------------------------------------------------


def test_demo_call_walks_idle_joining_in_call_idle() -> None:
    clock = Clock()
    demo = DemoCall(clock=clock)

    def step(seconds: float) -> str:
        clock.now += seconds
        return demo.monitor.poll_once().state.value

    assert step(0.1) == "idle"
    demo.act("call.demo", {"app": "Teams"})
    assert step(0.1) == "joining"
    assert step(callmod.DEMO_SETTLE_S + 0.1) == "in_call"
    demo.act("call.demo", {"app": None})
    assert step(0.1) == "in_call"
    assert step(callmod.DEMO_LEAVE_S + 0.1) == "idle"
    assert demo.state()["demo"] is True


def test_demo_call_validates_and_resets() -> None:
    demo = DemoCall()
    with pytest.raises(ActionError):
        demo.act("call.demo", {"app": 5})
    with pytest.raises(ActionError):
        demo.act("call.demo", {"app": "Teams", "corked": "no"})
    demo.act("call.demo", {"app": "Zoom", "capture": False})
    demo.act("call.set", {"enabled": False})
    demo.act("call.demo_reset", {})
    state = demo.state()
    assert (
        state["state"] == "idle" and state["muted_warning"]["enabled"] is True and state["muted_warning"]["count"] == 0
    )


def test_the_rig_routes_call_actions_and_carries_the_state() -> None:
    rig = Rig.demo()
    assert rig.snapshot()["call"]["demo"] is True
    rig.act("call.set", {"sensitivity": "low"})
    assert rig.snapshot()["call"]["muted_warning"]["sensitivity"] == "low"
    assert rig.call.own_muted == rig.audio.mic_muted
    plain = Rig(rig.audio, rig.video, rig.studio)
    assert plain.snapshot()["call"] == empty_state()
    with pytest.raises(ActionError) as err:
        plain.act("call.set", {"enabled": True})
    assert err.value.status == 409


def test_only_the_raw_plan_microphone_level_is_heard_never_the_voice_chain(desk: Desk) -> None:
    """`mic` is the meter of the Plan's raw source (upstream of the voice chain); `voice` is StreamStack Mic."""
    desk.join(corked=True)
    desk.warm_up()
    for _ in range(200):
        desk.clock.now += 0.02
        desk.call.on_meters({"mic": {"rms_db": -70.0}, "voice": {"rms_db": -15.0}})
    assert desk.toasts == []


def test_console_mute_delivers_digital_silence_so_it_is_the_one_case_that_cannot_be_heard(desk: Desk) -> None:
    """`audio.mute` is `pactl set-source-mute` on that same raw node, so every reader of it gets zeros."""
    desk.own_mute = True
    desk.join()
    desk.warm_up()
    for _ in range(200):
        desk.clock.now += 0.02
        desk.call.on_meters({"mic": {"rms_db": -90.0}})  # what a muted node delivers while the person talks
    assert desk.toasts == []
    assert desk.call.state()["muted_warning"]["count"] == 0
