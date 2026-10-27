"""The lighting automation engine: camera follow, the schedule, manual changes and who owns the light."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from streamstack import lightauto, lighting, lightplan
from streamstack.camerause import Consumer, Usage
from streamstack.console.errors import ActionError, Superseded
from streamstack.director import daylight

BERLIN = daylight.Location("Berlin", 52.52, 13.405, "settings")
BEAM = lighting.MODELS[0xC901]
TZ = timezone(timedelta(hours=2))
TEAMS = Usage((Consumer(4242, "ms-teams", "/dev/video43"),))
IDLE = Usage()


class FakeLights:
    """Litras as the engine sees them: the Console controller's `lights_view` and `write`, over plain state."""

    def __init__(self, *ids: str, on: bool = False, percent: float = 50, kelvin: int = 4000) -> None:
        self.state = {
            i: {"power": on, "lumen": lighting.percent_to_lumen(BEAM, percent), "kelvin": kelvin} for i in ids
        }
        self.names: dict[str, str] = {i: f"Light {n}" for n, i in enumerate(ids, 1)}
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self.fail: str | None = None
        self.gone: set[str] = set()
        self.after_view: Callable[[], None] | None = None  # runs once, right after the engine took its snapshot

    def lights_view(self) -> list[dict[str, Any]]:
        view = self._view()
        if self.after_view is not None:
            after, self.after_view = self.after_view, None
            after()  # a person acts between the engine's snapshot and its writes
        return view

    def _view(self) -> list[dict[str, Any]]:
        return [
            {
                "id": i,
                "name": self.names[i],
                "pid": "c901",
                "connected": i not in self.gone,
                "identifying": False,
                **s,
            }
            for i, s in self.state.items()
        ]

    def write(
        self,
        light_id: str,
        *,
        power: bool | None = None,
        percent: float | None = None,
        kelvin: float | None = None,
        expect: tuple[bool | None, int | None, int | None] | None = None,
    ) -> None:
        if self.fail:
            raise ActionError(self.fail, 409)
        s = self.state[light_id]
        if expect is not None:  # the controller's compare-and-set
            want_power, want_lumen, want_kelvin = expect
            if (
                (want_power is not None and s["power"] != want_power)
                or (want_lumen is not None and abs(s["lumen"] - want_lumen) >= lightauto.LUMEN_EPS)
                or (want_kelvin is not None and abs(s["kelvin"] - want_kelvin) >= lightauto.KELVIN_EPS)
            ):
                raise Superseded()
        self.writes.append((light_id, {"power": power, "percent": percent, "kelvin": kelvin}))
        if power is not None:
            s["power"] = power
        if percent is not None:
            s["lumen"] = lighting.percent_to_lumen(BEAM, percent)
        if kelvin is not None:
            s["kelvin"] = lighting.clamp_kelvin(kelvin)

    def percent(self, light_id: str) -> int:
        return lighting.lumen_to_percent(BEAM, self.state[light_id]["lumen"])

    def hand(self, light_id: str, **change: Any) -> None:
        """A person moves the light (its own buttons, the Console): the state changes without an engine write."""
        s = self.state[light_id]
        if "percent" in change:
            s["lumen"] = lighting.percent_to_lumen(BEAM, change.pop("percent"))
        s.update(change)


class Camera:
    def __init__(self) -> None:
        self.usage = IDLE
        self.devices: list[set[str]] = []

    def poll(self, follow: dict[str, Any], devices: set[str]) -> Usage:
        self.devices.append(devices)
        return self.usage


class Bench:
    """One engine on a manual clock: `step(seconds)` moves time and runs a tick per second."""

    def __init__(self, tmp_path: Path, lights: FakeLights, hour: int = 14, **kwargs: Any) -> None:
        self.lights, self.camera = lights, Camera()
        self.plan = lightplan.Store(tmp_path / "plan.json")
        self.director = False
        self.on_sleep: Callable[[float], None] = lambda seconds: None  # runs between the steps of a fade
        self.mono = 1000.0
        self.wall = datetime(2026, 6, 21, hour, 0, tzinfo=TZ)
        self.status_file = tmp_path / "status.json"
        self.engine = lightauto.Automation(
            lights,
            self.plan,
            self.camera,
            lambda: {"/dev/video43"},
            lambda: self.director,
            clock=lambda: self.mono,
            now=lambda: self.wall,
            location=lambda when: BERLIN,
            status_file=self.status_file,
            sleep=lambda seconds: self.on_sleep(seconds),
            **kwargs,
        )

    def step(self, seconds: float = 1) -> None:
        for _ in range(int(seconds)):
            self.mono += 1
            self.wall += timedelta(seconds=1)
            self.engine.tick()

    def follow(self, **patch: Any) -> None:
        self.plan.update("follow", patch)

    def schedule(self, **patch: Any) -> None:
        self.plan.update("schedule", patch)

    def events(self, kind: str) -> list[str]:
        return [e["text"] for e in self.engine.status()["events"] if e["kind"] == kind]


