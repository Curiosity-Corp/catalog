"""#182: a Litra Beam stayed on overnight on dm313red.

The journal of 2026-11-03 (local time): Chrome opened the camera at 17:03:06 and both Beams came on at 64 %. At
17:03:11 "Litra Beam 1 was changed by hand", although nobody touched it; at 17:07 the camera was released and Beam 2
went off while Beam 1 stayed on ("you changed it during the session"); at 17:33:11 the hold ran out and at 17:33:15 it
was "changed by hand" again. That Beam keeps its brightness at 204 lm whatever it is told (265 lm was asked for), so
what the engine read back never matched what it had written. These tests reproduce that sequence, and cover the rest
of the fix: the write journal and the hard rule (no camera user: lights off, at night too).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from test_lightauto import BEAM, IDLE, TEAMS, TZ, Bench, FakeLights

from streamstack import lightauto, lighting, lightjournal
from streamstack.console.lighting import LightingController
from streamstack.lighting import Found, Litra, SimulatedTransport

CEILING = 204  # the Beam on dm313red: what it reads back whatever it is asked for (measured 2026-11-03)


def bright(bench: Bench) -> None:
    """Every part of the day asks for 64 % (265 lm), 4400 K: what the lights were switched on at on 2026-11-03."""
    for slot in ("night", "dawn", "morning", "afternoon", "evening"):
        bench.plan.set_slot(slot, {"brightness": 64, "kelvin": 4400})


def sequence_bench(tmp_path: Path) -> tuple[Bench, FakeLights]:
    lights = FakeLights("a", "b")
    lights.ceilings["a"] = CEILING
    bench = Bench(tmp_path, lights)
    bench.schedule(fade_seconds=0, hold_minutes=30)
    bright(bench)
    bench.follow(start_delay_seconds=3, grace_seconds=120)
    return bench, lights


def manual(bench: Bench) -> list[str]:
    return bench.events("manual")


# ---- 1. the false "changed by hand" -----------------------------------------------------------------------------


def test_a_light_that_keeps_its_own_limit_is_not_changed_by_hand(tmp_path: Path) -> None:
    """17:03:06 on at 64 %, 17:03:11 "changed by hand": the read-back of the engine's own write."""
    bench, lights = sequence_bench(tmp_path)
    bench.camera.usage = TEAMS
    bench.step(4)
    assert lights.state["a"]["power"] and lights.state["b"]["power"]
    asked = lighting.percent_to_lumen(BEAM, 64)
    assert asked > CEILING  # the ask is above what the light takes: the read-back differs by more than the tolerance
    assert lights.state["a"]["lumen"] == CEILING
    bench.step(30)
    assert manual(bench) == []
    assert not bench.engine.status()["lights"]["a"]["held"]
    assert bench.engine.status()["lights"]["a"]["state"] != "Yours until"


def test_the_beam_that_stayed_on_goes_off_with_the_camera(tmp_path: Path) -> None:
    """17:07: Beam 2 went off, Beam 1 stayed on because it looked changed by hand."""
    bench, lights = sequence_bench(tmp_path)
    bench.camera.usage = TEAMS
    bench.step(30)
    bench.camera.usage = IDLE
    bench.step(130)
    assert not lights.state["a"]["power"] and not lights.state["b"]["power"]
    assert bench.events("kept") == []


def test_the_limit_is_learned_so_the_schedule_does_not_ask_again(tmp_path: Path) -> None:
    bench, lights = sequence_bench(tmp_path)
    bench.camera.usage = TEAMS
    bench.step(10)
    writes = len(lights.writes)
    bench.step(10 * 60)
    assert len(lights.writes) == writes  # it aims inside the limit, found it already there, and stays quiet
    assert manual(bench) == []


