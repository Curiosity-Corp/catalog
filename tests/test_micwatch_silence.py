"""Digital silence, the capture-control resync, the duplex guard and the default-source row (2026-10-27 incident)."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
from hardware_support import FakeMachine, studio_wave

from streamstack import micwatch
from streamstack.hardware import alsa, capturehold, doctor, duplex, schema, voice
from streamstack.hardware.found import Found
from streamstack.hardware.plan import resolve

ROOT = Path(__file__).parent.parent
MIC = "alsa_input.usb-Elgato_Systems_Elgato_Wave_XLR_ABC12345-00.mono-fallback"
CARD = "alsa_card.usb-Elgato_Systems_Elgato_Wave_XLR_ABC12345-00"
HEALTHY = micwatch.StallProbe(False, "healthy", "the microphone is captured and consumed")
STALLED = micwatch.StallProbe(True, "stalled", "hw_ptr is not advancing", micwatch.KIND_STALL)

WAVE_CONTENTS = """\
numid=5,iface=MIXER,name='Mic Capture Switch'
  ; type=BOOLEAN,access=rw------,values=1
  : values=on
numid=6,iface=MIXER,name='Mic Capture Volume'
  ; type=INTEGER,access=rw---R--,values=1,min=0,max=150,step=1
  : values=150
numid=7,iface=MIXER,name='Mic Playback Switch'
  ; type=BOOLEAN,access=rw------,values=1
  : values=on
numid=8,iface=MIXER,name='Sync Status'
  ; type=ENUMERATED,access=r-------,values=1,items=2
  ; Item #0 'a'
  ; Item #1 'b'
  : values=1
"""


class FakeMixer:
    def __init__(self, text: str = WAVE_CONTENTS, fail_on: int | None = None) -> None:
        self._controls = alsa.parse_contents(text)
        self.writes: list[tuple[int, list[int]]] = []
        self.fail_on = fail_on
        self.ok = True

    def available(self) -> bool:
        return self.ok

    def controls(self) -> dict[str, alsa.MixerControl]:
        return self._controls

    def get(self, numid: int) -> alsa.MixerControl:
        return next(c for c in self._controls.values() if c.numid == numid)

    def put(self, numid: int, values: list[int]) -> None:
        if self.fail_on is not None and len(self.writes) == self.fail_on:
            self.writes.append((numid, values))
            raise alsa.MixerError("refused")
        self.writes.append((numid, values))


# -- detection ------------------------------------------------------------------------------------------------------


def test_a_window_of_exact_zeros_on_a_moving_capture_is_digital_silence_not_a_stall() -> None:
    probe = micwatch.silence_verdict(HEALTHY, micwatch.SILENCE_WINDOW_S)
    assert probe.stalled is True and probe.state == "digital-silence" and probe.kind == micwatch.KIND_SILENCE
    assert "exactly zero" in probe.detail


def test_a_quiet_room_or_too_short_a_run_is_not_digital_silence() -> None:
    assert micwatch.silence_verdict(HEALTHY, None) is HEALTHY  # the latest frame had signal
    assert micwatch.silence_verdict(HEALTHY, micwatch.SILENCE_WINDOW_S - 0.5) is HEALTHY


def test_a_stall_is_never_relabelled_as_silence() -> None:
    assert micwatch.silence_verdict(STALLED, 60.0) is STALLED


def test_signal_sample_separates_zeros_from_a_noise_floor() -> None:
    rate = micwatch.SIGNAL_BYTES_PER_S
    assert micwatch.SignalSample(rate * 5, 0).digital_silence() is True
    assert micwatch.SignalSample(rate * 5, 1200).digital_silence() is False  # a noise floor
    assert micwatch.SignalSample(rate, 0).digital_silence() is None  # too little arrived to say
    assert micwatch.SignalSample(0, 0).digital_silence() is None  # nothing arrived: a stall


class FakeProc:
    """A parec whose stdout is a file with the given bytes (a pipe would block on a megabyte of zeros)."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.stdout = tempfile.TemporaryFile()  # noqa: SIM115 - closed with the process object
        self.stdout.write(b"".join(chunks))
        self.stdout.seek(0)
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = self.returncode or 0
        return self.returncode


