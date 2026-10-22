"""The persona's light for the hour: slot priors, learned lights, clamping."""

from __future__ import annotations

import dataclasses

import pytest

from streamstack import config
from streamstack.director import daylight, learning, pilot, settings


def pilot_at(slot: str, persona: str = "executive") -> pilot.Pilot:
    settings.set_persona(persona)
    p = pilot.Pilot(dict(config.DEFAULTS))
    assert p.sun is not None
    p.sun = dataclasses.replace(p.sun, slot=slot)
    return p


def test_every_slot_has_a_light_prior_and_the_dark_ones_raise_and_warm() -> None:
    assert set(daylight.LIGHT_PRIORS) == set(daylight.SLOTS)
    for slot in ("night", "dawn", "evening"):
        brightness, kelvin = daylight.LIGHT_PRIORS[slot]
        assert brightness > 0 and kelvin < 0
    assert daylight.LIGHT_PRIORS["morning"] == (0.0, 0.0)
    assert daylight.LIGHT_PRIORS["afternoon"][0] < 0 and daylight.LIGHT_PRIORS["afternoon"][1] > 0


@pytest.mark.parametrize("slot", daylight.SLOTS)
def test_the_goal_is_the_persona_light_plus_the_slot_prior_inside_the_limits(slot: str) -> None:
    p = pilot_at(slot)
    fraction, kelvin, source, why = p.light_goal()
    base = p.persona["light"]
    d_fraction, d_kelvin = daylight.LIGHT_PRIORS[slot]
    assert fraction == pytest.approx(max(pilot.LIGHT_FLOOR, min(1.0, base["brightness"] + d_fraction)))
    assert kelvin == pytest.approx(max(2700, min(6500, base["kelvin"] + d_kelvin)))
    assert source == "persona" and daylight.SLOT_LABELS[slot].lower() in why


def test_the_goal_is_clamped_for_a_very_bright_cool_persona_at_night() -> None:
    p = pilot_at("night", "crisp-newsroom")
    fraction, kelvin, _, _ = p.light_goal()
    assert fraction <= 1.0 and kelvin >= 2700


def test_what_the_person_kept_replaces_the_persona_baseline() -> None:
    p = pilot_at("night")
    p.remember_kept({"brightness": 0.0}, False, None, (0.9, 3100.0))
    fraction, kelvin, source, why = p.light_goal()
    assert (fraction, kelvin, source) == (0.9, 3100.0, "baseline") and "kept" in why
    other = pilot_at("morning")
    assert other.light_goal()[2] == "persona"  # another slot is unaffected


def test_a_kept_light_is_blended_like_the_look() -> None:
    first = learning.record("here", "p", "night", {}, light=(0.8, 3000.0))
    assert first["light"] == {"brightness": 0.8, "kelvin": 3000}
    second = learning.record("here", "p", "night", {}, light=(0.4, 4000.0))
    assert second["light"]["brightness"] == pytest.approx(0.8 + (0.4 - 0.8) * learning.KEPT_WEIGHT, abs=0.001)
    kept = learning.record("here", "p", "night", {})  # a pass with no lights keeps the old record
    assert kept["light"] == second["light"]
    assert learning.get_light("here", "p", "night") == (second["light"]["brightness"], second["light"]["kelvin"])
    assert learning.get_light("here", "p", "dawn") is None


def test_a_persona_without_a_light_block_has_no_goal() -> None:
    p = pilot_at("night")
    p.persona = {k: v for k, v in p.persona.items() if k != "light"}
    assert p.light_goal() is None
