"""The lighting controller: protocol-level state, hotplug, read-back, presets, errors, the rig and the HTTP API."""

from __future__ import annotations

import http.client
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from http_support import HTTP_TIMEOUT

from streamstack import lightauto, lighting, lightplan
from streamstack.console.errors import ActionError, Superseded
from streamstack.console.lighting import DemoLighting, LightingController, PresetLibrary
from streamstack.console.rig import Rig
from streamstack.console.server import ConsoleServer
from streamstack.console.studio import DemoStudio
from streamstack.lighting import MODELS, Found, LightingError, SimulatedTransport

BEAM = MODELS[0xC901]
GLOW = MODELS[0xC900]


def found(light_id: str, model=BEAM) -> Found:
    return Found(id=light_id, node=Path("/dev/null"), model=model, phys=light_id, name=model.name)


class Bench:
    """A scriptable USB bus: lights come and go, opens can be denied, each light is a SimulatedTransport."""

    def __init__(self, *lights: Found) -> None:
        self.present = {f.id: f for f in lights}
        self.wires: dict[str, SimulatedTransport] = {}
        self.denied: set[str] = set()
        self.scan_error: str | None = None

    def scan(self) -> list[Found]:
        if self.scan_error:
            raise LightingError(self.scan_error)
        return list(self.present.values())

    def open(self, light: Found) -> SimulatedTransport:
        if light.id in self.denied:
            raise LightingError(f"Permission denied opening {light.node}. {lighting.PERMISSION_HINT}", "permission")
        return self.wires.setdefault(light.id, SimulatedTransport(light.model, on=False, lumen=light.model.lumen_min))


@pytest.fixture
def bench() -> Bench:
    return Bench(found("c901-a"), found("c901-b"))


def controller(bench: Bench, tmp_path: Path, **kwargs) -> LightingController:
    notes: list[tuple[str, str]] = []
    ctl = LightingController(
        scan=bench.scan,
        opener=bench.open,
        library=PresetLibrary(tmp_path / "lighting.json"),
        blink_seconds=0,
        **kwargs,
    )
    ctl.notify = lambda level, text: notes.append((level, text))
    ctl.notes = notes  # type: ignore[attr-defined]
    ctl.poll_once()
    return ctl


def by_id(ctl: LightingController) -> dict[str, dict]:
    return {x["id"]: x for x in ctl.state()["lights"]}


def test_state_lists_every_light_with_its_ranges(bench: Bench, tmp_path: Path) -> None:
    state = controller(bench, tmp_path).state()
    assert [x["name"] for x in state["lights"]] == ["Litra Beam 1", "Litra Beam 2"]
    light = state["lights"][0]
    assert light["connected"] and light["error"] is None
    assert light["power"] is False and light["brightness"] == 1 and light["lumen_range"] == [30, 400]
    assert state["kelvin_range"] == [2700, 6500] and state["problem"] is None
    assert [p["name"] for p in state["presets"]][:5] == ["Key light", "Soft", "Warm evening", "Daylight", "Off"]


def test_a_single_light_is_not_numbered(tmp_path: Path) -> None:
    ctl = controller(Bench(found("g", GLOW)), tmp_path)
    assert ctl.state()["lights"][0]["name"] == "Litra Glow"


def test_power_brightness_and_temperature_reach_the_wire(bench: Bench, tmp_path: Path) -> None:
    ctl = controller(bench, tmp_path)
    ctl.act("lighting.power", {"id": "c901-a", "on": True})
    ctl.act("lighting.brightness", {"id": "c901-a", "percent": 100})
    ctl.act("lighting.temperature", {"id": "c901-a", "kelvin": 6520})
    wire = bench.wires["c901-a"]
    assert (wire.on, wire.lumen, wire.kelvin) == (True, 400, 6500)
    assert not bench.wires["c901-b"].on
    light = by_id(ctl)["c901-a"]
    assert (light["power"], light["brightness"], light["lumen"], light["kelvin"]) == (True, 100, 400, 6500)


