"""Console audio: level maths, pactl parsing, tone mapping, coalescing, mic check and the demo twin."""

from __future__ import annotations

import array
import json
import math
import os
import subprocess
import threading
import time
import wave
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from streamstack import config, micwatch, system
from streamstack.audiochain import chain as chainlib
from streamstack.audiochain import graph as chaingraph
from streamstack.audiochain import stages
from streamstack.console import audio
from streamstack.console.audio import AudioController, DemoAudio, LevelReader, Throttle
from streamstack.console.errors import ActionError

MIC = "alsa_input.usb-Elgato_Wave"
ENGINES = ["deepfilternet", "rnnoise"]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def pactl_json(gain: str = "-3.00 dB", mute: bool = False, with_voice: bool = True) -> str:
    sources = [
        {
            "name": MIC,
            "description": "Wave:3",
            "mute": mute,
            "volume": {"mono": {"value": 46000, "value_percent": "70%", "db": gain}},
        },
        {"name": "alsa_output.x.monitor", "description": "Monitor", "mute": False, "volume": {}},
    ]
    if with_voice:
        sources.append({"name": audio.VOICE_SOURCE_NAME, "description": "StreamStack Mic", "mute": False, "volume": {}})
    return json.dumps(sources)


class Rig:
    def __init__(self) -> None:
        self.clock = Clock()
        self.commands: list[list[str]] = []
        self.pactl_out = pactl_json()
        self.have = {"pactl", "parec", "parecord"}
        self.recorded: list[tuple[str, float]] = []
        self.record_ok = True
        self.procs: list[FakeProc] = []

    def spawn(self, argv: list[str], **_: Any) -> FakeProc:
        """Every parec the code under test starts is a fake: tests never read the real microphone."""
        proc = FakeProc()
        self.procs.append(proc)
        return proc

    def run(self, argv: list[str], timeout: float = 10) -> subprocess.CompletedProcess[str] | None:
        self.commands.append(argv)
        if argv[0] == "pactl" and "list" in argv:
            return subprocess.CompletedProcess(argv, 0, self.pactl_out, "")
        if argv[0] == "pactl" and argv[1] == "get-default-source":
            return subprocess.CompletedProcess(argv, 0, MIC + "\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def record(self, source: str, seconds: float, path: str) -> bool:
        self.recorded.append((source, seconds))
        if self.record_ok:
            Path(path).write_bytes(b"RIFF" + b"\0" * 4000)
        return self.record_ok


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch, isolated_home: Path) -> Iterator[Rig]:
    r = Rig()
    monkeypatch.setattr(system, "run", r.run)
    monkeypatch.setattr(system, "have", lambda tool: tool in r.have)
    monkeypatch.setattr(system, "default_device", lambda kind: MIC)
    monkeypatch.setattr(system, "record", r.record)
    monkeypatch.setattr(subprocess, "Popen", r.spawn)
    # the chain host is never started by these tests: no pipewire is touched, the plugins are "installed"
    monkeypatch.setattr(chaingraph, "available_engines", lambda: list(ENGINES))
    yield r
    for proc in r.procs:
        proc.kill()


@pytest.fixture
def ctl(rig: Rig) -> AudioController:
    controller = AudioController(config.load_settings(), clock=rig.clock)
    controller._probe()
    return controller


# ---------------------------------------------------------------------------
# dB maths
# ---------------------------------------------------------------------------


def test_meter_frame_has_every_field() -> None:
    assert audio.meter_frame() == {
        "peak_db": -90.0,
        "sample_db": -90.0,
        "rms_db": -90.0,
        "clip": False,
        "latched": False,
        "lufs_m": -90.0,
        "lufs_s": -90.0,
    }


# ---------------------------------------------------------------------------
# pactl parsing
# ---------------------------------------------------------------------------


def test_parse_sources_reads_gain_and_mute() -> None:
    found = audio.parse_sources(pactl_json(gain="4.50 dB", mute=True))
    mic = next(s for s in found if s["name"] == MIC)
    assert mic == {"name": MIC, "description": "Wave:3", "gain_db": 4.5, "muted": True}


def test_parse_volume_falls_back_to_cubic_value() -> None:
    assert audio.parse_volume_db({"mono": {"value": 65536}}) == 0.0
    assert audio.parse_volume_db({"mono": {"value": 32768}}) == pytest.approx(-18.06, abs=0.01)
    assert audio.parse_volume_db({"mono": {"value": 0, "db": "-inf dB"}}) is None
    assert audio.parse_volume_db({}) is None
    assert audio.parse_volume_db("nonsense") is None


def test_parse_sources_survives_garbage() -> None:
    assert audio.parse_sources("not json") == []
    assert audio.parse_sources('{"a": 1}') == []
    assert audio.parse_sources('[1, {"description": "no name"}]') == []


# ---------------------------------------------------------------------------
# Level readers
# ---------------------------------------------------------------------------


