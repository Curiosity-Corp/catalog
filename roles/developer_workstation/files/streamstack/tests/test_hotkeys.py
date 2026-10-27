"""Hotkey bindings: combos, the MIDI parser, matching, validation and the saved file."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from streamstack import hotkeys
from streamstack.hotkeys import HotkeyError, MidiEvent, MidiParser

# -- combos ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("Ctrl+Alt+M", "ctrl+alt+m"),
        ("alt+ctrl+m", "ctrl+alt+m"),
        (" Control + Shift + 5 ", "ctrl+shift+5"),
        ("super+F5", "super+f5"),
        ("Win+Enter", "super+return"),
        ("f9", "f9"),
        ("XF86AudioMute", "xf86audiomute"),
        ("ctrl+PageUp", "ctrl+pageup"),
        ("ctrl+alt+ArrowLeft", "ctrl+alt+left"),
        ("ctrl++", "ctrl+equal"),
        ("ctrl+alt+,", "ctrl+alt+comma"),
        ("ctrl+Numpad", None),
    ],
)
def test_combos_are_made_canonical(raw: str, canonical: str | None) -> None:
    if canonical is None:
        with pytest.raises(HotkeyError):
            hotkeys.clean_combo(raw)
    else:
        assert hotkeys.clean_combo(raw) == canonical


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "m", "5", "shift+m", "ctrl", "ctrl+alt", "ctrl+alt+ctrl+m", "hyper+m", "ctrl+nope", "a" * 70, None, 5],
)
def test_unusable_combos_are_refused(raw: object) -> None:
    with pytest.raises(HotkeyError):
        hotkeys.clean_combo(raw)


def test_a_bare_function_media_or_navigation_key_may_stand_alone() -> None:
    for combo in ("f1", "f24", "xf86audioplay", "pause", "kp_5", "pagedown"):
        assert hotkeys.clean_combo(combo) == combo


def test_xdg_trigger_and_description() -> None:
    assert hotkeys.xdg_trigger("ctrl+alt+m") == "CTRL+ALT+m"
    assert (
        hotkeys.xdg_trigger("super+shift+pageup") == "SHIFT+LOGO+Prior"
        or hotkeys.xdg_trigger("super+shift+pageup") == "LOGO+SHIFT+Prior"
    )
    assert hotkeys.xdg_trigger("f5") == "F5"
    assert hotkeys.describe_combo("ctrl+alt+m") == "Ctrl+Alt+M"
    assert hotkeys.describe_combo("super+f5") == "Super+F5"


def test_keysym_names() -> None:
    assert hotkeys.keysym("a") == "a"
    assert hotkeys.keysym("f12") == "F12"
    assert hotkeys.keysym("pageup") == "Prior"
    assert hotkeys.keysym("kp_3") == "KP_3"
    with pytest.raises(HotkeyError):
        hotkeys.keysym("hyper")


# -- MIDI --------------------------------------------------------------------------------------------------------------


def test_the_parser_reads_notes_controllers_and_programs() -> None:
    parser = MidiParser()
    events = parser.feed(bytes([0x90, 60, 100, 0xB0, 7, 64, 0xC2, 5]))
    assert events == [MidiEvent("note", 1, 60, 100), MidiEvent("cc", 1, 7, 64), MidiEvent("program", 3, 5)]


def test_note_on_with_velocity_zero_is_a_release_and_ignored() -> None:
    assert MidiParser().feed(bytes([0x90, 60, 0, 0x80, 60, 64])) == []


def test_running_status_and_split_messages() -> None:
    parser = MidiParser()
    assert parser.feed(bytes([0xB1, 7])) == []  # half a message
    assert parser.feed(bytes([10, 7, 20])) == [MidiEvent("cc", 2, 7, 10), MidiEvent("cc", 2, 7, 20)]


def test_realtime_bytes_may_sit_inside_a_message() -> None:
    assert MidiParser().feed(bytes([0x90, 0xF8, 60, 0xFE, 90])) == [MidiEvent("note", 1, 60, 90)]


def test_sysex_is_skipped_and_cancels_running_status() -> None:
    parser = MidiParser()
    data = bytes([0xB0, 7, 1, 0xF0, 1, 2, 3, 0xF7, 7, 2, 0xB0, 8, 9])
    assert parser.feed(data) == [MidiEvent("cc", 1, 7, 1), MidiEvent("cc", 1, 8, 9)]


def test_stray_data_bytes_before_any_status_are_dropped() -> None:
    assert MidiParser().feed(bytes([1, 2, 3])) == []


def test_unwanted_messages_are_ignored() -> None:
    assert MidiParser().feed(bytes([0xE0, 1, 2, 0xD0, 9, 0xA0, 1, 2])) == []


# -- triggers and bindings ---------------------------------------------------------------------------------------------


def binding(ident: str = "b1", **over: object) -> dict:
    raw: dict = {
        "id": ident, "trigger": {"kind": "key", "combo": "ctrl+alt+m"}, "action": "audio.mute", "params": {},
    }  # fmt: skip
    raw.update(over)
    return raw


def test_a_key_binding_is_cleaned() -> None:
    out = hotkeys.clean_binding(binding(trigger={"kind": "key", "combo": "Alt+Ctrl+M"}, label="  Mute "))
    assert out == {
        "id": "b1", "trigger": {"kind": "key", "combo": "ctrl+alt+m"}, "action": "audio.mute", "params": {},
        "label": "Mute",
    }  # fmt: skip


def test_a_midi_binding_defaults_to_any_channel() -> None:
    out = hotkeys.clean_binding(binding(trigger={"kind": "midi", "type": "note", "number": 60}))
    assert out["trigger"] == {"kind": "midi", "type": "note", "channel": 0, "number": 60}


@pytest.mark.parametrize(
    "trigger",
    [
        None,
        "ctrl+m",
        {"kind": "mouse"},
        {"kind": "midi", "type": "pitch", "number": 1},
        {"kind": "midi", "type": "note", "channel": 17, "number": 1},
        {"kind": "midi", "type": "note", "channel": True, "number": 1},
        {"kind": "midi", "type": "note", "number": 128},
        {"kind": "midi", "type": "note", "number": -1},
        {"kind": "midi", "type": "note"},
        {"kind": "key", "combo": "m"},
    ],
)
def test_bad_triggers_are_refused(trigger: object) -> None:
    with pytest.raises(HotkeyError):
        hotkeys.clean_binding(binding(trigger=trigger))


@pytest.mark.parametrize(
    "over",
    [
        {"id": ""}, {"id": "Bad Id"}, {"id": 5}, {"action": ""}, {"action": "mute"}, {"action": "a" * 50 + ".b"},
        {"params": []}, {"params": {"x": object()}}, {"params": {"x": "y" * 3000}}, {"label": 5}, {"label": "x" * 61},
    ],
)  # fmt: skip
def test_bad_bindings_are_refused(over: dict) -> None:
    with pytest.raises(HotkeyError):
        hotkeys.clean_binding(binding(**over))


def test_a_scale_belongs_to_a_control_change() -> None:
    cc = {"kind": "midi", "type": "cc", "channel": 1, "number": 7}
    ok = hotkeys.clean_binding(binding(trigger=cc, scale={"param": "db", "min": -20, "max": 20}))
    assert ok["scale"] == {"param": "db", "min": -20, "max": 20}
    for scale in ({"param": "db", "min": 1, "max": 1}, {"param": "Bad", "min": 0, "max": 1}, {"param": "db"}, "x"):
        with pytest.raises(HotkeyError):
            hotkeys.clean_binding(binding(trigger=cc, scale=scale))
    with pytest.raises(HotkeyError):
        hotkeys.clean_binding(binding(scale={"param": "db", "min": 0, "max": 1}))  # a key cannot set a value


def test_two_bindings_cannot_share_an_input_or_an_id() -> None:
    with pytest.raises(HotkeyError, match="already used"):
        hotkeys.clean_bindings([binding("a"), binding("b", trigger={"kind": "key", "combo": "alt+ctrl+m"})])
    with pytest.raises(HotkeyError, match="share the id"):
        hotkeys.clean_bindings([binding("a"), binding("a", trigger={"kind": "key", "combo": "f5"})])
    with pytest.raises(HotkeyError):
        hotkeys.clean_bindings("nope")
    with pytest.raises(HotkeyError, match="At most"):
        hotkeys.clean_bindings(
            [
                binding(f"b{i}", trigger={"kind": "midi", "type": "note", "number": i % 128, "channel": i // 128 % 17})
                for i in range(200)
            ]
        )


def test_new_ids_skip_the_taken_ones() -> None:
    assert hotkeys.new_id(set()) == "b1"
    assert hotkeys.new_id({"b1", "b2", "b4"}) == "b3"


def test_trigger_text() -> None:
    assert hotkeys.trigger_text({"kind": "key", "combo": "ctrl+alt+m"}) == "Ctrl+Alt+M"
    assert hotkeys.trigger_text({"kind": "midi", "type": "note", "channel": 0, "number": 60}) == "Note 60 (any channel)"
    assert hotkeys.trigger_text({"kind": "midi", "type": "cc", "channel": 3, "number": 7}) == "CC 7 (channel 3)"
    assert MidiEvent("program", 2, 9).label() == "Program 9 (channel 2)"


# -- matching ----------------------------------------------------------------------------------------------------------


def midi(kind: str, number: int, channel: int = 0, **over: object) -> dict:
    return binding(
        f"{kind}{number}x{channel}",
        trigger={"kind": "midi", "type": kind, "channel": channel, "number": number},
        **over,
    )


def test_a_note_fires_on_its_channel_or_any() -> None:
    bindings = hotkeys.clean_bindings([midi("note", 60, 0), midi("note", 61, 2)])
    matcher = hotkeys.Matcher()
    assert [b["id"] for b, _ in matcher.fire(bindings, MidiEvent("note", 5, 60))] == ["note60x0"]
    assert matcher.fire(bindings, MidiEvent("note", 1, 61)) == []
    assert [b["id"] for b, _ in matcher.fire(bindings, MidiEvent("note", 2, 61))] == ["note61x2"]
    assert matcher.fire(bindings, MidiEvent("program", 1, 60)) == []


def test_a_controller_button_fires_once_when_it_crosses_64() -> None:
    bindings = hotkeys.clean_bindings([midi("cc", 20)])
    matcher = hotkeys.Matcher()
    fired = [len(matcher.fire(bindings, MidiEvent("cc", 1, 20, v))) for v in (0, 30, 90, 127, 100, 10, 127)]
    assert fired == [0, 0, 1, 0, 0, 0, 1]


def test_a_button_on_another_channel_has_its_own_edge() -> None:
    bindings = hotkeys.clean_bindings([midi("cc", 20)])
    matcher = hotkeys.Matcher()
    assert len(matcher.fire(bindings, MidiEvent("cc", 1, 20, 127))) == 1
    assert len(matcher.fire(bindings, MidiEvent("cc", 2, 20, 127))) == 1


def test_a_fader_fires_on_every_change_with_a_scaled_value() -> None:
    bindings = hotkeys.clean_bindings([midi("cc", 7, 1, scale={"param": "db", "min": -20, "max": 20})])
    matcher = hotkeys.Matcher()
    out = [matcher.fire(bindings, MidiEvent("cc", 1, 7, v))[0][1] for v in (0, 127)]
    assert out == [{"db": -20}, {"db": 20}]
    assert matcher.fire(bindings, MidiEvent("cc", 1, 7, 64))[0][1] == {"db": 0}


def test_scaling_keeps_whole_numbers_whole_and_clamps() -> None:
    assert hotkeys.scaled({"param": "x", "min": 0, "max": 100}, 127) == 100
    assert isinstance(hotkeys.scaled({"param": "x", "min": 0, "max": 100}, 64), int)
    assert hotkeys.scaled({"param": "x", "min": 0.0, "max": 1.0}, 127) == 1.0
    assert hotkeys.scaled({"param": "x", "min": 100, "max": 0}, 0) == 100  # a reversed fader
    assert hotkeys.scaled({"param": "x", "min": 0, "max": 100}, 500) == 100


def test_the_matcher_can_be_reset() -> None:
    bindings = hotkeys.clean_bindings([midi("cc", 20)])
    matcher = hotkeys.Matcher()
    matcher.fire(bindings, MidiEvent("cc", 1, 20, 127))
    matcher.reset()
    assert len(matcher.fire(bindings, MidiEvent("cc", 1, 20, 127))) == 1


# -- the file ----------------------------------------------------------------------------------------------------------


def test_the_file_round_trips_with_mode_600(tmp_path: Path) -> None:
    path = tmp_path / "hotkeys.json"
    saved = Path(path)
    s = hotkeys.Store(path)
    assert s.load() == (True, [])
    s.save(False, [binding()])
    assert saved.exists()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    enabled, bindings = hotkeys.Store(path).load()
    assert enabled is False
    assert [b["id"] for b in bindings] == ["b1"]


def test_a_bad_binding_in_the_file_drops_only_itself(tmp_path: Path) -> None:
    path = tmp_path / "hotkeys.json"
    items = [binding("a"), binding("b", action="nope"), binding("c", trigger={"kind": "key", "combo": "ctrl+alt+m"}), 7]
    path.write_text(json.dumps({"version": 1, "enabled": True, "bindings": items}))
    _, bindings = hotkeys.Store(path).load()
    assert [b["id"] for b in bindings] == ["a"]  # b is invalid; c duplicates a's combo


def test_a_damaged_file_gives_the_defaults(tmp_path: Path) -> None:
    path = tmp_path / "hotkeys.json"
    path.write_text("{not json")
    assert hotkeys.Store(path).load() == (True, [])


def test_saving_an_invalid_list_writes_nothing(tmp_path: Path) -> None:
    s = hotkeys.Store(tmp_path / "hotkeys.json")
    with pytest.raises(HotkeyError):
        s.save(True, [binding(action="x")])
    assert not (tmp_path / "hotkeys.json").exists()


def test_the_default_file_lives_in_the_config_directory() -> None:
    assert hotkeys.Store().file().name == "hotkeys.json"
    assert hotkeys.Store().file().parent.name == "streamstack"


def test_export_and_import() -> None:
    doc = hotkeys.export_document(False, [binding()])
    assert doc["document"] == hotkeys.DOCUMENT
    enabled, bindings = hotkeys.import_document(json.loads(json.dumps(doc)))
    assert enabled is False and bindings[0]["id"] == "b1"
    assert hotkeys.import_document([binding()])[0] is True
    for bad in ({"document": "other"}, 5, {"document": hotkeys.DOCUMENT, "bindings": [{"id": "x"}]}):
        with pytest.raises(HotkeyError):
            hotkeys.import_document(bad)


def test_the_memory_store_validates_and_copies() -> None:
    m = hotkeys.MemoryStore()
    assert m.load() == (True, [])
    m.save(True, [binding()])
    assert m.load()[1][0]["id"] == "b1"
    with pytest.raises(HotkeyError):
        m.save(True, [{"id": "x"}])
