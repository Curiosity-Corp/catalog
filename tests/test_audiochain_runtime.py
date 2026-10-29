"""The chain's model, its link to PipeWire (with a fake runner), the migration and the command line helpers."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from streamstack import config
from streamstack.audiochain import chain, cli, control, graph, migrate, presets, runtime, stages


def done(stdout: str = "", code: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout, stderr)


def node(node_id: int, name: str, media_class: str, **props: object) -> dict[str, object]:
    return {
        "id": node_id,
        "type": "PipeWire:Interface:Node",
        "info": {"state": "running", "props": {"node.name": name, "media.class": media_class, **props}},
    }


DUMP = [
    node(40, "alsa_input.usb-Wave", "Audio/Source"),
    node(
        77,
        "streamstack_mic",
        "Audio/Source",
        **{"node.description": "StreamStack Mic", "audio.rate": 48000, "audio.channels": 1},
    ),
    node(78, "streamstack_mic.capture", "Stream/Input/Audio"),
    {"id": 3, "type": "PipeWire:Interface:Port", "info": {}},
]


class Fake:
    """A PipeWire command line that records what it was asked."""

    def __init__(self, dump: object = DUMP, set_ok: bool = True) -> None:
        self.dump = dump
        self.set_ok = set_ok
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], timeout: float = 10) -> subprocess.CompletedProcess[str] | None:
        self.calls.append(argv)
        if argv[0] == "pw-dump":
            return done(json.dumps(self.dump))
        if argv[0] == "pw-cli":
            return done("", 0 if self.set_ok else 1)
        return None


@pytest.fixture
def library_engines() -> list[str]:
    return ["deepfilternet", "rnnoise"]


def model(tmp_path: Path, engines: list[str], **kw: object) -> chain.ChainModel:
    return chain.ChainModel(store=tmp_path / "state.json", engines=lambda: engines, **kw)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# runtime
# ---------------------------------------------------------------------------


def test_the_nodes_and_the_48k_contract_come_from_pw_dump() -> None:
    nodes = runtime.find_nodes(Fake())
    assert nodes is not None and nodes.present
    assert (nodes.source_id, nodes.control_id) == (77, 78)
    assert nodes.contract() == {
        "name": "streamstack_mic",
        "description": "StreamStack Mic",
        "media_class": "Audio/Source",
        "rate": 48000,
        "channels": 1,
        "state": "running",
    }


def test_missing_nodes_and_unusable_pw_dump_are_told_apart() -> None:
    gone = runtime.find_nodes(Fake(dump=[DUMP[0]]))
    assert gone is not None and not gone.present and gone.control_id is None
    assert gone.contract()["name"] is None
    assert runtime.find_nodes(lambda argv, t: None) is None
    assert runtime.find_nodes(lambda argv, t: done("", 1)) is None
    assert runtime.find_nodes(lambda argv, t: done("not json")) is None
    assert runtime.find_nodes(Fake(dump={"x": 1})) is not None  # a dump that is not a list has no nodes


def test_controls_are_sent_as_quoted_props_and_a_refusal_is_reported() -> None:
    body = runtime.props_json({"comp:Ratio": 4.0, "gain:Mult": 1.2589254})
    assert body == '{ params = [ "comp:Ratio" 4 "gain:Mult" 1.25893 ] }'
    with pytest.raises(ValueError):
        runtime.props_json({'bad"name': 1.0})
    fake = Fake()
    assert runtime.set_controls(78, {"comp:Ratio": 4.0}, fake)
    assert fake.calls == [["pw-cli", "set-param", "78", "Props", '{ params = [ "comp:Ratio" 4 ] }']]
    assert runtime.set_controls(78, {}, fake) and len(fake.calls) == 1
    assert not runtime.set_controls(78, {"a:b": 1.0}, Fake(set_ok=False))
    assert not runtime.set_controls(78, {"a:b": 1.0}, lambda argv, t: done("Error: no such node"))
    assert not runtime.set_controls(78, {"a:b": 1.0}, lambda argv, t: None)


def test_the_conf_is_written_atomically_and_only_when_it_changes(tmp_path: Path) -> None:
    path = tmp_path / "audio-chain" / "filter-chain.conf"
    assert runtime.write_conf(path, "a") is True
    assert runtime.write_conf(path, "a") is False
    assert runtime.write_conf(path, "b") is True and path.read_text() == "b"
    assert not list(path.parent.glob("*.tmp"))


def test_host_processes_are_matched_on_the_exact_command_line(tmp_path: Path) -> None:
    conf = tmp_path / "filter-chain.conf"
    proc = tmp_path / "proc"
    for pid, argv in (
        (100, ["pipewire", "-c", str(conf)]),
        (101, ["pipewire", "-c", str(tmp_path / "other.conf")]),
        (102, ["pipewire"]),
        (103, ["pipewire", "-c", str(conf), "-v"]),
    ):
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (proc / "self").mkdir()
    assert sorted(runtime.host_pids(conf, proc)) == [100, 103]
    assert runtime.host_pids(conf, tmp_path / "nowhere") == []
    assert os.getuid() == (proc / "100").stat().st_uid


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------


def test_the_settings_are_saved_per_microphone_and_restored(tmp_path: Path, library_engines: list[str]) -> None:
    a = model(tmp_path, library_engines, rig_key="mic-a")
    a.set_params({"comp_ratio": 6})
    a.set_bypass(True)
    a.set_loudness(on=True, target=-23.0)
    assert a.flush()
    a.set_rig("mic-b")  # a microphone with no profile yet keeps the sound that is running
    assert a.params["comp_ratio"] == 6
    a.set_params({"hp_hz": 120, "comp_ratio": 2})
    a.flush()
    a.set_rig("mic-a")  # and a known microphone gets its own profile back
    assert a.params["comp_ratio"] == 6 and a.params["hp_hz"] != 120
    assert a.bypass and a.loudness_on and a.target == -23.0
    fresh = model(tmp_path, library_engines)  # the next start continues with the mic that was active
    assert fresh.params["comp_ratio"] == 6


def test_a_change_made_by_another_process_is_picked_up(tmp_path: Path, library_engines: list[str]) -> None:
    console = model(tmp_path, library_engines)
    console.flush()
    other = model(tmp_path, library_engines)
    other.set_bypass(True)
    other.flush()
    future = os.stat(tmp_path / "state.json").st_mtime + 5
    os.utime(tmp_path / "state.json", (future, future))
    assert console.sync_external() is True and console.bypass
    assert console.sync_external() is False


def test_a_strict_engine_preset_is_refused_without_its_plugin(tmp_path: Path) -> None:
    m = model(tmp_path, ["rnnoise"])
    with pytest.raises(chain.ChainError) as err:
        m.load_preset("StreamStack Voice DeepFilterNet")
    assert err.value.status == 409 and "libdeep_filter_ladspa" in str(err.value)
    with pytest.raises(chain.ChainError):
        m.set_engine("deepfilternet")
    m.set_engine("rnnoise")
    assert m.active_engine() == "rnnoise"
    with pytest.raises(chain.ChainError):
        m.load_preset("No Such Preset")


def test_the_engine_falls_back_when_the_wanted_one_is_not_installed(tmp_path: Path) -> None:
    assert model(tmp_path, ["rnnoise"]).active_engine() == "rnnoise"
    assert model(tmp_path / "x", []).active_engine() == "off"


def test_custom_reset_and_save_as_preset(tmp_path: Path, library_engines: list[str]) -> None:
    m = model(tmp_path, library_engines)
    assert not m.custom()
    m.set_params({"comp_ratio": 8})
    assert m.custom()
    m.reset()
    assert not m.custom() and m.params["comp_ratio"] != 8
    m.set_params({"comp_ratio": 8})
    saved = m.save_as_preset("Interview")
    assert saved.user and m.preset == "Interview" and not m.custom()
    assert "Interview" in presets.load_all()
    for bad in ("", "a/b", "x" * 81):
        with pytest.raises(chain.ChainError):
            m.save_as_preset(bad)


def test_bad_requests_are_refused_without_changing_anything(tmp_path: Path, library_engines: list[str]) -> None:
    m = model(tmp_path, library_engines)
    before = dict(m.params), m.revision
    for call in (
        lambda: m.set_params({"comp_ratio": 999}),
        lambda: m.set_params({}),
        lambda: m.set_engine("krisp"),
        lambda: m.set_loudness(target=-10.0),
    ):
        with pytest.raises(chain.ChainError) as err:
            call()
        assert err.value.status == 400
    assert (dict(m.params), m.revision) == before


def test_the_leveller_gain_reaches_the_controls_and_bumps_the_revision(
    tmp_path: Path, library_engines: list[str]
) -> None:
    m = model(tmp_path, library_engines)
    m.set_loudness(on=True)
    rev = m.revision
    for _ in range(50):
        m.level_step(0.1, -30.0 + m.auto_db, muted=False)
    assert m.auto_db > 0 and m.revision > rev
    assert m.controls()[f"{stages.GAIN}:Mult"] > stages.db_to_lin(float(m.params["makeup_db"]))
    frozen = m.auto_db
    rev = m.revision
    m.level_step(0.1, -20.0, muted=True)
    assert m.auto_db == frozen and m.revision == rev


def test_the_view_has_every_stage_and_the_loudness_block(tmp_path: Path, library_engines: list[str]) -> None:
    view = model(tmp_path, library_engines).view()
    assert [s["id"] for s in view["stages"]] == list(stages.STAGE_IDS)
    assert view["loudness"]["targets"] == [-14.0, -16.0, -23.0]
    assert view["loudness"]["true_peak_db"] == -1.0
    assert [e["id"] for e in view["engines"]] == list(stages.DENOISE_ENGINES)
    json.dumps(view)


def test_the_gain_reduction_estimate_follows_the_curves() -> None:
    p = stages.defaults()
    assert chain.estimate_reduction(p, -60.0, -50.0) == 0.0
    loud = chain.estimate_reduction(p, -10.0, -3.0)
    louder = chain.estimate_reduction(p, -4.0, 0.0)
    assert 0 < loud < louder
    p["comp_on"] = False
    p["limiter_on"] = False
    assert chain.estimate_reduction(p, 0.0, 0.0) == 0.0


# ---------------------------------------------------------------------------
# the runtime that keeps PipeWire matching the model
# ---------------------------------------------------------------------------


@pytest.fixture
def plugins_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graph, "missing_required", lambda: [])


def make_runtime(tmp_path: Path, engines: list[str], fake: Fake) -> chain.ChainRuntime:
    return chain.ChainRuntime(model(tmp_path, engines), tmp_path, runner=fake, autostart=False)


def test_a_fresh_graph_gets_everything_once_then_only_what_changed(
    tmp_path: Path, library_engines: list[str], plugins_present: None
) -> None:
    fake = Fake()
    rt = make_runtime(tmp_path, library_engines, fake)
    rt.supervise()
    assert rt.running and rt.status()["contract"]["rate"] == 48000
    rt.tick()
    sets = [c for c in fake.calls if c[0] == "pw-cli"]
    assert sets and sets[0][2] == "78"
    fake.calls.clear()
    rt.tick()
    assert not [c for c in fake.calls if c[0] == "pw-cli"]  # nothing changed, nothing sent
    rt.model.set_params({"comp_ratio": 7})
    rt.tick()
    sent = [c for c in fake.calls if c[0] == "pw-cli"]
    assert len(sent) == 1 and "comp:Ratio" in sent[0][4] and "gate:" not in sent[0][4]


def test_bypass_is_crossfaded_in_steps(
    tmp_path: Path, library_engines: list[str], plugins_present: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chain, "FADE_STEP_S", 0)
    fake = Fake()
    rt = make_runtime(tmp_path, library_engines, fake)
    rt.supervise()
    rt.tick()
    fake.calls.clear()
    rt.model.set_bypass(True)
    rt.tick()
    mixes = [c[4] for c in fake.calls if "mix:Gain 2" in c[4]]
    assert len(mixes) == chain.FADE_STEPS and mixes[-1].endswith('"mix:Gain 2" 1 ] }')


def test_a_refused_push_makes_the_runtime_look_the_node_up_again(
    tmp_path: Path, library_engines: list[str], plugins_present: None
) -> None:
    fake = Fake()
    rt = make_runtime(tmp_path, library_engines, fake)
    rt.supervise()
    fake.set_ok = False
    rt.tick()
    assert rt._node_id is None
    fake.set_ok = True
    rt.supervise()
    assert rt._node_id == 78


def test_missing_plugins_are_reported_instead_of_starting_a_broken_graph(
    tmp_path: Path, library_engines: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(graph, "missing_required", lambda: ["zam-plugins"])
    rt = make_runtime(tmp_path, library_engines, Fake())
    rt.supervise()
    st = rt.status()
    assert not st["running"] and not st["available"] and "zam-plugins" in st["error"]


def test_the_structure_signature_changes_with_the_engine_and_the_microphone() -> None:
    sig = chain.structure_signature
    assert sig("rnnoise", "a") != sig("deepfilternet", "a") != sig("deepfilternet", "b")
    assert sig("rnnoise", "a") == sig("rnnoise", "a")


def test_the_written_config_ends_with_the_structure_signature(
    tmp_path: Path, library_engines: list[str], plugins_present: None
) -> None:
    rt = make_runtime(tmp_path, library_engines, Fake())
    rt.set_microphone("alsa_input.usb-Wave")
    assert rt.write_conf() is True and rt.write_conf() is False
    text = rt.conf.read_text()
    assert text.rstrip().endswith("# structure " + rt.desired_signature())
    assert "alsa_input.usb-Wave" in text


# ---------------------------------------------------------------------------
# migration from Easy Effects
# ---------------------------------------------------------------------------


def ee_doc(bass: float = 4.0, engine: str | None = "rnnoise") -> dict[str, object]:
    left = {f"band{i}": {"gain": g} for i, g in enumerate((bass, 0.0, 2.5, 1.5))}
    doc: dict[str, object] = {"input": {"blocklist": [], "equalizer#0": {"left": left}}}
    if engine:
        doc["input"]["rnnoise#0"] = {"attenuation-limit": 40.0} if engine == "deepfilternet" else {}  # type: ignore[index]
        if engine == "deepfilternet":
            doc["input"][f"{engine}#0"] = doc["input"].pop("rnnoise#0")  # type: ignore[index, union-attr]
        else:
            doc["input"]["rnnoise#0"] = {}  # type: ignore[index]
    return doc


def test_easy_effects_presets_are_imported_once_without_touching_the_originals(tmp_path: Path) -> None:
    source = tmp_path / "ee"
    source.mkdir()
    (source / "My Podcast.json").write_text(json.dumps(ee_doc(bass=4.0)))
    (source / "StreamStack Voice Warm.json").write_text(json.dumps(ee_doc()))  # shipped name: not imported
    (source / f"{migrate.MANAGED_PRESET}.json").write_text(json.dumps(ee_doc()))  # the Console's own scratch preset
    (source / "Broken.json").write_text("{nope")
    original = (source / "My Podcast.json").read_text()
    assert migrate.migrate(source) == ["My Podcast"]
    preset = presets.load_all()["My Podcast"]
    assert preset.user and preset.engine == "rnnoise"
    assert preset.resolved()["bass_db"] == 4.0 and preset.resolved()["presence_db"] == 2.5
    assert (source / "My Podcast.json").read_text() == original
    assert migrate.marker().exists()
    (source / "Later.json").write_text(json.dumps(ee_doc()))
    assert migrate.migrate(source) == []  # once


def test_migration_with_no_easy_effects_folder_is_quiet_and_still_marks_itself(tmp_path: Path) -> None:
    assert migrate.migrate(tmp_path / "missing") == []
    assert migrate.marker().exists()


def test_the_previous_voice_choice_is_read_from_the_old_sidecar() -> None:
    assert migrate.previous_choice() is None
    config.state_dir().mkdir(parents=True, exist_ok=True)
    (config.state_dir() / "console-live.json").write_text(json.dumps({"base": "StreamStack Voice Warm"}))
    assert migrate.previous_choice() == "StreamStack Voice Warm"


def test_denoise_attenuation_carries_over_for_deepfilternet() -> None:
    doc = {"input": {"deepfilternet#0": {"attenuation-limit": 35}}}
    assert migrate.read_engine(doc) == ("deepfilternet", 35.0)
    assert migrate.convert("X", doc).resolved()["denoise_atten_db"] == 35.0
    assert migrate.read_engine({"input": {}}) is None


# ---------------------------------------------------------------------------
# control and command line
# ---------------------------------------------------------------------------


def test_another_process_can_choose_a_preset_and_bypass_and_the_live_graph_follows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(graph, "available_engines", lambda: ["deepfilternet", "rnnoise"])
    assert control.current_preset() is None
    fake = Fake()
    assert control.load_preset("StreamStack Voice Warm", fake)
    assert control.current_preset() == "StreamStack Voice Warm"
    assert [c for c in fake.calls if c[0] == "pw-cli"]
    assert not control.load_preset("Nope", fake)
    control.set_bypass(True, fake)
    assert control.bypassed() and control.voice_source_present(fake)
    assert not control.voice_source_present(Fake(dump=[]))
    assert control.apply_live(control.model(), Fake(dump=[])) is False
    assert "StreamStack Voice Warm" in control.preset_names()


def test_the_cli_prints_the_config_and_stops_nothing_when_nothing_runs(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(graph, "available_engines", lambda: ["rnnoise"])
    assert cli.print_config({}) == 0
    out = capsys.readouterr().out
    assert 'node.name = "streamstack_mic"' in out and "librnnoise_ladspa" in out
    assert cli.stop({}) == 0
    assert "not running" in capsys.readouterr().out


def test_cli_start_reports_why_the_chain_is_not_running(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(graph, "missing_required", lambda: ["zam-plugins"])
    lines: list[str] = []
    assert cli.start({}, wait=0, say=lines.append) == 1
    assert "zam-plugins" in lines[0]


def test_a_dragged_high_pass_corner_is_sent_once_when_it_settles(
    tmp_path: Path, library_engines: list[str], plugins_present: None
) -> None:
    now = [0.0]
    fake = Fake()
    rt = chain.ChainRuntime(
        model(tmp_path, library_engines), tmp_path, runner=fake, autostart=False, clock=lambda: now[0]
    )
    rt.supervise()
    rt.tick()  # a fresh graph is sent whole, at once
    fake.calls.clear()

    def hp_sets() -> list[list[str]]:
        return [c for c in fake.calls if c[0] == "pw-cli" and "hp1:Freq" in c[4]]

    for hz in (90, 100, 110, 120):  # a drag: a new value every 50 ms
        rt.model.set_params({"hp_hz": hz})
        now[0] += 0.05
        rt.tick()
    assert not hp_sets()  # nothing yet: it is still moving
    now[0] += chain.HP_SETTLE_S
    rt.tick()
    sent = hp_sets()
    assert len(sent) == 1 and "120" in sent[0][4]
    fake.calls.clear()
    now[0] += 1.0
    rt.tick()
    assert not hp_sets()


# ---------------------------------------------------------------------------
# a deliberate `chain stop` is honoured by the Console's supervisor (#145)
# ---------------------------------------------------------------------------


class FakeHost:
    """The host process: `start_host` brings it up, `stop_host` takes it down; nothing real is touched."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, running: bool = True) -> None:
        self.running = running
        self.starts = 0
        monkeypatch.setattr(runtime, "host_pids", lambda conf, proc=None: [4242] if self.running else [])
        monkeypatch.setattr(runtime, "start_host", self._start)
        monkeypatch.setattr(runtime, "stop_host", self._stop)

    def _start(self, conf: Path, log_path: Path | None = None) -> object:
        self.running = True
        self.starts += 1
        return object()

    def _stop(self, conf: Path, wait: float = 3.0) -> bool:
        self.running = False
        return True


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state-home"
    monkeypatch.setattr(config, "state_dir", lambda: state)
    return state