def test_a_guarded_write_lands_while_the_light_is_as_expected(bench: Bench, tmp_path: Path) -> None:
    ctl = controller(bench, tmp_path)
    ctl.act("lighting.power", {"id": "c901-a", "on": True})
    ctl.act("lighting.brightness", {"id": "c901-a", "percent": 50})
    light = by_id(ctl)["c901-a"]
    ctl.write("c901-a", percent=80, expect=(True, light["lumen"], light["kelvin"]))
    assert by_id(ctl)["c901-a"]["brightness"] == 80


def test_a_guarded_write_is_dropped_when_somebody_moved_the_light(bench: Bench, tmp_path: Path) -> None:
    ctl = controller(bench, tmp_path)
    ctl.act("lighting.power", {"id": "c901-a", "on": True})
    ctl.act("lighting.brightness", {"id": "c901-a", "percent": 50})
    seen = by_id(ctl)["c901-a"]
    expect = (True, seen["lumen"], seen["kelvin"])
    ctl.act("lighting.brightness", {"id": "c901-a", "percent": 90})  # a person, after the caller looked
    wire = bench.wires["c901-a"]
    writes = len(wire.writes)
    with pytest.raises(Superseded):
        ctl.write("c901-a", percent=20, expect=expect)
    assert len(wire.writes) == writes  # nothing reached the light
    assert by_id(ctl)["c901-a"]["brightness"] == 90
    ctl.act("lighting.power", {"id": "c901-a", "on": False})
    with pytest.raises(Superseded):
        ctl.write("c901-a", percent=20, expect=expect)  # a power change counts as well


def test_all_is_the_default_target(bench: Bench, tmp_path: Path) -> None:
    ctl = controller(bench, tmp_path)
    ctl.act("lighting.power", {"on": True})
    ctl.act("lighting.brightness", {"id": "all", "percent": 50})
    assert all(w.on and w.lumen == lighting.percent_to_lumen(BEAM, 50) for w in bench.wires.values())
    assert ctl.state()["master"]["on"] and ctl.state()["master"]["all_on"]


def test_master_averages_the_lights_that_are_on(bench: Bench, tmp_path: Path) -> None:
    ctl = controller(bench, tmp_path)
    ctl.act("lighting.power", {"id": "c901-a", "on": True})
    ctl.act("lighting.brightness", {"id": "c901-a", "percent": 80})
    master = ctl.state()["master"]
    assert master["on"] and not master["all_on"] and master["brightness"] == 80


@pytest.mark.parametrize(
    ("action", "params", "message"),
    [
        ("lighting.power", {"on": "yes"}, "on must be"),
        ("lighting.brightness", {"percent": "high"}, "percent must be a number"),
        ("lighting.brightness", {"percent": float("nan")}, "percent must be a number"),
        ("lighting.brightness", {"percent": True}, "percent must be a number"),
        ("lighting.temperature", {"id": 3, "kelvin": 4000}, "id must be"),
        ("lighting.power", {"id": "nope", "on": True}, "No light called"),
        ("lighting.preset", {"name": "Nonexistent"}, "No lighting preset"),
        ("lighting.preset", {}, "name is required"),
        ("lighting.bogus", {}, "Unknown action"),
    ],
)
def test_bad_requests_are_400(bench: Bench, tmp_path: Path, action: str, params: dict, message: str) -> None:
    with pytest.raises(ActionError) as err:
        controller(bench, tmp_path).act(action, params)
    assert err.value.status == 400 and message in err.value.message


def test_out_of_range_values_are_clamped(bench: Bench, tmp_path: Path) -> None:
    ctl = controller(bench, tmp_path)
    ctl.act("lighting.brightness", {"percent": 0})
    ctl.act("lighting.temperature", {"kelvin": 100})
    assert bench.wires["c901-a"].lumen == 30 and bench.wires["c901-a"].kelvin == 2700


def test_no_lights_is_a_409(tmp_path: Path) -> None:
    ctl = controller(Bench(), tmp_path)
    with pytest.raises(ActionError) as err:
        ctl.act("lighting.power", {"on": True})
    assert err.value.status == 409 and "No Litra light" in err.value.message
    assert ctl.state()["lights"] == []


