"""Time-of-day profiles: the slot clock, profile validation and the settings file."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path

import pytest

from streamstack import slots
from streamstack.director import daylight

NEW_YORK = daylight.Location("New York", 40.71, -74.01, "settings")
EDT = timezone(timedelta(hours=-4))


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 6, 21, hour, minute, tzinfo=EDT)


@pytest.mark.parametrize(
    ("hour", "minute", "slot"),
    [
        (3, 0, "night"),
        (5, 30, "dawn"),
        (8, 0, "morning"),
        (12, 0, "midday"),
        (17, 0, "afternoon"),
        (20, 0, "evening"),
        (23, 0, "night"),
    ],
)
def test_the_slot_follows_the_sun(hour: int, minute: int, slot: str) -> None:
    assert slots.slot_at(at(hour, minute), NEW_YORK)[0] == slot


def test_midday_is_a_profile_slot_that_maps_to_the_directors_morning_or_afternoon() -> None:
    assert slots.director_slot("midday", -10.0) == "morning"
    assert slots.director_slot("midday", 10.0) == "afternoon"
    assert slots.director_slot("dawn", 0.0) == "dawn"


def test_the_strip_covers_the_whole_day_in_order() -> None:
    strip = slots.strip(at(12), NEW_YORK)
    assert strip[0]["start"] == 0 and strip[-1]["end"] == 1440
    assert all(a["end"] == b["start"] for a, b in pairwise(strip))
    assert {s["slot"] for s in strip} <= set(slots.SLOTS)
    assert [s["slot"] for s in strip][:3] == ["night", "dawn", "morning"]


def test_a_profile_keeps_only_known_well_formed_parts() -> None:
    clean = slots.clean_profile(
        {
            "look": " Warm Studio ",
            "canon": {"exposure_comp": "0.33", "kelvin": 5640},
            "blur": {"on": True},
            "lighting": "Soft",
        }
    )
    assert clean == {
        "look": "Warm Studio",
        "canon": {"exposure_comp": "0.33", "kelvin": 5600},
        "lighting": "Soft",
        "blur": {"on": True},
    }
    assert list(clean) == [p for p in slots.PARTS if p in clean]  # stored in the order the engine applies them


@pytest.mark.parametrize(
    "bad",
    [
        "look",
        {"colour": "x"},
        {"look": ""},
        {"look": "x" * 200},
        {"look": "a\nb"},
        {"canon": {}},
        {"canon": {"kelvin": 100}},
        {"canon": {"kelvin": True}},
        {"canon": {"iso": 100}},
        {"blur": {"on": "yes"}},
        {"blur": {"on": True, "strength": 0}},
        {"blur": {"on": True, "strength": 1.5}},
        {"framing": "wide"},
        {"framing": {"shot": "panorama"}},
        {"framing": {"shot": "wide", "mode": "tight"}},
        {"framing": {}},
    ],
)
def test_a_bad_profile_is_refused_with_a_reason(bad: object) -> None:
    with pytest.raises(slots.SlotsError):
        slots.clean_profile(bad)


def test_modes_slots_and_fade_are_checked() -> None:
    assert slots.check_mode("suggest") == "suggest"
    assert slots.check_slot("dawn") == "dawn"
    assert slots.check_fade(2.04) == 2.0
    for call, value in (
        (slots.check_mode, "on"),
        (slots.check_slot, "noon"),
        (slots.check_fade, -1),
        (slots.check_fade, "4"),
        (slots.check_fade, float("nan")),
        (slots.check_fade, True),
        (slots.check_fade, 31),
    ):
        with pytest.raises(slots.SlotsError):
            call(value)


def test_the_store_round_trips_and_writes_a_private_file(tmp_path: Path) -> None:
    path = tmp_path / "slot-profiles.json"
    store = slots.Store(path)
    assert store.load() == slots.defaults()
    store.set_mode("auto")
    store.set_fade(6)
    store.set_profile("dawn", {"look": "Natural", "lighting": "Soft"})
    again = slots.Store(path).load()
    assert again["mode"] == "auto" and again["fade_seconds"] == 6.0
    assert again["profiles"] == {"dawn": {"look": "Natural", "lighting": "Soft"}}
    assert path.stat().st_mode & 0o777 == 0o600
    store.set_profile("dawn", {})  # an empty profile removes the slot
    assert store.load()["profiles"] == {}


def test_a_damaged_or_hand_edited_file_never_breaks_loading(tmp_path: Path) -> None:
    path = tmp_path / "slot-profiles.json"
    path.write_text(
        json.dumps(
            {
                "mode": "loud",
                "fade_seconds": "x",
                "profiles": {"noon": {"look": "A"}, "dawn": {"look": ""}, "night": {"look": "B"}},
            }
        )
    )
    data = slots.Store(path).load()
    assert data["mode"] == "off" and data["fade_seconds"] == slots.DEFAULT_FADE
    assert data["profiles"] == {"night": {"look": "B"}}
    path.write_text("{not json")
    assert slots.Store(path).load() == slots.defaults()


def test_starter_fills_only_the_empty_slots(tmp_path: Path) -> None:
    store = slots.Store(tmp_path / "s.json")
    store.set_profile("morning", {"look": "Mine"})
    data, filled = store.fill_starter()
    assert "morning" not in filled and set(filled) == set(slots.SLOTS) - {"morning"}
    assert data["profiles"]["morning"] == {"look": "Mine"}
    assert store.fill_starter()[1] == []


def test_export_and_import_carry_the_document(tmp_path: Path) -> None:
    a, b = slots.Store(tmp_path / "a.json"), slots.Store(tmp_path / "b.json")
    a.set_mode("suggest")
    a.set_profile("evening", {"look": "Golden Hour", "blur": {"on": False}})
    document = a.export_document()
    assert document["document"] == slots.DOCUMENT
    b.import_document(document)
    assert b.load()["profiles"] == a.load()["profiles"]
    assert b.load()["mode"] == "off"  # the mode is the person's choice on each machine
    with pytest.raises(slots.SlotsError):
        b.import_document({"document": "other/1"})
    with pytest.raises(slots.SlotsError):
        b.import_document("nope")


def test_a_profile_can_hold_a_framing_shot() -> None:
    assert slots.clean_profile({"framing": {"shot": "wide"}, "blur": {"on": True}}) == {
        "blur": {"on": True},
        "framing": {"shot": "wide"},
    }
    assert slots.clean_profile({"framing": {"shot": "auto"}}) == {"framing": {"shot": "auto"}}
    assert slots.PART_LABELS["framing"] == "Shot"