def test_a_real_hand_still_holds_and_the_hold_runs_out_without_latching_again(tmp_path: Path) -> None:
    """17:33:11 the hold ran out, 17:33:15 "changed by hand" again, every 30 minutes."""
    bench, lights = sequence_bench(tmp_path)
    bench.camera.usage = TEAMS
    bench.step(10)
    lights.hand("a", percent=20)  # a person, really
    bench.step(10)
    assert len(manual(bench)) == 1 and bench.engine.status()["lights"]["a"]["held"]
    bench.step(31 * 60)  # the hold (30 min) runs out and the schedule has the light again
    assert any("has Light 1 again" in t for t in bench.events("resume"))
    assert lights.state["a"]["lumen"] == CEILING  # the schedule wrote its level; the light kept its own limit
    bench.step(10 * 60)
    assert len(manual(bench)) == 1  # no second "by hand", then or ever
    assert not bench.engine.status()["lights"]["a"]["held"]


def test_a_limit_the_light_no_longer_keeps_is_forgotten(tmp_path: Path) -> None:
    bench, lights = sequence_bench(tmp_path)
    bench.camera.usage = TEAMS
    bench.step(10)
    del lights.ceilings["a"]  # a firmware or power change: the light takes its full range again
    lights.hand("a", power=False)
    bench.step(5)
    lights.hand("a", power=True)
    bench.step(5)
    run = bench.engine._runs["a"]
    assert run.lumen_ceiling == CEILING  # still believed until a write is read back
    bench.engine._write(lights.lights_view()[0], run, bench.mono, percent=100)
    assert run.lumen_ceiling is None and run.lumen == lighting.percent_to_lumen(BEAM, 100)


def test_the_real_controller_and_a_capped_simulated_beam_never_latch(tmp_path: Path) -> None:
    """The same sequence through the Console's controller and the wire format, not a fake."""
    now = [1000.0]
    transports: dict[str, SimulatedTransport] = {}
    found = []
    for phys in ("usb-1", "usb-2"):
        light = Found(id=f"c901-{phys}", node=Path("/dev/null"), model=BEAM, phys=phys, name="Litra Beam")
        found.append(light)
        transports[light.id] = SimulatedTransport(BEAM, kelvin=4400, lumen_ceiling=CEILING if phys == "usb-1" else None)
    controller = LightingController(
        scan=lambda: found,
        opener=lambda light: transports[light.id],
        clock=lambda: now[0],
        verify_seconds=0,
        sleep=lambda seconds: None,
    )
    bench = Bench(tmp_path, FakeLights("a"))  # only for its plan, clocks and camera
    engine = lightauto.Automation(
        controller,
        bench.plan,
        bench.camera,
        lambda: {"/dev/video43"},
        clock=lambda: now[0],
        now=lambda: bench.wall,
        epoch=lambda: bench.wall.timestamp(),
        location=lambda when: bench.engine.target.__self__._location_of(when),
        sleep=lambda seconds: None,
    )
    bench.schedule(fade_seconds=0)
    bright(bench)
    bench.follow(start_delay_seconds=0, grace_seconds=120)
    controller.poll_once()
    bench.camera.usage = TEAMS
    for _ in range(40):
        now[0] += 1
        bench.wall += timedelta(seconds=1)
        controller.poll_once()  # the controller's own read-back, as the service does every 2 s
        engine.tick()
    assert [e["kind"] for e in engine.status()["events"] if e["kind"] == "manual"] == []
    assert transports["c901-usb-1"].on and transports["c901-usb-1"].lumen == CEILING
    bench.camera.usage = IDLE
    for _ in range(130):
        now[0] += 1
        bench.wall += timedelta(seconds=1)
        controller.poll_once()
        engine.tick()
    assert not transports["c901-usb-1"].on and not transports["c901-usb-2"].on


# ---- 1b. who else writes: the journal ---------------------------------------------------------------------------


