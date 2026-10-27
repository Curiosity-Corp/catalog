"""Lighting automation settings and the daylight schedule: defaults, validation, curve, clamps, the settings file."""

from __future__ import annotations

import itertools
import json
import os
import stat
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from streamstack import lighting, lightplan
from streamstack.director import daylight

BERLIN = daylight.Location("Berlin", 52.52, 13.405, "settings")
UTC = UTC


def at(hour: int, minute: int = 0, month: int = 6) -> datetime:
    return datetime(2026, month, 21, hour, minute, tzinfo=timezone(timedelta(hours=2 if month == 6 else 1)))


@pytest.fixture
def store(tmp_path: Path) -> lightplan.Store:
    return lightplan.Store(tmp_path / "lighting-automation.json")


def test_defaults_cover_camera_follow_and_the_schedule() -> None:
    data = lightplan.defaults()
    assert data["follow"]["enabled"] and data["follow"]["turn_off"]
    assert all(data["follow"][source] for source in lightplan.FOLLOW_SOURCES)
    assert data["schedule"]["enabled"] and data["schedule"]["mode"] == "sun"
    assert set(data["schedule"]["slots"]) == set(daylight.SLOTS)
    assert data["schedule"]["hours"] is None


def test_slot_defaults_follow_the_directors_light_priors() -> None:
    slots = lightplan.defaults()["schedule"]["slots"]
    # the evening is warmer and the night dimmer than the morning, as the Director's priors have it
    assert slots["evening"]["kelvin"] < slots["morning"]["kelvin"]
    for slot in slots.values():
        assert 1 <= slot["brightness"] <= 100
        assert lighting.KELVIN_MIN <= slot["kelvin"] <= lighting.KELVIN_MAX


def test_a_missing_file_gives_the_defaults(store: lightplan.Store) -> None:
    assert store.load() == lightplan.defaults()


def test_saved_settings_are_mode_600_and_round_trip(store: lightplan.Store) -> None:
    store.update("follow", {"grace_seconds": 45, "ignore": ["Slack"]})
    mode = stat.S_IMODE(os.stat(store.file()).st_mode)
    assert mode == 0o600
    again = lightplan.Store(store.file()).load()
    assert again["follow"]["grace_seconds"] == 45
    assert again["follow"]["ignore"] == ["Slack"]


def test_numbers_are_clamped_to_their_limits(store: lightplan.Store) -> None:
    data = store.update("follow", {"grace_seconds": 99999, "start_delay_seconds": -4})
    assert data["follow"]["grace_seconds"] == lightplan.LIMITS["grace_seconds"][1]
    assert data["follow"]["start_delay_seconds"] == 0


@pytest.mark.parametrize(
    ("section", "patch", "message"),
    [
        ("follow", {"enabled": "yes"}, "true or false"),
        ("follow", {"grace_seconds": "soon"}, "number"),
        ("follow", {"grace_seconds": True}, "number"),
        ("follow", {"colour": 1}, "Unknown"),
        ("follow", {"devices": ["video0"]}, "/dev/"),
        ("schedule", {"mode": "lunar"}, "mode"),
        ("schedule", {"fade_seconds": float("nan")}, "number"),
    ],
)
def test_bad_settings_are_refused_with_a_reason(
    store: lightplan.Store, section: str, patch: dict, message: str
) -> None:
    with pytest.raises(lightplan.PlanError, match=message):
        store.update(section, patch)


def test_unknown_section_is_refused(store: lightplan.Store) -> None:
    with pytest.raises(lightplan.PlanError, match="section"):
        store.update("weather", {})


def test_slots_validate_and_save(store: lightplan.Store) -> None:
    data = store.set_slot("evening", {"brightness": 35, "kelvin": 2900})
    assert data["schedule"]["slots"]["evening"] == {"brightness": 35, "kelvin": 2900}
    with pytest.raises(lightplan.PlanError, match="slot"):
        store.set_slot("teatime", {"brightness": 35})
    with pytest.raises(lightplan.PlanError, match="number"):
        store.set_slot("evening", {"brightness": "bright"})
    assert store.set_slot("evening", {"brightness": 0})["schedule"]["slots"]["evening"]["brightness"] == 1  # clamped