@pytest.fixture
def lights() -> FakeLights:
    return FakeLights("a", "b")


@pytest.fixture
def bench(tmp_path: Path, lights: FakeLights) -> Bench:
    return Bench(tmp_path, lights)


def target(bench: Bench, light: str = "a") -> tuple[float, int]:
    settings = lightplan.light_settings(bench.plan.load(), light)
    goal = lightplan.for_light(bench.engine.target(bench.plan.load(), bench.wall), settings, 60)
    assert goal is not None
    return goal


def settle(bench: Bench) -> None:
    bench.step(5)


# ---- camera follow -----------------------------------------------------------------------------------------


def test_the_lights_come_on_when_an_app_opens_the_camera(bench: Bench, lights: FakeLights) -> None:
    bench.schedule(fade_seconds=0)
    bench.step(2)
    assert not lights.state["a"]["power"]
    bench.camera.usage = TEAMS
    bench.step(5)
    assert lights.state["a"]["power"] and lights.state["b"]["power"]
    assert any("Teams opened the camera" in text for text in bench.events("on"))
    assert "Camera in use" in bench.engine.status()["summary"]


def test_the_first_level_is_the_schedules(bench: Bench, lights: FakeLights) -> None:
    bench.schedule(fade_seconds=0)
    bench.camera.usage = TEAMS
    bench.step(5)
    percent, kelvin = target(bench)
    assert lights.percent("a") == pytest.approx(percent, abs=1)
    assert lights.state["a"]["kelvin"] == kelvin


def test_a_short_start_delay_ignores_a_flicker(bench: Bench, lights: FakeLights) -> None:
    bench.follow(start_delay_seconds=5)
    bench.camera.usage = TEAMS
    bench.step(3)
    bench.camera.usage = IDLE
    bench.step(3)
    assert not lights.state["a"]["power"] and not bench.events("camera-on")


def test_the_lights_go_off_only_after_the_grace_period(bench: Bench, lights: FakeLights) -> None:
    bench.follow(grace_seconds=30)
    bench.schedule(fade_seconds=0)
    bench.camera.usage = TEAMS
    bench.step(5)
    bench.camera.usage = IDLE
    bench.step(20)
    assert lights.state["a"]["power"]
    assert "go off in" in bench.engine.status()["summary"]
    bench.step(15)
    assert not lights.state["a"]["power"] and not lights.state["b"]["power"]
    assert any("switched off" in t for t in bench.events("camera-off"))


def test_the_camera_coming_back_within_grace_keeps_the_session(bench: Bench, lights: FakeLights) -> None:
    bench.follow(grace_seconds=30)
    bench.camera.usage = TEAMS
    bench.step(5)
    bench.camera.usage = IDLE
    bench.step(20)
    bench.camera.usage = TEAMS
    bench.step(40)
    assert lights.state["a"]["power"]
    assert len(bench.events("camera-on")) == 1


def test_only_lights_streamstack_switched_on_are_switched_off(bench: Bench, lights: FakeLights) -> None:
    lights.hand("a", power=True)  # already on when the call starts
    bench.follow(grace_seconds=5)
    bench.camera.usage = TEAMS
    bench.step(5)
    assert lights.state["b"]["power"]
    bench.camera.usage = IDLE
    bench.step(10)
    assert lights.state["a"]["power"]
    assert not lights.state["b"]["power"]


