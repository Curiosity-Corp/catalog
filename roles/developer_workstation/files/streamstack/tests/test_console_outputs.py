"""Console audio outputs: sink parsing, the generated test sound, volume/mute/default, the test run, remembered results,
hot-plug and the demo twin. No test touches a real sound card: pw-dump, wpctl, pw-play and `pw-dump --monitor` are
all fakes."""

from __future__ import annotations

import io
import json
import os
import struct
import subprocess
import threading
import time
import wave
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from streamstack import config, system
from streamstack.console import outputs
from streamstack.console.audio import AudioController, DemoAudio
from streamstack.console.errors import ActionError
from streamstack.console.outputs import DemoOutputs, Outputs

WAVE = "alsa_output.usb-Elgato_Wave_XLR.analog-stereo"
HDMI = "alsa_output.pci-0000_04_00.0.hdmi-stereo"
HEADSET = "bluez_output.AA_BB.1"
EE = "streamstack_route"


IDS = {WAVE: 45, HDMI: 50, HEADSET: 61, EE: 84}


def props_of(percent: int, mute: bool = False) -> dict[str, Any]:
    """A node's Props: PipeWire stores the linear gain, the cube of the percentage `wpctl` shows."""
    gain = (percent / 100) ** 3
    return {"volume": 1.0, "mute": mute, "channelVolumes": [gain, gain], "channelMap": ["FL", "FR"]}


def sink(name: str, description: str, *, percent: int = 40, mute: bool = False, **props: Any) -> dict[str, Any]:
    """One Audio/Sink node of a `pw-dump`."""
    return {
        "id": IDS[name],
        "type": "PipeWire:Interface:Node",
        "info": {
            "state": "suspended",
            "props": {"media.class": "Audio/Sink", "node.name": name, "node.description": description, **props},
            "params": {"Props": [props_of(percent, mute)]},
        },
    }


def desk() -> list[dict[str, Any]]:
    """What a real desk reports: a USB interface, the monitor's HDMI sound and the StreamStack route."""
    return [
        sink(HDMI, "DG2 Audio Controller Digital Stereo (HDMI)", **{"device.bus": "pci", "api.alsa.path": "hdmi:2"}),
        sink(WAVE, "Elgato Wave XLR Analog Stereo", **{"device.bus": "usb"}),
        sink(EE, "StreamStack Route Sink", percent=100, **{"node.virtual": True}),
    ]


def metadata(default: str) -> dict[str, Any]:
    return {
        "id": 33,
        "type": "PipeWire:Interface:Metadata",
        "props": {"metadata.name": "default"},
        "metadata": [
            {"subject": 0, "key": "default.audio.sink", "type": "Spa:String:JSON", "value": {"name": default}}
        ],
    }


class Sound:
    """A fake PipeWire: the nodes `pw-dump` lists, the default, every command it was given and every sound it played."""

    def __init__(self) -> None:
        self.sinks = desk()
        self.default = WAVE
        self.have = {"pw-dump", "wpctl", "pw-play"}
        self.commands: list[list[str]] = []
        self.played: list[str] = []
        self.play_error: str | None = None
        self.play_hold = threading.Event()  # tests set it to let a held play finish
        self.hold = False
        self.fail_wpctl = False
        self.listing_ok = True

    def run(self, argv: list[str], timeout: float = 10) -> subprocess.CompletedProcess[str] | None:
        self.commands.append(argv)
        if argv == ["pw-dump"]:
            if not self.listing_ok:
                return None
            return subprocess.CompletedProcess(argv, 0, json.dumps([*self.sinks, metadata(self.default)]), "")
        if self.fail_wpctl:
            return subprocess.CompletedProcess(argv, 1, "", "Failure: No such entity\n")
        if argv[:2] == ["wpctl", "set-volume"]:
            self.find_id(argv[2])["info"]["params"]["Props"] = [props_of(int(argv[3].rstrip("%")))]
        elif argv[:2] == ["wpctl", "set-mute"]:
            self.find_id(argv[2])["info"]["params"]["Props"][0]["mute"] = argv[3] == "1"
        elif argv[:2] == ["wpctl", "set-default"]:
            self.default = self.find_id(argv[2])["info"]["props"]["node.name"]
        return subprocess.CompletedProcess(argv, 0, "", "")

    def find(self, name: str) -> dict[str, Any]:
        return next(s for s in self.sinks if s["info"]["props"]["node.name"] == name)

    def find_id(self, node_id: str) -> dict[str, Any]:
        return next(s for s in self.sinks if str(s["id"]) == node_id)

    def set_volume(self, name: str, percent: int) -> None:
        self.find(name)["info"]["params"]["Props"] = [props_of(percent)]

    def play(self, sink_name: str, path: str, timeout: float, stop: threading.Event | None = None) -> str | None:
        assert Path(path).stat().st_size > 1000, "the test sound must exist while it plays"
        self.played.append(sink_name)
        if self.hold:
            deadline = time.monotonic() + 5
            while not self.play_hold.is_set() and not (stop and stop.is_set()) and time.monotonic() < deadline:
                time.sleep(0.005)
        return self.play_error

    def unplug(self, name: str) -> None:
        self.sinks = [s for s in self.sinks if s["info"]["props"]["node.name"] != name]
        if self.default == name:
            self.default = next(
                (s["info"]["props"]["node.name"] for s in self.sinks if not s["info"]["props"].get("node.virtual")), ""
            )


@pytest.fixture
def sound(monkeypatch: pytest.MonkeyPatch) -> Iterator[Sound]:
    s = Sound()
    monkeypatch.setattr(system, "run", s.run)
    monkeypatch.setattr(system, "have", lambda tool: tool in s.have)
    monkeypatch.setattr(system, "play", s.play)
    yield s
    s.play_hold.set()


class Toasts:
    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []

    def __call__(self, level: str, text: str) -> None:
        self.items.append((level, text))


@pytest.fixture
def toasts() -> Toasts:
    return Toasts()