def test_permission_denied_is_reported_per_light_and_in_the_problem(bench: Bench, tmp_path: Path) -> None:
    bench.denied = {"c901-a"}
    ctl = controller(bench, tmp_path)
    lights = by_id(ctl)
    assert not lights["c901-a"]["connected"] and lights["c901-a"]["error_kind"] == "permission"
    assert lights["c901-b"]["connected"]
    problem = ctl.state()["problem"]
    assert problem["kind"] == "permission" and "udev rule" in problem["message"]
    with pytest.raises(ActionError) as err:
        ctl.act("lighting.power", {"id": "c901-a", "on": True})
    assert err.value.status == 409 and "udev rule" in err.value.message
    # all still reaches the light that is reachable
    ctl.act("lighting.power", {"on": True})
    assert bench.wires["c901-b"].on


def test_the_light_recovers_once_the_rule_is_installed(bench: Bench, tmp_path: Path) -> None:
    bench.denied = {"c901-a", "c901-b"}
    ctl = controller(bench, tmp_path)
    with pytest.raises(ActionError, match="udev rule"):
        ctl.act("lighting.power", {"on": True})
    bench.denied = set()
    ctl.poll_once()
    assert all(x["connected"] for x in ctl.state()["lights"]) and ctl.state()["problem"] is None


def test_hotplug_announces_arrivals_and_departures(bench: Bench, tmp_path: Path) -> None:
    ctl = controller(bench, tmp_path)
    del bench.present["c901-b"]
    bench.present["g"] = found("g", GLOW)
    ctl._rescan(announce=True)
    assert sorted(by_id(ctl)) == ["c901-a", "g"]
    assert ctl.notes == [("warn", "Litra Beam was unplugged"), ("ok", "Litra Glow connected")]  # type: ignore[attr-defined]
    assert bench.wires["c901-b"].closed


def test_a_scan_failure_is_a_problem_not_a_crash(bench: Bench, tmp_path: Path) -> None:
    bench.scan_error = "Cannot list /sys/class/hidraw: boom"
    ctl = controller(bench, tmp_path)
    assert ctl.state()["problem"] == {"kind": "scan", "message": "Cannot list /sys/class/hidraw: boom"}
    assert ctl.health_checks()[0]["state"] == "warn"


def test_physical_button_changes_are_read_back(bench: Bench, tmp_path: Path) -> None:
    ctl = controller(bench, tmp_path)
    wire = bench.wires["c901-a"]
    wire.on, wire.lumen, wire.kelvin = True, 400, 3000
    ctl.poll_once()
    light = by_id(ctl)["c901-a"]
    assert (light["power"], light["brightness"], light["kelvin"]) == (True, 100, 3000)


def test_a_read_right_after_a_write_does_not_undo_it(bench: Bench, tmp_path: Path) -> None:
    ctl = controller(bench, tmp_path)
    ctl.act("lighting.power", {"id": "c901-a", "on": True})
    bench.wires["c901-a"].on = False  # the light has not caught up yet
    ctl.poll_once()
    assert by_id(ctl)["c901-a"]["power"] is True


def test_a_light_that_does_not_answer_keeps_the_last_known_state(bench: Bench, tmp_path: Path) -> None:
    ctl = controller(bench, tmp_path)
    ctl.act("lighting.brightness", {"id": "c901-a", "percent": 70})
    wire = bench.wires["c901-a"]
    wire._answer = lambda *a: None  # type: ignore[method-assign]
    ctl._clock = lambda: time.monotonic() + 100
    ctl.poll_once()
    assert by_id(ctl)["c901-a"]["brightness"] == 70


def test_a_light_unplugged_mid_write_is_marked_not_fatal(bench: Bench, tmp_path: Path) -> None:
    ctl = controller(bench, tmp_path)

    def gone(data: bytes) -> None:
        raise LightingError("/dev/hidraw3 was unplugged.", "gone")

    bench.wires["c901-a"].write = gone  # type: ignore[method-assign]
    ctl.act("lighting.power", {"on": True})  # the other light still takes it
    assert not by_id(ctl)["c901-a"]["connected"] and by_id(ctl)["c901-b"]["power"] is True
    with pytest.raises(ActionError, match="unplugged"):
        ctl.act("lighting.power", {"id": "c901-a", "on": True})


def test_every_light_failing_is_a_409(bench: Bench, tmp_path: Path) -> None:
    ctl = controller(bench, tmp_path)
    for wire in bench.wires.values():
        wire.write = lambda data: (_ for _ in ()).throw(LightingError("bus error"))  # type: ignore[method-assign]
    with pytest.raises(ActionError) as err:
        ctl.act("lighting.power", {"on": True})
    assert err.value.status == 409 and "bus error" in err.value.message


