"""The Console's `hardware` section: the controller, the demo replay, redaction, overrides and the recordings."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from hardware_support import FIXTURES, HOSTS, FakeMachine, studio_wave

from streamstack.console import hardware as console_hw
from streamstack.console.errors import ActionError
from streamstack.console.hardware import DemoHardware, HardwareController, overrides_from, plan_state, redact
from streamstack.console.rig import Rig
from streamstack.hardware import inventory, profiles, schema
from streamstack.hardware.plan import resolve

WAVE_NAME = "alsa_output.usb-Elgato_Systems_Elgato_Wave_XLR_ABC12345-00.analog-stereo"


def wait_for(predicate, what: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert predicate(), what


@pytest.fixture
def machine(tmp_path: Path) -> FakeMachine:
    return studio_wave(FakeMachine(tmp_path))


def test_redact_hides_the_usb_serial_only_in_pipewire_names() -> None:
    assert redact(WAVE_NAME) == "alsa_output.usb-Elgato_Systems_Elgato_Wave_XLR_<serial>-00.analog-stereo"
    assert redact("alsa_card.usb-Elgato_Systems_Elgato_Wave_XLR_ABC12345-00").endswith("_<serial>-00")
    assert redact(redact(WAVE_NAME)) == redact(WAVE_NAME)
    assert redact("auto_null") == "auto_null"
    assert redact("Dummy Output") == "Dummy Output"
    assert redact(None) is None


def test_the_process_environment_beats_the_settings_files() -> None:
    cfg = {"STREAMSTACK_RIG": "studio-deck", "STREAMSTACK_MICROPHONE": "auto"}
    found = overrides_from(cfg, {"STREAMSTACK_RIG": "studio-wave-xlr", "HOME": "/x"})
    assert found["STREAMSTACK_RIG"] == "studio-wave-xlr"
    assert found["STREAMSTACK_MICROPHONE"] == "auto"
    assert "HOME" not in found
    assert overrides_from({}, {})["STREAMSTACK_RIG"] == ""


@pytest.mark.parametrize("host,rig", [("dm313red", "studio-wave-xlr"), ("deskmeet1", "studio-deck")])
def test_the_demo_replays_each_recorded_host(host: str, rig: str) -> None:
    controller = DemoHardware(host)
    state = controller.state()
    assert state["demo"] is True and state["checked"] is True and state["host"] == host
    assert state["rig"]["id"] == rig
    assert state["hosts"] == ["deskmeet1", "dm313red"]
    assert controller.plan() is not None and controller.snapshot() is not None


def test_the_demo_switches_hosts_and_rejects_unknown_ones() -> None:
    controller = DemoHardware()
    assert controller.state()["host"] == "dm313red"
    assert "deskmeet1" in controller.act("hardware.demo_host", {"host": "deskmeet1"})
    assert controller.state()["rig"]["id"] == "studio-deck" and controller.state()["host"] == "deskmeet1"
    for bad in ({"host": "../etc/passwd"}, {"host": 3}, {}):
        with pytest.raises(ActionError) as caught:
            controller.act("hardware.demo_host", bad)
        assert caught.value.status == 400
    assert controller.state()["host"] == "deskmeet1"
    with pytest.raises(ActionError):
        controller.act("hardware.nonsense", {})
    assert controller.act("hardware.refresh", {}) == "Hardware probed again."


def test_an_unknown_demo_host_falls_back_to_the_default() -> None:
    assert DemoHardware("nowhere").state()["host"] == "dm313red"


def test_the_state_is_json_and_leaks_no_serial_or_path() -> None:
    for host in HOSTS:
        text = json.dumps(DemoHardware(host).state())
        assert "ABC12345" not in text
        assert "/home/" not in text


def test_live_controller_refuses_to_switch_the_recorded_host(machine: FakeMachine) -> None:
    controller = HardwareController(take=machine.snapshot, watch=False, env={})
    with pytest.raises(ActionError) as caught:
        controller.act("hardware.demo_host", {"host": "deskmeet1"})
    assert caught.value.status == 409
    with pytest.raises(ActionError):
        controller.act("hardware.other", {})


def test_the_live_controller_probes_in_the_background_and_serves_the_cache(machine: FakeMachine) -> None:
    probes = {"n": 0}
    release = threading.Event()

    def take():
        probes["n"] += 1
        release.wait(5)
        return machine.snapshot()

    controller = HardwareController(take=take, watch=False, env={})
    controller.start()
    try:
        assert controller.state()["checked"] is False  # the first probe is still running
        assert controller.plan() is None
        release.set()
        wait_for(lambda: controller.state()["checked"], "first probe published")
        before = probes["n"]
        for _ in range(50):
            controller.state()
        assert probes["n"] == before  # reads never probe
        assert controller.state()["rig"]["id"] == "studio-wave-xlr"
    finally:
        controller.stop()


def test_a_failing_first_probe_is_reported_not_raised() -> None:
    def take():
        raise OSError("no sysfs")

    controller = HardwareController(take=take, watch=False, env={})
    controller.start()
    try:
        wait_for(lambda: controller.state()["checked"], "failure published")
        assert "OSError: no sysfs" in controller.state()["error"]
        assert controller.plan() is None
    finally:
        controller.stop()


def test_refresh_without_the_monitor_probes_again(machine: FakeMachine) -> None:
    controller = HardwareController(take=machine.snapshot, watch=False, env={})
    assert controller.act("hardware.refresh", {}) == "Hardware probed again."
    assert controller.state()["checked"] and controller.state()["rig"]["id"] == "studio-wave-xlr"


def test_hot_plug_updates_the_state_records_the_change_and_toasts(machine: FakeMachine) -> None:
    toasts: list[tuple[str, str]] = []
    controller = HardwareController(take=machine.snapshot, watch=True, env={}, debounce=0.02)
    controller.notify = lambda level, text: toasts.append((level, text))
    controller.start()
    try:
        wait_for(
            lambda: controller._monitor is not None and controller.state()["hotplug"]["mode"] != "stopped", "monitor"
        )
        machine.remove_usb("0fd9:007d")
        machine.remove_alsa(3)
        machine.set_pipewire([], [], [])
        controller.act("hardware.refresh", {})  # stands in for the udev event; the poll fallback would do the same
        state = controller.state()
        wave = next(d for d in state["devices"] if d["id"] == "wave-xlr")
        assert wave["state"] == "absent"
        assert state["changes"] and "devices" in state["changes"][0]["sections"]
        assert any("Elgato Wave XLR disconnected" in text for _, text in toasts)
    finally:
        controller.stop()


def test_describe_change_names_only_devices_the_rig_knows(machine: FakeMachine) -> None:
    from streamstack.hardware.hotplug import Change, changed_sources

    old = machine.snapshot()
    catalogue = schema.packaged()
    machine.add_usb("1234:5678", "Some Gadget")
    same = machine.snapshot()
    change = Change(old, same, resolve(old, catalogue), resolve(same, catalogue), changed_sources(old, same), ())
    assert console_hw.describe_change(change) is None


def test_overrides_reach_the_plan(machine: FakeMachine) -> None:
    controller = HardwareController({"STREAMSTACK_RIG": "studio-deck"}, take=machine.snapshot, watch=False, env={})
    controller.act("hardware.refresh", {})
    assert controller.state()["rig"]["id"] == "studio-deck" and controller.state()["rig"]["via"] == "env"
    pinned = HardwareController(
        {"STREAMSTACK_RIG": "studio-deck"},
        take=machine.snapshot,
        watch=False,
        env={"STREAMSTACK_RIG": "studio-wave-xlr"},
    )
    pinned.act("hardware.refresh", {})
    assert pinned.state()["rig"]["id"] == "studio-wave-xlr"


def test_plan_state_matches_the_recorded_hosts() -> None:
    wave = inventory.packaged_snapshot("dm313red")
    state = plan_state(resolve(wave, schema.packaged()), wave)
    assert state["audio"]["rate"] == 48000 and state["audio"]["format"] == "s24le"
    assert state["encoder"]["codec"] == "av1"
    assert {g["node"] for g in state["gpus"]} == {"/dev/dri/renderD128"}
    deskmeet = inventory.packaged_snapshot("deskmeet1")
    split = plan_state(resolve(deskmeet, schema.packaged()), deskmeet)
    assert split["roles"]["encode"]["node"] == "/dev/dri/renderD129"
    assert split["roles"]["decode"]["node"] == "/dev/dri/renderD128"
    assert split["audio"]["interface"] is None


def test_the_rig_carries_the_section_and_routes_the_action() -> None:
    rig = Rig.demo()
    rig.start()
    try:
        assert rig.snapshot()["hardware"]["rig"]["id"] == "studio-wave-xlr"
        rig.act("hardware.demo_host", {"host": "deskmeet1"})
        assert rig.snapshot()["hardware"]["rig"]["id"] == "studio-deck"
        with pytest.raises(ActionError):
            rig.act("hardware.demo_host", {"host": "nope"})
    finally:
        rig.stop()


@pytest.mark.parametrize("host", HOSTS)
def test_the_packaged_recording_is_the_trimmed_fixture(host: str) -> None:
    fixture = (FIXTURES / host / "inventory.txt").read_text(encoding="utf-8")
    packaged = (inventory.recordings_dir() / f"{host}.txt").read_text(encoding="utf-8")
    assert packaged == inventory.trim(fixture), "regenerate the recording: it must be the fixture trimmed"
    assert inventory.snapshot_from_inventory(packaged) == inventory.snapshot_from_inventory(fixture)


def test_every_recorded_host_has_a_recording() -> None:
    assert inventory.packaged_hosts() == sorted(HOSTS)
    with pytest.raises(KeyError):
        inventory.packaged_snapshot("nowhere")


def test_the_migrated_catalogue_keeps_the_drivers_the_console_had() -> None:
    assert [(p.id, p.usb_id, p.family, p.generation) for p in profiles.PROFILES] == [
        ("scarlett-2i2-gen4", "1235:8219", "scarlett", "gen4"),
        ("scarlett-2i2-gen3", "1235:8210", "scarlett", "gen3"),
        ("wave-xlr", "0fd9:007d", "wave-xlr", ""),
    ]
    assert profiles.BY_ID["wave-xlr"].label == "Elgato Wave XLR"
    assert profiles.BY_ID["scarlett-2i2-gen4"].label == "Focusrite Scarlett 2i2 (4th Gen)"
    assert profiles.BY_ID["scarlett-2i2-gen3"].summary.startswith("Air, 48V, Line/Inst")


def _cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, machine: FakeMachine, *args: str) -> int:
    from streamstack import cli
    from streamstack.hardware import probe

    env_file = tmp_path / "streamstack.env"
    env_file.write_text("")
    monkeypatch.setenv("STREAMSTACK_ENV_FILE", str(env_file))
    monkeypatch.delenv("STREAMSTACK_RIG", raising=False)
    taken = machine.snapshot()  # before the patch: FakeMachine.snapshot() itself calls probe.snapshot
    monkeypatch.setattr(probe, "snapshot", lambda *a, **k: taken)
    return cli.main(["hardware", *args])


def test_cli_prints_the_plan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, machine: FakeMachine, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(monkeypatch, tmp_path, machine) == 0
    out = capsys.readouterr().out
    assert "rig: " in out and "studio-wave-xlr" in out and "/dev/dri/renderD128" in out and "ABC12345" not in out


def test_cli_json_is_the_console_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, machine: FakeMachine, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(monkeypatch, tmp_path, machine, "--json") == 0
    state = json.loads(capsys.readouterr().out)
    assert state["rig"]["id"] == "studio-wave-xlr" and state["audio"]["rate"] == 48000


def test_cli_identity_prints_only_the_hash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, machine: FakeMachine, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(monkeypatch, tmp_path, machine, "--identity") == 0
    out = capsys.readouterr().out.strip()
    assert len(out) == 32 and set(out) <= set("0123456789abcdef")
    assert out != "0123456789abcdef0123456789abcdef"  # a hash, never the machine id itself
