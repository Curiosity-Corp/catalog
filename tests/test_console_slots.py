"""The time-of-day profile controller on the demo rig: what it applies, when, and who wins."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from streamstack.console import lightauto as panel_module
from streamstack.console.errors import ActionError
from streamstack.console.rig import Rig
from streamstack.console.slots import DemoSlots

PINNED = "2026-06-21T12:00:00-04:00"  # New York: dawn 05:00, morning 06:20, midday 11:00, evening 19:40, night 21:10


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Rig]:
    monkeypatch.setenv(panel_module.DEMO_CLOCK_ENV, PINNED)
    rig = Rig.demo()
    rig.start()
    slots = rig.slots
    assert isinstance(slots, DemoSlots)
    slots.stop()  # the tests drive tick() themselves
    slots._stop.clear()
    slots.clock = Clock()
    slots._clock = slots.clock
    slots.act("slots.reset", {})
    yield rig
    rig.stop()


def act(rig: Rig, action: str, params: dict | None = None, **kw: object) -> str | None:
    return rig.act(action, {**(params or {}), **kw})


def block(rig: Rig) -> dict:
    return rig.snapshot()["slots"]


def go(rig: Rig, hhmm: str) -> None:
    act(rig, "slots.demo.clock", time=hhmm)


def profile(rig: Rig, slot: str, **parts: object) -> None:
    act(rig, "slots.profile", slot=slot, profile=parts)


def settle(rig: Rig, seconds: float = 30.0) -> None:
    rig.slots.clock.now += seconds


def call(rig: Rig, on: bool) -> None:
    act(rig, "call.demo", app="Teams" if on else None)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if rig.call.active() == on:
            return
        time.sleep(0.02)
    raise AssertionError("the demo call did not become active")


def test_the_snapshot_block_describes_the_day_and_the_options(rig: Rig) -> None:
    s = block(rig)
    assert s["available"] and s["mode"] == "off"
    assert [x["id"] for x in s["slots"]] == ["night", "dawn", "morning", "midday", "afternoon", "evening"]
    assert s["current"]["slot"] == "midday" and s["current"]["clock"] == "12:00"
    assert s["strip"][0]["slot"] == "night" and s["strip"][-1]["end"] == 1440
    assert "Natural" in s["options"]["looks"] and "Soft" in s["options"]["lighting"]
    assert s["status"]["state"] == "off"


def test_a_slot_change_applies_the_profile_in_automatic_mode(rig: Rig) -> None:
    profile(rig, "dawn", look="Golden Hour", lighting="Warm evening")
    act(rig, "slots.mode", mode="auto")
    rig.slots.tick()  # learns that it is midday
    go(rig, "05:30")
    s = block(rig)
    assert s["status"]["state"] == "applied" and s["current"]["slot"] == "dawn"
    assert rig.snapshot()["lighting"]["preset"] == "Warm evening"
    applied = {p["part"]: p["state"] for p in s["status"]["applied"]["parts"]}
    assert applied == {"look": "applied", "lighting": "applied"}
    assert any("Dawn profile applied" in e["text"] for e in s["events"])


def test_a_slot_change_in_a_call_waits_for_the_next_call(rig: Rig) -> None:
    profile(rig, "dawn", lighting="Warm evening")
    profile(rig, "midday", lighting="Daylight")
    act(rig, "slots.mode", mode="auto")
    rig.slots.tick()
    call(rig, True)
    rig.slots.tick()
    go(rig, "05:30")
    assert block(rig)["status"]["state"] != "applied"
    assert any("left alone" in e["text"] for e in block(rig)["events"])
    call(rig, False)
    rig.slots.tick()
    call(rig, True)
    rig.slots.tick()
    assert block(rig)["status"]["state"] == "applied"
    assert rig.snapshot()["lighting"]["preset"] == "Warm evening"


def test_suggest_mode_offers_the_profile_and_applies_on_request(rig: Rig) -> None:
    profile(rig, "dawn", lighting="Warm evening")
    act(rig, "slots.mode", mode="suggest")
    rig.slots.tick()
    go(rig, "05:30")
    s = block(rig)
    assert s["status"]["state"] == "suggested" and s["status"]["suggestion"]["slot"] == "dawn"
    assert rig.snapshot()["lighting"]["preset"] != "Warm evening"
    act(rig, "slots.apply")
    assert block(rig)["status"]["state"] == "applied"
    assert rig.snapshot()["lighting"]["preset"] == "Warm evening"


def test_off_mode_never_applies_by_itself(rig: Rig) -> None:
    profile(rig, "dawn", lighting="Warm evening")
    rig.slots.tick()
    go(rig, "05:30")
    assert block(rig)["status"]["state"] == "off"
    assert rig.snapshot()["lighting"]["preset"] != "Warm evening"


def test_a_manual_change_holds_the_profile_until_the_slot_ends(rig: Rig) -> None:
    profile(rig, "dawn", look="Golden Hour", lighting="Warm evening")
    profile(rig, "morning", lighting="Daylight")
    act(rig, "slots.mode", mode="auto")
    rig.slots.tick()
    go(rig, "05:30")
    settle(rig)
    act(rig, "lighting.preset", name="Daylight")  # a person picks another light setup
    rig.slots.tick()
    held = block(rig)["status"]
    assert held["state"] == "held" and held["held"]["part"] == "lighting"
    go(rig, "07:00")  # the slot ended: the morning profile is allowed again
    assert block(rig)["status"]["state"] == "applied"
    assert rig.snapshot()["lighting"]["preset"] == "Daylight"


def test_resume_hands_the_rig_back_to_profiles(rig: Rig) -> None:
    profile(rig, "dawn", lighting="Warm evening")
    act(rig, "slots.mode", mode="auto")
    rig.slots.tick()
    go(rig, "05:30")
    settle(rig)
    act(rig, "lighting.preset", name="Daylight")
    rig.slots.tick()
    assert block(rig)["status"]["state"] == "held"
    act(rig, "slots.resume")
    assert block(rig)["status"]["held"] is None
    act(rig, "slots.apply")
    assert rig.snapshot()["lighting"]["preset"] == "Warm evening"


def test_undo_puts_back_the_previous_look_and_lights(rig: Rig) -> None:
    before = rig.snapshot()
    profile(rig, "midday", look="Noir", lighting="Warm evening")
    act(rig, "slots.apply")
    assert rig.snapshot()["video"]["look"] != before["video"]["look"]
    assert block(rig)["status"]["can_undo"]
    act(rig, "slots.undo")
    after = rig.snapshot()
    assert after["video"]["look"] == before["video"]["look"]
    assert after["lighting"]["master"]["brightness"] == before["lighting"]["master"]["brightness"]
    assert not block(rig)["status"]["can_undo"]
    with pytest.raises(ActionError, match="nothing to undo"):
        act(rig, "slots.undo")


def test_with_the_director_on_the_profile_seeds_its_baseline_instead_of_writing(rig: Rig) -> None:
    act(rig, "auto.on")
    deadline = time.monotonic() + 5
    while not rig.snapshot()["auto"]["running"] and time.monotonic() < deadline:
        time.sleep(0.05)
    assert rig.snapshot()["auto"]["running"]
    profile(rig, "midday", look="Noir", lighting="Warm evening", voice="StreamStack Voice Deep", scene="Side by Side")
    look_before = rig.snapshot()["video"]["look"]
    act(rig, "slots.apply")
    snap = rig.snapshot()
    s = block(rig)
    states = {p["part"]: p["state"] for p in s["status"]["applied"]["parts"]}
    assert states == {"scene": "applied", "look": "seeded", "lighting": "seeded", "voice": "skipped"}
    assert snap["video"]["look"] == look_before  # not moved: the Director owns the picture
    assert snap["video"]["program_scene"] == "Side by Side"  # the scene is not the Director's
    assert rig.slots.seeded and rig.slots.seeded[-1][0] in ("morning", "afternoon")
    assert s["director"]["on"] is True


def test_save_current_names_what_is_on_now(rig: Rig) -> None:
    act(rig, "video.looks.apply", name="Noir")
    act(rig, "lighting.preset", name="Warm evening")
    act(rig, "slots.save_current", slot="evening")
    saved = next(x for x in block(rig)["slots"] if x["id"] == "evening")["profile"]
    assert saved["look"] == "Noir" and saved["lighting"] == "Warm evening"


def test_starter_import_clear_and_reset(rig: Rig) -> None:
    assert "Starter profiles filled in" in act(rig, "slots.starter")
    assert all(not x["empty"] for x in block(rig)["slots"])
    assert act(rig, "slots.starter") == "Every slot already has a profile"
    act(rig, "slots.clear", slot="night")
    assert next(x for x in block(rig)["slots"] if x["id"] == "night")["empty"]
    act(rig, "slots.reset")
    assert all(x["empty"] for x in block(rig)["slots"])


@pytest.mark.parametrize(
    ("action", "params", "message"),
    [
        ("slots.mode", {"mode": "loud"}, "mode must be"),
        ("slots.fade", {"seconds": 99}, "fade_seconds must be"),
        ("slots.profile", {"slot": "noon", "profile": {}}, "slot must be"),
        ("slots.profile", {"slot": "dawn", "profile": {"look": "No Such Look"}}, "no look called"),
        ("slots.profile", {"slot": "dawn", "profile": {"lighting": "Nope"}}, "no lighting preset called"),
        ("slots.profile", {"slot": "dawn", "profile": {"canon": {"exposure_comp": "9"}}}, "exposure compensation"),
        ("slots.profile", {"slot": "dawn", "profile": {"colour": 1}}, "no part"),
        ("slots.apply", {"slot": "dawn"}, "no profile yet"),
        ("slots.import", {"document": {"document": "x"}}, "not a slot profiles document"),
        ("slots.demo.clock", {"time": "25:99"}, "HH:MM"),
        ("slots.bogus", {}, "Unknown action"),
    ],
)
def test_bad_input_is_an_action_error_with_a_reason(rig: Rig, action: str, params: dict, message: str) -> None:
    with pytest.raises(ActionError, match=message):
        act(rig, action, params)


def test_the_profile_bundle_is_served_for_export(rig: Rig) -> None:
    profile(rig, "dawn", look="Noir")
    document = rig.slots.export()
    assert document["document"] == "streamstack.slot-profiles/1" and document["profiles"]["dawn"] == {"look": "Noir"}


def test_a_lights_fade_walks_in_steps_and_ends_on_the_preset(rig: Rig) -> None:
    act(rig, "slots.fade", seconds=4)
    act(rig, "lighting.preset", name="Daylight")
    profile(rig, "midday", lighting="Warm evening")
    steps: list[tuple[str, dict]] = []
    original = rig.slots.dispatch

    def spy(action: str, params: dict) -> str | None:
        if action.startswith("lighting."):
            steps.append((action, dict(params)))
        return original(action, params)

    rig.slots.dispatch = spy
    act(rig, "slots.apply")
    brightness = [p["percent"] for a, p in steps if a == "lighting.brightness"]
    assert len(brightness) >= 2 and steps[-1][0] == "lighting.preset"
    assert rig.snapshot()["lighting"]["preset"] == "Warm evening"


def test_the_deck_palette_offers_the_profile_keys(rig: Rig) -> None:
    from streamstack.deck import catalog

    actions = {e["action"] for e in catalog.palette(rig.snapshot())}
    assert {"slots.apply", "slots.undo", "slots.mode"} <= actions