def test_hours_start_from_the_seed_and_validate(store: lightplan.Store) -> None:
    seed = [{"brightness": 50, "kelvin": 4800}] * 24
    data = store.set_hour(9, {"brightness": 80}, seed)
    assert data["schedule"]["hours"][9] == {"brightness": 80, "kelvin": 4800}
    assert data["schedule"]["hours"][10] == seed[10]
    for bad in (24, -1, "9", True):
        with pytest.raises(lightplan.PlanError, match="hour"):
            store.set_hour(bad, {"brightness": 80}, seed)


def test_light_settings_store_only_what_differs(store: lightplan.Store) -> None:
    store.set_light("a", {"name": "  Desk  ", "role": "fill", "max_brightness": 70})
    assert lightplan.light_settings(store.load(), "a")["name"] == "Desk"
    assert store.load()["lights"]["a"] == {"name": "Desk", "role": "fill", "max_brightness": 70}
    store.set_light("a", {"name": "", "role": "key", "max_brightness": 100})
    assert "a" not in store.load()["lights"]
    assert lightplan.light_settings(store.load(), "unknown")["role"] == "key"


def test_light_limits_must_not_cross(store: lightplan.Store) -> None:
    with pytest.raises(lightplan.PlanError, match="minimum"):
        store.set_light("a", {"min_brightness": 80, "max_brightness": 40})
    with pytest.raises(lightplan.PlanError, match=r"coolest|warmer"):
        store.set_light("a", {"min_kelvin": 6000, "max_kelvin": 3000})
    with pytest.raises(lightplan.PlanError, match="role"):
        store.set_light("a", {"role": "backlight"})
    with pytest.raises(lightplan.PlanError, match="name"):
        store.set_light("a", {"name": "x" * 41})


def test_reset_one_section_or_everything(store: lightplan.Store) -> None:
    store.update("follow", {"grace_seconds": 10})
    store.update("schedule", {"fade_seconds": 9})
    store.reset("follow")
    assert store.load()["follow"]["grace_seconds"] == lightplan.defaults()["follow"]["grace_seconds"]
    assert store.load()["schedule"]["fade_seconds"] == 9
    store.reset("all")
    assert store.load()["schedule"]["fade_seconds"] == lightplan.defaults()["schedule"]["fade_seconds"]
    with pytest.raises(lightplan.PlanError):
        store.reset("everything")


def test_resume_stamps_the_file(store: lightplan.Store) -> None:
    before = store.load()["resume"]
    assert store.resume()["resume"] > before


def test_a_hand_edited_bad_file_keeps_the_good_fields(store: lightplan.Store) -> None:
    store.file().write_text(
        json.dumps(
            {
                "follow": {"grace_seconds": 30, "turn_off": "banana", "mystery": 1},
                "schedule": {"fade_seconds": 7, "mode": "sideways", "hours": [1, 2, 3]},
                "lights": {"a": {"role": "fill"}, "b": "nonsense"},
            }
        )
    )
    data = store.load()
    assert data["follow"]["grace_seconds"] == 30
    assert data["follow"]["turn_off"] is True
    assert data["schedule"]["fade_seconds"] == 7
    assert data["schedule"]["mode"] == "sun"
    assert data["schedule"]["hours"] is None
    assert data["lights"] == {"a": {"role": "fill"}}


@pytest.mark.parametrize("text", ["", "not json", "[1, 2]", "null"])
def test_a_damaged_file_means_the_defaults(store: lightplan.Store, text: str) -> None:
    store.file().write_text(text)
    assert store.load() == lightplan.defaults()


def test_the_store_notices_a_changed_file(store: lightplan.Store) -> None:
    store.update("follow", {"grace_seconds": 10})
    assert store.load()["follow"]["grace_seconds"] == 10
    other = lightplan.Store(store.file())
    other.update("follow", {"grace_seconds": 20})
    assert store.load()["follow"]["grace_seconds"] == 20


def test_the_sun_schedule_gives_more_light_when_the_day_gives_less() -> None:
    data = lightplan.defaults()
    noon, night = lightplan.level_at(data, at(13), BERLIN), lightplan.level_at(data, at(2), BERLIN)
    assert night["brightness"] > noon["brightness"]  # the room is dark at night: the Director's priors
    assert night["kelvin"] < noon["kelvin"]