@pytest.fixture
def out(sound: Sound, toasts: Toasts, isolated_home: Path) -> Iterator[Outputs]:
    o = Outputs(toasts, store=isolated_home / "outputs.json")
    o.refresh()
    yield o
    o.stop()


def wait_idle(o: Outputs, timeout: float = 60.0) -> dict[str, Any]:
    """The state once the running test (or Test all) has finished. The test thread first synthesises its sound in pure
    Python, which a loaded or coverage-traced runner can stretch past any short deadline, so wait on the condition with
    a generous bound that only a real hang reaches."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = o.state()
        if state["test"]["state"] == "idle":
            return state
        time.sleep(0.01)
    raise AssertionError("the output test did not finish")


def device(state: dict[str, Any], name: str) -> dict[str, Any]:
    return next(d for d in state["devices"] if d["name"] == name)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_sink_percent_is_the_cube_root_wpctl_shows() -> None:
    assert outputs.sink_percent(props_of(40)) == 40
    assert outputs.sink_percent({"channelVolumes": [0.063997, 0.063997]}) == 40  # a real Elgato Wave XLR
    assert outputs.sink_percent({"volume": 0.125}) == 50  # no channel volumes: the master one
    assert outputs.sink_percent({"channelVolumes": [1.0]}) == 100
    assert outputs.sink_percent({}) is None
    assert outputs.sink_percent(None) is None
    assert outputs.sink_percent({"channelVolumes": ["loud"]}) is None
    assert outputs.sink_percent({"channelVolumes": [-1.0]}) is None


@pytest.mark.parametrize(
    ("description", "label", "detail"),
    [
        ("Elgato Wave XLR Analog Stereo", "Elgato Wave XLR", "Analog Stereo"),
        ("DG2 Audio Controller Digital Stereo (HDMI)", "DG2 Audio Controller", "Digital Stereo (HDMI)"),
        ("Scarlett 2i2 4th Gen Pro", "Scarlett 2i2 4th Gen Pro", ""),
        ("Stereo", "Stereo", ""),
        ("WH-1000XM4", "WH-1000XM4", ""),
        ("StreamStack Route Sink", "StreamStack Route", "Sink"),
    ],
)
def test_split_label_separates_the_device_from_its_profile(description: str, label: str, detail: str) -> None:
    assert outputs.split_label(description) == (label, detail)


@pytest.mark.parametrize(
    ("props", "name", "description", "port", "kind"),
    [
        ({}, "alsa_output.pci.hdmi-stereo", "DG2 Audio Controller", "hdmi-output-0", "hdmi"),
        ({}, "alsa_output.x", "Monitor DisplayPort", "", "hdmi"),
        ({"device.form_factor": "headset"}, "bluez_output.x", "Headset", "", "headset"),
        ({"device.form_factor": "headphone"}, "alsa_output.x", "Card", "", "headphones"),
        ({"device.form_factor": "internal"}, "alsa_output.x", "Built-in Audio", "", "speaker"),
        ({"device.bus": "pci"}, "alsa_output.x", "Built-in Audio", "analog-output-headphones", "headphones"),
        ({"device.bus": "pci"}, "alsa_output.x", "Built-in Audio", "analog-output-speaker", "speaker"),
        ({"device.bus": "usb"}, "alsa_output.x", "Focusrite Scarlett 2i2", "analog-output", "interface"),
        ({"device.bus": "pci"}, "alsa_output.x", "Card", "", "other"),
    ],
)
def test_output_kind_comes_from_pipewire_hints_not_from_a_model_list(
    props: dict[str, str], name: str, description: str, port: str, kind: str
) -> None:
    assert outputs.output_kind(props, name, description, port) == kind


def test_parse_sinks_describes_devices_and_marks_the_effects_sink_as_a_route() -> None:
    parsed = {s["name"]: s for s in outputs.parse_sinks(json.dumps(desk()))}
    assert parsed[WAVE] | {"volume": 40} == {
        "id": 45,
        "name": WAVE,
        "label": "Elgato Wave XLR",
        "detail": "Analog Stereo",
        "kind": "interface",
        "bus": "usb",
        "virtual": False,
        "state": "suspended",
        "volume": 40,
        "muted": False,
    }
    assert parsed[HDMI]["kind"] == "hdmi"
    assert parsed[HDMI]["bus"] == "pci"
    assert parsed[EE]["virtual"] and parsed[EE]["kind"] == "route" and parsed[EE]["bus"] == "virtual"


def test_parse_sinks_knows_bluetooth_by_its_node_name_when_the_bus_is_missing() -> None:
    parsed = outputs.parse_sinks(json.dumps([sink(HEADSET, "Evolve2 65", **{"device.form-factor": "headset"})]))
    assert parsed[0]["bus"] == "bluetooth" and parsed[0]["kind"] == "headset"


@pytest.mark.parametrize(
    "text", ["", "not json", "{}", "[1, 2]", '[{"type": "PipeWire:Interface:Node", "info": {"props": {}}}]']
)
def test_parse_sinks_survives_garbage(text: str) -> None:
    assert outputs.parse_sinks(text) == []


# ---------------------------------------------------------------------------
# The test sound
# ---------------------------------------------------------------------------


def read_channels(path: Path) -> tuple[int, list[int], list[int]]:
    with wave.open(str(path), "rb") as src:
        assert (src.getnchannels(), src.getsampwidth(), src.getframerate()) == (2, 2, outputs.TEST_RATE)
        raw = src.readframes(src.getnframes())
    samples = struct.unpack(f"<{len(raw) // 2}h", raw)
    return len(samples) // 2, list(samples[0::2]), list(samples[1::2])


def peak(channel: list[int], start: float, end: float) -> int:
    return max(abs(v) for v in channel[int(start * outputs.TEST_RATE) : int(end * outputs.TEST_RATE)])


def test_test_sound_is_left_then_right_then_a_chime_on_both(tmp_path: Path) -> None:
    path = tmp_path / "test.wav"
    outputs.synth_test_wav(path)
    frames, left, right = read_channels(path)
    assert frames / outputs.TEST_RATE == pytest.approx(outputs.TEST_SECONDS, abs=0.01)
    (_, l0, l1), (_, r0, r1), (_, c0, _) = outputs.TEST_PHASES
    assert peak(left, l0, l1) > 5000 and peak(right, l0, l1) == 0  # the left beep is only on the left
    assert peak(right, r0, r1) > 5000 and peak(left, r0, r1) == 0  # the right beeps only on the right
    assert peak(left, c0, c0 + 1.0) > 3000 and peak(right, c0, c0 + 1.0) > 3000  # the chime on both
    assert peak(left, l1 + 0.02, r0 - 0.02) == 0  # silence between the parts
    assert max(map(abs, left + right)) < 32767 * 0.5  # never loud: it may land on a speaker left at 100 percent


def test_test_sound_has_one_left_beep_and_two_right_beeps(tmp_path: Path) -> None:
    path = tmp_path / "test.wav"
    outputs.synth_test_wav(path)
    _, left, right = read_channels(path)
    start, end = outputs.TEST_PHASES[1][1], outputs.TEST_PHASES[1][2]

    def bursts(channel: list[int], a: float, b: float) -> int:
        quiet, count, was = int(0.02 * outputs.TEST_RATE), 0, False
        for i in range(int(a * outputs.TEST_RATE), int(b * outputs.TEST_RATE), quiet):
            loud = max(abs(v) for v in channel[i : i + quiet]) > 1000
            count += loud and not was
            was = loud
        return count

    assert bursts(left, outputs.TEST_PHASES[0][1], outputs.TEST_PHASES[0][2]) == 1
    assert bursts(right, start, end) == 2


# ---------------------------------------------------------------------------
# Listing and state
# ---------------------------------------------------------------------------


def test_state_lists_devices_and_keeps_the_route_apart(out: Outputs) -> None:
    state = out.state()
    assert [d["name"] for d in state["devices"]] == [HDMI, WAVE]
    assert [r["name"] for r in state["routes"]] == [EE]
    assert state["default"] == WAVE and state["default_label"] == "Elgato Wave XLR"
    assert device(state, WAVE)["default"] and not device(state, HDMI)["default"]
    assert device(state, HDMI)["volume"] == 40 and device(state, HDMI)["result"] is None
    assert state["volume_range"] == [0, 100]
    assert state["available"] and state["problem"] is None and state["warning"] is None
    assert state["test"]["state"] == "idle" and state["test"]["seconds"] == outputs.TEST_SECONDS
    assert [p["id"] for p in state["test"]["phases"]] == ["left", "right", "chime"]


def test_a_volume_above_the_fader_range_reads_as_full(sound: Sound, out: Outputs) -> None:
    sound.set_volume(WAVE, 120)
    out.refresh()
    assert device(out.state(), WAVE)["volume"] == 100


def test_the_default_can_be_a_route(sound: Sound, out: Outputs) -> None:
    sound.default = EE
    out.refresh()
    state = out.state()
    assert state["routes"][0]["default"] and not any(d["default"] for d in state["devices"])


def test_the_default_is_read_from_the_default_metadata() -> None:
    objects = [*desk(), metadata(HDMI)]
    assert outputs.parse_dump(json.dumps(objects))[1] == HDMI
    objects[-1]["metadata"][0]["value"] = json.dumps({"name": WAVE})  # older PipeWire: a JSON string
    assert outputs.parse_dump(json.dumps(objects))[1] == WAVE
    assert outputs.parse_dump(json.dumps(desk()))[1] == ""


def test_no_pw_dump_is_a_clear_problem(sound: Sound, out: Outputs) -> None:
    sound.have.discard("pw-dump")
    out.refresh()
    state = out.state()
    assert state["problem"] == "pw-dump is missing (install pipewire-bin)."
    assert [d["name"] for d in state["devices"]] == [HDMI, WAVE]  # the last list stays


def test_an_unreadable_server_keeps_the_list_and_says_why(sound: Sound, out: Outputs) -> None:
    sound.listing_ok = False
    out.refresh()
    state = out.state()
    assert state["problem"] == "The audio server did not answer." and len(state["devices"]) == 2
    sound.listing_ok = True
    out.refresh()
    assert out.state()["problem"] is None


def test_an_answer_that_is_not_json_is_unreadable_not_empty(monkeypatch: pytest.MonkeyPatch, out: Outputs) -> None:
    monkeypatch.setattr(system, "run", lambda argv, timeout=10: subprocess.CompletedProcess(argv, 0, "oops", ""))
    out.refresh()
    state = out.state()
    assert state["problem"] == "The audio server gave an answer that could not be read." and len(state["devices"]) == 2


def test_no_outputs_at_all(sound: Sound, out: Outputs) -> None:
    sound.sinks = []
    out.refresh()
    state = out.state()
    assert not state["available"] and state["problem"] == "No audio output found." and state["devices"] == []


# ---------------------------------------------------------------------------
# Volume, mute, default
# ---------------------------------------------------------------------------


def test_volume_goes_to_that_sink_only(sound: Sound, out: Outputs) -> None:
    out.act("audio.output_volume", {"name": HDMI, "percent": 63.4})
    assert ["wpctl", "set-volume", "50", "63%"] in sound.commands
    assert device(out.state(), HDMI)["volume"] == 63
    assert device(out.state(), WAVE)["volume"] == 40


def test_volume_is_clamped_to_the_fader_range(sound: Sound, out: Outputs) -> None:
    out.act("audio.output_volume", {"name": WAVE, "percent": 250})
    out.act("audio.output_volume", {"name": HDMI, "percent": -4})
    assert ["wpctl", "set-volume", "45", "100%"] in sound.commands
    assert ["wpctl", "set-volume", "50", "0%"] in sound.commands


def test_mute_and_unmute(sound: Sound, out: Outputs) -> None:
    out.act("audio.output_mute", {"name": WAVE, "on": True})
    assert ["wpctl", "set-mute", "45", "1"] in sound.commands
    assert device(out.state(), WAVE)["muted"]
    out.act("audio.output_mute", {"name": WAVE, "on": False})
    assert not device(out.state(), WAVE)["muted"]


def test_set_default_switches_and_clears_the_warning(sound: Sound, out: Outputs) -> None:
    sound.unplug(WAVE)
    out.refresh()
    assert out.state()["warning"] is not None
    assert out.act("audio.output_default", {"name": HDMI}) == "Default output: DG2 Audio Controller"
    assert ["wpctl", "set-default", "50"] in sound.commands
    state = out.state()
    assert state["default"] == HDMI and device(state, HDMI)["default"] and state["warning"] is None


@pytest.mark.parametrize(
    ("action", "params"),
    [
        ("audio.output_volume", {"name": WAVE}),
        ("audio.output_volume", {"name": WAVE, "percent": "loud"}),
        ("audio.output_volume", {"name": WAVE, "percent": True}),
        ("audio.output_volume", {"name": WAVE, "percent": float("nan")}),
        ("audio.output_volume", {"name": 7, "percent": 5}),
        ("audio.output_volume", {"percent": 5}),
        ("audio.output_volume", {"name": "--help", "percent": 5}),
        ("audio.output_mute", {"name": WAVE, "on": "yes"}),
        ("audio.output_mute", {"name": WAVE}),
        ("audio.output_default", {"name": ""}),
        ("audio.output_result", {"name": WAVE, "heard": 1}),
        ("audio.output_nonsense", {}),
    ],
)
def test_bad_parameters_are_400(out: Outputs, action: str, params: dict[str, Any]) -> None:
    with pytest.raises(ActionError) as caught:
        out.act(action, params)
    assert caught.value.status == 400


def test_an_output_that_went_away_is_409_not_a_command(sound: Sound, out: Outputs) -> None:
    before = len(sound.commands)
    with pytest.raises(ActionError, match="not connected any more") as caught:
        out.act("audio.output_volume", {"name": "alsa_output.gone", "percent": 10})
    assert caught.value.status == 409
    assert len(sound.commands) == before


def test_a_refusing_audio_server_is_explained(sound: Sound, out: Outputs) -> None:
    sound.fail_wpctl = True
    with pytest.raises(
        ActionError, match=r"did not accept the change \(volume of Elgato Wave XLR\): Failure: No such entity"
    ):
        out.act("audio.output_volume", {"name": WAVE, "percent": 10})
    assert device(out.state(), WAVE)["volume"] == 40  # nothing is shown that did not happen


def test_wpctl_missing_blocks_changes(sound: Sound, out: Outputs) -> None:
    sound.have.discard("wpctl")
    with pytest.raises(ActionError, match="wpctl is missing"):
        out.act("audio.output_mute", {"name": WAVE, "on": True})


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


def test_test_plays_on_that_sink_only_and_never_touches_the_default(sound: Sound, out: Outputs) -> None:
    message = out.act("audio.output_test", {"name": HDMI})
    assert message and "DG2 Audio Controller" in message
    state = wait_idle(out)
    assert sound.played == [HDMI]
    assert not any(c[:2] == ["wpctl", "set-default"] for c in sound.commands)
    assert state["default"] == WAVE and sound.default == WAVE
    result = device(state, HDMI)["result"]
    assert result["result"] == "pending" and result["error"] is None and result["at"]
    assert device(state, WAVE)["result"] is None


def test_the_state_says_what_is_playing_and_how_far(sound: Sound, out: Outputs) -> None:
    sound.hold = True
    out.act("audio.output_test", {"name": WAVE})
    deadline = time.monotonic() + 3
    while not sound.played and time.monotonic() < deadline:
        time.sleep(0.005)
    state = out.state()
    assert state["test"]["state"] == "playing" and state["test"]["name"] == WAVE and state["test"]["all"] is False
    assert state["test"]["elapsed"] is not None and state["test"]["elapsed"] >= 0
    assert device(state, WAVE)["playing"] and not device(state, HDMI)["playing"]
    with pytest.raises(ActionError, match="already playing"):
        out.act("audio.output_test", {"name": HDMI})
    with pytest.raises(ActionError, match="already playing"):
        out.act("audio.output_test_all", {})
    sound.play_hold.set()
    assert wait_idle(out)["test"]["elapsed"] is None


def test_a_slow_disk_never_blocks_the_state_or_the_end_of_a_test(
    sound: Sound, out: Outputs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#163: the results file is written outside the lock `state()` takes: a stalled disk delays only the save."""
    writing, release = threading.Event(), threading.Event()

    def slow_write(path: Path | str, data: Any, **_: Any) -> Path:
        writing.set()
        assert release.wait(30), "the test never released the disk"
        return Path(path)

    monkeypatch.setattr(outputs.filestore, "write_json", slow_write)
    try:
        out.act("audio.output_test", {"name": WAVE})
        assert writing.wait(30), "the test thread never reached the save"
        answered: list[dict[str, Any]] = []
        poller = threading.Thread(target=lambda: answered.append(out.state()), daemon=True)
        poller.start()
        poller.join(timeout=5)
        assert answered, "state() is blocked behind the results write"
        assert device(answered[0], WAVE)["result"]["result"] == "pending"  # the verdict is already in memory
    finally:
        release.set()
    assert wait_idle(out, timeout=10)["test"]["state"] == "idle"