def supervising_runtime(state: Path, engines: list[str]) -> chain.ChainRuntime:
    """The Console's runtime: it autostarts the host."""
    rt = chain.ChainRuntime(chain.ChainModel(store=None, engines=lambda: engines), state, runner=Fake(), autostart=True)
    rt.set_microphone("alsa_input.usb-Wave")
    return rt


def test_the_supervisor_revives_a_dead_host_when_nobody_stopped_it(
    state_dir: Path, library_engines: list[str], plugins_present: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = FakeHost(monkeypatch, running=False)
    rt = supervising_runtime(state_dir, library_engines)
    rt.supervise()
    assert host.starts == 1 and not rt.held


def test_the_supervisor_leaves_a_chain_stopped_on_purpose_stopped(
    state_dir: Path, library_engines: list[str], plugins_present: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = FakeHost(monkeypatch, running=False)
    chain.mark_stopped(state_dir)
    rt = supervising_runtime(state_dir, library_engines)
    for _ in range(3):
        rt.supervise()
    assert host.starts == 0
    st = rt.status()
    assert st["held"] is True and st["running"] is False and "stopped on purpose" in st["error"]


def test_chain_stop_while_the_console_supervises_stays_stopped_until_chain_start(
    state_dir: Path,
    library_engines: list[str],
    plugins_present: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    host = FakeHost(monkeypatch, running=True)
    monkeypatch.setattr(cli, "release_default", lambda cfg: 0)
    rt = supervising_runtime(state_dir, library_engines)
    assert cli.stop({}) == 0
    assert "chain start" in capsys.readouterr().out
    rt.supervise()  # the Console's next tick: it must not bring the host back
    assert host.running is False and host.starts == 0 and rt.status()["held"] is True
    cli.clear_stop_marker()  # what `chain start` does first
    assert not chain.stopped_by_user(state_dir)
    rt.supervise()
    assert host.starts == 1 and rt.status()["held"] is False


def test_chain_stop_says_the_console_keeps_it_stopped(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    FakeHost(monkeypatch, running=True)
    monkeypatch.setattr(cli, "release_default", lambda cfg: 0)
    assert cli.stop({}) == 0
    assert "Console will keep it stopped until you run `streamstack chain start`" in capsys.readouterr().out
    assert chain.stopped_by_user(state_dir)


def test_chain_start_clears_the_stop_marker(
    state_dir: Path, library_engines: list[str], plugins_present: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeHost(monkeypatch, running=False)
    chain.mark_stopped(state_dir)
    monkeypatch.setattr(cli, "_runtime", lambda cfg, **kw: supervising_runtime(state_dir, library_engines))
    cli.start({}, wait=0, say=lambda line: None)
    assert not chain.stopped_by_user(state_dir)