def test_capture_signal_counts_zero_and_nonzero_bytes() -> None:
    quiet = bytes(micwatch.SIGNAL_BYTES_PER_S * 5)
    sample = micwatch.capture_signal(MIC, 0, sleep=lambda s: None, spawn=lambda *a, **k: FakeProc([quiet]))
    assert sample is not None and sample.digital_silence() is True
    live = quiet + bytes([0, 0, 0x3C, 0x3C])  # one nonzero float: signal
    sample = micwatch.capture_signal(MIC, 0, sleep=lambda s: None, spawn=lambda *a, **k: FakeProc([live]))
    assert sample is not None and sample.digital_silence() is False


def test_capture_signal_without_parec_is_none() -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("parec")

    assert micwatch.capture_signal(MIC, 0, sleep=lambda s: None, spawn=refuse) is None


def test_the_silence_check_is_its_own_row_with_its_own_fix() -> None:
    item = micwatch.check_item(True, "", micwatch.KIND_SILENCE)
    assert item is not None
    assert (item["id"], item["group"], item["state"]) == ("mic-digital-silence", "audio", "fail")
    assert "digital silence" in item["title"] and "mic-revive" in item["hint"] and "alsa-utils" in item["hint"]
    stall = micwatch.check_item(True, "", micwatch.KIND_STALL)
    assert stall is not None and stall["id"] == "mic-stall"


# -- the resync -----------------------------------------------------------------------------------------------------


def test_resync_rewrites_the_capture_switch_and_volume_and_ends_on_their_old_values() -> None:
    mixer = FakeMixer()
    touched = alsa.resync_capture(mixer, sleep=lambda s: None)
    assert touched == ["Mic Capture Switch (was 1)", "Mic Capture Volume (was 150)"]
    assert mixer.writes == [(5, [0]), (5, [1]), (6, [149]), (6, [150])]  # the incident's own sequence


def test_resync_keeps_a_muted_microphone_muted_and_ignores_playback_and_read_only() -> None:
    mixer = FakeMixer(WAVE_CONTENTS.replace("values=on\nnumid=6", "values=off\nnumid=6"))
    alsa.resync_capture(mixer, sleep=lambda s: None)
    assert mixer.writes[:2] == [(5, [1]), (5, [0])]
    assert all(numid in (5, 6) for numid, _ in mixer.writes)


def test_resync_at_the_bottom_of_the_range_steps_up() -> None:
    mixer = FakeMixer(WAVE_CONTENTS.replace("values=150", "values=0"))
    alsa.resync_capture(mixer, sleep=lambda s: None)
    assert (6, [1]) in mixer.writes and mixer.writes[-1] == (6, [0])


def test_a_failed_write_back_is_tried_once_more_and_then_raised() -> None:
    mixer = FakeMixer(fail_on=1)  # the first write back to the switch is refused
    touched = alsa.resync_capture(mixer, sleep=lambda s: None)
    assert touched[0].startswith("Mic Capture Switch") and mixer.writes[1:3] == [(5, [1]), (5, [1])]
    always = FakeMixer()
    always.put = lambda numid, values: (_ for _ in ()).throw(alsa.MixerError("refused"))  # type: ignore[method-assign]
    with pytest.raises(alsa.MixerError):
        alsa.resync_capture(always, sleep=lambda s: None)


def test_feature_unit_resync_reports_what_it_did_and_why_it_cannot() -> None:
    assert "Mic Capture Volume" in micwatch.feature_unit_resync(3, mixer=FakeMixer(), sleep=lambda s: None)()
    missing = FakeMixer()
    missing.ok = False
    with pytest.raises(micwatch.ResyncError, match="alsa-utils"):
        micwatch.feature_unit_resync(3, mixer=missing)()
    with pytest.raises(micwatch.ResyncError, match="no capture switch"):
        micwatch.feature_unit_resync(
            3, mixer=FakeMixer("numid=1,iface=CARD,name='X'\n  ; type=INTEGER,access=r---,values=1\n  : values=1")
        )()