def test_stop_ends_a_running_test_and_records_nothing(sound: Sound, out: Outputs) -> None:
    sound.hold = True
    out.act("audio.output_test", {"name": WAVE})
    while not sound.played:
        time.sleep(0.005)
    out.act("audio.output_test_stop", {})
    state = wait_idle(out)
    assert device(state, WAVE)["result"] is None
    out.act("audio.output_test_stop", {})  # stopping nothing is fine


def test_a_player_that_fails_is_recorded_and_toasted(sound: Sound, out: Outputs, toasts: Toasts) -> None:
    sound.play_error = "The audio server refused to play on it (pw-play exit 1)."
    out.act("audio.output_test", {"name": HDMI})
    result = device(wait_idle(out), HDMI)["result"]
    assert result["result"] == "failed" and "refused to play" in result["error"]
    assert ("warn", "Test on DG2 Audio Controller failed: " + sound.play_error) in toasts.items
    with pytest.raises(ActionError, match="first"):  # nothing to judge: it never played
        out.act("audio.output_result", {"name": HDMI, "heard": True})


def test_pw_play_missing_is_explained(sound: Sound, out: Outputs) -> None:
    sound.have.discard("pw-play")
    for action, params in (("audio.output_test", {"name": WAVE}), ("audio.output_test_all", {})):
        with pytest.raises(ActionError, match="pw-play is missing") as caught:
            out.act(action, params)
        assert caught.value.status == 409
    assert sound.played == []


