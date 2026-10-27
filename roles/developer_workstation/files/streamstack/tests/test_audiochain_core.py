"""The voice chain's pure parts: stage parameters, the filter-graph configuration, presets and the leveller."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from streamstack.audiochain import graph, leveller, presets, stages

# ---------------------------------------------------------------------------
# Stages and parameters
# ---------------------------------------------------------------------------


def test_every_stage_has_an_on_switch_and_parameters_inside_their_ranges() -> None:
    d = stages.defaults()
    for stage in stages.STAGES:
        assert d[stages.on_key(stage.id)] is True
    for p in stages.PARAMS:
        assert p.stage in stages.STAGE_IDS
        assert p.low <= p.default <= p.high, p.key
        assert d[p.key] == p.default


def test_validate_checks_keys_types_and_ranges() -> None:
    assert stages.validate({"comp_ratio": 4, "gate_on": False}) == {"comp_ratio": 4.0, "gate_on": False}
    for bad in (
        {"nope": 1},
        {"comp_ratio": 99},
        {"comp_ratio": True},
        {"comp_ratio": "3"},
        {"comp_ratio": float("nan")},
        {"gate_on": 1},
        {"bogus_on": True},
    ):
        with pytest.raises(ValueError):
            stages.validate(bad)


def test_merged_ignores_stale_keys_from_old_presets() -> None:
    out = stages.merged({"removed_param": 3, "hp_hz": 100}, {"comp_ratio": 2})
    assert out["hp_hz"] == 100 and out["comp_ratio"] == 2 and "removed_param" not in out


def test_the_default_chain_is_what_the_controls_say() -> None:
    c = stages.controls(stages.defaults())
    assert c[f"{stages.HP1}:Freq"] == c[f"{stages.HP2}:Freq"] == 80
    assert c[f"{stages.GATE}:Max gate close"] == -15


def test_a_stage_that_is_off_is_transparent_instead_of_removed() -> None:
    off = {stages.on_key(s): False for s in stages.STAGE_IDS}
    c = stages.controls(stages.merged({}, off))
    assert c[f"{stages.HP1}:Freq"] == stages.TRANSPARENT_HP_HZ
    assert c[f"{stages.GATE}:Max gate close"] == 0.0
    assert all(c[f"{node}:Gain"] == 0.0 for node in (stages.EQ_LOW, stages.EQ_MUD, stages.EQ_PRES, stages.EQ_AIR))


def test_bypass_crossfades_between_the_chain_and_the_dry_path() -> None:
    processed = stages.bypass_controls(False)
    raw = stages.bypass_controls(True)
    assert processed != raw
    assert raw[f"{stages.MIX}:Gain 1"] == 0.0 and raw[f"{stages.MIX}:Gain 2"] == 1.0
    assert stages.bypass_controls(False, 0.5)[f"{stages.MIX}:Gain 2"] == 0.5


def test_the_leveller_gain_is_added_on_the_makeup_gain_node_only() -> None:
    base = stages.controls(stages.defaults())
    boosted = stages.controls(stages.defaults(), auto_db=6.0)
    changed = {k for k in base if base[k] != boosted[k]}
    assert changed == {f"{stages.GAIN}:Mult"}
    assert boosted[f"{stages.GAIN}:Mult"] == pytest.approx(stages.db_to_lin(stages.defaults()["makeup_db"] + 6.0))


# ---------------------------------------------------------------------------
# The filter-graph configuration
# ---------------------------------------------------------------------------

PATHS = {
    "deepfilternet": "/x/libdeep_filter_ladspa.so",
    "rnnoise": "/x/librnnoise_ladspa.so",
    "gate": "/x/zamgate-ladspa.so",
    "deesser": "/x/zamdynamiceq-ladspa.so",
    "compressor": "/x/zamcomp-ladspa.so",
    "limiter": "/x/zamaximx2-ladspa.so",
}


def build(engine: str = "deepfilternet", **kw: object) -> str:
    params = kw.pop("params", stages.defaults())
    return graph.build_config(params, engine=engine, plugin_paths=PATHS, **kw)  # type: ignore[arg-type]


def test_the_config_exposes_a_48k_mono_source_named_streamstack_mic() -> None:
    text = build(microphone="alsa_input.usb-Wave")
    assert 'node.name = "streamstack_mic"' in text
    assert "StreamStack Mic" in text
    assert "Audio/Source" in text
    assert "audio.rate = 48000" in text
    assert "alsa_input.usb-Wave" in text


def test_the_stages_are_chained_in_signal_order() -> None:
    text = build()
    order = [
        text.index(f"name = {n}") for n in (stages.HP1, stages.GATE, "dfn", stages.EQ_LOW, stages.COMP, stages.GAIN)
    ]
    assert order == sorted(order)
    assert text.index("name = " + stages.COMP) < text.index("name = " + stages.LIM)


def test_the_neural_stage_follows_the_engine() -> None:
    assert "libdeep_filter_ladspa" in build("deepfilternet")
    assert "librnnoise_ladspa" in build("rnnoise") and "libdeep_filter_ladspa" not in build("rnnoise")
    plain = build("off")
    assert "libdeep_filter_ladspa" not in plain and "librnnoise_ladspa" not in plain


def test_the_config_is_deterministic_and_plain_text() -> None:
    assert build() == build()
    assert "\0" not in build()
    # braces balance: a PipeWire conf with a stray brace does not load
    text = build()
    assert text.count("{") == text.count("}") and text.count("[") == text.count("]")


def test_pick_engine_falls_back_to_the_best_installed_one() -> None:
    assert graph.pick_engine("deepfilternet", ["deepfilternet", "rnnoise"]) == "deepfilternet"
    assert graph.pick_engine("deepfilternet", ["rnnoise"]) == "rnnoise"
    assert graph.pick_engine("rnnoise", []) == "off"
    assert graph.pick_engine(None, ["rnnoise"]) == "rnnoise"


def test_plugins_are_found_on_the_ladspa_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LADSPA_PATH", str(tmp_path))
    monkeypatch.setattr(graph, "LADSPA_DIRS", ())
    assert graph.find_plugin("rnnoise") is None and graph.available_engines() == []
    (tmp_path / graph.PLUGINS["rnnoise"]["file"]).write_bytes(b"")
    assert graph.find_plugin("rnnoise") == tmp_path / graph.PLUGINS["rnnoise"]["file"]
    assert graph.available_engines() == ["rnnoise"]
    assert graph.missing_required()  # the ZAM plugins are not in that folder


def test_the_a_b_delay_is_known_for_every_engine() -> None:
    assert set(graph.CHAIN_DELAY_S) == {*stages.DENOISE_ENGINES, "off"}
    assert graph.CHAIN_DELAY_S["deepfilternet"] > graph.CHAIN_DELAY_S["rnnoise"] > graph.CHAIN_DELAY_S["off"]


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


def test_the_shipped_presets_keep_their_names_and_resolve_inside_the_ranges() -> None:
    library = presets.load_all()
    assert next(iter(library)) == "StreamStack Voice"
    assert {
        "StreamStack Voice Warm",
        "StreamStack Voice Bright",
        "StreamStack Voice Deep",
        "StreamStack Voice Minimal",
        "StreamStack Voice DeepFilterNet",
    } <= set(library)
    for preset in library.values():
        params = preset.resolved()
        stages.validate(params)  # inside every range
    assert library["StreamStack Voice DeepFilterNet"].strict_engine is True
    assert library["StreamStack Voice Minimal"].resolved()["bass_db"] == 0.0


def test_a_user_preset_round_trips_and_a_shipped_name_can_be_overridden(tmp_path: Path) -> None:
    preset = presets.Preset("Podcast", "mine", "rnnoise", False, {"comp_ratio": 5.0}, True)
    path = presets.save_user(preset, tmp_path)
    assert json.loads(path.read_text())["params"] == {"comp_ratio": 5.0}
    library = presets.load_all(user=tmp_path)
    assert library["Podcast"].engine == "rnnoise" and library["Podcast"].user is True
    presets.save_user(presets.Preset("StreamStack Voice Warm", "mine", "rnnoise", False, {}, True), tmp_path)
    assert presets.load_all(user=tmp_path)["StreamStack Voice Warm"].blurb == "mine"


@pytest.mark.parametrize(
    "doc",
    [
        [],
        {"name": ""},
        {"name": "x/y"},
        {"name": "A", "engine": "krisp"},
        {"name": "A", "params": {"comp_ratio": 99}},
        {"name": "A", "params": []},
        {"name": "A", "params": {"unknown": 1}},
    ],
)
def test_a_bad_preset_document_is_refused_with_a_reason(doc: object) -> None:
    with pytest.raises(presets.PresetError):
        presets.parse(doc, user=True)


def test_a_broken_file_in_the_preset_folder_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "broken.json").write_text("{not json")
    (tmp_path / "ok.json").write_text(json.dumps({"name": "Fine", "params": {}}))
    assert "Fine" in presets.load_all(user=tmp_path) and "broken" not in presets.load_all(user=tmp_path)


def test_delta_stores_only_what_differs_from_the_defaults() -> None:
    assert presets.delta(stages.merged({}, {"comp_ratio": 5})) == {"comp_ratio": 5.0}


# ---------------------------------------------------------------------------
# The leveller
# ---------------------------------------------------------------------------


def step_response(before: float, after: float, seconds: float = 30.0, target: float = -16.0) -> list[float]:
    """Output loudness over time when speech steps from `before` to `after` LUFS (input level), starting settled."""
    lv = leveller.Leveller(target)
    for _ in range(600):  # settle on the first level
        lv.update(0.1, before + lv.auto_db)
    out = []
    for _ in range(int(seconds * 10)):
        loud = after + lv.auto_db
        lv.update(0.1, loud)
        out.append(loud)
    return out


def settle_time(out: list[float], target: float = -16.0) -> float | None:
    for i in range(len(out)):
        if all(abs(x - target) <= 1.0 for x in out[i:]):
            return i / 10
    return None


@pytest.mark.parametrize("step_db", [3, 6, 9, 12])
@pytest.mark.parametrize("direction", [1, -1])
def test_speech_steps_settle_within_one_lu_of_the_target_inside_ten_seconds(step_db: int, direction: int) -> None:
    out = step_response(-16.0, -16.0 + direction * step_db)
    assert (settle_time(out) or 99) < 10.0


def test_a_twenty_db_swing_is_followed_in_two_steps_inside_the_window() -> None:
    """Speech 20 dB under the target is corrected up to the +12 dB window, then settles once it is reachable."""
    lv = leveller.Leveller()
    levels = [-26.0, -36.0, -16.0]  # +10, then +20 (out of reach: at-limit), then back to the target
    for level in levels:
        for _ in range(900):
            lv.update(0.1, level + lv.auto_db)
    assert abs(levels[0] + 10 - -16.0) < 1  # sanity of the scenario
    assert lv.auto_db <= lv.window


def test_the_approach_is_monotone_so_there_is_no_ringing() -> None:
    boost = step_response(-16.0, -28.0)
    cut = step_response(-16.0, -4.0)
    assert max(boost[10:]) <= -16.0 + 1.0 and min(cut[10:]) >= -16.0 - 1.0


def test_the_window_limits_how_far_the_leveller_can_go() -> None:
    lv = leveller.Leveller()  # speech at -42 LUFS needs +26 dB, the window gives +12
    for _ in range(1200):
        lv.update(0.1, -42.0 + lv.auto_db)
    assert lv.auto_db == lv.window and lv.state == "at-limit"


def test_the_gain_never_moves_in_silence_while_muted_or_without_a_signal_or_off() -> None:
    lv = leveller.Leveller()
    for _ in range(100):
        lv.update(0.1, -30.0 + lv.auto_db)
    gain = lv.auto_db
    assert gain > 0
    for kwargs, state in (
        ({"lufs_m": -70.0}, "silence"),
        ({"lufs_m": -20.0, "muted": True}, "muted"),
        ({"lufs_m": None}, "no-signal"),
        ({"lufs_m": float("nan")}, "no-signal"),
        ({"lufs_m": -20.0, "enabled": False}, "off"),
    ):
        for _ in range(600):
            assert lv.update(0.1, **kwargs) == gain
        assert lv.state == state


def test_the_gain_moves_at_most_half_a_db_a_second_near_the_target() -> None:
    lv = leveller.Leveller(-16.0)
    for _ in range(100):  # settle
        lv.update(0.1, -16.0 + lv.auto_db)
    before = lv.auto_db
    for _ in range(10):  # one second with speech 1.8 dB too quiet
        lv.update(0.1, -17.8 + lv.auto_db - before)
    assert abs(lv.auto_db - before) <= leveller.FINE_RATE_DB_S + 1e-9


def test_the_dead_band_holds_still() -> None:
    lv = leveller.Leveller(-16.0)
    for _ in range(300):
        lv.update(0.1, -16.3)
    assert lv.state == "holding" and lv.auto_db == 0.0


def test_reset_clamps_and_status_reports_the_smoothed_level() -> None:
    lv = leveller.Leveller()
    lv.reset(99.0)
    assert lv.auto_db == lv.window
    assert lv.status().level_lufs is None
    lv.update(0.1, -20.0)
    assert lv.status().level_lufs == pytest.approx(-20.0, abs=0.1)
    assert re.fullmatch("|".join(leveller.STATES), lv.status().state)


def test_pauses_between_words_do_not_drag_the_speech_level_down() -> None:
    sl = leveller.SpeechLevel()
    for _ in range(100):
        sl.update(0.1, -20.0)
    for _ in range(3):  # a short pause: readings far below the running level
        sl.update(0.1, -45.0)
    assert sl.value == pytest.approx(-20.0, abs=0.1)


def test_a_voice_that_gets_quieter_is_followed_not_ignored_as_a_pause() -> None:
    sl = leveller.SpeechLevel()
    for _ in range(100):
        sl.update(0.1, -10.0)
    for _ in range(30):  # three seconds of speech 25 dB lower
        sl.update(0.1, -35.0)
    assert sl.value is not None and sl.value < -30.0


def test_a_twenty_db_drop_is_climbed_back_inside_ten_seconds_with_gaps_in_the_speech() -> None:
    """Loud speech settled, then 20 dB quieter speech with pauses (silence readings) between the words."""
    lv = leveller.Leveller()
    for _ in range(600):
        lv.update(0.1, -2.0 + lv.auto_db)
    start = lv.auto_db
    reached = None
    for i in range(300):
        pause = (i % 20) >= 14  # 1.4 s of speech, 0.6 s of pause
        lv.update(0.1, -90.0 if pause else -22.0 + lv.auto_db)
        if reached is None and -22.0 + lv.auto_db >= -17.0:
            reached = i / 10
    assert start < -5.0 and reached is not None and reached < 10.0


def test_a_hot_microphone_is_cut_even_when_the_limiter_hides_it() -> None:
    """The output is measured after the limiter, so a voice 20 dB hotter reads the same: the raw level must win."""
    lv = leveller.Leveller(-16.0)
    makeup = 4.0
    for _ in range(300):  # a normal voice at -20 raw, output on target
        lv.update(0.1, -16.0, mic_lufs=-20.0, static_db=makeup)
    settled = lv.auto_db
    for _ in range(200):  # 20 dB hotter at the mic; the limited output still reads -15
        lv.update(0.1, -15.0, mic_lufs=0.0, static_db=makeup)
    assert lv.auto_db < settled - 10.0
    assert lv.state in ("capped", "at-limit")


def test_the_microphone_guard_never_lets_the_gain_climb_past_its_cap() -> None:
    lv = leveller.Leveller(-16.0)
    for _ in range(600):  # a loud mic but a very quiet output (a heavy chain): wants more gain
        lv.update(0.1, -40.0, mic_lufs=-2.0, static_db=0.0)
    cap = lv.cap(0.0)
    assert cap is not None and lv.auto_db <= cap + leveller.GUARD_SLACK_DB + 1e-6
    assert lv.state == "capped"