# -- revive ---------------------------------------------------------------------------------------------------------


class Pactl:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, argv: list[str], timeout: float = 10) -> subprocess.CompletedProcess[str]:
        self.commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, "[]", "")

    def mutations(self) -> list[list[str]]:
        return [c[1:] for c in self.commands if c[1].startswith(("suspend", "set-"))]


def test_digital_silence_is_fixed_by_the_resync_alone_when_audio_then_flows() -> None:
    pactl, said = Pactl(), []
    result = micwatch.revive_mic(
        MIC,
        run=pactl,
        sleep=lambda s: None,
        stalled=lambda: False,
        say=said.append,
        kind=micwatch.KIND_SILENCE,
        resync=lambda: "re-wrote the controls",
        silent=lambda: False,
    )
    assert result.ok and "capture controls re-written" in result.message
    assert result.steps == ["feature-unit resync", "re-wrote the controls"]
    assert pactl.mutations() == []  # nothing else was touched
    assert "capture switch" in said[0]


def test_a_resync_that_leaves_the_zeros_escalates_to_suspend_resume() -> None:
    pactl = Pactl()
    silent = iter([True, False])
    result = micwatch.revive_mic(
        MIC,
        run=pactl,
        sleep=lambda s: None,
        stalled=lambda: False,
        kind=micwatch.KIND_SILENCE,
        resync=lambda: "re-wrote",
        silent=lambda: next(silent),
    )
    assert result.ok and result.steps[-1] == "suspend/resume"
    assert pactl.mutations()[0] == ["suspend-source", MIC, "1"]


def test_a_resync_that_cannot_run_is_reported_and_the_escalation_continues() -> None:
    def broken() -> str:
        raise micwatch.ResyncError("amixer is missing (install alsa-utils)")

    result = micwatch.revive_mic(
        MIC,
        run=Pactl(),
        sleep=lambda s: None,
        stalled=lambda: False,
        kind=micwatch.KIND_SILENCE,
        resync=broken,
        silent=lambda: False,
    )
    assert result.ok and any("alsa-utils" in step for step in result.steps)
    unknown = micwatch.revive_mic(
        MIC, run=Pactl(), sleep=lambda s: None, stalled=lambda: False, kind=micwatch.KIND_SILENCE, resync=None
    )
    assert any("no sound card" in step for step in unknown.steps)


def test_silence_that_survives_everything_asks_for_hands() -> None:
    result = micwatch.revive_mic(
        MIC,
        run=Pactl(),
        sleep=lambda s: None,
        stalled=lambda: True,
        kind=micwatch.KIND_SILENCE,
        resync=lambda: "re-wrote",
    )
    assert not result.ok and "device's own mute" in result.message and result.steps[-1] == "manual help needed"


def test_a_plain_stall_never_runs_the_resync() -> None:
    called: list[bool] = []
    micwatch.revive_mic(
        MIC, run=Pactl(), sleep=lambda s: None, stalled=lambda: False, resync=lambda: called.append(True) or ""
    )
    assert called == []


# -- the wireplumber re-probe ---------------------------------------------------------------------------------------


def test_reprobe_restarts_wireplumber_and_waits_for_the_input_profile() -> None:
    pactl, ready = Pactl(), iter([False, False, True])
    result = micwatch.reprobe_wireplumber(run=pactl, sleep=lambda s: None, ready=lambda: next(ready))
    assert result.ok and pactl.commands == [["systemctl", "--user", "restart", "wireplumber"]]


def test_reprobe_that_does_not_bring_the_input_back_says_what_to_do() -> None:
    result = micwatch.reprobe_wireplumber(run=Pactl(), sleep=lambda s: None, ready=lambda: False, wait=3)
    assert not result.ok and "replug" in result.message


def test_reprobe_reports_a_refused_restart() -> None:
    def refuse(argv: list[str], timeout: float = 10) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "Unit not found")

    result = micwatch.reprobe_wireplumber(run=refuse, sleep=lambda s: None, ready=lambda: True)
    assert not result.ok and "Unit not found" in result.message