# -- presets -----------------------------------------------------------------


def test_builtin_presets_apply_to_every_light(bench: Bench, tmp_path: Path) -> None:
    ctl = controller(bench, tmp_path)
    ctl.act("lighting.preset", {"name": "Warm evening"})
    for wire in bench.wires.values():
        assert wire.on and wire.kelvin == 2700 and wire.lumen == lighting.percent_to_lumen(BEAM, 35)
    assert ctl.state()["preset"] == "Warm evening"
    ctl.act("lighting.preset", {"name": "Off"})
    assert not any(w.on for w in bench.wires.values()) and ctl.state()["preset"] == "Off"


def test_a_preset_is_not_active_after_a_light_moves_off_it(bench: Bench, tmp_path: Path) -> None:
    ctl = controller(bench, tmp_path)
    ctl.act("lighting.preset", {"name": "Soft"})
    ctl.act("lighting.brightness", {"id": "c901-a", "percent": 90})
    assert ctl.state()["preset"] is None


def test_saved_presets_round_trip_through_disk(bench: Bench, tmp_path: Path) -> None:
    ctl = controller(bench, tmp_path)
    ctl.act("lighting.preset", {"name": "Key light"})
    ctl.act("lighting.temperature", {"kelvin": 3500})
    assert ctl.act("lighting.presets.save", {"name": " Podcast "}) == "Saved lighting preset Podcast"
    mine = [p for p in ctl.state()["presets"] if not p["builtin"]]
    assert mine == [
        {"name": "Podcast", "builtin": False, "blurb": "Saved by you", "on": True, "brightness": 80, "kelvin": 3500}
    ]
    path = tmp_path / "lighting.json"
    assert path.stat().st_mode & 0o777 == 0o600
    again = LightingController(library=PresetLibrary(path))
    assert [p["name"] for p in again.state()["presets"]][-1] == "Podcast"
    ctl.act("lighting.preset", {"name": "Off"})
    ctl.act("lighting.preset", {"name": "Podcast"})
    assert bench.wires["c901-a"].kelvin == 3500
    ctl.act("lighting.presets.delete", {"name": "Podcast"})
    assert not [p for p in ctl.state()["presets"] if not p["builtin"]]


@pytest.mark.parametrize("name", ["Soft", "soft", "x" * 41, "bad\nname", "  "])
def test_preset_names_are_validated(bench: Bench, tmp_path: Path, name: str) -> None:
    with pytest.raises(ActionError) as err:
        controller(bench, tmp_path).act("lighting.presets.save", {"name": name})
    assert err.value.status == 400


def test_builtin_and_unknown_presets_cannot_be_deleted(bench: Bench, tmp_path: Path) -> None:
    ctl = controller(bench, tmp_path)
    for name in ("Off", "Ghost"):
        with pytest.raises(ActionError) as err:
            ctl.act("lighting.presets.delete", {"name": name})
        assert err.value.status == 400


def test_saving_needs_a_light(tmp_path: Path) -> None:
    with pytest.raises(ActionError) as err:
        controller(Bench(), tmp_path).act("lighting.presets.save", {"name": "Mine"})
    assert err.value.status == 409


def test_a_hand_edited_preset_file_is_survived(tmp_path: Path) -> None:
    path = tmp_path / "lighting.json"
    path.write_text(json.dumps({"Good": {"brightness": 60, "kelvin": 4012}, "Bad": {"brightness": "x"}, "Worse": 3}))
    names = [p["name"] for p in PresetLibrary(path).listing() if not p["builtin"]]
    assert names == ["Good"]
    path.write_text("not json")
    assert PresetLibrary(path).user() == {}


