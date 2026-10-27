"""The Console's deck controller (profile, navigation, presses, device) and its HTTP endpoints."""

from __future__ import annotations

import http.client
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from http_support import HTTP_TIMEOUT

from streamstack.console import deck as console_deck
from streamstack.console.errors import ActionError
from streamstack.console.rig import Rig
from streamstack.console.server import ConsoleServer
from streamstack.deck import profile, render
from streamstack.deck.models import for_grid


@pytest.fixture
def rig() -> Iterator[Rig]:
    r = Rig.demo()
    r.start()
    yield r
    r.stop()


def deck(rig: Rig) -> dict:
    return rig.snapshot()["deck"]


def find(rig: Rig, action: str, **params: object) -> int:
    d = deck(rig)
    page = next(p for p in d["profile"]["pages"] if p["id"] == d["surface"]["page"]["id"])
    for view in page["keys"]:
        raw = view.get("raw")
        if raw and raw["action"] == action and all(raw["params"].get(k) == v for k, v in params.items()):
            return view["i"]
    raise AssertionError(f"no {action} key on this page")


def test_the_demo_deck_is_a_connected_4x4_with_the_default_profile(rig: Rig) -> None:
    d = deck(rig)
    assert d["device"]["connected"] and d["device"]["source"] == "demo"
    assert (d["surface"]["rows"], d["surface"]["cols"]) == (4, 4)
    assert d["profile"]["pristine"] is True and len(d["surface"]["keys"]) == 16
    assert d["surface"]["page"]["id"] == "main" and not d["surface"]["can_back"]
    assert {m["id"] for m in d["models"]} >= {"virtual-4x4", "mini", "xl"}


def test_pressing_mute_runs_the_console_action_and_lights_the_key(rig: Rig) -> None:
    i = find(rig, "audio.mute")
    assert deck(rig)["surface"]["keys"][i]["lit"] is False
    rig.act("deck.press", {"key": i})
    assert rig.snapshot()["audio"]["mic"]["muted"] is True
    assert deck(rig)["surface"]["keys"][i]["lit"] is True


def test_an_empty_key_does_nothing_and_a_bad_index_is_refused(rig: Rig) -> None:
    rig.act("deck.press", {"key": 15})
    for bad in ({"key": -1}, {"key": "x"}, {}):
        with pytest.raises(ActionError):
            rig.act("deck.press", bad)


def test_folders_open_and_back_returns(rig: Rig) -> None:
    folder = find(rig, "deck.page")
    rig.act("deck.press", {"key": folder})
    d = deck(rig)
    assert d["surface"]["page"]["id"] != "main" and d["surface"]["can_back"]
    rig.act("deck.press", {"key": find(rig, "deck.back")})
    assert deck(rig)["surface"]["page"]["id"] == "main"
    rig.act("deck.press", {"key": folder})
    rig.act("deck.home", {})
    assert deck(rig)["surface"]["page"]["id"] == "main"


def test_stopping_the_recording_needs_a_second_press(rig: Rig) -> None:
    i = find(rig, "video.record")
    rig.act("deck.press", {"key": i})
    assert rig.snapshot()["video"]["recording"]["active"] is True
    assert rig.act("deck.press", {"key": i}) == "Press again to confirm."
    assert rig.snapshot()["video"]["recording"]["active"] is True
    assert deck(rig)["surface"]["keys"][i]["armed"] is True
    rig.act("deck.press", {"key": i})
    assert rig.snapshot()["video"]["recording"]["active"] is False


def test_an_action_the_rig_refuses_is_toasted_and_raised(rig: Rig) -> None:
    rig.act("deck.key_set", {"page": "main", "key": 15, "spec": {"action": "video.scene", "params": {"name": "Nope"}}})
    seen: list[tuple[str, str]] = []
    rig.deck.notify = lambda level, text: seen.append((level, text))
    with pytest.raises(ActionError):
        rig.act("deck.press", {"key": 15})
    assert seen and seen[0][0] == "warn"


