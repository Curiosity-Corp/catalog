"""The Console's hotkeys controller: bindings, triggering through the deck catalog, confirms, learn, backends."""

from __future__ import annotations

import http.client
import json
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from http_support import HTTP_TIMEOUT

from streamstack import hotkeys
from streamstack.console import hotkeys as console_hotkeys
from streamstack.console.errors import ActionError
from streamstack.console.hotkeys import DemoHotkeys, HotkeysController
from streamstack.console.rig import Rig
from streamstack.console.server import ConsoleServer


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def rig(clock: Clock) -> Iterator[Rig]:
    r = Rig.demo()
    r.hotkeys = DemoHotkeys(clock)
    r.hotkeys.rig = r
    r.hotkeys.notify = r.notify
    r.start()
    yield r
    r.stop()


def hk(rig: Rig) -> DemoHotkeys:
    assert isinstance(rig.hotkeys, DemoHotkeys)
    return rig.hotkeys


def state(rig: Rig) -> dict:
    return rig.snapshot()["hotkeys"]


def add(rig: Rig, trigger: dict, action: str = "audio.mute", **extra: object) -> None:
    rig.act("hotkeys.add", {"trigger": trigger, "action": action, **extra})


def key(combo: str) -> dict:
    return {"kind": "key", "combo": combo}


def pad(kind: str, number: int, channel: int = 0) -> dict:
    return {"kind": "midi", "type": kind, "channel": channel, "number": number}


@pytest.fixture
def toasts(rig: Rig) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    rig.on_toast = lambda level, text: seen.append((level, text))
    return seen


def test_the_section_starts_empty_and_demo_backends_are_listening(rig: Rig) -> None:
    s = state(rig)
    assert s["bindings"] == [] and s["enabled"] is True and s["available"] is True
    assert s["keys"] == {"backend": "demo", "ok": True, "reason": None}
    assert s["midi"]["ok"] is True and s["midi"]["devices"][0]["name"] == "Demo pad"
    assert s["learn"] == {"active": False, "event": None}


def test_a_key_press_runs_its_action_and_is_logged(rig: Rig) -> None:
    add(rig, key("Ctrl+Alt+M"), "audio.mute")
    assert state(rig)["bindings"][0]["status"] == "ok"
    muted = rig.snapshot()["audio"]["mic"]["muted"]
    rig.act("hotkeys.demo.key", {"combo": "ctrl+alt+m"})
    assert rig.snapshot()["audio"]["mic"]["muted"] is (not muted)  # a toggle flips, like a deck key
    s = state(rig)
    assert s["events"][0]["level"] == "ok" and "Ctrl+Alt+M" in s["events"][0]["text"]
    assert s["bindings"][0]["last"]["ok"] is True


def test_a_held_key_repeating_runs_once(rig: Rig, clock: Clock) -> None:
    add(rig, key("ctrl+alt+m"), "audio.mute")
    start = rig.snapshot()["audio"]["mic"]["muted"]
    for _ in range(5):
        rig.act("hotkeys.demo.key", {"combo": "ctrl+alt+m"})
    assert rig.snapshot()["audio"]["mic"]["muted"] is (not start)
    clock.now += 1
    rig.act("hotkeys.demo.key", {"combo": "ctrl+alt+m"})
    assert rig.snapshot()["audio"]["mic"]["muted"] is start


def test_a_binding_with_params_passes_them(rig: Rig) -> None:
    add(rig, key("ctrl+alt+g"), "audio.gain", params={"db": -6.0})
    rig.act("hotkeys.demo.key", {"combo": "ctrl+alt+g"})
    assert rig.snapshot()["audio"]["mic"]["gain_db"] == -6.0


def test_a_midi_note_runs_its_action(rig: Rig) -> None:
    add(rig, pad("note", 60, 1), "video.privacy", params={"on": True})
    rig.act("hotkeys.demo.midi", {"type": "note", "channel": 1, "number": 60, "value": 100})
    assert rig.snapshot()["video"]["privacy"] is True


def test_a_midi_fader_sets_a_scaled_value(rig: Rig, clock: Clock) -> None:
    add(rig, pad("cc", 7, 1), "audio.gain", scale={"param": "db", "min": -12, "max": 12})
    rig.act("hotkeys.demo.midi", {"type": "cc", "channel": 1, "number": 7, "value": 127})
    assert rig.snapshot()["audio"]["mic"]["gain_db"] == 12
    rig.act("hotkeys.demo.midi", {"type": "cc", "channel": 1, "number": 7, "value": 0})
    assert rig.snapshot()["audio"]["mic"]["gain_db"] == -12
    rig.act("hotkeys.demo.midi", {"type": "cc", "channel": 1, "number": 7, "value": 0})  # unchanged: no second write
    assert len([e for e in state(rig)["events"] if e["level"] == "ok"]) == 1  # a sweep is one log line