def test_a_write_by_an_agent_is_not_a_change_by_hand(tmp_path: Path) -> None:
    lights = FakeLights("a", "b", on=True)
    bench = Bench(tmp_path, lights)
    bench.camera.usage = TEAMS
    bench.schedule(fade_seconds=0, hold_minutes=30)
    bench.step(10)
    lights.hand("a", percent=90)
    lightjournal.record("a", writer="agent", lumen=lights.state["a"]["lumen"], wall=bench.wall.timestamp())
    bench.step(10)
    assert manual(bench) == []
    assert not bench.engine.status()["lights"]["a"]["held"]
    assert any("by agent" in t for t in bench.events("software"))


def test_a_power_toggle_by_a_script_is_not_a_change_by_hand(tmp_path: Path) -> None:
    lights = FakeLights("a", "b", on=True)
    bench = Bench(tmp_path, lights)
    bench.camera.usage = TEAMS
    bench.step(10)
    lights.hand("a", power=False)
    lightjournal.record("a", writer="test", power=False, wall=bench.wall.timestamp())
    bench.step(10)
    assert manual(bench) == []
    assert bench.events("software") == ["Light 1 was switched off by test"]


def test_the_same_change_without_a_journal_line_or_by_a_person_is_a_hand(tmp_path: Path) -> None:
    lights = FakeLights("a", "b", on=True)
    bench = Bench(tmp_path, lights)
    bench.camera.usage = TEAMS
    bench.schedule(fade_seconds=0, hold_minutes=30)
    bench.step(10)
    lights.hand("a", percent=90)
    lightjournal.record("a", writer="console", lumen=lights.state["a"]["lumen"], wall=bench.wall.timestamp())
    lightjournal.record("a", writer="", lumen=lights.state["a"]["lumen"], wall=bench.wall.timestamp())
    bench.step(10)
    assert len(manual(bench)) == 1 and bench.engine.status()["lights"]["a"]["held"]


def test_a_journal_line_that_does_not_explain_the_change_does_not_excuse_it(tmp_path: Path) -> None:
    lights = FakeLights("a", "b", on=True)
    bench = Bench(tmp_path, lights)
    bench.camera.usage = TEAMS
    bench.schedule(fade_seconds=0, hold_minutes=30)
    bench.step(10)
    lightjournal.record("a", writer="agent", lumen=300, wall=bench.wall.timestamp())
    lights.hand("a", percent=10)  # a person moved it somewhere else
    bench.step(10)
    assert len(manual(bench)) == 1


def test_an_old_journal_line_does_not_explain_a_new_change(tmp_path: Path) -> None:
    lights = FakeLights("a", "b", on=True)
    bench = Bench(tmp_path, lights)
    bench.camera.usage = TEAMS
    bench.schedule(fade_seconds=0, hold_minutes=30)
    bench.step(10)
    lightjournal.record("a", writer="agent", lumen=lights.state["a"]["lumen"], wall=bench.wall.timestamp() - 600)
    lights.hand("a", percent=90)
    bench.step(10)
    assert len(manual(bench)) == 1


def test_litra_writes_are_journalled_with_their_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    found = Found(id="c901-x", node=Path("/dev/null"), model=BEAM, phys="usb", name="Litra Beam")
    light = Litra(found, SimulatedTransport(BEAM))
    monkeypatch.delenv(lightjournal.ENV, raising=False)
    light.set_power(True)  # untagged: counts as a person's
    light.set_lumen(150, writer="director")
    monkeypatch.setenv(lightjournal.ENV, "Agent")
    light.set_kelvin(5000)  # this process says who it is
    entries = lightjournal.recent("c901-x", 0)
    assert [(e["writer"], e.get("power"), e.get("lumen"), e.get("kelvin")) for e in entries] == [
        ("", True, None, None),
        ("director", None, 150, None),
        ("agent", None, None, 5000),
    ]
    assert [lightjournal.is_person(e["writer"]) for e in entries] == [True, False, False]
    light.transport.write(lighting.report_power(BEAM, False))


