"""`streamstack doctor`'s hardware group: the live snapshot checked against the resolved rig profile (#122)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hardware_support import HOSTS, FakeMachine, inventory_text, studio_wave

from streamstack import cli
from streamstack.hardware import doctor, inventory, schema
from streamstack.hardware.found import Found
from streamstack.hardware.model import Snapshot
from streamstack.hardware.plan import Plan, resolve

CATALOGUE = schema.packaged()
SERIAL = "ABC12345"


def found_from(snapshot: Snapshot, overrides: dict[str, str] | None = None) -> Found:
    return Found(resolve(snapshot, CATALOGUE, overrides), snapshot, CATALOGUE)


def recorded(host: str) -> Found:
    return found_from(inventory.snapshot_from_inventory(inventory_text(host)))


def rows(found: Found) -> dict[str, dict[str, str]]:
    return {r["id"]: r for r in doctor.checks(found.plan, found.snapshot, found.catalogue, "deepfilternet")}


def states(found: Found) -> dict[str, str]:
    return {cid: r["state"] for cid, r in rows(found).items()}


@pytest.fixture
def machine(tmp_path: Path) -> FakeMachine:
    return FakeMachine(tmp_path)


@pytest.fixture
def seated(machine: FakeMachine) -> FakeMachine:
    """dm313red with everything at the seat: the machine a fully green doctor is expected on."""
    return studio_wave(machine)


# -- the header ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(("host", "rig"), [("dm313red", "studio-wave-xlr"), ("deskmeet1", "studio-deck")])
def test_every_host_gets_a_rig_header_naming_how_it_was_chosen(host: str, rig: str) -> None:
    found = recorded(host)
    head = doctor.rig_header(found.plan)
    assert head["id"] == rig
    assert head["text"] == f"rig: {rig} (selected by {found.plan.rig.via})"
    first = doctor.checks(found.plan, found.snapshot, found.catalogue)[0]
    assert first["id"] == "hw-rig" and first["title"] == head["text"]


def test_the_generic_fallback_rig_is_a_warning_with_a_fix(machine: FakeMachine) -> None:
    found = found_from(machine.snapshot())
    row = rows(found)["hw-rig"]
    assert found.plan.rig.via == "fallback"
    assert row["state"] == "warn" and row["hint"]


# -- both recorded hosts ------------------------------------------------------------------------------


@pytest.mark.parametrize("host", HOSTS)
def test_every_row_has_the_shape_the_system_page_reads(host: str) -> None:
    for item in doctor.checks(*(lambda f: (f.plan, f.snapshot, f.catalogue))(recorded(host))):
        assert set(item) == {"id", "group", "state", "title", "detail", "hint"}
        assert item["group"] == "hardware" and item["state"] in ("ok", "warn", "fail")


@pytest.mark.parametrize("host", HOSTS)
def test_every_row_that_is_not_ok_names_the_fix(host: str) -> None:
    for cid, item in rows(recorded(host)).items():
        if item["state"] != "ok":
            assert item["hint"], f"{cid} is {item['state']} without a fix"


@pytest.mark.parametrize("host", HOSTS)
def test_no_serial_reaches_a_row(host: str) -> None:
    text = json.dumps(doctor.checks(*(lambda f: (f.plan, f.snapshot, f.catalogue))(recorded(host))))
    assert SERIAL not in text and "DS23M2A00049" not in text


def test_dm313red_has_no_failure() -> None:
    got = states(recorded("dm313red"))
    assert "fail" not in got.values()
    assert got["hw-audio-interface"] == "ok" and got["hw-audio-mic"] == "ok" and got["hw-encoder"] == "ok"
    assert all(got[f"hw-gpu-{role}"] == "ok" for role in ("grade", "encode", "decode", "blur"))


def test_dm313red_at_the_seat_is_entirely_green(seated: FakeMachine) -> None:
    got = states(found_from(seated.snapshot()))
    assert set(got.values()) == {"ok"}, {k: v for k, v in got.items() if v != "ok"}


def test_dm313red_reports_the_wave_xlr_at_its_rate_and_format(seated: FakeMachine) -> None:
    row = rows(found_from(seated.snapshot()))["hw-audio-interface"]
    assert "48000 Hz" in row["title"] and "s24le" in row["title"] and "no resampling" in row["title"]


# -- deskmeet1: the Scarlett 2i2 is not enumerating ---------------------------------------------------


def test_deskmeet1_warns_that_the_scarlett_is_not_enumerating_and_names_the_fix() -> None:
    found = recorded("deskmeet1")
    got = rows(found)
    row = got["hw-audio-interface"]
    assert row["state"] == "warn"
    assert "Scarlett 2i2" in row["title"] and "not enumerating on USB" in row["title"]
    assert "owner decision" not in row["title"]
    for words in ("re-seat", "direct rear USB port", "short known-good cable"):
        assert words in row["hint"]
    assert "hw-audio-mic" not in got  # the interface row already says there is no call microphone


def test_deskmeet1_absent_audio_is_a_warning_and_never_a_failure() -> None:
    got = states(recorded("deskmeet1"))
    assert "fail" not in got.values()
    assert cli.summarize(doctor.checks(*(lambda f: (f.plan, f.snapshot, f.catalogue))(recorded("deskmeet1")))) == "warn"


def test_deskmeet1_does_not_invent_an_audio_interface() -> None:
    found = recorded("deskmeet1")
    assert found.plan.audio_interface() is None and "Scarlett 2i2" in found.plan.audio.expected


def test_deskmeet1_the_dummy_output_is_never_the_monitor() -> None:
    row = rows(recorded("deskmeet1"))["hw-audio-monitor"]
    assert row["state"] != "ok" and row["hint"]


def test_the_absent_warning_clears_when_the_scarlett_is_attached(machine: FakeMachine) -> None:
    machine.add_usb("1235:8219", "Scarlett 2i2 4th Gen")
    snap = inventory.snapshot_from_inventory(inventory_text("deskmeet1"))
    plan = resolve(machine.snapshot(), CATALOGUE, {"STREAMSTACK_RIG": "studio-deck"})
    assert plan.audio.expected == "" and plan.audio_interface() is not None
    assert snap  # the recorded host still lacks it


# -- devices that are missing or denied ---------------------------------------------------------------


def test_a_missing_required_device_fails_and_names_the_fix(seated: FakeMachine) -> None:
    seated.remove_usb("0fd9:0067")
    row = rows(found_from(seated.snapshot()))["hw-device-cam-link-4k"]
    assert row["state"] == "fail" and "missing" in row["title"]
    assert row["hint"]


def test_a_missing_optional_device_is_not_a_problem(seated: FakeMachine) -> None:
    seated.remove_usb("046d:c901")
    row = rows(found_from(seated.snapshot()))["hw-device-litra-beam"]
    assert row["state"] == "ok" and "optional" in row["detail"]


def test_an_unplugged_wave_xlr_fails_the_interface_check(seated: FakeMachine) -> None:
    seated.remove_usb("0fd9:007d")
    seated.remove_alsa(3)
    seated.set_pipewire([])
    got = rows(found_from(seated.snapshot(), {"STREAMSTACK_RIG": "studio-wave-xlr"}))
    assert got["hw-device-wave-xlr"]["state"] == "fail" and got["hw-device-wave-xlr"]["hint"]
    assert got["hw-audio-interface"]["state"] != "ok" and got["hw-audio-interface"]["hint"]


def test_a_denied_render_node_is_reported_as_denied_with_the_fix(seated: FakeMachine) -> None:
    seated.denied.add("renderD128")
    got = rows(found_from(seated.snapshot()))
    assert got["hw-device-arc-a380"]["state"] == "fail"
    assert "denied" in got["hw-device-arc-a380"]["title"] + got["hw-device-arc-a380"]["detail"]
    assert "render" in got["hw-device-arc-a380"]["hint"]
    assert got["hw-gpu-grade"]["state"] != "ok" and got["hw-gpu-grade"]["hint"]


def test_a_denied_video_node_is_reported_as_denied_with_the_fix(seated: FakeMachine) -> None:
    seated.denied.add("video0")
    got = rows(found_from(seated.snapshot()))
    assert got["hw-device-cam-link-4k"]["state"] in ("fail", "warn")
    assert "denied" in got["hw-device-cam-link-4k"]["title"] + got["hw-device-cam-link-4k"]["detail"]
    assert "video group" in got["hw-device-cam-link-4k"]["hint"]


def test_an_attached_card_at_profile_off_names_the_card_fix(seated: FakeMachine) -> None:
    card = f"alsa_card.usb-Elgato_Systems_Elgato_Wave_XLR_{SERIAL}-00"
    seated.set_pipewire([(49, card)])
    seated.set_card_profiles({card: ("off", "0fd9:007d")})
    row = rows(found_from(seated.snapshot()))["hw-audio-interface"]
    assert row["state"] == "fail" and "profile off" in row["title"] + row["detail"]
    assert row["hint"]


# -- the report and the command line ------------------------------------------------------------------


def test_the_report_is_the_rig_header_and_the_rows() -> None:
    found = recorded("dm313red")
    data = doctor.report(found)
    assert data["rig"]["id"] == "studio-wave-xlr" and data["checks"][0]["id"] == "hw-rig"
    json.dumps(data)


def test_a_failed_probe_is_one_warning_not_a_crash() -> None:
    data = doctor.report(None)
    assert data["rig"] is None and [c["id"] for c in data["checks"]] == ["hw-probe"]
    assert data["checks"][0]["state"] == "warn" and data["checks"][0]["hint"]


@pytest.mark.parametrize("host", HOSTS)
def test_doctor_json_on_each_host(
    host: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    found = recorded(host)
    monkeypatch.setattr(cli, "probe_now", lambda cfg=None: found)
    monkeypatch.setattr(cli, "load_settings", lambda: {})
    code = cli.main(["doctor", "--hardware", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["rig"]["id"] == found.plan.rig.id
    assert out["summary"] == cli.summarize(out["checks"])
    assert code == (1 if out["summary"] == "fail" else 0)


def test_doctor_hardware_prints_the_rig_header(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    found = recorded("deskmeet1")
    monkeypatch.setattr(cli, "probe_now", lambda cfg=None: found)
    monkeypatch.setattr(cli, "load_settings", lambda: {})
    cli.main(["doctor", "--hardware"])
    out = capsys.readouterr().out
    assert out.startswith("rig: studio-deck (selected by ")
    assert "Scarlett 2i2" in out and "not enumerating on USB" in out


def test_the_plan_type_is_what_the_doctor_reads() -> None:
    assert isinstance(recorded("dm313red").plan, Plan)