def test_editing_a_key_saves_a_profile_and_the_default_stops_following_the_rig(rig: Rig) -> None:
    rig.act("deck.key_set", {"page": "main", "key": 15, "spec": {"action": "audio.mute", "label": "Shh"}})
    d = deck(rig)
    assert d["profile"]["pristine"] is False
    assert d["profile"]["pages"][0]["keys"][15]["label"] == "Shh"
    assert d["surface"]["keys"][15]["label"] == "Shh"
    rig.act("deck.key_clear", {"page": "main", "key": 15})
    assert deck(rig)["profile"]["pages"][0]["keys"][15]["empty"] is True


def test_keys_swap_and_bad_edits_are_refused(rig: Rig) -> None:
    before = deck(rig)["profile"]["pages"][0]["keys"]
    rig.act("deck.key_move", {"page": "main", "key": 0, "to": 1})
    after = deck(rig)["profile"]["pages"][0]["keys"]
    assert (after[0]["raw"], after[1]["raw"]) == (before[1]["raw"], before[0]["raw"])
    with pytest.raises(ActionError):
        rig.act("deck.key_set", {"page": "main", "key": 99, "spec": {"action": "a.b"}})
    with pytest.raises(ActionError):
        rig.act("deck.key_set", {"page": "main", "key": 0, "spec": {"action": "Not An Action"}})
    with pytest.raises(ActionError):
        rig.act("deck.key_set", {"page": "ghost", "key": 0, "spec": {"action": "a.b"}})


def test_pages_can_be_added_renamed_moved_and_deleted_and_links_follow(rig: Rig) -> None:
    rig.act("deck.page_add", {"name": "Stream"})
    pages = deck(rig)["profile"]["pages"]
    new = pages[-1]
    assert new["name"] == "Stream"
    rig.act("deck.page_rename", {"page": new["id"], "name": "Show"})
    rig.act("deck.page_move", {"page": new["id"], "to": 0})
    pages = deck(rig)["profile"]["pages"]
    assert pages[0]["name"] == "Show" and pages[0]["id"] == new["id"]
    rig.act("deck.page_delete", {"page": new["id"]})
    assert new["id"] not in [p["id"] for p in deck(rig)["profile"]["pages"]]
    for page in deck(rig)["profile"]["pages"][1:]:
        rig.act("deck.page_delete", {"page": page["id"]})
    with pytest.raises(ActionError, match="at least one page"):
        rig.act("deck.page_delete", {"page": deck(rig)["profile"]["pages"][0]["id"]})
    with pytest.raises(ActionError):
        rig.act("deck.page_rename", {"page": deck(rig)["profile"]["pages"][0]["id"], "name": ""})


def test_deleting_a_page_removes_the_keys_that_opened_it(rig: Rig) -> None:
    rig.act("deck.key_set", {"page": "main", "key": 15, "spec": {"action": "deck.page", "params": {"page": "lights"}}})
    rig.act("deck.page_delete", {"page": "lights"})
    for page in deck(rig)["profile"]["pages"]:
        for key in page["keys"]:
            raw = key.get("raw")
            assert not (raw and raw["action"] == "deck.page" and raw["params"].get("page") == "lights")


def test_settings_are_kept_without_forking_the_default_profile(rig: Rig) -> None:
    rig.act("deck.settings", {"brightness": 40, "sleep_minutes": 5})
    d = deck(rig)
    assert d["profile"]["settings"] == {"brightness": 40, "sleep_minutes": 5} and d["profile"]["pristine"] is True
    rig.act("deck.settings", {"step": 10})
    assert deck(rig)["profile"]["settings"]["brightness"] == 50
    rig.act("deck.settings", {"step": 500})
    assert deck(rig)["profile"]["settings"]["brightness"] == 100
    for bad in ({"brightness": 1}, {"sleep_minutes": 7}, {"step": "up"}):
        with pytest.raises(ActionError):
            rig.act("deck.settings", bad)


