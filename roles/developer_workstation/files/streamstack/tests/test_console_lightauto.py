"""The Lighting page's automation: the Console panel, the demo rig, the controller the engine writes through."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from streamstack import lightauto, lighting, lightplan, lightservice
from streamstack.camerause import Consumer, Usage
from streamstack.console import lightauto as panel_module
from streamstack.console.errors import ActionError
from streamstack.console.lighting import DemoLighting, LightingController, PresetLibrary
from streamstack.lighting import MODELS, Found, SimulatedTransport

BEAM = MODELS[0xC901]
ONE, TWO = "c901-usb-demo-1", "c901-usb-demo-2"


@pytest.fixture
def demo() -> Iterator[DemoLighting]:
    ctl = DemoLighting()
    ctl.start()
    yield ctl
    ctl.stop()


def automation(ctl: LightingController) -> dict[str, Any]:
    return ctl.state()["automation"]


def until(check: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return check()


def test_the_demo_starts_with_the_schedule_off_so_the_demo_lights_stay_put(demo: DemoLighting) -> None:
    state = demo.state()
    assert state["automation"]["available"] and state["automation"]["demo"]
    assert state["automation"]["settings"]["schedule"]["enabled"] is False
    assert [x["brightness"] for x in state["lights"]] == [70, 55]
    assert state["automation"]["status"]["where"] == "console"


def test_the_state_carries_everything_the_page_draws(demo: DemoLighting) -> None:
    auto = automation(demo)
    assert [s["id"] for s in auto["slots"]] == ["night", "dawn", "morning", "afternoon", "evening"]
    assert {r["id"] for r in auto["roles"]} == {"key", "fill", "manual"}
    assert [m["id"] for m in auto["modes"]] == ["sun", "clock"]
    assert [s["id"] for s in auto["sources"]] == ["apps"]
    assert len(auto["curve"]["brightness"]) == len(auto["curve"]["kelvin"]) == 97
    assert len(auto["hours"]) == 24
    assert {"sunrise", "sunset"} <= auto["sun"].keys()
    assert 0 <= auto["now"]["minute"] < 1440 and auto["now"]["slot"] in {s["id"] for s in auto["slots"]}
    assert set(auto["settings"]["lights"]) == {ONE, TWO}


def test_lights_carry_default_names_custom_names_roles_and_limits(demo: DemoLighting) -> None:
    first = next(x for x in demo.state()["lights"] if x["id"] == ONE)
    assert first["name"] == first["default_name"] == "Litra Beam 1"
    assert first["role"] == "key" and first["limits"]["max_brightness"] == 100
    demo.act("lighting.light.settings", {"id": ONE, "name": "Desk key", "role": "fill", "max_brightness": 60})
    first = next(x for x in demo.state()["lights"] if x["id"] == ONE)
    assert (first["name"], first["default_name"], first["role"]) == ("Desk key", "Litra Beam 1", "fill")
    assert first["limits"]["max_brightness"] == 60
    demo.act("lighting.light.settings", {"id": ONE, "name": ""})
    assert next(x for x in demo.state()["lights"] if x["id"] == ONE)["name"] == "Litra Beam 1"


@pytest.mark.parametrize(
    ("action", "params", "message"),
    [
        ("lighting.auto.follow", {}, "Nothing to change"),
        ("lighting.auto.follow", {"grace_seconds": "x"}, "number"),
        ("lighting.auto.schedule", {"mode": "lunar"}, "mode"),
        ("lighting.auto.slot", {"slot": "teatime", "brightness": 5}, "slot"),
        ("lighting.auto.hour", {"hour": 30, "brightness": 5}, "hour"),
        ("lighting.light.settings", {"id": "ghost", "name": "x"}, "No light called"),
        ("lighting.light.settings", {"id": ONE, "role": "backlight"}, "role"),
        ("lighting.light.settings", {"id": ONE, "min_brightness": 90, "max_brightness": 10}, "minimum"),
        ("lighting.auto.defaults", {"what": "weather"}, "section"),
        ("lighting.demo.camera", {"on": "yes"}, "true or false"),
        ("lighting.demo.camera", {"on": True, "app": ""}, "short name"),
        ("lighting.demo.director", {"on": 1}, "true or false"),
        ("lighting.service.enable", {}, "demo rig"),
        ("lighting.auto.nonsense", {}, "Unknown action"),
    ],
)
def test_bad_automation_requests_are_refused_with_a_reason(
    demo: DemoLighting, action: str, params: dict, message: str
) -> None:
    with pytest.raises(ActionError, match=message) as err:
        demo.act(action, params)
    assert err.value.status in (400, 409)


def test_settings_survive_in_the_users_file(demo: DemoLighting) -> None:
    demo.act("lighting.auto.follow", {"grace_seconds": 45, "ignore": ["Slack"]})
    demo.act("lighting.auto.slot", {"slot": "evening", "brightness": 33, "kelvin": 3000})
    again = lightplan.Store().load()
    assert again["follow"]["grace_seconds"] == 45 and again["follow"]["ignore"] == ["Slack"]
    assert again["schedule"]["slots"]["evening"] == {"brightness": 33, "kelvin": 3000}
    demo.act("lighting.auto.defaults", {"what": "follow"})
    assert automation(demo)["settings"]["follow"]["grace_seconds"] == 120


def test_the_clock_table_is_seeded_from_the_sun_and_editable(demo: DemoLighting) -> None:
    demo.act("lighting.auto.schedule", {"mode": "clock"})
    seeded = automation(demo)["settings"]["schedule"]["hours"]
    assert len(seeded) == 24
    demo.act("lighting.auto.hour", {"hour": 9, "brightness": 88})
    assert automation(demo)["hours"][9]["brightness"] == 88
    assert automation(demo)["hours"][10] == seeded[10]


def test_an_unwritable_settings_file_is_a_409(
    demo: DemoLighting, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    monkeypatch.setattr(demo.plan, "path", blocker / "plan.json")
    with pytest.raises(ActionError, match="Could not save") as err:
        demo.act("lighting.auto.follow", {"enabled": False})
    assert err.value.status == 409


def test_a_demo_call_switches_the_demo_lights_on_and_off(demo: DemoLighting) -> None:
    demo.act("lighting.power", {"on": False})
    demo.act("lighting.auto.follow", {"grace_seconds": 1, "start_delay_seconds": 0})
    demo.act("lighting.demo.camera", {"on": True, "app": "Teams"})
    assert until(lambda: all(x["power"] for x in demo.state()["lights"]))
    status = automation(demo)["status"]
    assert "Teams opened the camera" in status["summary"] and status["session"]["reason"] == "Teams opened the camera"
    demo.act("lighting.demo.camera", {"on": False})
    assert until(lambda: not any(x["power"] for x in demo.state()["lights"]))
    assert any(e["kind"] == "camera-off" for e in automation(demo)["status"]["events"])


def test_a_demo_director_takes_brightness_from_the_schedule(demo: DemoLighting) -> None:
    demo.act("lighting.auto.schedule", {"enabled": True})
    demo.act("lighting.demo.director", {"on": True})
    assert until(lambda: automation(demo)["status"].get("owner") == "director")
    demo.act("lighting.demo.director", {"on": False})
    assert until(lambda: automation(demo)["status"].get("owner") == "schedule")


def test_resume_is_a_stamp_the_engine_notices(demo: DemoLighting) -> None:
    before = lightplan.Store().load()["resume"]
    demo.act("lighting.auto.resume", {})
    assert lightplan.Store().load()["resume"] > before


# ---- the live panel: service status and the unit --------------------------------------------------------------


@pytest.fixture
def live(tmp_path: Path) -> LightingController:
    return LightingController(
        scan=lambda: [], opener=lambda found: None, library=PresetLibrary(tmp_path / "lighting.json"), blink_seconds=0
    )


def test_without_the_service_the_page_says_how_to_start_it(
    live: LightingController, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lightservice, "installed_unit", lambda extra_dirs=(): None)
    auto = automation(live)
    assert not auto["demo"] and auto["service"]["state"] == "missing"
    assert "streamstack setup" in auto["service"]["message"] and not auto["status"]["running"]
    monkeypatch.setattr(lightservice, "installed_unit", lambda extra_dirs=(): Path("/x/streamstack-lights.service"))
    assert automation(live)["service"]["state"] == "stopped"


def test_a_fresh_status_file_means_the_service_is_running(live: LightingController) -> None:
    bench_status = {
        "running": True,
        "heartbeat": time.time(),
        "owner": "schedule",
        "owner_label": "The daylight schedule",
        "summary": "Camera in use since 14:02: Teams opened the camera.",
        "camera": {"in_use": True, "reason": "Teams opened the camera", "apps": []},
        "lights": {},
        "events": [],
    }
    from streamstack.director import settings

    # Written the way the service writes it every tick (`ephemeral`: atomic, no fsync). The durable write used before
    # stamps the heartbeat first and can then wait on the disk for longer than the ten seconds a heartbeat stays
    # fresh, on a runner whose disk is busy (pipeline 96694: 'missing' == 'running').
    settings.write_json(lightplan.state_path(), bench_status, ephemeral=True)
    auto = automation(live)
    assert auto["service"]["state"] == "running"
    assert auto["status"]["summary"].startswith("Camera in use since 14:02")
    settings.write_json(lightplan.state_path(), {**bench_status, "heartbeat": 1.0})
    assert automation(live)["service"]["state"] in ("missing", "stopped")


def test_the_service_actions_call_systemd_and_report_failures(
    live: LightingController, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(lightservice, "enable", lambda: calls.append("enable") or "started")
    monkeypatch.setattr(lightservice, "disable", lambda: calls.append("disable") or "stopped")
    live.act("lighting.service.enable", {})
    live.act("lighting.service.disable", {})
    assert calls == ["enable", "disable"]

    def fail() -> str:
        raise lightservice.ServiceError("systemctl --user did not answer")

    monkeypatch.setattr(lightservice, "enable", fail)
    with pytest.raises(ActionError, match="did not answer") as err:
        live.act("lighting.service.enable", {})
    assert err.value.status == 409
    with pytest.raises(ActionError, match="Only the demo rig"):
        live.act("lighting.demo.camera", {"on": True})


# ---- the controller the engine writes through -----------------------------------------------------------------


class Wires:
    def __init__(self, *ids: str) -> None:
        self.found = [Found(id=i, node=Path("/dev/null"), model=BEAM, phys=i, name=BEAM.name) for i in ids]
        self.wires = {i: SimulatedTransport(BEAM, on=False, lumen=BEAM.lumen_min, kelvin=3000) for i in ids}

    def controller(self, tmp_path: Path) -> LightingController:
        ctl = LightingController(
            scan=lambda: list(self.found),
            opener=lambda found: self.wires[found.id],
            library=PresetLibrary(tmp_path / "lighting.json"),
            blink_seconds=0,
            plan=lightplan.Store(tmp_path / "plan.json"),
        )
        ctl.poll_once()
        return ctl


def test_the_engine_writes_through_the_controller_to_the_wire(tmp_path: Path) -> None:
    wires = Wires("c901-a")
    ctl = wires.controller(tmp_path)
    ctl.write("c901-a", power=True, percent=80, kelvin=5100)
    wire = wires.wires["c901-a"]
    assert wire.on and wire.kelvin == 5100 and wire.lumen == lighting.percent_to_lumen(BEAM, 80)
    view = ctl.lights_view()[0]
    assert (view["power"], view["lumen"], view["kelvin"], view["pid"]) == (True, wire.lumen, 5100, "c901")
    ctl.write("c901-a", power=False)
    ctl.write("c901-a", percent=40)  # accepted while off, and it does not wake the light
    assert not wire.on and wire.lumen == lighting.percent_to_lumen(BEAM, 40)


def test_a_write_to_an_unknown_light_is_an_action_error(tmp_path: Path) -> None:
    ctl = Wires("c901-a").controller(tmp_path)
    with pytest.raises(ActionError, match="No light called"):
        ctl.write("c901-ghost", power=True)


def test_the_real_controller_and_the_engine_follow_a_camera(tmp_path: Path) -> None:
    wires = Wires("c901-a", "c901-b")
    ctl = wires.controller(tmp_path)
    ctl.plan.update("schedule", {"fade_seconds": 0})
    seen: list[Any] = []

    class Camera:
        usage = Usage()

        def poll(self, follow: dict, devices: set[str]) -> Usage:
            seen.append(devices)
            return self.usage

    camera = Camera()
    clock = [100.0]
    engine = lightauto.Automation(
        ctl, ctl.plan, camera, lambda: {"/dev/video43"}, clock=lambda: clock[0], sleep=lambda s: None
    )
    camera.usage = Usage((Consumer(1, "zoom", "/dev/video43"),))
    for _ in range(6):
        clock[0] += 1
        engine.tick()
    assert all(w.on for w in wires.wires.values())
    assert seen and seen[0] == {"/dev/video43"}


# ---- the pinned demo clock -----------------------------------------------------------------------------


@pytest.mark.parametrize("hour", [3, 7, 12, 18, 23])
def test_a_pinned_demo_clock_fixes_the_hour_and_the_place(
    hour: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(panel_module.DEMO_CLOCK_ENV, f"2026-06-21T{hour:02d}:30:00-04:00")
    monkeypatch.setenv("TZ", "Asia/Tokyo")  # the host zone must not matter
    ctl = DemoLighting(plan=lightplan.Store(tmp_path / "plan.json"))
    ctl.poll_once()
    auto = ctl.state()["automation"]
    assert auto["now"]["minute"] == hour * 60 + 30
    assert (auto["location"]["latitude"], auto["location"]["longitude"]) == (40.71, -74.01)
    assert auto["sun"]["sunrise"].startswith("05:")  # New York in June, not the host zone


def test_the_demo_follows_the_real_clock_unless_one_is_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(panel_module.DEMO_CLOCK_ENV, raising=False)
    assert panel_module.demo_clock() is None


@pytest.mark.parametrize("value", ["noon", "2026-06-21T12:00:00"])
def test_a_bad_demo_clock_is_refused_with_a_message(value: str) -> None:
    with pytest.raises(ValueError, match=panel_module.DEMO_CLOCK_ENV):
        panel_module.demo_clock({panel_module.DEMO_CLOCK_ENV: value})