def test_a_route_cannot_be_tested(sound: Sound, out: Outputs) -> None:
    with pytest.raises(ActionError, match="route, not a device") as caught:
        out.act("audio.output_test", {"name": EE})
    assert caught.value.status == 409 and sound.played == []


def test_a_muted_or_silent_output_is_refused_before_anything_plays(sound: Sound, out: Outputs) -> None:
    out.act("audio.output_mute", {"name": WAVE, "on": True})
    with pytest.raises(ActionError, match="muted"):
        out.act("audio.output_test", {"name": WAVE})
    out.act("audio.output_volume", {"name": HDMI, "percent": 0})
    with pytest.raises(ActionError, match="all the way down"):
        out.act("audio.output_test", {"name": HDMI})
    assert sound.played == []


def test_the_person_judges_a_played_test(sound: Sound, out: Outputs) -> None:
    out.act("audio.output_test", {"name": WAVE})
    wait_idle(out)
    out.act("audio.output_result", {"name": WAVE, "heard": True})
    first = device(out.state(), WAVE)["result"]
    assert first["result"] == "heard"
    out.act("audio.output_result", {"name": WAVE, "heard": False})
    assert device(out.state(), WAVE)["result"] == {**first, "result": "not-heard"}


def test_judging_before_any_test_is_409(out: Outputs) -> None:
    with pytest.raises(ActionError, match="Play the test on Elgato Wave XLR first"):
        out.act("audio.output_result", {"name": WAVE, "heard": True})