def frame(value: float, count: int = 960) -> bytes:
    """One 20 ms frame of float32 alternating +value/-value (a fs/2 tone: its true peak is the sample peak)."""
    return array.array("f", [value, -value] * (count // 2)).tobytes()


def silent_frames(reader: LevelReader, n: int) -> None:
    for _ in range(n):
        reader.feed(frame(0.0))


def test_level_reader_frames_across_chunk_boundaries() -> None:
    clock = Clock()
    reader = LevelReader("src", clock)
    data = frame(0.5)
    reader.feed(data[:700])
    assert reader.take()["peak_db"] == -90.0  # no complete frame yet
    reader.feed(data[700:])
    taken = reader.take()
    assert taken["sample_db"] == -6.0
    assert taken["rms_db"] == -6.0
    assert taken["peak_db"] >= -6.0  # a true peak is never below the sample peak
    assert taken["clip"] is False
    assert taken["latched"] is False


def test_level_reader_keeps_the_loudest_peak_between_polls() -> None:
    clock = Clock()
    reader = LevelReader("src", clock)
    reader.feed(frame(1.0) + frame(0.03) + frame(0.06))
    taken = reader.take()
    assert taken["clip"] is True
    assert taken["peak_db"] >= 0.0
    assert taken["sample_db"] == 0.0
    assert taken["rms_db"] == audio.to_db(0.06)  # the latest frame's RMS
    # nothing new: the last frame's own values, no lingering clip
    again = reader.take()
    assert again["clip"] is False
    assert again["sample_db"] == audio.to_db(0.06)


def test_level_reader_latches_a_clip_until_cleared() -> None:
    clock = Clock()
    reader = LevelReader("src", clock)
    reader.feed(frame(0.9) + frame(0.1))  # 0.9 is -0.9 dBFS: over the -1 dBTP ceiling
    assert reader.take()["latched"] is True
    silent_frames(reader, 3)
    assert reader.take()["latched"] is True  # stays lit long after the peak is gone
    clock.advance(5.0)
    assert reader.take()["latched"] is True  # and through a silent, stale reader
    reader.latch.clear()
    assert reader.take()["latched"] is False


def test_level_reader_ignores_a_non_finite_frame() -> None:
    clock = Clock()
    reader = LevelReader("src", clock)
    reader.feed(array.array("f", [float("nan")] * 960).tobytes() + frame(0.25))
    taken = reader.take()
    assert taken["sample_db"] == pytest.approx(-12.0, abs=0.1)
    assert taken["clip"] is False


def test_level_reader_reports_loudness() -> None:
    clock = Clock()
    reader = LevelReader("src", clock)
    tone = array.array("f", (0.1 * math.sin(2 * math.pi * 997 * i / 48000) for i in range(48000)))
    for start in range(0, len(tone), 960):
        reader.feed(tone[start : start + 960].tobytes())
    taken = reader.take()
    assert taken["lufs_m"] == pytest.approx(-23.0, abs=0.1)  # -20 dBFS mono sine: -23.0 LUFS
    assert taken["lufs_s"] == pytest.approx(-23.0, abs=0.1)


def test_level_reader_goes_silent_when_frames_stop() -> None:
    clock = Clock()
    reader = LevelReader("src", clock)
    reader.feed(frame(0.5))
    clock.advance(0.6)
    assert reader.take() == audio.meter_frame()


class FakeProc:
    def __init__(self) -> None:
        self._r, self._w = os.pipe()
        self.stdout = os.fdopen(self._r, "rb", buffering=0)
        self.killed = False

    def write(self, data: bytes) -> None:
        os.write(self._w, data)

    def kill(self) -> None:
        self.killed = True
        if self._w >= 0:
            os.close(self._w)
            self._w = -1

    def poll(self) -> int | None:
        return 0 if self.killed else None

    def send_signal(self, sig: int) -> None:
        self.kill()

    def wait(self, timeout: float | None = None) -> int:
        return 0


def test_level_reader_reads_a_process_and_stops_it() -> None:
    proc = FakeProc()
    argv_seen: list[list[str]] = []

    def spawn(argv: list[str], **_: Any) -> FakeProc:
        argv_seen.append(argv)
        return proc

    reader = LevelReader("mysrc", spawn=spawn)
    assert reader.start()
    assert "--device=mysrc" in argv_seen[0]
    assert "--latency-msec=20" in argv_seen[0]  # 20 ms frames
    assert "--rate=48000" in argv_seen[0]  # the sources' native rate, no resampling before the meter
    assert "--format=float32le" in argv_seen[0]
    proc.write(array.array("f", (0.25 * math.sin(2 * math.pi * 1000 * i / 48000) for i in range(960))).tobytes())
    for _ in range(100):
        if reader.take()["peak_db"] > -90:
            break
        threading.Event().wait(0.02)
    assert reader.take()["peak_db"] == pytest.approx(-12.0, abs=0.2)
    reader.stop()
    assert proc.killed
    assert reader.dead


def test_level_reader_start_failure_marks_dead() -> None:
    def spawn(argv: list[str], **_: Any) -> None:
        raise FileNotFoundError("parec")

    reader = LevelReader("s", spawn=spawn)
    assert not reader.start()
    assert reader.dead


def test_controller_manages_readers_as_sources_come_and_go(rig: Rig) -> None:
    procs: list[FakeProc] = []
    devices: list[str] = []

    def spawn(argv: list[str], **_: Any) -> FakeProc:
        devices.append(argv[-1].removeprefix("--device="))
        procs.append(FakeProc())
        return procs[-1]

    ctl = AudioController(config.load_settings(), clock=rig.clock, spawn=spawn)
    ctl._probe()
    assert sorted(devices) == sorted([MIC, audio.VOICE_SOURCE_NAME])
    assert ctl.meters()["voice"] is not None

    rig.pactl_out = pactl_json(with_voice=False)  # the voice chain went away
    ctl._probe()
    assert ctl.meters()["voice"] is None
    assert sum(p.killed for p in procs) == 1

    rig.pactl_out = "[]"  # microphone unplugged
    ctl._probe()
    assert ctl.state()["mic"]["present"] is False
    assert ctl.meters()["mic"]["peak_db"] == -90.0

    rig.pactl_out = pactl_json()  # back
    ctl._probe()
    assert devices.count(MIC) == 2
    ctl.stop()
    assert all(p.killed for p in procs)


def test_dead_reader_restarts_no_faster_than_every_two_seconds(rig: Rig) -> None:
    spawned: list[FakeProc] = []

    def spawn(argv: list[str], **_: Any) -> FakeProc:
        spawned.append(FakeProc())
        return spawned[-1]

    ctl = AudioController(config.load_settings(), clock=rig.clock, spawn=spawn)
    ctl._probe()
    first = len(spawned)
    for reader in ctl._readers.values():
        assert reader is not None
        reader.dead = True
    ctl._probe()
    assert len(spawned) == first  # too soon
    rig.clock.advance(2.5)
    ctl._probe()
    assert len(spawned) == first * 2


def test_no_parec_means_silent_meters_not_a_crash(rig: Rig) -> None:
    rig.have.discard("parec")
    ctl = AudioController(config.load_settings(), clock=rig.clock)
    ctl.start()
    try:
        assert ctl.meters()["mic"]["peak_db"] == -90.0
        assert ctl.state()["mic"]["present"] is True
    finally:
        ctl.stop()


# ---------------------------------------------------------------------------
# Coalescing
# ---------------------------------------------------------------------------


def test_throttle_fires_at_once_when_idle_then_trailing_newest_wins() -> None:
    clock = Clock()
    throttle = Throttle(0.4, clock)
    assert throttle.ready_in() is None
    throttle.submit(1)
    assert throttle.take() == 1  # idle: leading edge
    for value in (2, 3, 4):
        clock.advance(0.05)
        throttle.submit(value)
        assert throttle.take() is None
    assert throttle.ready_in() == pytest.approx(0.25)
    clock.advance(0.25)
    assert throttle.take() == 4  # trailing edge carries the newest
    assert throttle.take() is None
    assert throttle.ready_in() is None


def test_throttle_cancel() -> None:
    throttle = Throttle(0.4, Clock())
    throttle.submit(1)
    throttle.cancel()
    assert throttle.ready_in() is None
    assert throttle.take() is None


# ---------------------------------------------------------------------------
# The voice chain: presets, stages, denoise engine, loudness, bypass
# ---------------------------------------------------------------------------


def test_state_carries_the_chain_view_and_its_runtime_status(ctl: AudioController) -> None:
    chain = ctl.state()["chain"]
    assert chain["preset"] == "StreamStack Voice"
    assert [s["id"] for s in chain["stages"]] == [s.id for s in stages.STAGES]
    assert chain["source"] == "streamstack_mic" and chain["rate"] == 48000
    assert chain["running"] is False  # the host is only started by start(): nothing touched pipewire
    assert chain["loudness"]["target_lufs"] == -16.0 and chain["loudness"]["targets"] == [-14.0, -16.0, -23.0]
    assert {p["name"] for p in chain["presets"]} >= {
        "StreamStack Voice",
        "StreamStack Voice Warm",
        "StreamStack Voice Bright",
        "StreamStack Voice Deep",
        "StreamStack Voice Minimal",
        "StreamStack Voice DeepFilterNet",
    }
    json.dumps(ctl.state())


def test_tone_is_set_through_the_chain_and_marks_it_custom(ctl: AudioController) -> None:
    rev = ctl._chain.revision
    ctl.act("audio.tone", {"bass_db": 5, "treble_db": -2})
    assert ctl._chain.params["bass_db"] == 5.0 and ctl._chain.params["treble_db"] == -2.0
    assert ctl._chain.revision > rev
    state = ctl.state()
    assert state["tone"]["bass_db"] == 5.0 and state["tone"]["custom"] is True
    ctl.act("audio.chain_reset", {})
    assert ctl.state()["tone"]["bass_db"] == 2.0 and ctl.state()["tone"]["custom"] is False


def test_loading_a_preset_resets_the_tone_and_names_it(ctl: AudioController) -> None:
    ctl.act("audio.tone", {"bass_db": 9})
    assert ctl.act("audio.preset", {"name": "StreamStack Voice Deep"}) == "Voice preset: Voice Deep"
    state = ctl.state()
    assert state["chain"]["preset"] == "StreamStack Voice Deep"
    assert state["tone"]["custom"] is False and state["tone"]["bass_db"] == 5.0


def test_any_stage_parameter_is_live_adjustable(ctl: AudioController) -> None:
    ctl.act("audio.chain", {"params": {"comp_ratio": 4.5, "gate_on": False, "hp_hz": 120}})
    params = ctl._chain.params
    assert params["comp_ratio"] == 4.5 and params["gate_on"] is False and params["hp_hz"] == 120.0
    gate = next(s for s in ctl.state()["chain"]["stages"] if s["id"] == "gate")
    assert gate["on"] is False
    assert ctl.state()["chain"]["custom"] is True


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"params": {}},
        {"params": []},
        {"params": {"nope": 1}},
        {"params": {"comp_ratio": 99}},
        {"params": {"comp_ratio": "x"}},
        {"params": {"gate_on": 3}},
    ],
)
def test_bad_chain_parameters_are_400(ctl: AudioController, params: dict[str, Any]) -> None:
    with pytest.raises(ActionError) as err:
        ctl.act("audio.chain", params)
    assert err.value.status == 400