def test_export_import_round_trips_and_rejects_nonsense(rig: Rig) -> None:
    rig.act("deck.key_set", {"page": "main", "key": 15, "spec": {"action": "audio.mute", "label": "Shh"}})
    exported = rig.deck.export()
    rig.act("deck.reset", {})
    assert deck(rig)["profile"]["pristine"] is True
    rig.act("deck.import", {"profile": exported})
    assert deck(rig)["profile"]["pages"][0]["keys"][15]["label"] == "Shh"
    with pytest.raises(ActionError):
        rig.act("deck.import", {"profile": {"pages": "no"}})
    with pytest.raises(ActionError):
        rig.act("deck.import", {})


def test_another_deck_model_reflows_the_profile_and_flags_the_mismatch(rig: Rig) -> None:
    rig.act("deck.attach", {"model": "mini"})
    d = deck(rig)
    assert (d["surface"]["rows"], d["surface"]["cols"]) == (2, 3)
    assert d["surface"]["fitted"] is False  # nothing saved yet: the default is made for this deck
    rig.act("deck.key_set", {"page": "main", "key": 0, "spec": {"action": "audio.mute"}})
    rig.act("deck.attach", {"model": "xl"})
    d = deck(rig)
    assert (d["surface"]["rows"], d["surface"]["cols"]) == (4, 8) and d["surface"]["fitted"] is True
    assert d["profile"]["mismatch"] is True
    rig.act("deck.layout", {"model": "xl"})
    assert deck(rig)["profile"]["mismatch"] is False
    with pytest.raises(ActionError):
        rig.act("deck.attach", {"model": "nope"})


def test_unplugging_leaves_the_editor_with_a_model_to_pick(rig: Rig) -> None:
    rig.act("deck.detach", {})
    assert deck(rig)["device"]["connected"] is False
    rig.act("deck.layout", {"model": "mini"})
    d = deck(rig)
    assert (d["profile"]["layout"]["rows"], d["profile"]["layout"]["cols"]) == (2, 3)
    rig.act("deck.layout", {"rows": 3, "cols": 3})
    assert deck(rig)["profile"]["layout"] == {"rows": 3, "cols": 3}
    rig.act("deck.reset", {})
    assert deck(rig)["device"]["connected"] is True


def test_unknown_deck_actions_are_refused(rig: Rig) -> None:
    with pytest.raises(ActionError):
        rig.act("deck.explode", {})


# -- a real device reporting in --------------------------------------------------------------------------------


class Time:
    now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def live(tmp_path: Path) -> Iterator[tuple[Rig, Time, console_deck.DeckController]]:
    clock = Time()
    ctl = console_deck.DeckController(profile.Store(tmp_path / "deck.json"), clock)
    demo = Rig.demo()
    demo.deck = ctl
    ctl.rig = demo
    demo.start()
    yield demo, clock, ctl
    demo.stop()


def test_no_device_until_the_service_reports_and_again_when_it_goes_quiet(live) -> None:
    rig, clock, _ = live
    assert deck(rig)["device"]["connected"] is False
    rig.act("deck.attach", {"name": "Stream Deck Original", "serial": "A1", "rows": 3, "cols": 5, "size": 72})
    d = deck(rig)
    assert d["device"]["connected"] and d["device"]["source"] == "usb" and d["device"]["model"] == "original"
    assert d["surface"]["rows"] == 3
    clock.now += console_deck.DEVICE_FRESH_SECONDS + 1
    assert deck(rig)["device"]["connected"] is False
    rig.act("deck.attach", {"name": "Stream Deck Original", "rows": 3, "cols": 5, "size": 72})
    rig.act("deck.detach", {})
    assert deck(rig)["device"]["connected"] is False