def test_a_light_switched_off_by_hand_during_the_call_stays_off(bench: Bench, lights: FakeLights) -> None:
    bench.camera.usage = TEAMS
    bench.step(6)
    lights.hand("a", power=False)
    bench.step(10)
    assert not lights.state["a"]["power"]
    assert any("switched off by hand" in t for t in bench.events("manual"))
    bench.follow(grace_seconds=5)
    bench.camera.usage = IDLE
    bench.step(10)
    assert not lights.state["b"]["power"]


def test_a_light_changed_by_hand_during_the_call_stays_on_afterwards(bench: Bench, lights: FakeLights) -> None:
    bench.follow(grace_seconds=5)
    bench.camera.usage = TEAMS
    bench.step(6)
    lights.hand("a", percent=95)
    bench.step(4)
    bench.camera.usage = IDLE
    bench.step(10)
    assert lights.state["a"]["power"] and lights.percent("a") == 95
    assert not lights.state["b"]["power"]
    assert any("stays on" in t for t in bench.events("kept"))


def test_turn_off_can_be_declined(bench: Bench, lights: FakeLights) -> None:
    bench.follow(grace_seconds=5, turn_off=False)
    bench.camera.usage = TEAMS
    bench.step(5)
    bench.camera.usage = IDLE
    bench.step(12)
    assert lights.state["a"]["power"]
    assert any("stay as they are" in t for t in bench.events("camera-off"))


def test_camera_follow_off_leaves_everything_alone(bench: Bench, lights: FakeLights) -> None:
    bench.follow(enabled=False)
    bench.camera.usage = TEAMS
    bench.step(10)
    assert not lights.state["a"]["power"] and not lights.writes


def test_switching_camera_follow_off_mid_call_ends_the_session_without_switching_off(
    bench: Bench, lights: FakeLights
) -> None:
    bench.camera.usage = TEAMS
    bench.step(5)
    bench.follow(enabled=False)
    bench.step(3)
    assert lights.state["a"]["power"]
    assert bench.engine.status()["session"] is None


def test_the_off_light_keeps_its_level_for_next_time(bench: Bench, lights: FakeLights) -> None:
    bench.follow(grace_seconds=5)
    bench.schedule(fade_seconds=3, enabled=False)
    lights.hand("a", percent=77)
    lights.hand("a", power=False)
    bench.camera.usage = TEAMS
    bench.step(5)
    lights.hand("a", percent=77)
    bench.camera.usage = IDLE
    bench.step(12)
    assert not lights.state["a"]["power"]
    assert lights.percent("a") == 77  # faded down to switch off, then restored while off


def test_a_missing_light_or_a_failed_write_is_an_event_not_a_crash(bench: Bench, lights: FakeLights) -> None:
    lights.fail = "The light is not responding"
    bench.camera.usage = TEAMS
    bench.step(6)
    assert any("not responding" in t for t in bench.events("error"))
    lights.fail = None
    lights.gone.add("a")
    bench.step(3)
    assert "a" not in bench.engine.status()["lights"]


def test_no_lights_is_reported_and_harmless(tmp_path: Path) -> None:
    bench = Bench(tmp_path, FakeLights())
    bench.camera.usage = TEAMS
    bench.step(5)
    assert bench.engine.status()["summary"] == "No Litra light is connected."


def test_a_manual_role_is_never_switched_or_scheduled(bench: Bench, lights: FakeLights) -> None:
    bench.plan.set_light("b", {"role": "manual"})
    bench.camera.usage = TEAMS
    bench.step(6)
    assert lights.state["a"]["power"] and not lights.state["b"]["power"]
    assert "never touches" in bench.engine.status()["lights"]["b"]["state"]


def test_a_fill_light_follows_at_the_fill_level_and_clamps_apply(bench: Bench, lights: FakeLights) -> None:
    bench.schedule(fade_seconds=0, fill_percent=50)
    bench.plan.set_light("b", {"role": "fill", "max_brightness": 25})
    bench.camera.usage = TEAMS
    bench.step(6)
    key, _ = target(bench, "a")
    assert lights.percent("a") == pytest.approx(key, abs=1)
    assert lights.percent("b") <= 25


# ---- the schedule -------------------------------------------------------------------------------------------


def test_the_schedule_moves_lights_that_are_on(bench: Bench, lights: FakeLights) -> None:
    lights.hand("a", power=True)
    bench.schedule(fade_seconds=0)
    bench.step(5)
    percent, kelvin = target(bench)
    assert lights.percent("a") == pytest.approx(percent, abs=1)
    assert lights.state["a"]["kelvin"] == kelvin
    assert not lights.state["b"]["power"]  # off stays off: the schedule never switches power
    assert bench.engine.status()["owner"] == "schedule"