def test_the_journal_survives_damage_and_stays_small() -> None:
    target = lightjournal.path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('not json\n{"light":"x","ts":"soon"}\n{"light":"x","ts":5,"writer":"test","power":true}\n')
    assert [e["writer"] for e in lightjournal.recent("x", 0)] == ["test"]
    assert lightjournal.recent("y", 0) == []
    for _ in range(lightjournal.TRIM_BYTES // 40):
        lightjournal.record("x", writer="test", lumen=100, wall=10)
    assert target.stat().st_size <= lightjournal.TRIM_BYTES
    assert len(lightjournal.recent("x", 0)) <= lightjournal.KEEP_LINES


def test_an_unwritable_journal_never_blocks_a_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("x")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocked / "state"))
    found = Found(id="c901-x", node=Path("/dev/null"), model=BEAM, phys="usb", name="Litra Beam")
    wire = SimulatedTransport(BEAM)
    assert Litra(found, wire).set_lumen(200) == 200
    assert wire.lumen == 200


# ---- 2. the hard rule ---------------------------------------------------------------------------------------------


def lit(tmp_path: Path, hour: int, **kwargs) -> tuple[Bench, FakeLights]:
    lights = FakeLights("a", "b", on=True)
    return Bench(tmp_path, lights, hour=hour, **kwargs), lights


def test_no_camera_user_and_the_lights_are_off_within_five_minutes(tmp_path: Path) -> None:
    bench, lights = lit(tmp_path, 14)
    bench.step(int(lightauto.NO_CAMERA_OFF_SECONDS) - 5)
    assert lights.state["a"]["power"] and lights.state["b"]["power"]
    bench.step(10)
    assert not lights.state["a"]["power"] and not lights.state["b"]["power"]
    assert lightauto.NO_CAMERA_OFF_SECONDS < 300
    assert len(bench.events("hard-off")) == 2


def test_nothing_overrides_the_hard_rule(tmp_path: Path) -> None:
    lights = FakeLights("a", "b", "c", on=True)
    bench = Bench(tmp_path, lights, hour=14)
    bench.schedule(enabled=False, hold_minutes=0)  # a hold that never runs out
    bench.follow(enabled=False, turn_off=False)
    bench.plan.set_light("c", {"role": "manual"})
    bench.step(5)
    lights.hand("a", percent=12)  # held by hand
    bench.step(5)
    assert bench.engine.status()["lights"]["a"]["held"]
    bench.step(int(lightauto.NO_CAMERA_OFF_SECONDS) + 5)
    assert not lights.is_on()


def test_a_light_switched_on_by_hand_with_no_camera_gets_its_own_window(tmp_path: Path) -> None:
    """#195: the owner switched the Beam on with a mouse button an hour after the last call; it went off in seconds."""
    bench, lights = lit(tmp_path, 14)
    bench.step(int(lightauto.NO_CAMERA_OFF_SECONDS) + 5)
    assert not lights.is_on()
    bench.step(3600)  # the camera has been idle for an hour
    lights.hand("a", power=True)
    bench.step(int(lightauto.HAND_ON_SECONDS) - 10)
    assert lights.state["a"]["power"] and not lights.state["b"]["power"]
    assert any("goes off at" in e for e in bench.events("manual"))
    bench.step(20)
    assert not lights.state["a"]["power"]  # still nothing on for good without a camera


def test_a_hand_window_at_night_is_the_short_one(tmp_path: Path) -> None:
    bench, lights = lit(tmp_path, 23)
    bench.step(int(lightauto.NIGHT_OFF_SECONDS) + 5)
    assert not lights.is_on()
    lights.hand("b", power=True)
    bench.step(int(lightauto.NIGHT_OFF_SECONDS) + 30)
    assert lights.state["b"]["power"]
    bench.step(int(lightauto.HAND_ON_NIGHT_SECONDS))
    assert not lights.is_on()
    assert lightauto.HAND_ON_NIGHT_SECONDS < lightauto.HAND_ON_SECONDS