def test_a_service_error_is_shown_until_it_goes_stale(live) -> None:
    rig, clock, _ = live
    rig.act("deck.attach", {"error": "Another app has the deck."})
    assert deck(rig)["device"]["error"] == "Another app has the deck."
    clock.now += console_deck.DEVICE_FRESH_SECONDS + 1
    assert "error" not in deck(rig)["device"]
    with pytest.raises(ActionError):
        rig.act("deck.attach", {"error": "x" * 400})
    with pytest.raises(ActionError):
        rig.act("deck.attach", {"rows": 0, "cols": 3, "size": 72})


def test_a_saved_profile_survives_a_restart_and_the_arming_times_out(live, tmp_path: Path) -> None:
    rig, clock, _ = live
    rig.act("deck.key_set", {"page": "main", "key": 0, "spec": {"action": "video.record"}})
    again = console_deck.DeckController(profile.Store(tmp_path / "deck.json"), clock)
    assert again.state(rig.snapshot())["profile"]["pages"][0]["keys"][0]["raw"]["action"] == "video.record"
    rig.act("deck.press", {"key": 0})
    rig.act("deck.press", {"key": 0})  # armed
    assert deck(rig)["surface"]["keys"][0]["armed"] is True
    clock.now += console_deck.ARM_SECONDS + 1
    assert deck(rig)["surface"]["keys"][0]["armed"] is False


# -- HTTP --------------------------------------------------------------------------------------------------------


@pytest.fixture
def server(rig: Rig, tmp_path: Path) -> Iterator[ConsoleServer]:
    root = tmp_path / "console"
    root.mkdir()
    (root / "index.html").write_text("<!doctype html>")
    srv = ConsoleServer(rig, port=0, token="tok", static_dir=root, allow_hosts=set())
    srv.start()
    yield srv
    srv.close()


def get(server: ConsoleServer, path: str, token: bool = True) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=HTTP_TIMEOUT)
    try:
        conn.request("GET", path, headers={"X-StreamStack-Token": "tok"} if token else {})
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


def test_the_palette_endpoint_lists_actions_and_needs_the_token(server: ConsoleServer) -> None:
    status, _, body = get(server, "/api/deck/palette")
    assert status == 200 and any(e["action"] == "audio.mute" for e in json.loads(body)["palette"])
    assert get(server, "/api/deck/palette", token=False)[0] == 401


def test_the_profile_endpoint_is_importable(server: ConsoleServer, rig: Rig) -> None:
    status, _, body = get(server, "/api/deck/profile.json")
    assert status == 200
    exported = json.loads(body)
    assert profile.clean_profile(exported)["layout"] == {"rows": 4, "cols": 4}
    rig.act("deck.import", {"profile": exported})


def test_unknown_deck_endpoints_are_404(server: ConsoleServer) -> None:
    assert get(server, "/api/deck/nope")[0] == 404


def test_key_pictures_are_pngs(server: ConsoleServer, rig: Rig) -> None:
    if not render.available():
        pytest.skip("Pillow is not installed")
    status, headers, body = get(server, "/api/deck/key.png?page=main&key=0&size=96")
    assert status == 200 and headers["content-type"] == "image/png" and body.startswith(b"\x89PNG")
    assert get(server, "/api/deck/key.png?key=0")[0] == 200  # the page the deck is on
    assert get(server, "/api/deck/key.png?page=main&key=99")[0] == 404
    assert get(server, "/api/deck/key.png?page=" + "x" * 50 + "&key=0")[0] == 400


def test_a_rig_without_a_deck_says_so(tmp_path: Path) -> None:
    bare = Rig.demo()
    bare.deck = None
    root = tmp_path / "console"
    root.mkdir()
    (root / "index.html").write_text("x")
    srv = ConsoleServer(bare, port=0, token="tok", static_dir=root, allow_hosts=set())
    srv.start()
    try:
        assert get(srv, "/api/deck/palette")[0] == 404
        with pytest.raises(ActionError):
            bare.act("deck.press", {"key": 0})
        assert bare.snapshot()["deck"]["device"]["connected"] is False
    finally:
        srv.close()


def test_the_default_model_for_a_grid() -> None:
    assert for_grid(4, 4).id == "virtual-4x4"
