"""The deck profile: validation, the default layout for every model, re-flowing to another deck, and the file."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from streamstack.console.rig import Rig
from streamstack.deck import catalog, icons, profile
from streamstack.deck.models import MODELS, for_device, for_grid


@pytest.fixture(scope="module")
def snap() -> dict:
    rig = Rig.demo()
    rig.start()
    try:
        return {**rig.snapshot()}
    finally:
        rig.stop()


def pairs(prof: dict) -> list[dict]:
    return [k for page in prof["pages"] for k in page["keys"] if k]


def test_clean_key_normalises_and_rejects() -> None:
    key = profile.clean_key({"action": "video.scene", "params": {"name": "Canon"}, "label": " Cam ", "icon": "camera"})
    assert key == {"action": "video.scene", "params": {"name": "Canon"}, "label": "Cam", "icon": "camera"}
    assert profile.clean_key(None) is None
    assert profile.clean_key({"action": "", "params": {}}) == {"action": "", "params": {}}
    for bad in (
        "scene", {"action": "Scene"}, {"action": "scene"}, {"action": "a." + "b" * 40},
        {"action": "a.b", "params": [1]},
        {"action": "a.b", "label": "x" * 25}, {"action": "a.b", "icon": "Bad Icon"},
        {"action": "a.b", "params": {"x": "y" * 3000}}, {"action": "a.b", "params": {"x": object()}},
    ):  # fmt: skip
        with pytest.raises(profile.ProfileError):
            profile.clean_key(bad)


def test_clean_settings_bounds() -> None:
    assert profile.clean_settings({"brightness": 40, "sleep_minutes": 5}) == {"brightness": 40, "sleep_minutes": 5}
    assert profile.clean_settings(None) == profile.DEFAULT_SETTINGS
    for bad in ({"brightness": 3}, {"brightness": 101}, {"brightness": True}, {"sleep_minutes": 7}, "x"):
        with pytest.raises(profile.ProfileError):
            profile.clean_settings(bad)


def test_clean_profile_pads_trims_and_checks_pages() -> None:
    raw = {
        "layout": {"rows": 2, "cols": 2},
        "pages": [{"id": "main", "name": "Live", "keys": [{"action": "audio.mute"}] * 9}, {"id": "b", "name": "B"}],
    }
    clean = profile.clean_profile(raw)
    assert [len(p["keys"]) for p in clean["pages"]] == [4, 4]
    assert clean["home"] == "main" and clean["model"] == for_grid(2, 2).id
    for bad in (
        {**raw, "layout": {"rows": 0, "cols": 2}},
        {**raw, "layout": {"rows": 2, "cols": 9}},
        {**raw, "pages": []},
        {**raw, "pages": [raw["pages"][0], raw["pages"][0]]},
        {**raw, "pages": [{"id": "Bad Id", "name": "x"}]},
        {**raw, "pages": [{"id": "ok", "name": ""}]},
        {**raw, "pages": [{"id": f"p{i}", "name": "x"} for i in range(profile.MAX_PAGES + 1)]},
        [],
    ):
        with pytest.raises(profile.ProfileError):
            profile.clean_profile(bad)


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.id)
def test_the_default_profile_fits_every_model_and_every_page_is_reachable(snap: dict, model) -> None:
    prof = profile.default_profile(snap, model)
    cap = model.keys
    assert prof["layout"] == {"rows": model.rows, "cols": model.cols}
    assert all(len(p["keys"]) == cap for p in prof["pages"])
    ids = {p["id"] for p in prof["pages"]}
    targets = {k["params"]["page"] for k in pairs(prof) if k["action"] == "deck.page"}
    assert targets <= ids
    reachable, frontier = {prof["home"]}, [prof["home"]]
    while frontier:
        current = frontier.pop()
        page = next(p for p in prof["pages"] if p["id"] == current)
        for k in page["keys"]:
            if k and k["action"] == "deck.page" and k["params"]["page"] not in reachable:
                reachable.add(k["params"]["page"])
                frontier.append(k["params"]["page"])
    assert reachable == ids
    for page in prof["pages"]:
        if page["id"] != prof["home"]:
            assert any(k and k["action"] == "deck.back" for k in page["keys"]), page["id"]
    for k in pairs(prof):
        assert catalog.ACTION_NAME.fullmatch(k["action"])


def test_a_4x4_default_holds_the_core_controls_and_a_folder_per_group(snap: dict) -> None:
    prof = profile.default_profile(snap, for_grid(4, 4))
    main = prof["pages"][0]["keys"]
    actions = [k["action"] for k in main if k]
    for wanted in ("audio.mute", "video.privacy", "video.record", "video.stream", "video.scene", "video.virtualcam"):
        assert wanted in actions
    assert len([k for k in main if k and k["action"] == "deck.page"]) == 4
    assert None not in main
    every = {(k["action"], json.dumps(k["params"], sort_keys=True)) for k in pairs(prof)}
    for scene in snap["video"]["scenes"]:
        assert ("video.scene", json.dumps({"name": scene["name"]})) in every
    for preset in snap["audio"]["chain"]["presets"]:
        assert ("audio.preset", json.dumps({"name": preset["name"]})) in every
    for preset in snap["lighting"]["presets"]:
        assert ("lighting.preset", json.dumps({"name": preset["name"]})) in every


def test_every_icon_a_default_key_names_exists(snap: dict) -> None:
    known = set(icons.names())
    for model in MODELS:
        for k in pairs(profile.default_profile(snap, model)):
            if k.get("icon"):
                assert k["icon"] in known, k


def test_reflow_to_a_bigger_deck_keeps_every_key_in_place(snap: dict) -> None:
    small = profile.default_profile(snap, for_grid(2, 3))
    big = profile.reflow(small, 4, 8)
    assert big["layout"] == {"rows": 4, "cols": 8}
    assert [p["id"] for p in big["pages"]] == [p["id"] for p in small["pages"]]
    for old, new in zip(small["pages"], big["pages"], strict=True):
        for i, k in enumerate(old["keys"]):
            assert new["keys"][(i // 3) * 8 + i % 3] == k


def test_reflow_to_a_smaller_deck_loses_no_key(snap: dict) -> None:
    big = profile.default_profile(snap, for_grid(4, 4))
    small = profile.reflow(big, 2, 3)
    assert all(len(p["keys"]) == 6 for p in small["pages"])

    def signature(prof: dict) -> list[str]:
        return sorted(
            f"{k['action']}{json.dumps(k['params'], sort_keys=True)}"
            for k in pairs(prof)
            if k["action"] not in ("deck.back", "deck.page")
        )

    assert signature(small) == signature(big)
    ids = {p["id"] for p in small["pages"]}
    assert {k["params"]["page"] for k in pairs(small) if k["action"] == "deck.page"} <= ids
    assert profile.reflow(small, 2, 3) == small
    assert big["layout"] == {"rows": 4, "cols": 4}  # the original is never changed


def test_new_page_ids_are_unique_slugs() -> None:
    prof = {"pages": [{"id": "main"}, {"id": "my-page"}]}
    assert profile.new_page_id(prof, "My Page!") == "my-page-2"
    assert profile.new_page_id(prof, "???") == "page"


def test_the_store_saves_atomically_with_mode_600(tmp_path: Path, snap: dict) -> None:
    store = profile.Store(tmp_path / "cfg" / "deck.json")
    assert store.load() is None and store.load_settings() is None
    prof = profile.default_profile(snap, for_grid(4, 4))
    store.save(prof)
    assert stat.S_IMODE((tmp_path / "cfg" / "deck.json").stat().st_mode) == 0o600
    assert store.load() == prof
    assert not list((tmp_path / "cfg").glob("*.tmp"))
    store.delete()
    assert store.load() is None
    store.delete()  # deleting nothing is fine


def test_settings_only_file_is_not_a_profile(tmp_path: Path) -> None:
    store = profile.Store(tmp_path / "deck.json")
    store.save_settings({"brightness": 30, "sleep_minutes": 0})
    assert store.load() is None
    assert store.load_settings() == {"brightness": 30, "sleep_minutes": 0}


def test_a_damaged_file_falls_back_to_the_default(tmp_path: Path) -> None:
    path = tmp_path / "deck.json"
    store = profile.Store(path)
    path.write_text("{not json")
    assert store.load() is None
    path.write_text(json.dumps({"layout": {"rows": 1}, "pages": []}))
    assert store.load() is None


def test_the_store_reports_a_file_it_cannot_write(tmp_path: Path, snap: dict) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the folder should be")
    with pytest.raises(profile.ProfileError):
        profile.Store(blocked / "deck.json").save(profile.default_profile(snap, for_grid(4, 4)))


def test_models_know_the_librarys_names() -> None:
    assert for_device("Stream Deck Original", 3, 5, 72).id == "original"
    assert for_device("Stream Deck XL", 4, 8, 96).keys == 32
    odd = for_device("Stream Deck Future", 5, 5, 0)
    assert odd.id == "custom-5x5" and odd.size == 96