def test_the_schedule_follows_the_sun_through_the_evening(tmp_path: Path) -> None:
    lights = FakeLights("a", on=True, percent=50, kelvin=4800)
    bench = Bench(tmp_path, lights, hour=14)
    bench.schedule(fade_seconds=0)
    bench.step(5)
    afternoon = lights.percent("a")
    bench.wall = bench.wall.replace(hour=22, minute=30)
    bench.step(5)
    assert lights.percent("a") != afternoon


def test_a_big_jump_is_faded_in_steps(bench: Bench, lights: FakeLights) -> None:
    lights.hand("a", power=True, percent=5)
    bench.schedule(fade_seconds=4)
    bench.step(4)
    writes = [w for light, w in lights.writes if light == "a" and w["percent"] is not None]
    assert len(writes) > 3
    assert writes == sorted(writes, key=lambda w: w["percent"])  # monotonic ramp up


def test_a_hand_on_the_light_stops_a_fade_and_is_not_written_over(bench: Bench, lights: FakeLights) -> None:
    """A person's change that lands while a fade walks the light wins: the fade stops, the light is held."""
    lights.hand("a", power=True, percent=5)
    bench.schedule(fade_seconds=4, hold_minutes=30)
    steps = 0

    def nudge(_seconds: float) -> None:
        nonlocal steps
        steps += 1
        if steps == 3:
            lights.hand("a", percent=90)

    bench.on_sleep = nudge
    bench.step(1)
    assert lights.percent("a") == 90  # nothing of the fade landed after the hand
    bench.step(10)
    assert lights.percent("a") == 90
    assert bench.engine.status()["lights"]["a"]["held"]
    assert any("Light 1 was changed by hand" in text for text in bench.events("manual"))


def test_a_hand_between_the_snapshot_and_the_schedule_write_is_not_written_over(
    bench: Bench, lights: FakeLights
) -> None:
    """The engine decides from a snapshot taken at the start of the tick; a hand that lands after it must win."""
    lights.hand("a", power=True, percent=5)
    bench.schedule(fade_seconds=0, hold_minutes=30, enabled=False)
    bench.step(3)
    bench.schedule(enabled=True)
    lights.after_view = lambda: lights.hand("a", percent=60)
    bench.step(1)
    assert lights.percent("a") == 60
    bench.step(10)
    assert lights.percent("a") == 60
    assert bench.engine.status()["lights"]["a"]["held"]


def test_resuming_adopts_a_hand_change_that_was_still_inside_the_settle_window(
    bench: Bench, lights: FakeLights
) -> None:
    """A hand within two seconds of the engine's own write is noticed late; Resume must not be undone by it."""
    lights.hand("a", power=True, percent=5)
    bench.schedule(fade_seconds=0, hold_minutes=30)
    bench.step(3)
    bench.schedule(enabled=True)
    bench.step(1)  # the engine writes the schedule level
    lights.hand("a", percent=77)  # a person, inside the engine's settle window
    bench.plan.resume()
    bench.step(10)
    assert not bench.engine.status()["lights"]["a"]["held"]
    assert not bench.events("manual") or all("changed by hand" not in t for t in bench.events("manual"))


def test_a_manual_change_holds_the_schedule_off(bench: Bench, lights: FakeLights) -> None:
    lights.hand("a", power=True)
    bench.schedule(fade_seconds=0, hold_minutes=30)
    bench.step(5)
    lights.hand("a", percent=12)
    bench.step(5)
    assert lights.percent("a") == 12
    assert "Yours until" in bench.engine.status()["lights"]["a"]["state"]
    assert bench.engine.status()["lights"]["a"]["held"]
    assert any("changed by hand" in t for t in bench.events("manual"))
    bench.step(10 * 60)
    assert lights.percent("a") == 12


def test_the_hold_runs_out(bench: Bench, lights: FakeLights) -> None:
    lights.hand("a", power=True)
    bench.schedule(fade_seconds=0, hold_minutes=1)
    bench.step(5)
    lights.hand("a", percent=12)
    bench.step(5)
    bench.step(70)
    assert lights.percent("a") != 12
    assert any("has Light 1 again" in t for t in bench.events("resume"))