def test_an_unwritable_library_is_a_409(bench: Bench, tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory is needed")
    ctl = controller(bench, tmp_path)
    ctl.library = PresetLibrary(blocker / "lighting.json")
    with pytest.raises(ActionError) as err:
        ctl.act("lighting.presets.save", {"name": "Mine"})
    assert err.value.status == 409


# -- identify ---------------------------------------------------------------------


def wait_for(probe, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not probe():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_identify_flashes_then_restores_the_light(bench: Bench, tmp_path: Path) -> None:
    ctl = controller(bench, tmp_path)
    ctl.act("lighting.power", {"id": "c901-a", "on": True})
    ctl.act("lighting.brightness", {"id": "c901-a", "percent": 60})
    wire = bench.wires["c901-a"]
    before = len(wire.writes)
    ctl.act("lighting.identify", {"id": "c901-a"})
    wait_for(lambda: not by_id(ctl)["c901-a"]["identifying"])
    assert len(wire.writes) > before + 6
    assert wire.on and wire.lumen == lighting.percent_to_lumen(BEAM, 60)


def test_identify_leaves_an_off_light_off(bench: Bench, tmp_path: Path) -> None:
    ctl = controller(bench, tmp_path)
    ctl.act("lighting.identify", {"id": "c901-b"})
    wait_for(lambda: not by_id(ctl)["c901-b"]["identifying"])
    assert not bench.wires["c901-b"].on


# -- health ---------------------------------------------------------------------


def test_health_check_reports_found_and_permission(bench: Bench, tmp_path: Path) -> None:
    ctl = controller(bench, tmp_path)
    [check] = ctl.health_checks()
    assert (check["id"], check["group"], check["state"], check["title"]) == (
        "lighting",
        "video",
        "ok",
        "2 Litra lights ready",
    )
    bench.denied = {"c901-a"}
    del ctl._slots["c901-a"]
    ctl.poll_once()
    [check] = ctl.health_checks()
    assert check["state"] == "fail" and "udev rule" in check["hint"] and lighting.UDEV_RULE_FILE in check["hint"]


def test_health_check_with_no_lights_is_not_a_problem(tmp_path: Path) -> None:
    [check] = controller(Bench(), tmp_path).health_checks()
    assert check["state"] == "ok" and "no Logitech Litra" in check["title"]


# -- demo and rig -----------------------------------------------------------------


def test_demo_has_two_beams_from_the_first_snapshot(tmp_path: Path) -> None:
    demo = DemoLighting(library=PresetLibrary(tmp_path / "l.json"))
    demo.start()
    try:
        state = demo.state()
        assert [x["name"] for x in state["lights"]] == ["Litra Beam 1", "Litra Beam 2"]
        assert all(x["connected"] and x["power"] for x in state["lights"])
        demo.act("lighting.power", {"id": "all", "on": False})
        assert not demo.state()["master"]["on"]
    finally:
        demo.stop()


def _held(demo: DemoLighting) -> list[str]:
    return [i for i, light in demo.engine.status()["lights"].items() if light["held"]]


def test_the_demo_reset_puts_everything_back_and_never_creates_a_hold(tmp_path: Path) -> None:
    demo = DemoLighting(library=PresetLibrary(tmp_path / "l.json"), plan=lightplan.Store(tmp_path / "plan.json"))
    demo.poll_once()
    engine = demo.engine
    engine.tick()
    # Leave the rig as a test would: lights moved by hand and held, the schedule on, a camera in use.
    demo.act("lighting.auto.schedule", {"enabled": True, "hold_minutes": 30})
    demo.act("lighting.demo.camera", {"on": True})
    demo.act("lighting.brightness", {"id": "all", "percent": 20})
    demo.act("lighting.temperature", {"id": "all", "kelvin": 6200})
    for _ in range(3):
        engine.tick()
    assert _held(demo)

    demo.act("lighting.demo.reset", {})

    assert [(x["power"], x["brightness"], x["kelvin"]) for x in demo.state()["lights"]] == [
        (True, 70, 5000),
        (True, 55, 4200),
    ]
    assert _held(demo) == []
    assert engine.status()["events"] == [] and engine.status()["session"] is None
    assert demo.plan.load()["schedule"]["enabled"] is False
    assert demo.panel.scripted.app is None
    time.sleep(lightauto.SETTLE_SECONDS + 0.1)  # past any settle window, where a late read-back would be compared
    for _ in range(4):
        engine.tick()
    assert _held(demo) == []
    assert engine.status()["events"] == []


def test_a_hand_write_after_the_demo_reset_is_still_held(tmp_path: Path) -> None:
    demo = DemoLighting(library=PresetLibrary(tmp_path / "l.json"), plan=lightplan.Store(tmp_path / "plan.json"))
    demo.poll_once()
    demo.act("lighting.demo.reset", {})
    demo.act("lighting.brightness", {"id": "c901-usb-demo-1", "percent": 20})
    demo.engine.tick()
    assert _held(demo) == ["c901-usb-demo-1"]


def test_only_the_demo_rig_can_reset_its_lights_for_a_test(tmp_path: Path) -> None:
    with pytest.raises(ActionError, match="Only the demo rig"):
        controller(Bench(), tmp_path).act("lighting.demo.reset", {})


def test_rig_routes_lighting_actions_and_snapshots_the_state(tmp_path: Path) -> None:
    rig = Rig.demo()
    rig.start()
    try:
        assert len(rig.snapshot()["lighting"]["lights"]) == 2
        rig.act("lighting.power", {"on": False})
        assert rig.snapshot()["lighting"]["master"]["on"] is False
        assert any(c["id"] == "lighting" for c in rig.snapshot()["health"]["checks"])
    finally:
        rig.stop()


def test_a_rig_without_lights_reports_an_empty_lighting_object() -> None:
    rig = Rig(*(type("C", (), {"notify": None, "state": lambda self: {}})() for _ in range(2)), DemoStudio(delay=0))
    snapshot = rig.snapshot()["lighting"]
    assert snapshot["lights"] == [] and snapshot["problem"] is None and snapshot["presets"]
    with pytest.raises(ActionError, match="Unknown action"):
        rig.act("lighting.power", {"on": True})


# -- over HTTP ---------------------------------------------------------------------


@pytest.fixture
def server() -> Iterator[ConsoleServer]:
    srv = ConsoleServer(Rig.demo(), port=0, token="tok", static_dir=Path("/nonexistent"), allow_hosts=set())
    srv.start()
    yield srv
    srv.close()


def call(server: ConsoleServer, method: str, path: str, body: object = None) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=HTTP_TIMEOUT)
    try:
        headers = {"X-StreamStack-Token": server.token}
        if body is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())
    finally:
        conn.close()