# -- the duplex guard -----------------------------------------------------------------------------------------------

CATALOGUE = schema.packaged()


def wave_found(machine: FakeMachine, profile: str) -> Found:
    studio_wave(machine)
    machine.set_pipewire(
        [(49, CARD)],
        sinks=[(55, "alsa_output.usb-Elgato_Systems_Elgato_Wave_XLR_ABC12345-00.analog-stereo", "s24le 2ch 48000Hz")],
        sources=[(56, MIC, "s24le 1ch 48000Hz")] if "input" in profile else [],
    )
    machine.set_card_profiles({CARD: (profile, "0fd9:007d")})
    snapshot = machine.snapshot()
    return Found(resolve(snapshot, CATALOGUE), snapshot, CATALOGUE)


@pytest.mark.parametrize("profile", ["output:analog-stereo", "output:stereo-fallback"])
def test_an_attached_interface_with_output_profiles_only_is_the_duplex_fault(tmp_path: Path, profile: str) -> None:
    found = wave_found(FakeMachine(tmp_path), profile)
    lost = duplex.lost_input(found.plan, found.snapshot)
    assert lost is not None and lost.profile == profile
    rows = {r["id"]: r for r in doctor.checks(found.plan, found.snapshot, found.catalogue, "deepfilternet")}
    row = rows["hw-audio-duplex"]
    assert row["state"] == "fail" and "no input profile" in row["title"]
    assert (
        "set-card-profile" in row["hint"]
        and "mic-revive --reprobe" in row["hint"]
        and "drops every audio" in row["hint"]
    )


def test_a_duplex_card_is_fine(tmp_path: Path) -> None:
    found = wave_found(FakeMachine(tmp_path), "output:analog-stereo+input:mono-fallback")
    assert duplex.lost_input(found.plan, found.snapshot) is None
    rows = {r["id"] for r in doctor.checks(found.plan, found.snapshot, found.catalogue, "deepfilternet")}
    assert "hw-audio-duplex" not in rows


def test_profile_names_with_and_without_input() -> None:
    assert duplex.profile_has_input("output:analog-stereo+input:mono-fallback")
    assert duplex.profile_has_input("input:mono-fallback")
    assert duplex.profile_has_input("pro-audio")
    assert not duplex.profile_has_input("output:stereo-fallback")


def test_cards_have_input_reads_pactl_json() -> None:
    cards = json.dumps([{"name": CARD, "active_profile": "output:analog-stereo+input:mono-fallback"}])
    assert duplex.cards_have_input(cards, CARD)
    assert not duplex.cards_have_input(cards, "other")
    assert not duplex.cards_have_input("junk", CARD)
    assert not duplex.cards_have_input(json.dumps([{"name": CARD, "active_profile": "output:x"}]), CARD)


# -- the default source ---------------------------------------------------------------------------------------------


def test_the_default_source_must_be_the_voice_chain_while_it_runs() -> None:
    row = voice.default_source_row("alsa_input.usb-Cam_Link.analog-stereo", MIC, True)
    assert row is not None and row["state"] == "warn" and row["id"] == "default-source"
    assert "StreamStack Mic" in row["title"] and "streamstack default-source" in row["hint"]
    ok = voice.default_source_row("streamstack_mic", MIC, True)
    assert ok is not None and ok["state"] == "ok"


def test_without_the_chain_the_default_must_be_the_resolved_microphone() -> None:
    row = voice.default_source_row("cam_link", MIC, False, lambda n: {"cam_link": "Cam Link"}.get(n, n))
    assert row is not None and row["state"] == "warn" and "Cam Link" in row["title"] and MIC in row["title"]
    assert voice.default_source_row(MIC, MIC, False)["state"] == "ok"  # type: ignore[index]


def test_nothing_to_judge_gives_no_row() -> None:
    assert voice.default_source_row("", MIC, True) is None
    assert voice.default_source_row("cam_link", None, False) is None
    assert voice.wanted_default_source(MIC, True) == "streamstack_mic"
    assert voice.wanted_default_source(MIC, False) == MIC