def test_denoise_reports_engines_and_switches(ctl: AudioController) -> None:
    dn = ctl.state()["denoise"]
    assert dn["engine"] == "deepfilternet" and dn["atten_limit_db"] == 100.0
    assert {e["id"] for e in dn["engines"]} == {"deepfilternet", "rnnoise"}
    assert ctl.act("audio.denoise", {"engine": "rnnoise"}) == "Noise removal: RNNoise"
    ctl.act("audio.denoise", {"atten_limit_db": 25})
    dn = ctl.state()["denoise"]
    assert dn["engine"] == "rnnoise" and dn["atten_limit_db"] == 25.0 and dn["custom"] is True


def test_a_missing_denoise_plugin_is_refused_with_the_install_hint(
    ctl: AudioController, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chaingraph, "available_engines", lambda: ["rnnoise"])
    with pytest.raises(ActionError) as err:
        ctl.act("audio.denoise", {"engine": "deepfilternet"})
    assert err.value.status == 409
    dn = ctl.state()["denoise"]
    assert dn["engine"] == "rnnoise"  # the chain falls back, as the host would
    assert next(e for e in dn["engines"] if e["id"] == "deepfilternet")["available"] is False
    strict = next(p for p in ctl.state()["chain"]["presets"] if p["name"] == "StreamStack Voice DeepFilterNet")
    assert strict["unavailable"]


@pytest.mark.parametrize(
    "params",
    [{}, {"engine": "krisp"}, {"engine": 3}, {"atten_limit_db": 101}, {"atten_limit_db": "x"}, {"bogus": 1}],
)
def test_bad_denoise_parameters_are_400(ctl: AudioController, params: dict[str, Any]) -> None:
    with pytest.raises(ActionError) as err:
        ctl.act("audio.denoise", params)
    assert err.value.status == 400


def test_loudness_switch_and_targets(ctl: AudioController) -> None:
    assert ctl.act("audio.loudness", {"on": True}) == "Loudness leveller on"
    assert ctl.act("audio.loudness", {"target_lufs": -14}) == "Loudness target: -14 LUFS"
    loud = ctl.state()["chain"]["loudness"]
    assert loud["on"] is True and loud["target_lufs"] == -14.0
    assert ctl.act("audio.loudness", {"on": False}) == "Loudness leveller off"


@pytest.mark.parametrize(
    "params",
    [{}, {"on": 1}, {"target_lufs": -10}, {"target_lufs": "x"}, {"target_lufs": True}],
)
def test_bad_loudness_parameters_are_400(ctl: AudioController, params: dict[str, Any]) -> None:
    with pytest.raises(ActionError) as err:
        ctl.act("audio.loudness", params)
    assert err.value.status == 400


def test_bypass_is_a_chain_switch(ctl: AudioController) -> None:
    assert ctl.act("audio.bypass", {"on": True}) == "Voice effects bypassed"
    assert ctl.state()["chain"]["bypass"] is True
    assert ctl.act("audio.bypass", {"on": False}) == "Voice effects on"


def test_save_preset_keeps_the_current_settings(ctl: AudioController) -> None:
    ctl.act("audio.tone", {"bass_db": 6})
    assert ctl.act("audio.preset_save", {"name": "Podcast", "blurb": "mine"}) == "Saved voice preset: Podcast"
    ctl.act("audio.preset", {"name": "StreamStack Voice"})
    ctl.act("audio.preset", {"name": "Podcast"})
    assert ctl.state()["tone"]["bass_db"] == 6.0
    for bad in ({}, {"name": " "}, {"name": 3}, {"name": "x", "blurb": 3}, {"name": "../x"}):
        with pytest.raises(ActionError) as err:
            ctl.act("audio.preset_save", bad)
        assert err.value.status == 400


def test_the_chain_is_remembered_per_microphone(rig: Rig) -> None:
    first = AudioController(config.load_settings(), clock=rig.clock)
    first._probe()
    first.act("audio.preset", {"name": "StreamStack Voice Warm"})
    first.act("audio.loudness", {"on": True, "target_lufs": -23})
    first._chain.flush()
    again = AudioController(config.load_settings(), clock=rig.clock)
    again._probe()
    chain = again.state()["chain"]
    assert chain["preset"] == "StreamStack Voice Warm"
    assert chain["loudness"]["on"] is True and chain["loudness"]["target_lufs"] == -23.0


def test_another_process_changing_the_chain_is_picked_up(ctl: AudioController, rig: Rig) -> None:
    other = chainlib.ChainModel(store=config.state_dir() / "audio-chain" / "state.json", rig_key=MIC)
    other.set_bypass(True)
    other.flush()
    ctl._chain.flush()
    later = time.time() + 5
    os.utime(config.state_dir() / "audio-chain" / "state.json", (later, later))
    ctl._probe()
    assert ctl.state()["chain"]["bypass"] is True


def test_the_gain_reduction_meter_is_an_estimate_from_the_mic_levels(ctl: AudioController) -> None:
    ctl.act("audio.chain", {"params": {"comp_threshold_db": -30, "comp_ratio": 4}})
    loud = chainlib.estimate_reduction(ctl._chain.params, -14.0, -6.0, 0.0)
    assert loud > 0.0
    assert chainlib.estimate_reduction(ctl._chain.params, -70.0, -60.0, 0.0) == 0.0