def test_test_all_steps_through_every_device_in_turn_and_skips_routes(
    monkeypatch: pytest.MonkeyPatch, sound: Sound, out: Outputs, toasts: Toasts
) -> None:
    monkeypatch.setattr(outputs, "TEST_GAP_S", 0.0)
    assert out.act("audio.output_test_all", {}) == "Testing 2 outputs one after the other"
    state = wait_idle(out)
    assert sound.played == [HDMI, WAVE]
    assert all(device(state, n)["result"]["result"] == "pending" for n in (HDMI, WAVE))
    assert ("ok", "Output test finished: mark each output as heard or not heard") in toasts.items


def test_test_all_reports_progress_and_can_be_stopped(sound: Sound, out: Outputs) -> None:
    sound.hold = True
    out.act("audio.output_test_all", {})
    while not sound.played:
        time.sleep(0.005)
    test = out.state()["test"]
    assert (test["all"], test["index"], test["total"], test["name"]) == (True, 1, 2, HDMI)
    out.act("audio.output_test_stop", {})
    state = wait_idle(out)
    assert sound.played == [HDMI]
    assert device(state, WAVE)["result"] is None and device(state, HDMI)["result"] is None


def test_test_all_records_a_muted_output_as_failed_and_goes_on(
    monkeypatch: pytest.MonkeyPatch, sound: Sound, out: Outputs
) -> None:
    monkeypatch.setattr(outputs, "TEST_GAP_S", 0.0)
    out.act("audio.output_mute", {"name": HDMI, "on": True})
    out.act("audio.output_test_all", {})
    state = wait_idle(out)
    assert sound.played == [WAVE]
    assert device(state, HDMI)["result"]["result"] == "failed" and "muted" in device(state, HDMI)["result"]["error"]
    assert device(state, WAVE)["result"]["result"] == "pending"


def test_an_output_unplugged_mid_test_all_is_a_failure_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, sound: Sound, out: Outputs
) -> None:
    monkeypatch.setattr(outputs, "TEST_GAP_S", 0.0)
    sound.hold = True
    out.act("audio.output_test_all", {})
    while not sound.played:
        time.sleep(0.005)
    sound.unplug(WAVE)
    out.refresh()
    sound.play_hold.set()
    state = wait_idle(out)
    assert sound.played == [HDMI]
    assert state["devices"][0]["name"] == HDMI
    assert out._results[WAVE]["result"] == "failed" and "not connected" in out._results[WAVE]["error"]


def test_a_test_sound_that_cannot_be_written_is_an_error_toast(
    monkeypatch: pytest.MonkeyPatch, sound: Sound, out: Outputs, toasts: Toasts
) -> None:
    def broken(path: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(outputs, "synth_test_wav", broken)
    out.act("audio.output_test", {"name": WAVE})
    wait_idle(out)
    assert ("error", "The test sound could not be written: disk full") in toasts.items
    assert sound.played == []


def test_the_test_file_is_removed_afterwards(monkeypatch: pytest.MonkeyPatch, sound: Sound, out: Outputs) -> None:
    seen: list[str] = []
    original = sound.play

    def spy(sink_name: str, path: str, timeout: float, stop: threading.Event | None = None) -> str | None:
        seen.append(path)
        return original(sink_name, path, timeout, stop)

    monkeypatch.setattr(system, "play", spy)
    out.act("audio.output_test", {"name": WAVE})
    wait_idle(out)
    assert seen and not Path(seen[0]).exists()


# ---------------------------------------------------------------------------
# Remembered results
# ---------------------------------------------------------------------------


def test_results_survive_a_restart(sound: Sound, toasts: Toasts, isolated_home: Path) -> None:
    store = isolated_home / "outputs.json"
    first = Outputs(toasts, store=store)
    first.refresh()
    first.act("audio.output_test", {"name": WAVE})
    wait_idle(first)
    first.act("audio.output_result", {"name": WAVE, "heard": True})
    second = Outputs(toasts, store=store)
    second.refresh()
    assert device(second.state(), WAVE)["result"]["result"] == "heard"
    assert device(second.state(), HDMI)["result"] is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[]",
        '{"results": []}',
        '{"results": {"a": "x", "b": {"result": "bogus", "at": "t"}, "c": {"result": "heard"}}}',
    ],
)
def test_a_damaged_results_file_starts_empty(sound: Sound, toasts: Toasts, isolated_home: Path, text: str) -> None:
    store = isolated_home / "outputs.json"
    store.write_text(text)
    o = Outputs(toasts, store=store)
    o.refresh()
    assert all(d["result"] is None for d in o.state()["devices"])