def test_a_hold_of_zero_lasts_until_the_next_camera_session(bench: Bench, lights: FakeLights) -> None:
    lights.hand("a", power=True)
    bench.schedule(fade_seconds=0, hold_minutes=0)
    bench.follow(grace_seconds=5)
    bench.step(5)
    lights.hand("a", percent=12)
    bench.step(3600 // 4)
    assert lights.percent("a") == 12
    bench.camera.usage = TEAMS
    bench.step(6)
    assert lights.percent("a") != 12  # a new camera session hands the light back to the schedule
    assert not bench.engine.status()["lights"]["a"]["held"]


def test_resume_hands_the_lights_back_now(bench: Bench, lights: FakeLights) -> None:
    lights.hand("a", power=True)
    bench.schedule(fade_seconds=0)
    bench.step(5)
    lights.hand("a", percent=12)
    bench.step(5)
    bench.plan.resume()
    bench.step(5)
    assert lights.percent("a") != 12
    assert any("asked the schedule to resume" in t for t in bench.events("resume"))


def test_switching_a_light_off_and_on_again_by_hand_clears_the_hold(bench: Bench, lights: FakeLights) -> None:
    lights.hand("a", power=True)
    bench.schedule(fade_seconds=0)
    bench.step(5)
    lights.hand("a", percent=12)
    bench.step(5)
    lights.hand("a", power=False)
    bench.step(4)
    lights.hand("a", power=True)
    bench.step(5)
    assert lights.percent("a") != 12


def test_the_schedule_off_means_nobody_owns_the_level(bench: Bench, lights: FakeLights) -> None:
    lights.hand("a", power=True, percent=33)
    bench.schedule(enabled=False)
    bench.step(10)
    assert lights.percent("a") == 33 and not lights.writes
    assert bench.engine.status()["owner"] == "none"


# ---- the AI Director -----------------------------------------------------------------------------------------


def test_the_director_owns_brightness_and_colour_but_not_power(bench: Bench, lights: FakeLights) -> None:
    bench.director = True
    lights.hand("a", power=True, percent=33)
    bench.schedule(fade_seconds=0)
    bench.step(15)
    assert lights.percent("a") == 33  # the schedule yielded
    assert bench.engine.status()["owner"] == "director"
    assert "AI Director" in bench.engine.status()["summary"]
    bench.camera.usage = TEAMS
    bench.step(6)
    assert lights.state["b"]["power"]  # camera follow still switches power
    assert lights.percent("b") == 50  # and sets no schedule level while the Director owns it


def test_the_schedule_takes_the_lights_back_when_the_director_stops(bench: Bench, lights: FakeLights) -> None:
    bench.director = True
    lights.hand("a", power=True, percent=33)
    bench.schedule(fade_seconds=0)
    bench.step(15)
    bench.director = False
    bench.step(lightauto.DIRECTOR_CHECK_SECONDS + 5)
    assert lights.percent("a") != 33
    assert bench.engine.status()["owner"] == "schedule"


# ---- status and restarts -------------------------------------------------------------------------------------


def test_the_status_says_why_the_lights_are_as_they_are(bench: Bench, lights: FakeLights) -> None:
    bench.step(2)
    assert "daylight schedule" in bench.engine.status()["summary"]
    bench.camera.usage = TEAMS
    bench.step(6)
    status = bench.engine.status()
    assert status["camera"]["in_use"] and status["camera"]["apps"][0]["app"] == "Teams"
    assert status["session"]["reason"] == "Teams opened the camera"
    assert status["lights"]["a"]["ours"]
    assert status["lights"]["a"]["state"].startswith("On for the camera session")
    assert status["events"][0]["time"] and status["where"] == "service"


def test_the_status_file_is_written_and_read_back(bench: Bench) -> None:
    bench.camera.usage = TEAMS
    bench.step(6)
    status = lightauto.read_status(bench.status_file)
    assert status is not None and status["running"] and status["fresh"]
    assert status["summary"].startswith("Camera in use")
    assert oct(bench.status_file.stat().st_mode & 0o777) == "0o600"


def test_a_stale_status_is_not_running(bench: Bench) -> None:
    bench.step(2)
    stale = lightauto.read_status(bench.status_file)
    assert stale is not None
    bench.status_file.write_text(
        bench.status_file.read_text().replace(f'"heartbeat": {stale["heartbeat"]}', '"heartbeat": 1.0')
    )
    old = lightauto.read_status(bench.status_file)
    assert old is not None and not old["running"] and not old["fresh"]


def test_no_status_file_means_none(tmp_path: Path) -> None:
    assert lightauto.read_status(tmp_path / "missing.json") is None


def test_a_restarted_service_picks_the_session_back_up(tmp_path: Path) -> None:
    lights = FakeLights("a", "b")
    first = Bench(tmp_path, lights)
    first.follow(grace_seconds=5)
    first.camera.usage = TEAMS
    first.step(6)
    assert lights.state["a"]["power"]
    second = Bench(tmp_path, lights)  # same status file, same lights: a restart in the middle of a call
    second.camera.usage = TEAMS
    second.step(3)
    assert len(second.engine.status()["events"]) >= 2  # the log survived
    second.camera.usage = IDLE
    second.step(10)
    assert not lights.state["a"]["power"]  # it still knew these lights were its own to switch off


def test_the_events_are_capped(bench: Bench, lights: FakeLights) -> None:
    bench.follow(grace_seconds=0, start_delay_seconds=0)
    for _ in range(lightauto.EVENTS_KEPT):
        bench.camera.usage = TEAMS
        bench.step(2)
        bench.camera.usage = IDLE
        bench.step(2)
    assert len(bench.engine.status()["events"]) == lightauto.EVENTS_KEPT


def test_the_engine_survives_a_failing_tick(
    bench: Bench, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = []

    def boom(follow: dict, devices: set) -> Usage:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("scan blew up")
        return IDLE

    monkeypatch.setattr(bench.camera, "poll", boom)
    bench.engine.tick_seconds = 0.01
    bench.engine.start()
    deadline = datetime.now() + timedelta(seconds=5)
    while len(calls) < 3 and datetime.now() < deadline:
        pass
    bench.engine.stop()
    assert len(calls) >= 3
    assert "lighting automation tick failed" in caplog.text


def test_the_director_check_is_the_users_setting_and_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lightauto.system, "user_unit_active", lambda unit: unit == lightauto.DIRECTOR_UNIT)
    cfg = {"STREAMSTACK_AUTO_LIGHTS": "1"}
    assert lightauto.director_drives_lights(cfg)
    assert not lightauto.director_drives_lights({"STREAMSTACK_AUTO_LIGHTS": "0"})
    monkeypatch.setattr(lightauto.system, "user_unit_active", lambda unit: False)
    assert not lightauto.director_drives_lights(cfg)


def test_an_outside_power_toggle_counts_as_a_manual_change(bench: Bench, lights: FakeLights) -> None:
    """`litra toggle` from the G604 thumb button (or the light's own button) is a person, not the engine."""
    bench.follow(grace_seconds=5, start_delay_seconds=0)
    bench.camera.usage = TEAMS
    bench.step(6)
    assert lights.state["a"]["power"] is True  # camera follow switched it on
    lights.hand("a", power=False)  # the mouse button toggles it off during the call
    bench.step(10)
    assert lights.state["a"]["power"] is False  # follow does not fight a person
    assert any("by hand" in t for t in bench.events("manual"))
    bench.step(10 * 60)
    assert lights.state["a"]["power"] is False
    lights.hand("a", power=True)  # toggled back on by the same button
    bench.step(10)
    bench.camera.usage = IDLE
    bench.step(30)
    assert lights.state["a"]["power"] is True  # a light a person switched on is not switched off by the grace period


def test_an_outside_toggle_holds_the_schedule_off(bench: Bench, lights: FakeLights) -> None:
    lights.hand("a", power=True)
    bench.schedule(fade_seconds=0, hold_minutes=30)
    bench.step(5)
    lights.hand("a", power=False)
    bench.step(5)
    bench.step(10)
    assert lights.state["a"]["power"] is False  # the schedule does not switch a light a person turned off back on
    lights.hand("a", power=True)  # a power cycle hands the light back to the schedule...
    bench.step(10)
    lights.hand("a", percent=30)  # ...and the next hand adjustment holds it again
    bench.step(10)
    assert lights.percent("a") == 30
    assert bench.engine.status()["lights"]["a"]["held"]