# ---------------------------------------------------------------------------
# Gain, mute and state
# ---------------------------------------------------------------------------


def test_state_shape_and_mic_readback(ctl: AudioController) -> None:
    state = ctl.state()
    assert state["mic"] == {
        "name": MIC,
        "description": "Wave:3",
        "present": True,
        "gain_db": -3.0,
        "muted": False,
        "stalled": False,
        "stalled_since": None,
        "revive": {"state": "idle", "message": None},
    }
    assert state["gain_range"] == [-30.0, 12.0]
    assert {"running", "available", "preset", "presets", "bypass", "stages", "loudness", "engines"} <= set(
        state["chain"]
    )
    assert set(state["tone_ranges"]) == {"bass_db", "presence_db", "treble_db"}
    assert state["mic_check"] == {"state": "idle", "seconds": 6.0, "error": None, "clips": []}
    json.dumps(state)


def test_gain_is_clamped_sent_to_pactl_and_optimistic(ctl: AudioController, rig: Rig) -> None:
    ctl.act("audio.gain", {"db": 40})
    assert ["pactl", "set-source-volume", MIC, "12.0dB"] in rig.commands
    assert ctl.state()["mic"]["gain_db"] == 12.0
    ctl.act("audio.gain", {"db": -99})
    assert ["pactl", "set-source-volume", MIC, "-30.0dB"] in rig.commands
    ctl.act("audio.gain", {"db": 2.345})
    assert ["pactl", "set-source-volume", MIC, "2.35dB"] in rig.commands or [
        "pactl",
        "set-source-volume",
        MIC,
        "2.34dB",
    ] in rig.commands


def test_probe_does_not_clobber_a_fresh_gain_change(ctl: AudioController, rig: Rig) -> None:
    ctl.act("audio.gain", {"db": 6.0})
    ctl._probe()  # pactl still reports -3 dB for a moment
    assert ctl.state()["mic"]["gain_db"] == 6.0
    rig.clock.advance(2)
    ctl._probe()
    assert ctl.state()["mic"]["gain_db"] == -3.0


def test_mute(ctl: AudioController, rig: Rig) -> None:
    ctl.act("audio.mute", {"on": True})
    assert ["pactl", "set-source-mute", MIC, "1"] in rig.commands
    assert ctl.state()["mic"]["muted"] is True
    ctl.act("audio.mute", {"on": False})
    assert ["pactl", "set-source-mute", MIC, "0"] in rig.commands


def test_gain_and_mute_need_a_microphone(rig: Rig) -> None:
    rig.pactl_out = "[]"
    ctl = AudioController(config.load_settings(), clock=rig.clock)
    ctl._probe()
    for action, params in (("audio.gain", {"db": 1}), ("audio.mute", {"on": True})):
        with pytest.raises(ActionError) as err:
            ctl.act(action, params)
        assert err.value.status == 409


def test_pactl_failure_is_a_409(ctl: AudioController, rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system, "run", lambda argv, timeout=10: subprocess.CompletedProcess(argv, 1, "", "boom"))
    with pytest.raises(ActionError) as err:
        ctl.act("audio.mute", {"on": True})
    assert err.value.status == 409
    assert ctl.state()["mic"]["muted"] is False  # no optimistic change when it failed


@pytest.mark.parametrize(
    ("action", "params"),
    [
        ("audio.gain", {}),
        ("audio.gain", {"db": "3"}),
        ("audio.gain", {"db": True}),
        ("audio.gain", {"db": float("nan")}),
        ("audio.gain", {"db": float("inf")}),
        ("audio.mute", {"on": "yes"}),
        ("audio.mute", {}),
        ("audio.bypass", {"on": 1}),
        ("audio.preset", {"name": 3}),
        ("audio.preset", {"name": "Nope"}),
        ("audio.preset", {"name": "../../etc/passwd"}),
        ("audio.tone", {}),
        ("audio.tone", {"bass_db": 13}),
        ("audio.tone", {"pitch_semitones": -7}),
        ("audio.tone", {"treble_db": "loud"}),
        ("audio.tone", {"bass_db": None}),
        ("audio.tone", {"bogus": 1}),
        ("audio.tone", {"bass_db": 1, "bogus": 1}),
        ("audio.mic_check", {"seconds": 1}),
        ("audio.mic_check", {"seconds": 16}),
        ("audio.mic_check", {"seconds": "6"}),
        ("audio.mic_check", {"seconds": False}),
        ("audio.nope", {}),
    ],
)
def test_bad_parameters_are_400(ctl: AudioController, action: str, params: dict[str, Any]) -> None:
    with pytest.raises(ActionError) as err:
        ctl.act(action, params)
    assert err.value.status == 400


def test_demo_denoise_switches_engine_and_follows_the_preset() -> None:
    demo = DemoAudio()
    assert demo.state()["denoise"]["engine"] == "deepfilternet"
    assert demo.act("audio.denoise", {"engine": "rnnoise"}) == "Noise removal: RNNoise"
    demo.act("audio.denoise", {"atten_limit_db": 25})
    dn = demo.state()["denoise"]
    assert dn["engine"] == "rnnoise" and dn["atten_limit_db"] == 25.0 and dn["custom"] is True
    assert demo.state()["tone"]["custom"] is True
    demo.act("audio.preset", {"name": "StreamStack Voice"})
    assert demo.state()["denoise"]["engine"] == "deepfilternet"
    assert demo.state()["denoise"]["custom"] is False


def test_demo_without_the_library_refuses_deepfilternet() -> None:
    demo = DemoAudio()
    demo.act("audio.demo_deepfilternet", {"on": False})
    state = demo.state()
    assert state["denoise"]["engine"] == "rnnoise"  # falls back, as a missing plugin would
    assert next(e for e in state["denoise"]["engines"] if e["id"] == "deepfilternet")["available"] is False
    for action, params in (
        ("audio.denoise", {"engine": "deepfilternet"}),
        ("audio.preset", {"name": "StreamStack Voice DeepFilterNet"}),
    ):
        with pytest.raises(ActionError) as err:
            demo.act(action, params)
        assert err.value.status == 409
    demo.act("audio.demo_deepfilternet", {"on": True})
    assert demo.act("audio.denoise", {"engine": "deepfilternet"}) == "Noise removal: DeepFilterNet"


@pytest.mark.parametrize(
    ("action", "bad"),
    [
        ("audio.gain", {"db": "x"}),
        ("audio.mute", {"on": 1}),
        ("audio.tone", {"bass_db": 99}),
        ("audio.denoise", {"engine": "krisp"}),
        ("audio.denoise", {"atten_limit_db": 101}),
        ("audio.mic_check", {"seconds": 99}),
        ("audio.preset", {"name": "Nope"}),
    ],
)
def test_demo_validates_like_the_real_thing(action: str, bad: dict[str, Any]) -> None:
    with pytest.raises(ActionError) as err:
        DemoAudio().act(action, bad)
    assert err.value.status == 400


# ---------------------------------------------------------------------------
# Mic check
# ---------------------------------------------------------------------------