def test_an_unwritable_store_does_not_stop_the_test(sound: Sound, toasts: Toasts, isolated_home: Path) -> None:
    blocker = isolated_home / "file"
    blocker.write_text("x")
    o = Outputs(toasts, store=blocker / "outputs.json")  # a directory cannot be made under a file
    o.refresh()
    o.act("audio.output_test", {"name": WAVE})
    assert device(wait_idle(o), WAVE)["result"]["result"] == "pending"


def test_only_the_newest_results_are_kept(sound: Sound, out: Outputs) -> None:
    for i in range(outputs.MAX_RESULTS + 5):
        out._results[f"old-{i}"] = {"at": f"2020-01-01T00:00:{i:02d}", "result": "heard", "error": None}
    out.act("audio.output_test", {"name": WAVE})
    wait_idle(out)
    assert len(out._results) == outputs.MAX_RESULTS and WAVE in out._results and "old-0" not in out._results


# ---------------------------------------------------------------------------
# Hot-plug and the default going away
# ---------------------------------------------------------------------------


def test_an_unplugged_default_raises_one_warning_that_stays(sound: Sound, out: Outputs, toasts: Toasts) -> None:
    sound.unplug(WAVE)
    out.refresh()
    out.refresh()  # a second look must not repeat the toast
    state = out.state()
    assert [d["name"] for d in state["devices"]] == [HDMI]
    assert state["default"] == HDMI
    warning = state["warning"]
    assert warning["kind"] == "default-lost" and warning["name"] == WAVE and warning["label"] == "Elgato Wave XLR"
    assert (
        warning["message"]
        == "Elgato Wave XLR was the default output and is gone. Sound now plays on DG2 Audio Controller."
    )
    assert toasts.items == [("warn", warning["message"])]


def test_the_warning_goes_when_the_device_comes_back(sound: Sound, out: Outputs) -> None:
    gone = sound.find(WAVE)
    sound.unplug(WAVE)
    out.refresh()
    assert out.state()["warning"] is not None
    sound.sinks.append(gone)
    out.refresh()
    assert out.state()["warning"] is None


def test_the_warning_can_be_dismissed(sound: Sound, out: Outputs) -> None:
    sound.unplug(WAVE)
    out.refresh()
    out.act("audio.output_dismiss", {})
    assert out.state()["warning"] is None
    out.refresh()
    assert out.state()["warning"] is None  # dismissed is dismissed


def test_losing_the_last_output_says_nothing_is_selected(sound: Sound, out: Outputs) -> None:
    sound.sinks = [s for s in sound.sinks if s["info"]["props"]["node.name"] == WAVE]
    sound.default = WAVE
    out.refresh()
    sound.sinks, sound.default = [], ""
    out.refresh()
    assert out.state()["warning"]["message"].endswith("No output is selected now.")


def test_a_non_default_output_unplugged_is_no_warning(sound: Sound, out: Outputs) -> None:
    sound.unplug(HDMI)
    out.refresh()
    assert out.state()["warning"] is None and len(out.state()["devices"]) == 1


def test_a_changed_default_from_outside_is_followed_without_a_warning(sound: Sound, out: Outputs) -> None:
    sound.default = HDMI
    out.refresh()
    state = out.state()
    assert state["default"] == HDMI and state["warning"] is None


class Subscription:
    """A fake `pw-dump --monitor`: the JSON the test writes appears on stdout, closing it ends the stream."""

    def __init__(self) -> None:
        read, self._write = os.pipe()
        self.stdout = os.fdopen(read, "rb", buffering=0)
        self.closed = False

    def emit(self, *objects: Any) -> None:
        """One pw-dump batch: a pretty-printed JSON array, as the real tool writes it."""
        os.write(self._write, json.dumps(list(objects), indent=2).encode() + b"\n")

    def poll(self) -> int | None:
        return 0 if self.closed else None

    def send_signal(self, sig: int) -> None:
        self.close()

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            os.close(self._write)


