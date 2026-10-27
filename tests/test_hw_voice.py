"""The voice chain's target comes from the hardware Plan: microphone and monitor by USB id, the graph's rate and
format, the quantum, hot-plug and the generic fallback (#118)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from hardware_support import FakeMachine, inventory_text, studio_wave

from streamstack import config, system
from streamstack.audiochain import chain, graph, stages
from streamstack.console import audio
from streamstack.console.audio import AudioController
from streamstack.hardware import inventory, schema, voice
from streamstack.hardware.plan import Plan, resolve

CATALOGUE = schema.packaged()
WAVE_MIC = "alsa_input.usb-Elgato_Systems_Elgato_Wave_XLR_ABC12345-00.mono-fallback"
WAVE_CARD = "alsa_card.usb-Elgato_Systems_Elgato_Wave_XLR_ABC12345-00"


def recorded(host: str) -> Plan:
    return resolve(inventory.snapshot_from_inventory(inventory_text(host)), CATALOGUE)


@pytest.fixture
def machine(tmp_path: Path) -> FakeMachine:
    return FakeMachine(tmp_path)


def wave_plan(machine: FakeMachine) -> Plan:
    return resolve(studio_wave(machine).snapshot(), CATALOGUE)


def wave_with_rates(machine: FakeMachine, rates: str, fmt: str = "S24_3LE") -> Plan:
    studio_wave(machine)
    machine.remove_alsa(3)
    machine.add_alsa_usb(3, "XLR", "0fd9:007d", "Elgato Systems Elgato Wave XLR", rates, fmt=fmt)
    return resolve(machine.snapshot(), CATALOGUE)


# -- the graph rate ------------------------------------------------------------------------------------


def test_the_rate_is_the_conferencing_rate_when_there_is_no_interface() -> None:
    assert graph.pick_rate(None, "deepfilternet")[0] == 48000


def test_an_interface_that_offers_48000_runs_the_graph_at_48000() -> None:
    rate, why = graph.pick_rate(48000, "deepfilternet")
    assert rate == 48000 and "offers" in why


@pytest.mark.parametrize("engine", ["deepfilternet", "rnnoise"])
def test_the_neural_stages_keep_a_96000_interface_at_48000_and_say_why(engine: str) -> None:
    rate, why = graph.pick_rate(96000, engine)
    assert rate == 48000 and "48 kHz model" in why and "96000" in why


def test_a_graph_without_a_neural_stage_may_run_at_the_native_rate() -> None:
    assert graph.pick_rate(96000, "off") == (96000, "the interface's native rate")


def test_the_config_carries_the_rate_and_the_scaled_latency() -> None:
    text = graph.build_config(stages.defaults(), engine="off", rate=96000, quantum=512)
    assert "audio.rate = 96000" in text and '"512/96000"' in text and '"256/48000"' in text
    plain = graph.build_config(stages.defaults(), engine="off")
    assert "96000" not in plain and "node.latency" not in plain.split("playback")[0]


def test_the_latency_string() -> None:
    assert graph.latency(256, 48000) == "256/48000" and graph.latency(None, 48000) is None


def test_a_graph_at_another_rate_or_quantum_restarts_the_host() -> None:
    base = chain.structure_signature("deepfilternet", "mic")
    assert chain.structure_signature("deepfilternet", "mic", 48000, None) == base
    assert chain.structure_signature("deepfilternet", "mic", 96000, None) != base
    assert chain.structure_signature("deepfilternet", "mic", 48000, 256) != base


# -- the target for each host --------------------------------------------------------------------------


def test_dm313red_reads_the_wave_xlr_by_its_usb_id() -> None:
    target = voice.voice_target(recorded("dm313red"), "deepfilternet")
    assert target.state == voice.INTERFACE and target.interface == "wave-xlr"
    assert target.microphone and target.microphone.startswith("alsa_input.usb-Elgato_Systems_Elgato_Wave_XLR")
    assert target.monitor and target.monitor.endswith("analog-stereo")
    assert (target.rate, target.native_rate, target.format, target.quantum) == (48000, 48000, "s24le", 256)
    assert "0fd9:007d" in target.reason


def test_the_public_target_hides_the_serial() -> None:
    target = voice.voice_target(recorded("dm313red"), "deepfilternet").public()
    assert "<serial>" in str(target["microphone"]) and "DS23M2A00049" not in json.dumps(target)


def test_deskmeet1_declines_cleanly_and_says_the_scarlett_is_not_enumerating() -> None:
    target = voice.voice_target(recorded("deskmeet1"), "deepfilternet")
    assert target.state == voice.ABSENT and target.microphone is None
    assert "Scarlett 2i2" in target.reason and target.reason.endswith("is not enumerating on USB")
    assert target.rate == 48000 and target.interface is None


def test_a_wave_xlr_offering_only_96000_resamples_to_the_conferencing_rate(machine: FakeMachine) -> None:
    target = voice.voice_target(wave_with_rates(machine, "96000"), "deepfilternet")
    assert (target.rate, target.native_rate) == (48000, 96000) and "resampled" in target.rate_reason


def test_without_a_neural_stage_the_96000_interface_runs_at_96000_with_a_scaled_quantum(machine: FakeMachine) -> None:
    target = voice.voice_target(wave_with_rates(machine, "96000"), "off")
    assert (target.rate, target.quantum) == (96000, 512)


def test_the_format_follows_the_interface(machine: FakeMachine) -> None:
    assert voice.voice_target(wave_with_rates(machine, "48000", "S16_LE"), "off").format == "s16le"


def test_an_unknown_interface_uses_the_default_source_and_says_so(machine: FakeMachine) -> None:
    machine.add_usb("abcd:0001", "Mystery Mic")
    machine.set_pipewire(
        [],
        sinks=[(55, "alsa_output.usb-Mystery_Mic.analog-stereo", "s16le 2ch 48000Hz")],
        sources=[(56, "alsa_input.usb-Mystery_Mic.analog-stereo", "s16le 1ch 48000Hz")],
    )
    target = voice.voice_target(resolve(machine.snapshot(), CATALOGUE), "deepfilternet")
    assert target.state == voice.GENERIC and target.microphone == "alsa_input.usb-Mystery_Mic.analog-stereo"
    assert target.interface is None and "no audio interface profile" in target.reason


def test_a_card_at_profile_off_declines_and_names_the_card_profile_to_switch_on(machine: FakeMachine) -> None:
    studio_wave(machine)
    machine.set_pipewire([(49, WAVE_CARD)])
    machine.set_card_profiles({WAVE_CARD: ("off", "0fd9:007d")})
    target = voice.voice_target(resolve(machine.snapshot(), CATALOGUE), "deepfilternet")
    assert target.state == voice.CARD_OFF and target.microphone is None
    assert target.activate == (WAVE_CARD, "output:analog-stereo+input:mono-fallback")


def test_the_microphone_setting_wins_and_is_reported_as_an_override(machine: FakeMachine) -> None:
    studio_wave(machine)
    plan = resolve(machine.snapshot(), CATALOGUE, {"STREAMSTACK_MICROPHONE": WAVE_MIC})
    assert voice.voice_target(plan, "off").state == voice.OVERRIDE


def test_activate_card_runs_pactl_and_reports_a_refusal() -> None:
    calls: list[list[str]] = []

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0 if len(calls) == 1 else 1, "", "no such card")

    assert voice.activate_card("card", "profile", run) is True
    assert calls == [["pactl", "set-card-profile", "card", "profile"]]
    assert voice.activate_card("card", "profile", run) is False
    assert voice.activate_card("card", "profile", lambda argv: None) is False


# -- the runtime follows the Plan ----------------------------------------------------------------------


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> chain.ChainRuntime:
    monkeypatch.setattr(graph, "missing_required", lambda: [])
    model = chain.ChainModel(store=tmp_path / "state.json", engines=lambda: ["deepfilternet"])
    return chain.ChainRuntime(model, tmp_path, runner=lambda argv, timeout=10: None, autostart=False)


def test_the_runtime_reads_its_microphone_rate_and_quantum_from_the_plan(runtime: chain.ChainRuntime) -> None:
    runtime.set_plan(recorded("dm313red"))
    assert runtime.microphone and runtime.microphone.startswith("alsa_input.usb-Elgato")
    assert (runtime.rate(), runtime.quantum(), runtime.declined()) == (48000, 256, None)
    assert runtime.status()["target"]["interface"] == "wave-xlr"
    assert runtime.status()["graph_rate"] == 48000


def test_the_runtime_declines_without_a_microphone_and_does_not_start(
    runtime: chain.ChainRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[object] = []
    monkeypatch.setattr(chain.runtime, "host_pids", lambda conf: [])
    monkeypatch.setattr(chain.runtime, "start_host", lambda *a, **k: started.append(a))
    runtime._autostart = True
    runtime.set_plan(recorded("deskmeet1"))
    runtime.supervise()
    assert started == [] and runtime.running is False
    assert runtime.error and runtime.error.endswith("is not enumerating on USB") and "Scarlett 2i2" in runtime.error


def test_unplugging_the_interface_does_not_tear_down_a_running_host(
    runtime: chain.ChainRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped: list[object] = []
    monkeypatch.setattr(chain.runtime, "host_pids", lambda conf: [4242])
    monkeypatch.setattr(chain.runtime, "stop_host", lambda conf: stopped.append(conf))
    monkeypatch.setattr(chain.runtime, "find_nodes", lambda runner: None)
    runtime._autostart = True
    runtime.set_plan(recorded("deskmeet1"))
    runtime.supervise()
    assert stopped == []


# -- the audio controller: hot-plug, re-resolve ---------------------------------------------------------


class Pactl:
    def __init__(self, sources: list[str]) -> None:
        self.sources = sources
        self.commands: list[list[str]] = []

    def __call__(self, argv: list[str], timeout: float = 10) -> subprocess.CompletedProcess[str] | None:
        self.commands.append(argv)
        if argv[0] == "pactl" and "list" in argv:
            listing = [
                {"name": n, "description": n, "mute": False, "volume": {"mono": {"value": 1, "db": "0.00 dB"}}}
                for n in self.sources
            ]
            return subprocess.CompletedProcess(argv, 0, json.dumps(listing), "")
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def controller(
    monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> tuple[AudioController, Pactl, list[Plan | None]]:
    pactl = Pactl([WAVE_MIC, "alsa_input.pci-onboard.analog-stereo"])
    monkeypatch.setattr(system, "run", pactl)
    monkeypatch.setattr(system, "have", lambda tool: tool == "pactl")
    monkeypatch.setattr(system, "default_device", lambda kind: "alsa_input.pci-onboard.analog-stereo")
    monkeypatch.setattr(graph, "available_engines", lambda: ["deepfilternet"])
    ctl = AudioController(config.load_settings(), clock=lambda: 1000.0)
    plans: list[Plan | None] = [None]
    ctl.plan_source = lambda: plans[0]
    return ctl, pactl, plans


def test_the_controller_picks_the_microphone_from_the_plan_not_from_the_default_source(
    controller: tuple[AudioController, Pactl, list[Plan | None]], machine: FakeMachine
) -> None:
    ctl, _pactl, plans = controller
    plans[0] = wave_plan(machine)
    ctl._probe()
    assert ctl._mic == WAVE_MIC  # the default source is the onboard one; the interface's USB id wins


def test_unplugging_and_replugging_re_resolves_the_microphone(
    controller: tuple[AudioController, Pactl, list[Plan | None]], machine: FakeMachine, tmp_path: Path
) -> None:
    ctl, pactl, plans = controller
    plans[0] = wave_plan(machine)
    ctl._probe()
    assert ctl._mic == WAVE_MIC
    pactl.sources = ["alsa_input.pci-onboard.analog-stereo"]
    plans[0] = recorded("deskmeet1")
    ctl._probe()
    assert ctl._mic == ""
    pactl.sources = [WAVE_MIC, "alsa_input.pci-onboard.analog-stereo"]
    plans[0] = wave_plan(FakeMachine(tmp_path / "again"))
    ctl._probe()
    assert ctl._mic == WAVE_MIC


def test_a_plan_that_names_a_vanished_node_asks_for_a_fresh_probe(
    controller: tuple[AudioController, Pactl, list[Plan | None]], machine: FakeMachine
) -> None:
    ctl, pactl, plans = controller
    asked: list[bool] = []
    ctl.plan_refresh = lambda: asked.append(True)
    plans[0] = wave_plan(machine)
    pactl.sources = ["alsa_input.pci-onboard.analog-stereo"]
    ctl._probe()
    assert asked == [True] and ctl._mic == ""


def test_a_card_at_profile_off_is_switched_on_once_per_retry_window(
    controller: tuple[AudioController, Pactl, list[Plan | None]], machine: FakeMachine
) -> None:
    ctl, pactl, plans = controller
    studio_wave(machine)
    machine.set_pipewire([(49, WAVE_CARD)])
    machine.set_card_profiles({WAVE_CARD: ("off", "0fd9:007d")})
    plans[0] = resolve(machine.snapshot(), CATALOGUE)
    ctl._probe()
    ctl._probe()
    switches = [c for c in pactl.commands if c[:2] == ["pactl", "set-card-profile"]]
    assert switches == [["pactl", "set-card-profile", WAVE_CARD, "output:analog-stereo+input:mono-fallback"]]


def test_without_a_plan_the_controller_keeps_the_name_rule(
    controller: tuple[AudioController, Pactl, list[Plan | None]],
) -> None:
    ctl, _pactl, _plans = controller
    ctl._probe()
    assert ctl._mic in (WAVE_MIC, "alsa_input.pci-onboard.analog-stereo")
    assert audio.VOICE_SOURCE_NAME not in ctl._mic