def test_set_default_source_runs_pactl_and_reports_refusal() -> None:
    seen: list[list[str]] = []

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    assert voice.set_default_source("streamstack_mic", run)
    assert seen == [["pactl", "set-default-source", "streamstack_mic"]]
    assert not voice.set_default_source("x", lambda argv: None)


# -- the Wave XLR capture-hold rule (issue #126) ---------------------------------------------------------------------


def pw_dump(props: dict[str, Any]) -> str:
    return json.dumps(
        [
            {"type": "PipeWire:Interface:Device", "info": {"props": {"node.name": MIC, "node.always-process": True}}},
            {"type": "PipeWire:Interface:Node", "info": {"props": {"node.name": MIC, **props}}},
        ]
    )


@pytest.mark.parametrize(
    ("props", "expected"),
    [
        ({"node.always-process": True}, True),
        ({"node.always-process": "true"}, True),
        ({"node.pause-on-idle": False}, False),
        ({"node.always-process": False}, False),
    ],
)
def test_the_effective_always_process_property_is_read_from_pw_dump(props: dict[str, Any], expected: bool) -> None:
    assert capturehold.always_process(pw_dump(props), MIC) is expected


def test_always_process_is_unknown_without_the_node_or_a_readable_dump() -> None:
    assert capturehold.always_process(pw_dump({}), "other") is None
    assert capturehold.always_process("junk", MIC) is None
    assert capturehold.always_process("{}", MIC) is None


def test_the_doctor_warns_with_the_fix_when_the_rule_is_absent() -> None:
    row = capturehold.check_row(MIC, False)
    assert row is not None and row["state"] == "warn" and row["id"] == "wave-xlr-capture-hold"
    assert "52-streamstack-wave-xlr-capture-hold.conf" in row["hint"] and "drops every audio" in row["hint"]
    held = capturehold.check_row(MIC, True)
    assert held is not None and held["state"] == "ok"


def test_the_capture_hold_row_is_for_the_wave_xlr_only_and_only_when_judgeable() -> None:
    assert capturehold.check_row("alsa_input.usb-Focusrite_Scarlett_2i2.analog-stereo", False) is None
    assert capturehold.check_row(MIC, None) is None
    assert capturehold.check_row(None, False) is None


def test_the_shipped_drop_in_sets_the_property_for_the_xlr_capture_node_only() -> None:
    text = (ROOT / "packaging/wireplumber/wireplumber.conf.d/52-streamstack-wave-xlr-capture-hold.conf").read_text()
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "node.always-process = true" in body and "Elgato_Systems_Elgato_Wave_XLR_" in body
    assert "alsa_input" in body and "alsa_output" not in body  # the playback node is untouched


def test_the_repro_tool_classifies_a_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("xlr_capture_order", ROOT / "tools/xlr_capture_order.py")
    assert spec is not None and spec.loader is not None
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    second = tool.BYTES_PER_SECOND
    assert tool.classify(bytes(second), 1.0) == "zeros"
    assert tool.classify(bytes(second - 4) + bytes([0, 0, 0x80, 0x3F]), 1.0) == "signal"
    assert tool.classify(bytes(100), 1.0) == "short"
    assert tool.summarize(["zeros", "signal", "signal", "short"]) == "4 cycles: 1 all-zero, 2 with signal, 1 short"


def test_every_shipped_wireplumber_drop_in_parses_as_spa_json() -> None:
    """WirePlumber 0.5 rejects a drop-in whose quoted string has a backslash escape ("section ... has no value") and
    ignores it, so the rule never applies. Regex dots are written `[.]`, as the workstation role's own drop-in does."""
    drop_ins = sorted((ROOT / "packaging/wireplumber/wireplumber.conf.d").glob("*.conf"))
    assert drop_ins
    for path in drop_ins:
        body = "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("#"))
        assert "\\" not in body, f"{path.name}: a backslash is not valid in a WirePlumber (SPA JSON) string; use [.]"
        assert body.count("[") == body.count("]") and body.count("{") == body.count("}"), path.name