def wait_check(ctl: AudioController) -> None:
    thread = ctl._check_thread
    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_mic_check_runs_raw_then_processed_and_publishes_clips(
    rig: Rig, ctl: AudioController, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    toasts: list[tuple[str, str]] = []
    ctl.notify = lambda level, text: toasts.append((level, text))
    original = rig.record

    def record(source: str, seconds: float, path: str) -> bool:
        seen.append(ctl.state()["mic_check"]["state"])
        return original(source, seconds, path)

    monkeypatch.setattr(system, "record", record)
    assert ctl.act("audio.mic_check", {"seconds": 3}) == "Recording 3 s - talk now"
    wait_check(ctl)
    assert seen == ["recording-raw", "recording-processed"]
    assert rig.recorded == [(MIC, 3.0), (audio.VOICE_SOURCE_NAME, 3.0)]
    check = ctl.state()["mic_check"]
    assert check["state"] == "done"
    assert [c["label"] for c in check["clips"]] == ["raw microphone", "processed voice"]
    for clip in check["clips"]:
        assert clip["url"] == f"api/clips/{clip['id']}.wav"
        path = ctl.clip_path(clip["id"])
        assert path is not None and path.is_file()
        assert path.parent.stat().st_mode & 0o777 == 0o700
    assert toasts[-1][0] == "ok"
    ctl.stop()
    assert ctl.clip_path(check["clips"][0]["id"]) is None


def test_mic_check_without_easy_effects_records_only_the_raw_mic(rig: Rig) -> None:
    rig.pactl_out = pactl_json(with_voice=False)
    ctl = AudioController(config.load_settings(), clock=rig.clock)
    ctl._probe()
    ctl.act("audio.mic_check", {})
    wait_check(ctl)
    assert rig.recorded == [(MIC, 6.0)]
    assert [c["label"] for c in ctl.state()["mic_check"]["clips"]] == ["raw microphone"]
    ctl.stop()


def test_mic_check_that_captures_nothing_ends_in_error(rig: Rig, ctl: AudioController) -> None:
    rig.record_ok = False
    toasts: list[tuple[str, str]] = []
    ctl.notify = lambda level, text: toasts.append((level, text))
    ctl.act("audio.mic_check", {})
    wait_check(ctl)
    check = ctl.state()["mic_check"]
    assert check["state"] == "error"
    assert check["error"]
    assert check["clips"] == []
    assert toasts[-1][0] == "error"


def test_mic_check_survives_a_recorder_crash(rig: Rig, ctl: AudioController, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(source: str, seconds: float, path: str) -> bool:
        raise OSError("parecord vanished")

    monkeypatch.setattr(system, "record", boom)
    ctl.act("audio.mic_check", {})
    wait_check(ctl)
    assert ctl.state()["mic_check"]["state"] == "error"


def test_mic_check_refuses_overlap_and_missing_tools(rig: Rig, ctl: AudioController) -> None:
    ctl._check["state"] = "recording-raw"
    with pytest.raises(ActionError) as err:
        ctl.act("audio.mic_check", {})
    assert err.value.status == 409
    ctl._check["state"] = "idle"
    rig.have.discard("parecord")
    with pytest.raises(ActionError) as err:
        ctl.act("audio.mic_check", {})
    assert err.value.status == 409


def test_clip_path_rejects_anything_but_known_ids(rig: Rig, ctl: AudioController) -> None:
    ctl.act("audio.mic_check", {})
    wait_check(ctl)
    good = ctl.state()["mic_check"]["clips"][0]["id"]
    assert ctl.clip_path(good) is not None
    for bad in (
        "",
        "../" + good,
        good + "/../x",
        good.upper(),
        good[:-1],
        good + "0",
        "0123456789abcdef",
        "..",
        "/etc/passwd",
        good + ".wav",
        None,
        7,
    ):
        assert ctl.clip_path(bad) is None  # type: ignore[arg-type]
    ctl.stop()


def test_clips_expire_after_ten_minutes_and_are_capped_at_four() -> None:
    clock = Clock()
    store = audio.ClipStore(clock)
    ids = []
    for i in range(6):
        clip_id, path = store.reserve()
        path.write_bytes(b"x")
        store.add(clip_id, f"clip {i}")
        ids.append(clip_id)
    assert [c["id"] for c in store.listing()] == ids[2:]
    assert store.path(ids[0]) is None
    clock.advance(599)
    assert len(store.listing()) == 4
    clock.advance(2)
    assert store.listing() == []
    assert store.path(ids[5]) is None
    assert not list(store.reserve()[1].parent.glob("*.wav"))
    store.close()


# ---------------------------------------------------------------------------
# Demo twin
# ---------------------------------------------------------------------------


def test_demo_state_has_the_same_shape_as_the_real_one(ctl: AudioController) -> None:
    real, demo = ctl.state(), DemoAudio().state()
    assert real.keys() == demo.keys()
    assert real["mic"].keys() == demo["mic"].keys()
    assert real["chain"].keys() == demo["chain"].keys()
    assert real["tone"].keys() == demo["tone"].keys()
    assert real["mic_check"].keys() == demo["mic_check"].keys()
    assert demo["chain"]["presets"][0] == {
        "name": "StreamStack Voice",
        "label": "Voice",
        "blurb": "default broadcast voice",
        "engine": "deepfilternet",
        "user": False,
        "unavailable": None,
    }


def test_demo_actions_change_state() -> None:
    demo = DemoAudio()
    demo.act("audio.gain", {"db": 5})
    demo.act("audio.gain", {"db": 99})
    assert demo.state()["mic"]["gain_db"] == 12.0
    demo.act("audio.mute", {"on": True})
    assert demo.state()["mic"]["muted"] is True
    assert demo.act("audio.preset", {"name": "StreamStack Voice Deep"}) == "Voice preset: Voice Deep"
    state = demo.state()
    assert state["chain"]["preset"] == "StreamStack Voice Deep"
    assert state["tone"]["bass_db"] == 5.0
    demo.act("audio.tone", {"bass_db": 9})
    assert demo.state()["tone"]["bass_db"] == 9.0
    assert demo.state()["tone"]["custom"] is True
    demo.act("audio.bypass", {"on": True})
    assert demo.state()["chain"]["bypass"] is True
    with pytest.raises(ActionError):
        demo.act("audio.preset", {"name": "Nope"})
    with pytest.raises(ActionError):
        demo.act("audio.nope", {})


def test_demo_meters_speak_pause_and_mute() -> None:
    clock = Clock()
    demo = DemoAudio(clock=clock)
    mic_peaks, voice_peaks = [], []
    for _ in range(400):
        clock.advance(0.05)
        frame_ = demo.meters()
        assert -90 <= frame_["mic"]["peak_db"] <= 0
        assert frame_["mic"]["rms_db"] <= frame_["mic"]["peak_db"]
        mic_peaks.append(frame_["mic"]["peak_db"])
        voice_peaks.append(frame_["voice"]["peak_db"])
    assert max(mic_peaks) > -25  # speech
    assert min(mic_peaks) < -50  # pauses
    mean = lambda xs: sum(xs) / len(xs)  # noqa: E731
    loud = [(m, v) for m, v in zip(mic_peaks, voice_peaks, strict=True) if m > -45]
    assert mean([v for _, v in loud]) > mean([m for m, _ in loud])  # the voice is hotter
    assert max(voice_peaks) - mean([v for _, v in loud]) < max(mic_peaks) - mean([m for m, _ in loud])  # and squashed
    demo.act("audio.mute", {"on": True})
    frame_ = demo.meters()
    assert frame_["mic"] == audio.meter_frame()
    assert frame_["voice"]["peak_db"] == -90.0


def test_demo_meters_carry_loudness_and_a_clip_latch() -> None:
    clock = Clock()
    demo = DemoAudio(clock=clock)
    clock.advance(0.9)
    mic = demo.meters()["mic"]
    assert set(mic) == set(audio.meter_frame())
    assert mic["sample_db"] <= mic["peak_db"]  # a true peak is never below the sample peak
    assert -90.0 <= mic["lufs_s"] <= 0.0 and -90.0 <= mic["lufs_m"] <= 0.0
    assert mic["latched"] is False
    demo.act("audio.demo_clip", {})
    assert demo.meters()["mic"]["latched"] is True
    demo.act("audio.mute", {"on": True})
    assert demo.meters()["mic"]["latched"] is True  # muting does not forget a clip
    demo.act("audio.clip_clear", {})
    assert demo.meters()["mic"]["latched"] is False


def test_demo_hot_gain_clips_and_latches() -> None:
    clock = Clock()
    demo = DemoAudio(clock=clock)
    demo.act("audio.gain", {"db": 12})
    for _ in range(200):
        clock.advance(0.05)
        demo.meters()
    assert demo.meters()["mic"]["latched"] is True


def test_controller_clip_clear_drops_every_latch(ctl: AudioController) -> None:
    for latch in ctl._latches.values():
        latch.trip(0.0, 0.0)
    assert ctl.meters()["mic"]["latched"] is True
    ctl.act("audio.clip_clear", {})
    assert ctl.meters()["mic"]["latched"] is False
    assert not any(latch.latched for latch in ctl._latches.values())


def test_demo_gain_moves_the_mic_meter() -> None:
    clock = Clock()
    demo = DemoAudio(clock=clock)
    clock.advance(0.9)
    quiet = demo.meters()["mic"]["peak_db"]
    demo.act("audio.gain", {"db": 10})
    assert demo.meters()["mic"]["peak_db"] == pytest.approx(min(0.0, quiet + 10), abs=0.11)


def test_demo_mic_check_progresses_with_the_clock_and_makes_real_wavs() -> None:
    clock = Clock()
    toasts: list[tuple[str, str]] = []
    demo = DemoAudio(lambda level, text: toasts.append((level, text)), clock=clock)
    assert demo.act("audio.mic_check", {"seconds": 2}) == "Recording 2 s - talk now"
    assert demo.state()["mic_check"]["state"] == "recording-raw"
    with pytest.raises(ActionError):
        demo.act("audio.mic_check", {})
    clock.advance(1.0)
    assert demo.state()["mic_check"]["clips"] == []
    clock.advance(1.5)
    check = demo.state()["mic_check"]
    assert check["state"] == "recording-processed"
    assert [c["label"] for c in check["clips"]] == ["raw microphone"]
    clock.advance(2.0)
    check = demo.state()["mic_check"]
    assert check["state"] == "done"
    assert len(check["clips"]) == 2
    assert toasts == [("ok", "Mic check ready - play it back")]
    for clip in check["clips"]:
        path = demo.clip_path(clip["id"])
        assert path is not None
        with wave.open(str(path), "rb") as wav:
            assert wav.getnchannels() == 1
            assert wav.getsampwidth() == 2
            assert wav.getnframes() == 2 * wav.getframerate()
            samples = array.array("h")
            samples.frombytes(wav.readframes(wav.getnframes()))
            assert max(samples) > 1000  # audible, not silence
    assert demo.clip_path("../../x") is None
    # a new check replaces the old clips
    demo.act("audio.mic_check", {})
    assert demo.state()["mic_check"]["clips"] == []
    demo.stop()


def test_demo_meters_never_touch_the_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("the demo must not run helpers")

    monkeypatch.setattr(system, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    demo = DemoAudio()
    demo.start()
    demo.meters()
    demo.state()
    demo.act("audio.mic_check", {"seconds": 2})
    demo.stop()


def test_controller_start_stop_is_clean(rig: Rig) -> None:
    ctl = AudioController(config.load_settings(), clock=rig.clock)
    ctl.start()
    ctl.start()  # idempotent
    assert ctl.state()["mic"]["present"] is True
    ctl.stop()
    assert not any(t.name.startswith("audio-") and t.is_alive() for t in threading.enumerate())


def test_toast_callback_errors_do_not_escape(rig: Rig, ctl: AudioController) -> None:
    def explode(level: str, text: str) -> None:
        raise RuntimeError("ui gone")

    ctl.notify = explode
    ctl.act("audio.mic_check", {})
    wait_check(ctl)
    assert ctl.state()["mic_check"]["state"] == "done"


# ---------------------------------------------------------------------------
# Mic watchdog: stall detection and revive
# ---------------------------------------------------------------------------

STALLED = micwatch.StallProbe(True, "stalled", "the device's hw_ptr is not advancing")
HEALTHY = micwatch.StallProbe(False, "healthy", "the microphone is captured and consumed")


class Probe:
    """A scripted stall probe (the real one reads /proc/asound; see test_micwatch for that signal)."""

    def __init__(self, probe: micwatch.StallProbe = HEALTHY) -> None:
        self.probe = probe
        self.calls = 0
        self.silent: list[bool] = []

    def __call__(self, mic: str, listing: str, silent: bool = False) -> micwatch.StallProbe:
        self.calls += 1
        self.silent.append(silent)
        return self.probe


class Verify:
    """A scripted re-verification after a revive step: True = still stalled."""

    def __init__(self, results: list[bool | None]) -> None:
        self.results = list(results)
        self.calls = 0

    def __call__(self, mic: str) -> bool | None:
        self.calls += 1
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def watcher(
    rig: Rig, probe: Probe, verify: Verify | None = None, **kw: Any
) -> tuple[AudioController, list[tuple[str, str]]]:
    toasts: list[tuple[str, str]] = []
    controller = AudioController(
        config.load_settings(),
        lambda level, text: toasts.append((level, text)),
        clock=rig.clock,
        stall_probe=probe,
        verify=verify or Verify([False]),
        sleep=lambda seconds: None,
        **kw,
    )
    controller._probe()
    return controller, toasts


def tick(rig: Rig, ctl: AudioController, seconds: float, step: float = 1.0) -> None:
    for _ in range(int(seconds / step)):
        rig.clock.advance(step)
        ctl._probe()


def feed_mic(ctl: AudioController) -> None:
    """Pretend parec delivered bytes just now (the real reader thread would call feed())."""
    reader = ctl._readers["mic"]
    assert reader is not None
    reader.feed(frame(1000))


def test_level_reader_idle_clock_counts_from_the_last_byte() -> None:
    clock = Clock()
    reader = LevelReader("src", clock)
    clock.advance(3)
    assert reader.idle_for() == 3
    reader.feed(b"\0\0")  # even a partial frame proves the source is alive
    clock.advance(1)
    assert reader.idle_for() == 1
    inherited = LevelReader("src", clock, idle_since=reader.idle_since() - 10)
    assert inherited.idle_for() == 11  # a restart does not forget how long the silence has lasted


def test_a_silent_reader_is_restarted_but_keeps_counting_the_silence(rig: Rig) -> None:
    ctl = AudioController(config.load_settings(), clock=rig.clock, stall_probe=Probe(), sleep=lambda s: None)
    ctl._probe()
    first = ctl._readers["mic"]
    assert first is not None
    rig.clock.advance(audio.READER_IDLE_RESTART + 0.5)
    ctl._probe()
    second = ctl._readers["mic"]
    assert second is not None and second is not first
    assert rig.procs[0].killed
    assert second.idle_for() >= audio.READER_IDLE_RESTART  # not reset to zero by the restart


def test_a_stalled_pcm_is_flagged_with_one_warn_toast_and_nothing_acts_alone(rig: Rig) -> None:
    probe = Probe(STALLED)
    ctl, toasts = watcher(rig, probe)
    for _ in range(5):
        feed_mic(ctl)  # parec still delivers: the PCM signal speaks alone
        tick(rig, ctl, 1)
    mic = ctl.state()["mic"]
    assert mic["stalled"] is True
    assert mic["stalled_since"] is not None
    assert mic["revive"] == {"state": "idle", "message": None}
    warns = [t for t in toasts if t[0] == "warn"]
    assert len(warns) == 1 and "nobody can hear you" in warns[0][1]
    assert pactl_mutations(rig) == []  # never revives by itself


def test_the_pcm_is_sampled_on_every_probe(rig: Rig) -> None:
    probe = Probe(HEALTHY)
    ctl, _ = watcher(rig, probe)
    tick(rig, ctl, 10)
    assert probe.calls == 11


def test_a_healthy_pcm_never_flags(rig: Rig) -> None:
    ctl, toasts = watcher(rig, Probe(HEALTHY))
    for _ in range(20):
        feed_mic(ctl)
        tick(rig, ctl, 1)
    assert ctl.state()["mic"]["stalled"] is False
    assert toasts == []


def test_a_silent_reader_alone_never_flags_but_is_passed_as_corroboration(rig: Rig) -> None:
    probe = Probe(HEALTHY)  # the PCM moves; parec hears nothing (it does that in some sandboxes)
    ctl, toasts = watcher(rig, probe)
    tick(rig, ctl, 12)
    assert ctl.state()["mic"]["stalled"] is False and toasts == []
    assert probe.silent[-1] is True and probe.silent[0] is False


def test_an_unjudgeable_source_has_no_flag_and_no_check(rig: Rig) -> None:
    ctl, toasts = watcher(rig, Probe(micwatch.StallProbe(None, "unsupported", "not an ALSA device")))
    tick(rig, ctl, 20)
    assert ctl.state()["mic"]["stalled"] is False and toasts == []
    assert ctl.health_checks() == []


def test_a_muted_microphone_is_not_a_stall(rig: Rig) -> None:
    rig.pactl_out = pactl_json(mute=True)
    ctl, toasts = watcher(rig, Probe(STALLED))
    tick(rig, ctl, 20)
    assert ctl.state()["mic"]["stalled"] is False
    assert toasts == []


def test_no_microphone_is_not_a_stall(rig: Rig) -> None:
    rig.pactl_out = "[]"
    ctl, _ = watcher(rig, Probe(STALLED))
    tick(rig, ctl, 20)
    assert ctl.state()["mic"]["stalled"] is False


def test_stall_clears_after_recovery_and_says_so(rig: Rig) -> None:
    probe = Probe(STALLED)
    ctl, toasts = watcher(rig, probe)
    tick(rig, ctl, 3)
    assert ctl.state()["mic"]["stalled"] is True
    probe.probe = HEALTHY
    for _ in range(4):
        feed_mic(ctl)
        tick(rig, ctl, 1)
    assert ctl.state()["mic"]["stalled"] is False
    assert ctl.state()["mic"]["stalled_since"] is None
    assert toasts[-1] == ("ok", "Microphone is delivering audio again")


def test_health_check_follows_the_flag(rig: Rig) -> None:
    probe = Probe(HEALTHY)
    ctl, _ = watcher(rig, probe)
    tick(rig, ctl, 2)
    assert ctl.health_checks()[0]["state"] == "ok"
    probe.probe = STALLED
    tick(rig, ctl, 2)
    (item,) = ctl.health_checks()
    assert (item["id"], item["state"]) == ("mic-stall", "fail")
    assert "nobody can hear you" in item["title"]


def test_health_check_stays_quiet_without_evidence_or_a_microphone(rig: Rig) -> None:
    ctl, _ = watcher(rig, Probe(micwatch.StallProbe(None, "watching", "watching")))
    assert ctl.health_checks() == []
    rig.pactl_out = "[]"
    ctl._probe()
    assert ctl.health_checks() == []


def test_the_real_monitor_reads_the_pcm_from_the_pactl_listing(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end through the default probe: pactl's source properties -> procfs status -> verdict."""
    listing = json.loads(pactl_json())
    listing[0].update(
        state="RUNNING",
        properties={"device.api": "alsa", "alsa.card": "1", "alsa.device": "0", "alsa.subdevice": "0"},
    )
    rig.pactl_out = json.dumps(listing)
    reads: list[Path] = []

    def frozen(pcm: micwatch.AlsaPcm, clock: Any = None) -> micwatch.PcmSample:
        reads.append(pcm.path)
        return micwatch.PcmSample("RUNNING", 1000, 900, rig.clock())  # nothing advances: a stalled mic

    monkeypatch.setattr(micwatch, "read_pcm_status", frozen)
    toasts: list[tuple[str, str]] = []
    ctl = AudioController(
        config.load_settings(), lambda level, text: toasts.append((level, text)), clock=rig.clock, sleep=lambda s: None
    )
    ctl._probe()
    tick(rig, ctl, 8)
    assert reads and str(reads[0]) == "/proc/asound/card1/pcm0c/sub0/status"
    assert ctl.state()["mic"]["stalled"] is True
    assert [t[0] for t in toasts] == ["warn"]


def wait_revive(ctl: AudioController, states: tuple[str, ...] = ("ok", "failed")) -> dict[str, Any]:
    deadline = threading.Event()
    for _ in range(300):
        revive = ctl.state()["mic"]["revive"]
        if revive["state"] in states:
            return revive
        deadline.wait(0.02)
    pytest.fail("the revive did not finish")


def pactl_mutations(rig: Rig) -> list[list[str]]:
    return [c[1:] for c in rig.commands if c[0] == "pactl" and c[1].startswith(("suspend", "set-"))]


def test_revive_action_suspends_resumes_and_reports(rig: Rig) -> None:
    probe = Probe(STALLED)
    verify = Verify([False])  # the same PCM signal says audio flows after the suspend/resume
    ctl, toasts = watcher(rig, probe, verify)
    tick(rig, ctl, 3)
    assert ctl.state()["mic"]["stalled"] is True
    probe.probe = HEALTHY
    assert ctl.act("audio.revive", {}) == "Reviving the microphone..."
    revive = wait_revive(ctl)
    assert revive["state"] == "ok" and "suspend and resume" in revive["message"]
    assert verify.calls == 1
    assert pactl_mutations(rig) == [["suspend-source", MIC, "1"], ["suspend-source", MIC, "0"]]
    mic = ctl.state()["mic"]
    assert mic["stalled"] is False and mic["stalled_since"] is None
    assert toasts[-1][0] == "ok"
    assert len([r for r in rig.procs if r.killed]) >= 2  # the meter readers were replaced
    rig.clock.advance(audio.REVIVE_SHOW_S + 1)
    assert ctl.state()["mic"]["revive"] == {"state": "idle", "message": None}


def test_revive_refuses_a_microphone_that_is_delivering_audio(rig: Rig) -> None:
    ctl, _ = watcher(rig, Probe(HEALTHY))
    tick(rig, ctl, 5)
    with pytest.raises(ActionError) as err:
        ctl.act("audio.revive", {})
    assert err.value.status == 409 and "nothing to revive" in str(err.value)
    assert pactl_mutations(rig) == []


def test_revive_with_force_cycles_a_healthy_microphone(rig: Rig) -> None:
    ctl, _ = watcher(rig, Probe(HEALTHY))
    ctl.act("audio.revive", {"force": True})
    assert wait_revive(ctl)["state"] == "ok"
    assert pactl_mutations(rig)[0] == ["suspend-source", MIC, "1"]


def test_revive_that_does_not_help_ends_failed_with_the_manual_advice(rig: Rig) -> None:
    ctl, toasts = watcher(rig, Probe(STALLED), Verify([True]))
    tick(rig, ctl, 3)
    ctl.act("audio.revive", {})
    revive = wait_revive(ctl)
    assert revive["state"] == "failed"
    assert "replug" in revive["message"].lower() and "wireplumber" in revive["message"]
    assert ctl.state()["mic"]["stalled"] is True  # still stalled: the banner must stay up
    assert toasts[-1][0] == "error"
    assert not any(c[0] == "systemctl" for c in rig.commands)


def test_a_failed_revive_can_be_retried_and_forgotten_when_the_mic_recovers(rig: Rig) -> None:
    probe = Probe(STALLED)
    ctl, _ = watcher(rig, probe, Verify([True]))
    tick(rig, ctl, 3)
    ctl.act("audio.revive", {})
    assert wait_revive(ctl)["state"] == "failed"
    probe.probe = HEALTHY
    for _ in range(4):
        feed_mic(ctl)
        tick(rig, ctl, 1)
    assert ctl.state()["mic"]["stalled"] is False
    assert ctl.state()["mic"]["revive"]["state"] == "idle"  # resolved by itself: the failure is forgotten


def test_revive_progress_is_visible_while_it_runs(rig: Rig) -> None:
    gate = threading.Event()
    ctl = AudioController(
        config.load_settings(),
        clock=rig.clock,
        stall_probe=Probe(HEALTHY),
        verify=Verify([False]),
        sleep=lambda s: gate.wait(5),
    )
    ctl._probe()
    ctl.act("audio.revive", {"force": True})
    assert ctl.state()["mic"]["revive"]["state"] == "reviving"
    assert "Suspending" in ctl.state()["mic"]["revive"]["message"]
    with pytest.raises(ActionError) as busy:
        ctl.act("audio.revive", {"force": True})
    assert busy.value.status == 409
    gate.set()
    assert wait_revive(ctl)["state"] == "ok"


def test_revive_needs_a_microphone_and_pactl(rig: Rig) -> None:
    ctl, _ = watcher(rig, Probe(HEALTHY))
    rig.have.discard("pactl")
    with pytest.raises(ActionError, match="pactl"):
        ctl.act("audio.revive", {"force": True})
    rig.have.add("pactl")
    rig.pactl_out = "[]"
    ctl._probe()
    with pytest.raises(ActionError, match="No microphone"):
        ctl.act("audio.revive", {"force": True})


def test_the_real_controller_has_no_demo_stall(rig: Rig) -> None:
    ctl, _ = watcher(rig, Probe(HEALTHY))
    with pytest.raises(ActionError) as err:
        ctl.act("audio.demo_stall", {"on": True})
    assert err.value.status == 400


def test_the_watchdog_pauses_while_a_revive_runs(rig: Rig) -> None:
    gate = threading.Event()
    probe = Probe(STALLED)
    ctl = AudioController(
        config.load_settings(), clock=rig.clock, stall_probe=probe, verify=Verify([False]), sleep=lambda s: gate.wait(5)
    )
    ctl._probe()
    ctl.act("audio.revive", {})
    calls = probe.calls
    tick(rig, ctl, 12)
    assert probe.calls == calls  # a half-resumed node is not judged
    gate.set()
    wait_revive(ctl)


# ---------------------------------------------------------------------------
# Mic watchdog: the demo twin
# ---------------------------------------------------------------------------


def test_demo_stall_is_triggerable_and_revives_after_1_5_seconds() -> None:
    clock = Clock()
    toasts: list[tuple[str, str]] = []
    demo = DemoAudio(lambda level, text: toasts.append((level, text)), clock=clock)
    assert demo.state()["mic"]["stalled"] is False
    assert demo.health_checks()[0]["state"] == "ok"

    demo.act("audio.demo_stall", {"on": True})
    mic = demo.state()["mic"]
    assert mic["stalled"] is True and mic["stalled_since"]
    assert mic["revive"] == {"state": "idle", "message": None}
    assert [t[0] for t in toasts] == ["warn"]
    assert demo.meters()["mic"]["peak_db"] == -90.0 and demo.meters()["voice"]["peak_db"] == -90.0
    assert demo.health_checks()[0]["state"] == "fail"
    demo.act("audio.demo_stall", {"on": True})  # already stalled: no second toast
    assert len(toasts) == 1

    demo.act("audio.revive", {})
    assert demo.state()["mic"]["revive"]["state"] == "reviving"
    with pytest.raises(ActionError) as busy:
        demo.act("audio.revive", {})
    assert busy.value.status == 409
    clock.advance(0.5)
    assert "Waiting" in demo.state()["mic"]["revive"]["message"]
    assert demo.state()["mic"]["stalled"] is True
    clock.advance(1.1)
    mic = demo.state()["mic"]
    assert mic["stalled"] is False and mic["revive"]["state"] == "ok"
    assert toasts[-1][0] == "ok"
    assert demo.health_checks()[0]["state"] == "ok"
    clock.advance(audio.REVIVE_SHOW_S + 1)
    assert demo.state()["mic"]["revive"] == {"state": "idle", "message": None}


def test_demo_stall_off_returns_to_healthy_and_validates() -> None:
    demo = DemoAudio()
    demo.act("audio.demo_stall", {"on": True})
    demo.act("audio.demo_stall", {"on": False})
    assert demo.state()["mic"]["stalled"] is False and demo.state()["mic"]["stalled_since"] is None
    with pytest.raises(ActionError) as err:
        demo.act("audio.demo_stall", {"on": "yes"})
    assert err.value.status == 400


def test_demo_revive_refuses_a_healthy_microphone_like_the_real_one() -> None:
    demo = DemoAudio(clock=Clock())
    with pytest.raises(ActionError) as err:
        demo.act("audio.revive", {})
    assert err.value.status == 409