def test_api_drives_the_lights_and_reports_errors(server: ConsoleServer) -> None:
    status, body = call(server, "POST", "/api/action", {"action": "lighting.brightness", "percent": 25, "id": "all"})
    assert (status, body) == (200, {"ok": True})
    _, state = call(server, "GET", "/api/state")
    assert [x["brightness"] for x in state["lighting"]["lights"]] == [25, 25]
    status, body = call(server, "POST", "/api/action", {"action": "lighting.brightness", "percent": "x"})
    assert status == 400 and "percent must be a number" in body["error"]
    status, body = call(server, "POST", "/api/action", {"action": "lighting.power", "id": "ghost", "on": True})
    assert status == 400 and "No light called" in body["error"]


def test_a_preset_matches_within_the_light_rounding(bench: Bench, tmp_path: Path) -> None:
    ctl = controller(bench, tmp_path)
    ctl.act("lighting.preset", {"name": "Soft"})
    soft = next(p for p in ctl.state()["presets"] if p["name"] == "Soft")
    assert ctl.state()["preset"] == "Soft"
    ctl.act("lighting.brightness", {"id": "c901-a", "percent": soft["brightness"] + 2})
    assert ctl.state()["preset"] == "Soft"  # a lumen step or two is not a different preset


def test_the_demo_rig_saves_presets_without_flushing_the_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Save current stalled past 30 s on a busy CI disk when each save fsynced the file, its folder and a backup."""
    flushed: list[int] = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (flushed.append(fd), real(fd))[1])
    PresetLibrary(tmp_path / "durable.json").save("Interview", 33, 3300)
    assert flushed
    flushed.clear()
    quick = PresetLibrary(tmp_path / "quick.json", ephemeral=True)
    quick.save("Interview", 33, 3300)
    assert flushed == []
    assert not (tmp_path / "quick.json.bak").exists()
    assert [p["name"] for p in quick.listing() if not p["builtin"]] == ["Interview"]
    monkeypatch.setenv("STREAMSTACK_ENV_FILE", str(tmp_path / "env"))
    assert DemoLighting().library.ephemeral is True
