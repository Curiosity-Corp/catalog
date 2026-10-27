"""The director on the Console: panel state, actions, the demo twin, the sheet endpoint, journal fields, the CLI."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from streamstack import cli
from streamstack.console import director, studio
from streamstack.console.errors import ActionError
from streamstack.console.rig import Rig
from streamstack.console.studio import DemoStudio
from streamstack.director import commands, personas, settings, status

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


class Panel(director.DirectorPanel):
    def _now(self) -> datetime:
        return NOW


def persona_doc(persona_id: str = "executive") -> dict:
    return json.loads((personas.shipped_dir() / f"{persona_id}.json").read_text())


# -- the file-backed panel ------------------------------------------------------------------------------


def test_state_has_everything_the_page_draws() -> None:
    state = Panel({}).state()
    assert state["persona"] == settings.DEFAULT_PERSONA
    assert {p["id"] for p in state["personas"]} >= {
        "executive",
        "bright-podcast",
        "late-night-calm",
        "daylight-natural",
    }
    assert state["time"]["sun"]["slot"] in {"night", "dawn", "morning", "afternoon", "evening"}
    assert state["time"]["strip"][0]["start"] == 0 and state["learned"] == {} and state["sheet"] is None
    assert state["report"] == {}


def test_state_is_cached_until_something_changes() -> None:
    panel = Panel({})
    first = panel.state()
    assert panel.state() is first
    panel.act("auto.persona", {"id": "executive"})
    again = panel.state()
    assert again is not first and again["persona"] == "executive"


def test_the_sheet_and_the_report_appear_once_a_pass_has_run(tmp_path: Path) -> None:
    sheet = tmp_path / "sheet.jpg"
    sheet.write_bytes(b"\xff\xd8jpeg\xff\xd9")
    status.publish_sheet(sheet)
    status.write({"time": "2026-10-22T12:00", "findings": []})
    panel = Panel({})
    assert panel.sheet() == b"\xff\xd8jpeg\xff\xd9"
    state = panel.state()
    assert state["sheet"] and state["report"]["time"] == "2026-10-22T12:00"


def test_choosing_a_persona() -> None:
    panel = Panel({})
    assert panel.act("auto.persona", {"id": "late-night-calm"}) == "Persona: Late-night calm"
    assert settings.load()["persona"] == "late-night-calm"
    with pytest.raises(ActionError) as err:
        panel.act("auto.persona", {"id": "nobody"})
    assert err.value.status == 404
    with pytest.raises(ActionError) as err:
        panel.act("auto.persona", {})
    assert err.value.status == 400


def test_saving_an_edited_persona_validates_it_and_reset_restores_the_original() -> None:
    panel = Panel({})
    edited = persona_doc()
    edited["name"] = "Executive, my way"
    assert panel.act("auto.persona_save", {"persona": edited}) == "Saved Executive, my way"
    listing = {p["id"]: p for p in panel.state()["personas"]}
    assert listing["executive"]["name"] == "Executive, my way" and listing["executive"]["source"] == "edited"
    assert panel.act("auto.persona_reset", {"id": "executive"}) == "Persona restored"
    assert {p["id"]: p for p in panel.state()["personas"]}["executive"]["name"] == "Executive"


def test_a_bad_persona_is_a_400_that_names_the_field() -> None:
    bad = persona_doc()
    bad["picture"]["face_luma"] = [200, 100]
    with pytest.raises(ActionError) as err:
        Panel({}).act("auto.persona_save", {"persona": bad})
    assert err.value.status == 400 and "face_luma" in str(err.value)
    with pytest.raises(ActionError) as err:
        Panel({}).act("auto.persona_save", {"persona": "text"})
    assert err.value.status == 400


def test_reset_with_nothing_to_undo_is_a_409_and_a_custom_persona_can_be_deleted() -> None:
    panel = Panel({})
    with pytest.raises(ActionError) as err:
        panel.act("auto.persona_reset", {"id": "executive"})
    assert err.value.status == 409
    mine = persona_doc()
    mine["id"], mine["name"] = "mine", "Mine"
    panel.act("auto.persona_save", {"persona": mine})
    assert panel.act("auto.persona_reset", {"id": "mine"}) == "Persona deleted"
    assert "mine" not in {p["id"] for p in panel.state()["personas"]}


def test_location_is_validated_saved_and_cleared() -> None:
    panel = Panel({})
    assert (
        panel.act("auto.location", {"name": "Lake house", "latitude": 44.0, "longitude": -79.5})
        == "Property location saved"
    )
    place = panel.state()["time"]["location"]
    assert (place["name"], place["latitude"], place["longitude"]) == ("Lake house", 44.0, -79.5)
    for bad in (
        {"name": "x", "latitude": 99, "longitude": 0},
        {"name": "x", "latitude": "north", "longitude": 0},
        {"name": 5},
    ):
        with pytest.raises(ActionError) as err:
            panel.act("auto.location", bad)
        assert err.value.status == 400
    panel.act("auto.location", {"clear": True})
    assert panel.state()["time"]["location"]["source"] != "settings"


def test_unwritable_settings_are_a_500_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise settings.SettingsError("disk full")

    monkeypatch.setattr(settings, "set_persona", refuse)
    with pytest.raises(ActionError) as err:
        Panel({}).act("auto.persona", {"id": "executive"})
    assert err.value.status == 500 and "disk full" in str(err.value)


def test_safe_state_reports_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    panel = Panel({})

    def broken() -> dict:
        raise OSError("unreadable")

    monkeypatch.setattr(panel, "_status", broken)
    assert "unreadable" in director.safe_state(panel)["error"]


# -- the demo twin ------------------------------------------------------------------------------------------


def test_the_demo_panel_touches_no_files(isolated_home: Path) -> None:
    panel = director.DemoPanel()
    panel.act("auto.persona", {"id": "executive"})
    edited = persona_doc()
    edited["tagline"] = "Edited in the demo"
    panel.act("auto.persona_save", {"persona": edited})
    panel.act("auto.location", {"name": "Demo", "latitude": 10, "longitude": 20})
    state = panel.state()
    assert state["persona"] == "executive" and state["time"]["location"]["name"] == "Demo"
    assert {p["id"]: p for p in state["personas"]}["executive"]["tagline"] == "Edited in the demo"
    assert state["report"]["persona"] == "executive" and state["report"]["findings"] and state["learned"]
    assert not list(isolated_home.rglob("*.json")), "the demo wrote a file"
    assert panel.sheet()[:2] == b"\xff\xd8"


def test_the_demo_report_uses_real_finding_ids_and_lists_the_hardware_actuators_as_ready() -> None:
    report = director.demo_report(personas.load_library().get("executive"), "morning")
    assert {f["id"] for f in report["findings"]} <= {"face-exposure", "white-balance", "voice-ok"}
    rows = {a["id"]: a for a in report["actuators"]}
    assert {"canon-exposure", "litra", "interface-gain", "face-finder"} <= set(rows)
    assert all(a["available"] and a["kind"] != "planned" for a in rows.values())
    assert {c["actuator"] for c in report["changes"]} >= {"canon-exposure", "litra"}
    assert report["audio"]["features"]["scores"]["clean"] > 0


def test_the_demo_studio_carries_the_director_and_answers_its_actions() -> None:
    demo = DemoStudio(delay=0, clock=lambda: 0.0)
    assert demo.state()["auto"]["director"]["persona"] == settings.DEFAULT_PERSONA
    demo.act("auto.persona", {"id": "bright-podcast"})
    assert demo.state()["auto"]["director"]["persona"] == "bright-podcast"
    assert demo.sheet()[:2] == b"\xff\xd8"
    entries = demo.state()["auto"]["journal"]
    assert any(e["event"] == "change" and e["reason"] and e["change"]["key"] for e in entries)
    assert next(e for e in entries if e["event"] == "ai")["persona"]
    total = demo.state()["auto"]["journal_total"]
    for _ in range(12):  # the list is capped, the counter the e2e specs wait on is not
        demo.act("auto.once", {})
    assert demo.state()["auto"]["journal_total"] == total + 24


def test_the_rig_exposes_the_sheet(tmp_path: Path) -> None:
    class Quiet:
        def __getattr__(self, name: str) -> object:
            return lambda *a, **k: {}

    rig = Rig(Quiet(), Quiet(), DemoStudio(delay=0, clock=lambda: 0.0))
    assert rig.director_sheet()[:2] == b"\xff\xd8"
    assert Rig(Quiet(), Quiet(), Quiet()).director_sheet() == {}


# -- the journal and the command line ------------------------------------------------------------------------


def test_a_change_event_reaches_the_page_with_its_reason() -> None:
    entry = {
        "time": "2026-10-22T09:00:00",
        "event": "change",
        "actuator": "program-look",
        "target": "Cam",
        "key": "warmth",
        "before": 0.0,
        "after": 0.1,
        "reason": "Skin read cool.",
        "source": "claude",
        "kind": "video",
    }
    out = studio.journal_entry(entry)
    assert out["kind"] == "video" and out["reason"] == "Skin read cool."
    assert out["change"]["key"] == "warmth" and "warmth" in out["text"]


def test_an_override_event_reads_plainly() -> None:
    text = cli.describe({"event": "override", "keys": ["contrast"], "hold_minutes": 30})
    assert "contrast" in text and "30" in text


def test_the_persona_command_lists_and_chooses(capsys: pytest.CaptureFixture[str]) -> None:
    assert commands.persona([]) == 0
    assert "executive" in capsys.readouterr().out
    assert commands.persona(["executive"]) == 0 and settings.load()["persona"] == "executive"
    assert commands.persona(["nobody"]) == 1


def test_the_property_command_sets_shows_and_clears(capsys: pytest.CaptureFixture[str]) -> None:
    assert commands.prop(["Studio", "40.7", "-74.0"]) == 0
    out = capsys.readouterr().out
    assert "Studio" in out and "sunrise" in out
    assert commands.prop(["Studio", "400", "0"]) == 1
    assert commands.prop(["only-a-name"]) == 1
    assert commands.prop(["clear"]) == 0