def test_a_midi_button_toggles_on_the_rising_edge(rig: Rig) -> None:
    add(rig, pad("cc", 20), "audio.mute")
    start = rig.snapshot()["audio"]["mic"]["muted"]
    for value in (127, 127, 0):
        rig.act("hotkeys.demo.midi", {"type": "cc", "channel": 1, "number": 20, "value": value})
    assert rig.snapshot()["audio"]["mic"]["muted"] is (not start)


def test_a_confirm_rule_needs_the_second_press(
    rig: Rig, clock: Clock, toasts: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[tuple[str, dict]] = []
    monkeypatch.setattr(hotkeys.catalog, "resolve_press", lambda k, ctx: (k["action"], dict(k["params"]), True))
    monkeypatch.setattr(hk(rig), "rig", type("FakeRig", (), {
        "snapshot": staticmethod(lambda: {}),
        "act": staticmethod(lambda action, params: ran.append((action, params))),
    }))  # fmt: skip
    rig.act("hotkeys.add", {"trigger": key("ctrl+alt+x"), "action": "stream.stop", "label": "Stop"})
    rig.act("hotkeys.demo.key", {"combo": "ctrl+alt+x"})
    assert ran == [] and toasts[-1][0] == "warn" and "again to confirm" in toasts[-1][1]
    clock.now += 1  # past the debounce, inside the arming window
    rig.act("hotkeys.demo.key", {"combo": "ctrl+alt+x"})
    assert ran == [("stream.stop", {})]
    clock.now += 1
    rig.act("hotkeys.demo.key", {"combo": "ctrl+alt+x"})  # armed again, not run
    assert len(ran) == 1
    clock.now += console_hotkeys.ARM_SECONDS + 1
    rig.act("hotkeys.demo.key", {"combo": "ctrl+alt+x"})  # the window lapsed: arms again instead of running
    assert len(ran) == 1


def test_a_press_the_rig_refuses_is_reported_not_raised(rig: Rig, toasts: list[tuple[str, str]]) -> None:
    add(rig, key("ctrl+alt+n"), "video.scene", params={"name": "no-such-scene"})
    rig.act("hotkeys.demo.key", {"combo": "ctrl+alt+n"})
    s = state(rig)
    assert s["bindings"][0]["last"]["ok"] is False
    assert s["events"][0]["level"] == "warn"
    assert toasts and toasts[-1][0] == "warn"


def test_a_rule_not_available_here_is_reported(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(key: dict, ctx: dict) -> tuple[str, dict, bool]:
        raise hotkeys.catalog.PressError("That is not available on this setup right now.")

    monkeypatch.setattr(hotkeys.catalog, "resolve_press", refuse)
    add(rig, key("ctrl+alt+n"), "lighting.preset")
    rig.act("hotkeys.demo.key", {"combo": "ctrl+alt+n"})
    assert "not available" in state(rig)["bindings"][0]["last"]["text"]


def test_switching_off_stops_every_trigger_and_releases_the_keys(rig: Rig, clock: Clock) -> None:
    add(rig, key("ctrl+alt+m"), "audio.mute")
    start = rig.snapshot()["audio"]["mic"]["muted"]
    rig.act("hotkeys.enable", {"on": False})
    assert state(rig)["bindings"][0]["status"] == "off"
    assert hk(rig).fake_keys.held == []
    rig.act("hotkeys.demo.key", {"combo": "ctrl+alt+m"})
    rig.act("hotkeys.demo.midi", {"type": "note", "channel": 1, "number": 60})
    assert rig.snapshot()["audio"]["mic"]["muted"] is start
    rig.act("hotkeys.enable", {"on": True})
    assert hk(rig).fake_keys.held == ["ctrl+alt+m"]


def test_a_conflicting_combo_is_marked_and_logged(rig: Rig) -> None:
    rig.act("hotkeys.demo.conflict", {"combo": "ctrl+alt+m"})
    add(rig, key("ctrl+alt+m"), "audio.mute")
    b = state(rig)["bindings"][0]
    assert b["status"] == "conflict" and "Another program" in b["detail"]
    rig.act("hotkeys.demo.conflict", {"combo": "ctrl+alt+m", "on": False})
    assert state(rig)["bindings"][0]["status"] == "ok"


def test_a_late_status_from_the_backend_is_kept_and_logged(rig: Rig) -> None:
    add(rig, key("ctrl+alt+m"), "audio.mute")
    hk(rig)._on_key_status({"ctrl+alt+m": "refused"})
    assert state(rig)["bindings"][0]["status"] == "refused"
    assert "did not accept" in state(rig)["events"][0]["text"]


def test_adding_updating_removing(rig: Rig) -> None:
    add(rig, key("ctrl+alt+m"), "audio.mute", label="Mute")
    add(rig, pad("note", 60), "video.privacy", params={"on": True})
    ids = [b["id"] for b in state(rig)["bindings"]]
    assert ids == ["b1", "b2"]
    rig.act("hotkeys.update", {"id": "b1", "trigger": key("ctrl+alt+k"), "label": None})
    b1 = state(rig)["bindings"][0]
    assert b1["trigger"]["combo"] == "ctrl+alt+k" and "label" not in b1
    assert hk(rig).fake_keys.held == ["ctrl+alt+k"]
    rig.act("hotkeys.remove", {"id": "b2"})
    assert [b["id"] for b in state(rig)["bindings"]] == ["b1"]
    rig.act("hotkeys.clear", {})
    assert state(rig)["bindings"] == [] and hk(rig).fake_keys.held == []


def test_a_new_trigger_drops_a_scale_that_no_longer_fits(rig: Rig) -> None:
    add(rig, pad("cc", 7), "audio.gain", scale={"param": "db", "min": 0, "max": 1})
    rig.act("hotkeys.update", {"id": "b1", "trigger": key("f9")})
    assert "scale" not in state(rig)["bindings"][0]


@pytest.mark.parametrize(
    ("action", "params", "status"),
    [
        ("hotkeys.add", {"trigger": key("m"), "action": "audio.mute"}, 400),
        ("hotkeys.add", {"trigger": key("f9"), "action": "mute"}, 400),
        ("hotkeys.add", {"action": "audio.mute"}, 400),
        ("hotkeys.update", {"id": "zzz"}, 404),
        ("hotkeys.remove", {"id": "zzz"}, 404),
        ("hotkeys.fire", {"id": "zzz"}, 404),
        ("hotkeys.enable", {"on": "yes"}, 400),
        ("hotkeys.learn", {"on": 3}, 400),
        ("hotkeys.import", {"document": {"document": "x"}}, 400),
        ("hotkeys.nope", {}, 400),
        ("hotkeys.demo.midi", {"type": "pitch", "number": 1}, 400),
        ("hotkeys.demo.midi", {"type": "note", "number": 1, "value": 999}, 400),
        ("hotkeys.demo.key", {"combo": "m"}, 400),
    ],
)
def test_bad_requests_are_refused(rig: Rig, action: str, params: dict, status: int) -> None:
    with pytest.raises(ActionError) as caught:
        rig.act(action, params)
    assert caught.value.status == status


def test_a_duplicate_trigger_is_refused_and_nothing_changes(rig: Rig) -> None:
    add(rig, key("ctrl+alt+m"), "audio.mute")
    with pytest.raises(ActionError, match="already used"):
        add(rig, key("Alt+Ctrl+M"), "video.privacy")
    assert len(state(rig)["bindings"]) == 1


def test_the_binding_limit(rig: Rig) -> None:
    for i in range(hotkeys.MAX_BINDINGS):
        add(rig, pad("note", i % 128, i // 128), "audio.mute")
    with pytest.raises(ActionError, match="At most"):
        add(rig, pad("note", 5, 16), "audio.mute")


def test_fire_runs_a_binding_from_the_page_even_right_after_a_press(rig: Rig) -> None:
    add(rig, key("ctrl+alt+m"), "audio.mute")
    start = rig.snapshot()["audio"]["mic"]["muted"]
    rig.act("hotkeys.demo.key", {"combo": "ctrl+alt+m"})
    rig.act("hotkeys.fire", {"id": "b1"})
    assert rig.snapshot()["audio"]["mic"]["muted"] is start


def test_learn_captures_the_next_midi_message_instead_of_running_it(rig: Rig) -> None:
    add(rig, pad("note", 60), "audio.mute")
    start = rig.snapshot()["audio"]["mic"]["muted"]
    rig.act("hotkeys.learn", {"on": True})
    assert state(rig)["learn"] == {"active": True, "event": None}
    rig.act("hotkeys.demo.midi", {"type": "note", "channel": 3, "number": 60})
    learn = state(rig)["learn"]
    assert learn["active"] is False
    assert learn["event"] == {"type": "note", "channel": 3, "number": 60, "text": "Note 60 (channel 3)"}
    assert rig.snapshot()["audio"]["mic"]["muted"] is start  # captured, not run
    assert state(rig)["midi"]["last"]["text"] == "Note 60 (channel 3)"
    rig.act("hotkeys.demo.midi", {"type": "note", "channel": 3, "number": 60})
    assert rig.snapshot()["audio"]["mic"]["muted"] is (not start)  # back to normal


def test_learn_can_be_cancelled(rig: Rig) -> None:
    rig.act("hotkeys.learn", {"on": True})
    rig.act("hotkeys.learn", {"on": False})
    assert state(rig)["learn"]["active"] is False


def test_learn_without_midi_says_why() -> None:
    ctl = HotkeysController(hotkeys.MemoryStore(), lambda a, b: None, lambda a, b: _NoMidi(), Clock(), Clock())
    ctl.start()
    with pytest.raises(ActionError, match="no MIDI"):
        ctl.act("hotkeys.learn", {"on": True})


class _NoMidi:
    reason: str | None = None

    def start(self) -> str | None:
        self.reason = "There is no MIDI here."
        return self.reason

    def stop(self) -> None:
        return None

    def devices(self) -> list[dict]:
        return []


def test_midi_bindings_wait_for_a_device(rig: Rig) -> None:
    add(rig, pad("note", 60), "audio.mute")
    assert state(rig)["bindings"][0]["status"] == "ok"
    hk(rig).fake_midi.plugged = False
    b = state(rig)["bindings"][0]
    assert b["status"] == "waiting" and "No MIDI controller" in b["detail"]


def test_export_import_round_trip(rig: Rig) -> None:
    add(rig, key("ctrl+alt+m"), "audio.mute", label="Mute")
    add(rig, pad("cc", 7, 2), "audio.gain", scale={"param": "db", "min": -6, "max": 6})
    document = hk(rig).export()
    assert document["document"] == hotkeys.DOCUMENT
    rig.act("hotkeys.clear", {})
    rig.act("hotkeys.import", {"document": json.loads(json.dumps(document))})
    assert [b["id"] for b in state(rig)["bindings"]] == ["b1", "b2"]
    assert hk(rig).fake_keys.held == ["ctrl+alt+m"]


def test_demo_reset_puts_everything_back(rig: Rig) -> None:
    add(rig, key("ctrl+alt+m"), "audio.mute")
    rig.act("hotkeys.demo.conflict", {"combo": "ctrl+alt+m"})
    rig.act("hotkeys.learn", {"on": True})
    rig.act("hotkeys.demo.reset", {})
    s = state(rig)
    assert s["bindings"] == [] and s["events"] == [] and s["learn"] == {"active": False, "event": None}
    assert hk(rig).fake_keys.conflicts == set()


def test_the_events_log_is_capped(rig: Rig, clock: Clock) -> None:
    add(rig, key("ctrl+alt+m"), "audio.mute")
    for _ in range(console_hotkeys.EVENT_LIMIT + 10):
        clock.now += 1
        rig.act("hotkeys.demo.key", {"combo": "ctrl+alt+m"})
    assert len(state(rig)["events"]) == console_hotkeys.EVENT_LIMIT


def test_bindings_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "hotkeys.json"
    first = HotkeysController(hotkeys.Store(path), lambda a, b: None, lambda a, b: _NoMidi())
    first.act("hotkeys.add", {"trigger": key("ctrl+alt+m"), "action": "audio.mute"})
    first.act("hotkeys.enable", {"on": False})
    second = HotkeysController(hotkeys.Store(path), lambda a, b: None, lambda a, b: _NoMidi())
    s = second.state()
    assert s["enabled"] is False and [b["id"] for b in s["bindings"]] == ["b1"]


def test_a_save_that_fails_is_reported_and_changes_nothing(tmp_path: Path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("x")
    ctl = HotkeysController(hotkeys.Store(blocked / "hotkeys.json"), lambda a, b: None, lambda a, b: _NoMidi())
    with pytest.raises(ActionError, match="could not be saved"):
        ctl.act("hotkeys.add", {"trigger": key("f9"), "action": "audio.mute"})
    assert ctl.state()["bindings"] == []


# -- the real backends' selection --------------------------------------------------------------------------------------


def test_session_kind() -> None:
    assert console_hotkeys.session_kind({"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"}) == "wayland"
    assert console_hotkeys.session_kind({"WAYLAND_DISPLAY": "wayland-0"}) == "wayland"
    assert console_hotkeys.session_kind({"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}) == "x11"
    assert console_hotkeys.session_kind({"DISPLAY": ":1"}) == "x11"
    assert console_hotkeys.session_kind({}) == "none"


def test_no_session_means_no_keys_but_a_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE"):
        monkeypatch.delenv(name, raising=False)
    ctl = HotkeysController(hotkeys.MemoryStore(), midi_factory=lambda a, b: _NoMidi())
    ctl.start()
    s = ctl.state()
    assert s["keys"]["ok"] is False and "neither X11" in s["keys"]["reason"]
    assert s["available"] is False
    ctl.act("hotkeys.add", {"trigger": key("f9"), "action": "audio.mute"})
    assert ctl.state()["bindings"][0]["status"] == "unavailable"
    ctl.stop()


def test_a_backend_that_cannot_start_gives_its_reason() -> None:
    class Broken:
        name = "x11"

        def start(self) -> str | None:
            return "Could not open the X11 display."

        def stop(self) -> None:
            return None

        def set_combos(self, combos: list[str], timeout: float = 0.0) -> dict[str, str]:
            return {}

    ctl = HotkeysController(hotkeys.MemoryStore(), lambda a, b: Broken(), lambda a, b: _NoMidi())
    ctl.start()
    assert ctl.state()["keys"] == {"backend": "none", "ok": False, "reason": "Could not open the X11 display."}


def test_a_missing_extra_is_reported() -> None:
    def factory(on_combo: Callable[[str], None], on_status: Callable[[dict[str, str]], None]) -> None:
        raise ImportError("No module named jeepney")

    ctl = HotkeysController(hotkeys.MemoryStore(), factory, lambda a, b: _NoMidi())
    ctl.start()
    assert "not installed" in ctl.state()["keys"]["reason"]


def test_the_default_midi_factory_builds_a_source() -> None:
    source = console_hotkeys.default_midi(lambda p, e: None, lambda d: None)
    assert source.devices() == []


def test_the_empty_state_matches_the_live_shape(rig: Rig) -> None:
    assert set(console_hotkeys.empty_state()) == set(state(rig))


# -- over HTTP ---------------------------------------------------------------------------------------------------------


@pytest.fixture
def server(rig: Rig, tmp_path: Path) -> Iterator[ConsoleServer]:
    root = tmp_path / "console"
    root.mkdir()
    (root / "index.html").write_text("<!doctype html>")
    srv = ConsoleServer(rig, port=0, token="tok", static_dir=root, allow_hosts=set())
    srv.start()
    yield srv
    srv.close()


def get(server: ConsoleServer, path: str, token: bool = True) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=HTTP_TIMEOUT)
    try:
        conn.request("GET", path, headers={"X-StreamStack-Token": "tok"} if token else {})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def test_the_export_endpoint_needs_the_token(server: ConsoleServer, rig: Rig) -> None:
    add(rig, key("ctrl+alt+m"), "audio.mute")
    status, raw = get(server, "/api/hotkeys/bindings.json")
    body = json.loads(raw)
    assert status == 200 and body["document"] == hotkeys.DOCUMENT and body["bindings"][0]["id"] == "b1"
    assert get(server, "/api/hotkeys/bindings.json", token=False)[0] == 401
    rig.hotkeys = None
    assert get(server, "/api/hotkeys/bindings.json")[0] == 404


def test_a_rig_without_hotkeys_reports_none_and_refuses_the_actions() -> None:
    bare = Rig.demo()
    bare.hotkeys = None
    assert bare.snapshot()["hotkeys"]["available"] is False
    with pytest.raises(ActionError) as caught:
        bare.act("hotkeys.add", {})
    assert caught.value.status == 409


def post(server: ConsoleServer, body: dict) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=HTTP_TIMEOUT)
    try:
        conn.request(
            "POST",
            "/api/action",
            json.dumps(body),
            {"X-StreamStack-Token": "tok", "Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())
    finally:
        conn.close()


def test_a_binding_travels_nested_over_http_because_action_names_the_request(server: ConsoleServer, rig: Rig) -> None:
    binding = {"trigger": key("ctrl+alt+m"), "action": "audio.mute", "params": {}}
    status, body = post(server, {"action": "hotkeys.add", "binding": binding})
    assert status == 200, body
    assert [(b["id"], b["action"]) for b in state(rig)["bindings"]] == [("b1", "audio.mute")]
    status, body = post(server, {"action": "hotkeys.update", "id": "b1", "binding": {"label": "Mic"}})
    assert status == 200, body
    assert state(rig)["bindings"][0]["label"] == "Mic"
    assert post(server, {"action": "hotkeys.add", "binding": "nope"})[0] == 400