def test_the_sun_schedule_blends_slot_changes() -> None:
    data = lightplan.defaults()
    values = [lightplan.level_at(data, at(20, m), BERLIN)["brightness"] for m in range(0, 60, 10)]
    steps = [abs(b - a) for a, b in itertools.pairwise(values)]
    assert max(steps) <= abs(
        data["schedule"]["slots"]["afternoon"]["brightness"] - data["schedule"]["slots"]["evening"]["brightness"]
    )
    assert len(set(values)) > 1 or len(set(steps)) == 1  # a smooth ramp, never more than one slot apart


def test_editing_a_slot_moves_the_curve() -> None:
    data = lightplan.defaults()
    data["schedule"]["slots"]["afternoon"] = {"brightness": 91, "kelvin": 5200}
    assert lightplan.level_at(data, at(14), BERLIN)["brightness"] == pytest.approx(91, abs=0.5)


def test_clock_mode_interpolates_between_hour_centres() -> None:
    hours = [{"brightness": 10.0 * (h % 10 + 1), "kelvin": 3000 + 100 * h} for h in range(24)]
    data = lightplan.defaults()
    data["schedule"].update(mode="clock", hours=hours)
    assert lightplan.level_at(data, at(9, 30), BERLIN)["brightness"] == hours[9]["brightness"]
    middle = lightplan.level_at(data, at(10, 0), BERLIN)
    assert middle["brightness"] == pytest.approx((hours[9]["brightness"] + hours[10]["brightness"]) / 2, abs=0.2)
    wrap = lightplan.level_at(data, at(0, 0), BERLIN)  # hour 23 blends into hour 0 across midnight
    assert wrap["brightness"] == pytest.approx((hours[23]["brightness"] + hours[0]["brightness"]) / 2, abs=0.2)


def test_clock_mode_without_a_table_falls_back_to_the_sun() -> None:
    data = lightplan.defaults()
    data["schedule"]["mode"] = "clock"
    assert lightplan.level_at(data, at(13), BERLIN) == lightplan.sun_level(data["schedule"], at(13), BERLIN)


def test_the_hours_table_is_seeded_from_the_sun() -> None:
    hours = lightplan.hours_from_sun(lightplan.defaults(), at(12), BERLIN)
    assert len(hours) == 24
    assert hours[2]["brightness"] > hours[13]["brightness"]


def test_the_curve_covers_the_whole_day() -> None:
    points = lightplan.curve(lightplan.defaults(), at(12).date(), at(12).tzinfo, BERLIN, 15)
    assert len(points) == 97
    assert points[0]["minute"] == 0 and points[-1]["minute"] == 1440


def test_role_and_clamps_shape_what_a_light_gets() -> None:
    target = {"brightness": 80.0, "kelvin": 5600}
    light = {**lightplan.LIGHT_DEFAULTS}
    assert lightplan.for_light(target, light) == (80.0, 5600)
    assert lightplan.for_light(target, {**light, "role": "fill"}, 50) == (40.0, 5600)
    assert lightplan.for_light(target, {**light, "role": "manual"}) is None
    clamped = lightplan.for_light(target, {**light, "max_brightness": 60, "max_kelvin": 4500})
    assert clamped == (60.0, 4500)
    floor = lightplan.for_light(
        {"brightness": 5.0, "kelvin": 2700}, {**light, "min_brightness": 20, "min_kelvin": 3200}
    )
    assert floor == (20.0, 3200)


def test_a_fade_is_for_big_jumps_only() -> None:
    model = lighting.MODELS[0xC901]
    now = lighting.percent_to_lumen(model, 50)
    assert not lightplan.fade_needed(model, now, 52, 4800, 4800)
    assert lightplan.fade_needed(model, now, 90, 4800, 4800)
    assert lightplan.fade_needed(model, now, 50, 4000, 4800)
    assert not lightplan.fade_needed(model, None, 90, None, 4800)


def test_an_ephemeral_store_saves_without_flushing_the_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On a busy CI disk every fsync can take seconds, and the demo rig's settings save on each click."""
    flushed: list[int] = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (flushed.append(fd), real(fd))[1])
    lightplan.Store(tmp_path / "durable.json").update("schedule", {"enabled": True})
    assert flushed
    flushed.clear()
    quick = lightplan.Store(tmp_path / "quick.json", ephemeral=True)
    quick.update("schedule", {"enabled": True})
    assert flushed == []
    assert quick.load()["schedule"]["enabled"] is True
