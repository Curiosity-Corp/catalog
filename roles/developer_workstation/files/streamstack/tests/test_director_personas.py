"""Persona files: the schema, the shipped set, the person's own edits."""

from __future__ import annotations

import copy
import json

import pytest

from streamstack import config
from streamstack.director import limits, personas, settings

SHIPPED = {"executive", "bright-podcast", "late-night-calm", "daylight-natural", "crisp-newsroom"}


def shipped(persona_id: str = "executive") -> dict:
    return json.loads((personas.shipped_dir() / f"{persona_id}.json").read_text())


def test_the_shipped_personas_all_load_without_problems() -> None:
    library = personas.load_library()
    assert library.problems == []
    assert set(library.personas) >= SHIPPED
    assert settings.DEFAULT_PERSONA in library.personas
    assert {p["source"] for p in library.listing()} == {"shipped"}


@pytest.mark.parametrize("persona_id", sorted(SHIPPED))
def test_every_shipped_persona_stays_inside_the_hard_limits_and_names_a_real_voice_preset(persona_id: str) -> None:
    persona = personas.validate(shipped(persona_id))
    for key, value in persona["picture"]["look"].items():
        low, high = limits.LIMITS[key]
        assert low <= value <= high, key
    face, frame = persona["picture"]["face_luma"], persona["picture"]["frame_luma"]
    assert limits.FACE_TARGET[0] <= face[0] < face[1] <= limits.FACE_TARGET[1] + 20
    assert 0 < frame[0] < frame[1] < 255
    presets = {p.stem for p in (config.data_dir() / "easyeffects/input").glob("*.json")}
    assert persona["voice"]["preset"] in presets | {""}
    assert persona["id"] == persona_id and len(persona["mood"]) >= 3 and len(persona["swatch"]) == 2


def test_the_executive_is_warmer_than_the_daylight_persona() -> None:
    executive, daylight = shipped("executive"), shipped("daylight-natural")
    assert executive["picture"]["warmth_k"] < daylight["picture"]["warmth_k"]
    assert executive["picture"]["look"]["warmth"] > daylight["picture"]["look"]["warmth"]


def test_validation_names_the_field_that_is_wrong() -> None:
    for path, mutate, message in (
        ("picture.look.warmth", lambda p: p["picture"]["look"].update(warmth=2), "picture.look.warmth"),
        ("picture.face_luma", lambda p: p["picture"].update(face_luma=[200, 100]), "face_luma"),
        ("id", lambda p: p.update(id="Bad Id!"), "id"),
        ("mood", lambda p: p.update(mood=[]), "mood"),
        ("voice.targets", lambda p: p["voice"].update(targets=[1]), "targets"),
        ("swatch", lambda p: p.update(swatch=["red", "#fff"]), "swatch"),
        ("name", lambda p: p.update(name=""), "name"),
        ("schema", lambda p: p.update(schema=9), "schema"),
    ):
        broken = copy.deepcopy(shipped())
        mutate(broken)
        with pytest.raises(personas.PersonaError) as error:
            personas.validate(broken)
        assert message in str(error.value), path


def test_non_objects_and_non_numbers_are_refused() -> None:
    for junk in (None, [], "executive", 3):
        with pytest.raises(personas.PersonaError):
            personas.validate(junk)
    broken = shipped()
    broken["picture"]["look"]["contrast"] = float("nan")
    with pytest.raises(personas.PersonaError):
        personas.validate(broken)
    broken["picture"]["look"]["contrast"] = True
    with pytest.raises(personas.PersonaError):
        personas.validate(broken)


def test_unknown_keys_are_dropped_rather_than_trusted() -> None:
    raw = shipped()
    raw["exec"] = "rm -rf"
    raw["picture"]["shell"] = "x"
    clean = personas.validate(raw)
    assert "exec" not in clean and "shell" not in clean["picture"]


def test_a_user_edit_overlays_a_shipped_persona_and_can_be_undone() -> None:
    edited = shipped()
    edited["tagline"] = "Mine"
    personas.save_user(edited)
    assert personas.user_dir().joinpath("executive.json").stat().st_mode & 0o022 == 0
    library = personas.load_library()
    assert library.personas["executive"]["tagline"] == "Mine" and library.personas["executive"]["source"] == "edited"
    assert personas.remove_user("executive") is True
    assert personas.load_library().personas["executive"]["source"] == "shipped"
    assert personas.remove_user("executive") is False


def test_a_custom_persona_is_marked_custom_and_a_damaged_file_is_reported_not_fatal() -> None:
    custom = shipped()
    custom.update(id="my-look", name="My look")
    personas.save_user(custom)
    (personas.user_dir() / "broken.json").write_text("{nope")
    wrong = shipped()
    wrong["id"] = "other"
    (personas.user_dir() / "mismatch.json").write_text(json.dumps(wrong))
    library = personas.load_library()
    assert library.personas["my-look"]["source"] == "custom"
    assert len(library.problems) == 2 and any("broken.json" in p for p in library.problems)
    assert library.get("missing")["id"] == settings.DEFAULT_PERSONA


def test_remove_user_refuses_a_path_instead_of_an_id() -> None:
    with pytest.raises(personas.PersonaError):
        personas.remove_user("../../etc/passwd")
