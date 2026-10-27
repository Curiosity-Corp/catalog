"""The deck catalog: what can be put on a key, how a key looks for the rig's state, and what pressing it runs."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from streamstack.console.rig import Rig
from streamstack.deck import catalog, icons


@pytest.fixture
def snap() -> Iterator[dict]:
    rig = Rig.demo()
    rig.start()
    yield rig.snapshot()
    rig.stop()


def with_deck(snap: dict) -> dict:
    return {**snap, "deck": {"pages": [{"id": "main", "name": "Live"}], "settings": {"brightness": 70}}}


def key(action: str, **params: object) -> dict:
    return {"action": action, "params": params}


def test_palette_is_grouped_unique_and_ends_with_the_custom_entry(snap: dict) -> None:
    entries = catalog.palette(with_deck(snap))
    ids = [e["id"] for e in entries]
    assert len(ids) == len(set(ids))
    assert entries[-1]["custom"] is True
    groups = [e["group"] for e in entries]
    assert groups == sorted(groups, key=lambda g: catalog.GROUPS.index(g))
    actions = {e["action"] for e in entries}
    for wanted in (
        "video.scene", "audio.mute", "video.privacy", "audio.preset", "lighting.power", "lighting.preset",
        "lighting.brightness", "lighting.auto.follow", "lighting.auto.schedule", "video.record", "video.stream",
        "video.virtualcam", "auto.on", "video.blur", "deck.page",
    ):  # fmt: skip
        assert wanted in actions


def test_palette_lists_the_rigs_own_scenes_and_presets(snap: dict) -> None:
    entries = catalog.palette(with_deck(snap))
    scenes = {e["params"]["name"] for e in entries if e["action"] == "video.scene"}
    assert scenes == {s["name"] for s in snap["video"]["scenes"]}
    voices = {e["params"]["name"] for e in entries if e["action"] == "audio.preset"}
    assert voices == {p["name"] for p in snap["audio"]["chain"]["presets"]}
    lights = {e["params"]["name"] for e in entries if e["action"] == "lighting.preset"}
    assert lights == {p["name"] for p in snap["lighting"]["presets"]}


def test_every_palette_action_is_a_valid_name_the_rig_routes(snap: dict) -> None:
    for entry in catalog.palette(with_deck(snap)):
        if entry.get("custom"):
            continue
        assert catalog.ACTION_NAME.fullmatch(entry["action"]) and len(entry["action"]) <= 40
        assert entry["label"] and entry["icon"] and entry["group"] in catalog.GROUPS


def test_every_icon_the_catalog_names_exists_in_the_console_icon_set() -> None:
    known = set(icons.names())
    used = {r["icon"] for r in catalog.RULES} | {r["lit_icon"] for r in catalog.RULES if "lit_icon" in r}
    assert used <= known, used - known


def test_a_toggle_key_follows_the_state_and_flips_on_press(snap: dict) -> None:
    ctx = with_deck(snap)
    mute = key("audio.mute")
    view = catalog.view_of(mute, ctx)
    assert (view["lit"], view["label"]) == (False, "Mute")
    assert catalog.resolve_press(mute, ctx) == ("audio.mute", {"on": True}, False)
    ctx["audio"]["mic"]["muted"] = True
    view = catalog.view_of(mute, ctx)
    assert view["lit"] is True and view["tone"] == "red" and view["label"] == "Muted"
    assert catalog.resolve_press(mute, ctx)[1] == {"on": False}


def test_a_fixed_flag_only_lights_when_it_is_the_state_it_sets(snap: dict) -> None:
    ctx = with_deck(snap)
    ctx["audio"]["mic"]["muted"] = True
    on, off = key("audio.mute", on=True), key("audio.mute", on=False)
    assert catalog.view_of(on, ctx)["lit"] is True
    assert catalog.view_of(off, ctx)["lit"] is False
    assert catalog.resolve_press(on, ctx)[1] == {"on": True}


def test_an_option_key_is_lit_when_it_is_the_current_choice(snap: dict) -> None:
    ctx = with_deck(snap)
    live = snap["video"]["program_scene"]
    other = next(s["name"] for s in snap["video"]["scenes"] if s["name"] != live)
    assert catalog.view_of(key("video.scene", name=live), ctx)["lit"] is True
    assert catalog.view_of(key("video.scene", name=other), ctx)["lit"] is False
    assert catalog.view_of(key("video.scene", name=live), ctx)["label"] == live


def test_blur_strengths_are_their_own_keys_and_the_plain_toggle_stays_plain(snap: dict) -> None:
    ctx = with_deck(snap)
    assert catalog.view_of(key("video.blur", on=True, strength=3), ctx)["label"] == "Blur Light"
    assert catalog.view_of(key("video.blur"), ctx)["label"] == "Blur"


def test_stopping_asks_twice_and_starting_does_not(snap: dict) -> None:
    ctx = with_deck(snap)
    record = key("video.record")
    assert catalog.resolve_press(record, ctx) == ("video.record", {"on": True}, False)
    ctx["video"]["recording"]["active"] = True
    action, params, confirm = catalog.resolve_press(record, ctx)
    assert (action, params, confirm) == ("video.record", {"on": False}, True)
    assert catalog.view_of(record, ctx, armed=True)["label"] == "Again to stop"


def test_a_swap_key_runs_the_opposite_action(snap: dict) -> None:
    ctx = with_deck(snap)
    director = key("auto.on")
    ctx["auto"]["running"] = False
    assert catalog.resolve_press(director, ctx)[0] == "auto.on"
    ctx["auto"]["running"] = True
    assert catalog.resolve_press(director, ctx)[0] == "auto.off"


def test_step_keys_move_the_reading_and_stay_in_range(snap: dict) -> None:
    ctx = with_deck(snap)
    ctx["lighting"]["master"]["brightness"] = 95
    up = key("lighting.brightness", id="all", step=10)
    assert catalog.resolve_press(up, ctx) == ("lighting.brightness", {"id": "all", "percent": 100}, False)
    ctx["lighting"]["master"]["brightness"] = 4
    down = key("lighting.brightness", id="all", step=-10)
    assert catalog.resolve_press(down, ctx)[1]["percent"] == 1
    ctx["lighting"]["master"]["kelvin"] = 2800
    warmer = key("lighting.temperature", id="all", step=-300)
    assert catalog.resolve_press(warmer, ctx)[1] == {"id": "all", "kelvin": 2700}
    assert catalog.find_rule("lighting.brightness", {"step": -5})["label"] == "Dimmer"


def test_a_step_without_a_reading_is_refused(snap: dict) -> None:
    ctx = with_deck(snap)
    ctx["lighting"]["master"]["brightness"] = None
    with pytest.raises(catalog.PressError):
        catalog.resolve_press(key("lighting.brightness", id="all", step=10), ctx)


def test_an_unknown_action_still_runs_as_typed(snap: dict) -> None:
    ctx = with_deck(snap)
    assert catalog.resolve_press(key("audio.mic_check"), ctx) == ("audio.mic_check", {}, False)
    custom = key("future.thing", x=1)
    assert catalog.resolve_press(custom, ctx) == ("future.thing", {"x": 1}, False)
    view = catalog.view_of(custom, ctx)
    assert view["label"] == "future.thing" and view["icon"] == "command"


def test_a_key_without_an_action_asks_for_one(snap: dict) -> None:
    ctx = with_deck(snap)
    blank = {"action": "", "params": {}}
    assert catalog.view_of(blank, ctx)["dim"] is True
    with pytest.raises(catalog.PressError):
        catalog.resolve_press(blank, ctx)


def test_a_view_revision_changes_only_when_the_picture_would(snap: dict) -> None:
    ctx = with_deck(snap)
    first = catalog.view_of(key("audio.mute"), ctx)
    assert catalog.view_of(key("audio.mute"), ctx)["rev"] == first["rev"]
    ctx["audio"]["mic"]["muted"] = True
    assert catalog.view_of(key("audio.mute"), ctx)["rev"] != first["rev"]
    assert catalog.view_of(None, ctx, 3) == {"i": 3, "empty": True, "rev": "0"}


def test_the_persons_label_and_icon_win(snap: dict) -> None:
    view = catalog.view_of({"action": "audio.mute", "params": {}, "label": "Shh", "icon": "volume"}, with_deck(snap))
    assert (view["label"], view["icon"]) == ("Shh", "volume")


def test_a_capability_the_rig_reports_missing_leaves_the_palette_and_dims_the_key(snap: dict) -> None:
    ctx = with_deck(snap)
    ctx["video"]["blur"]["available"] = False
    assert not any(e["action"] == "video.blur" for e in catalog.palette(ctx))
    blur = key("video.blur")
    assert catalog.view_of(blur, ctx)["dim"] is True
    with pytest.raises(catalog.PressError, match="not available"):
        catalog.resolve_press(blur, ctx)
    ctx["video"]["blur"]["available"] = True
    assert catalog.view_of(blur, ctx)["dim"] is False


def test_nothing_in_the_catalog_names_a_specific_backend() -> None:
    text = repr(catalog.RULES) + repr(catalog.GROUPS)
    assert "websocket" not in text.lower() and "backend" not in text.lower()