def test_a_call_after_a_hand_switch_on_ends_with_the_normal_rule(tmp_path: Path) -> None:
    bench, lights = lit(tmp_path, 14)
    bench.step(int(lightauto.NO_CAMERA_OFF_SECONDS) + 5)
    lights.hand("a", power=True)
    bench.step(5)
    bench.camera.usage = TEAMS
    bench.step(600)
    bench.camera.usage = IDLE
    bench.step(int(lightauto.NO_CAMERA_OFF_SECONDS) + 5)
    assert not lights.is_on()  # the call ended: four minutes, not the hand window again


def test_a_camera_user_keeps_the_lights_on_by_day_and_by_night(tmp_path: Path) -> None:
    for hour in (14, 23, 3):
        bench, lights = lit(tmp_path / str(hour), hour)
        (tmp_path / str(hour)).mkdir(exist_ok=True)
        bench.camera.usage = TEAMS
        bench.step(15 * 60)
        assert lights.state["a"]["power"] and lights.state["b"]["power"], hour
        assert bench.events("hard-off") == []


def test_apps_that_use_the_camera_count_whatever_the_follow_settings_say(tmp_path: Path) -> None:
    bench, lights = lit(tmp_path, 23)

    class Unfiltered:
        usage = TEAMS

        def poll(self, follow: dict, devices: set[str]):
            return self.usage if follow.get("apps", True) else IDLE

    bench.engine.watcher = Unfiltered()
    bench.follow(apps=False)
    bench.step(10 * 60)
    assert lights.is_on()  # a call is on, the person only asked camera follow not to look at apps


def test_at_night_nothing_is_on_without_a_camera(tmp_path: Path) -> None:
    bench, lights = lit(tmp_path, 23)
    bench.step(int(lightauto.NIGHT_OFF_SECONDS) - 5)
    assert lights.is_on()
    bench.step(10)
    assert not lights.is_on()


def test_the_night_after_a_call_ends_is_dark_in_a_minute(tmp_path: Path) -> None:
    bench, lights = lit(tmp_path, 22)
    bench.camera.usage = TEAMS
    bench.step(300)
    assert lights.is_on()
    bench.camera.usage = IDLE
    bench.step(int(lightauto.NIGHT_OFF_SECONDS) + 5)  # the grace period is 120 s; the night rule does not wait for it
    assert not lights.is_on()


def test_the_overnight_scenario(tmp_path: Path) -> None:
    """A Beam that was on when the camera went, and an hour of nothing after, with a stale override on it."""
    bench, lights = sequence_bench(tmp_path)
    bench.wall = datetime(2026, 11, 3, 22, 3, 0, tzinfo=TZ)
    bench.camera.usage = TEAMS
    bench.step(240)
    bench.camera.usage = IDLE
    lights.hand("a", power=True)
    bench.step(3600)
    assert not lights.is_on()


@pytest.mark.parametrize(
    ("hour", "minute", "night"),
    [(21, 59, False), (22, 0, True), (23, 59, True), (0, 0, True), (6, 59, True), (7, 0, False), (12, 0, False)],
)
def test_night_is_22_to_07_local(hour: int, minute: int, night: bool) -> None:
    assert lightauto.is_night(datetime(2026, 11, 3, hour, minute, tzinfo=TZ)) is night


def test_a_light_that_will_not_switch_off_is_retried_and_reported(tmp_path: Path) -> None:
    bench, lights = lit(tmp_path, 14)
    lights.fail = "the hidraw node went away"
    bench.step(int(lightauto.NO_CAMERA_OFF_SECONDS) + 5)
    assert lights.is_on()
    assert any("Could not switch" in t for t in bench.events("error"))
    lights.fail = None
    bench.step(int(lightauto.OFF_RETRY_SECONDS) + 5)
    assert not lights.is_on()


def test_the_demo_rig_alone_opts_out_of_the_hard_rule(tmp_path: Path) -> None:
    bench, lights = lit(tmp_path, 14, hard_off=False)
    bench.step(3600)
    assert lights.is_on()