def wait_for(probe: Callable[[], Any], timeout: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := probe():
            return value
        time.sleep(0.01)
    raise AssertionError("condition not reached")


def test_plug_events_refresh_the_list_at_once(monkeypatch: pytest.MonkeyPatch, sound: Sound, toasts: Toasts) -> None:
    monkeypatch.setattr(outputs, "SETTLE_S", 0.01)
    subs: list[Subscription] = []

    def spawn(argv: list[str], **_: Any) -> Subscription:
        assert argv == ["pw-dump", "--monitor"]
        subs.append(Subscription())
        return subs[-1]

    monkeypatch.setattr(subprocess, "Popen", spawn)
    o = Outputs(toasts)
    o.start()
    try:
        wait_for(lambda: subs)
        assert len(o.state()["devices"]) == 2
        sound.unplug(HDMI)
        subs[0].emit({"id": IDS[HDMI], "info": None})  # a known sink goes
        wait_for(lambda: len(o.state()["devices"]) == 1)
        assert o.state()["warning"] is None
        sound.unplug(WAVE)
        subs[0].emit(
            {"id": 200, "type": "PipeWire:Interface:Node", "info": {"props": {"media.class": "Stream/Output/Audio"}}}
        )
        subs[0].emit({"id": 201, "info": None})  # a stream node, not a sink: no refresh yet
        time.sleep(0.15)
        assert len(o.state()["devices"]) == 1 and o.state()["warning"] is None
        subs[0].emit(metadata(WAVE))  # the default moved
        wait_for(lambda: o.state()["warning"])
    finally:
        o.stop()
    assert subs[0].closed


def test_the_watch_restarts_when_pw_dump_monitor_dies(
    monkeypatch: pytest.MonkeyPatch, sound: Sound, toasts: Toasts
) -> None:
    monkeypatch.setattr(outputs, "SETTLE_S", 0.01)
    monkeypatch.setattr(outputs, "WATCH_RETRY_S", 0.01)
    subs: list[Subscription] = []

    def spawn(argv: list[str], **_: Any) -> Subscription:
        subs.append(Subscription())
        return subs[-1]

    monkeypatch.setattr(subprocess, "Popen", spawn)
    o = Outputs(toasts)
    o.start()
    try:
        wait_for(lambda: subs)
        subs[0].close()  # the server went away
        wait_for(lambda: len(subs) >= 2)
    finally:
        o.stop()


def test_the_watch_survives_a_pw_dump_that_cannot_start(
    monkeypatch: pytest.MonkeyPatch, sound: Sound, toasts: Toasts
) -> None:
    monkeypatch.setattr(outputs, "WATCH_RETRY_S", 0.01)
    tries: list[int] = []

    def spawn(argv: list[str], **_: Any) -> Subscription:
        tries.append(1)
        raise FileNotFoundError("pw-dump")

    monkeypatch.setattr(subprocess, "Popen", spawn)
    o = Outputs(toasts)
    o.start()
    try:
        wait_for(lambda: len(tries) >= 2)
    finally:
        o.stop()


def test_poll_skips_the_probe_while_the_watch_is_alive_and_recent(sound: Sound, out: Outputs) -> None:
    out.refresh()
    before = len(sound.commands)
    out.poll()  # no watch: refresh
    assert len(sound.commands) > before
    out._watching = True
    before = len(sound.commands)
    out.poll()  # watched and fresh: nothing
    assert len(sound.commands) == before
    out._last_refresh = -1e9
    out.poll()  # watched but stale: the safety net
    assert len(sound.commands) > before


def test_no_pw_dump_means_no_watch_thread(sound: Sound, toasts: Toasts) -> None:
    sound.have.discard("pw-dump")
    o = Outputs(toasts)
    o.start()
    assert o._threads == []
    o.stop()


# ---------------------------------------------------------------------------
# system.play, the one place pw-play is run
# ---------------------------------------------------------------------------


class Player:
    def __init__(self, code: int | None, stderr: bytes = b"", ends_after: float = 0.0) -> None:
        self.code, self.returncode = code, None
        self.stderr = io.BytesIO(stderr)
        self.pid = 4242
        self._ends = time.monotonic() + ends_after
        self.signals: list[int] = []

    def poll(self) -> int | None:
        if self.code is not None and time.monotonic() >= self._ends:
            self.returncode = self.code
        return self.returncode

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        self.code, self._ends = -sig, 0.0

    def wait(self, timeout: float | None = None) -> int:
        self.poll()
        if self.returncode is None:
            raise subprocess.TimeoutExpired("pw-play", timeout or 0)
        return self.returncode


def patch_player(monkeypatch: pytest.MonkeyPatch, player: Player | Exception) -> list[list[str]]:
    argvs: list[list[str]] = []

    def spawn(argv: list[str], **kwargs: Any) -> Player:
        argvs.append(argv)
        if isinstance(player, Exception):
            raise player
        return player

    monkeypatch.setattr(subprocess, "Popen", spawn)
    return argvs


def test_play_names_the_sink_and_changes_no_default(monkeypatch: pytest.MonkeyPatch) -> None:
    argvs = patch_player(monkeypatch, Player(0))
    assert system.play(WAVE, "/tmp/x.wav", 5) is None
    assert argvs == [["pw-play", "--target", WAVE, "/tmp/x.wav"]]


def test_play_explains_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_player(monkeypatch, Player(1, b"Failure: No such entity\n"))
    assert (
        system.play(WAVE, "x.wav", 5)
        == "The audio server refused to play on it: Failure: No such entity (pw-play exit 1)."
    )


def test_play_that_cannot_start_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_player(monkeypatch, FileNotFoundError("no pw-play"))
    assert system.play(WAVE, "x.wav", 5) == "pw-play could not start (no pw-play)."


def test_play_has_a_hard_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    player = Player(None)  # never finishes by itself
    patch_player(monkeypatch, player)
    result = system.play(WAVE, "x.wav", 0.1, grace=(0.0, 0.0, 0.0))
    assert result == "The audio server did not finish playing within 0.1 s."
    assert player.signals  # it was reaped, not left running


def test_play_stops_when_asked_and_that_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    player = Player(None)
    patch_player(monkeypatch, player)
    stop = threading.Event()
    stop.set()
    assert system.play(WAVE, "x.wav", 30, stop, grace=(0.0, 0.0, 0.0)) is None
    assert player.signals


# ---------------------------------------------------------------------------
# The demo twin and the controller wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def demo(monkeypatch: pytest.MonkeyPatch, toasts: Toasts) -> Iterator[DemoOutputs]:
    monkeypatch.setattr(outputs, "DEMO_PLAY_S", 0.05)
    monkeypatch.setattr(outputs, "TEST_GAP_S", 0.0)
    d = DemoOutputs(toasts)
    yield d
    d.stop()


def test_the_demo_has_four_outputs_and_a_route(demo: DemoOutputs) -> None:
    state = demo.state()
    assert [d["kind"] for d in state["devices"]] == ["headphones", "interface", "hdmi", "headset"]
    assert [d["bus"] for d in state["devices"]] == ["usb", "usb", "pci", "bluetooth"]
    assert [r["label"] for r in state["routes"]] == ["StreamStack Route"]
    assert state["devices"][0]["default"] and sum(d["default"] for d in state["devices"]) == 1


def test_the_real_and_the_demo_state_have_the_same_shape(sound: Sound, out: Outputs, demo: DemoOutputs) -> None:
    real, simulated = out.state(), demo.state()
    assert real.keys() == simulated.keys()
    assert real["devices"][0].keys() == simulated["devices"][0].keys()
    assert real["routes"][0].keys() == simulated["routes"][0].keys()
    assert real["test"].keys() == simulated["test"].keys()


def test_demo_actions_change_nothing_on_the_machine(demo: DemoOutputs, sound: Sound) -> None:
    sound.have.clear()  # not even wpctl or pw-play exist: the demo never needs them
    demo.act("audio.output_volume", {"name": demo.state()["devices"][2]["name"], "percent": 12})
    demo.act("audio.output_mute", {"name": demo.state()["devices"][1]["name"], "on": True})
    demo.act("audio.output_default", {"name": demo.state()["devices"][3]["name"]})
    state = demo.state()
    assert state["devices"][2]["volume"] == 12 and state["devices"][1]["muted"] and state["devices"][3]["default"]
    demo.act("audio.output_test", {"name": state["devices"][3]["name"]})
    assert device(wait_idle(demo), state["devices"][3]["name"])["result"]["result"] == "pending"
    assert sound.commands == [] and sound.played == []


def test_demo_unplugging_the_default_falls_back_and_warns(demo: DemoOutputs, toasts: Toasts) -> None:
    first = demo.state()["devices"][0]["name"]
    demo.act("audio.demo_output_plug", {"name": first, "plugged": False})
    state = demo.state()
    assert state["devices"][0]["name"] != first and len(state["devices"]) == 3
    assert state["warning"]["name"] == first and state["devices"][0]["default"]
    assert toasts.items and toasts.items[0][0] == "warn"
    demo.act("audio.demo_output_plug", {"name": first, "plugged": True})
    assert len(demo.state()["devices"]) == 4 and demo.state()["warning"] is None


@pytest.mark.parametrize(
    "params", [{}, {"name": "nope", "plugged": True}, {"name": "x", "plugged": "yes"}, {"plugged": False}]
)
def test_demo_plug_validates(demo: DemoOutputs, params: dict[str, Any]) -> None:
    with pytest.raises(ActionError) as caught:
        demo.act("audio.demo_output_plug", params)
    assert caught.value.status == 400


def test_demo_reset_restores_everything(demo: DemoOutputs) -> None:
    names = [d["name"] for d in demo.state()["devices"]]
    baseline = demo.state()
    demo.act("audio.output_volume", {"name": names[0], "percent": 5})
    demo.act("audio.output_mute", {"name": names[1], "on": True})
    demo.act("audio.output_default", {"name": names[2]})
    demo.act("audio.output_test", {"name": names[3]})
    demo.act("audio.demo_output_plug", {"name": names[1], "plugged": False})
    demo.act("audio.demo_output_reset", {})
    assert wait_idle(demo) == baseline


def test_demo_reset_stops_a_running_test(demo: DemoOutputs, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outputs, "DEMO_PLAY_S", 30.0)
    demo.act("audio.output_test_all", {})
    demo.act("audio.demo_output_reset", {})
    assert demo.state()["test"]["state"] == "idle"
    assert all(d["result"] is None for d in demo.state()["devices"])


def test_the_audio_controllers_carry_the_outputs(sound: Sound, isolated_home: Path) -> None:
    controller = AudioController(config.load_settings())
    controller._probe()
    assert [d["name"] for d in controller.state()["outputs"]["devices"]] == [HDMI, WAVE]
    controller.act("audio.output_volume", {"name": WAVE, "percent": 30})
    assert ["wpctl", "set-volume", "45", "30%"] in sound.commands
    with pytest.raises(ActionError, match="unknown audio action"):
        controller.act("audio.nonsense", {})
    simulated = DemoAudio()
    assert simulated.state()["outputs"].keys() == controller.state()["outputs"].keys()
    simulated.act("audio.output_mute", {"name": simulated.state()["outputs"]["devices"][0]["name"], "on": True})
    assert simulated.state()["outputs"]["devices"][0]["muted"]
    with pytest.raises(ActionError, match="unknown audio action"):
        simulated.act("audio.output_nonsense", {})
    simulated.stop()


# ---------------------------------------------------------------------------
# The watch stream
# ---------------------------------------------------------------------------


def test_json_stream_waits_for_a_value_to_finish() -> None:
    stream = outputs.JsonStream()
    text = json.dumps([{"id": 1}], indent=2) + "\n" + json.dumps([{"id": 2}])
    cut = text.index('"id": 2') + 3
    assert stream.feed(text[:cut].encode()) == [[{"id": 1}]]  # the second batch is half written
    assert stream.feed(text[cut:].encode()) == [[{"id": 2}]]
    assert stream.feed(b"  \n") == []


def test_json_stream_gives_up_on_a_stream_that_never_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outputs, "WATCH_BUFFER", 10)
    with pytest.raises(ValueError, match="does not parse"):
        outputs.JsonStream().feed(b"this is not json at all")


def test_only_changes_to_sinks_devices_and_the_default_concern_the_outputs() -> None:
    node = {"type": "PipeWire:Interface:Node", "info": {"props": {"media.class": "Stream/Output/Audio"}}}
    sink_node = {"type": "PipeWire:Interface:Node", "id": 7, "info": {"props": {"media.class": "Audio/Sink"}}}
    assert not outputs.concerns_outputs([[node, {"id": 9, "info": None}]], {45})
    assert outputs.concerns_outputs([[{"id": 45, "info": None}]], {45})  # a known sink is removed
    assert outputs.concerns_outputs([[sink_node]], set())  # a new sink
    assert outputs.concerns_outputs([[{"type": "PipeWire:Interface:Device", "id": 3, "info": {}}]], set())
    assert outputs.concerns_outputs([[metadata(WAVE)]], set())
    assert not outputs.concerns_outputs(["junk", [1]], set())
