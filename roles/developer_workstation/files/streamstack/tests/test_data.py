"""Shipped presets, OBS templates and icons."""

from __future__ import annotations

import json
import re

from streamstack.config import DEFAULTS, data_dir

SHARE = data_dir()

# Enum labels Easy Effects 8 accepts in presets (src/contents/kcfg/*.kcfg).
# An unknown label is silently ignored on load, so a typo would ship a preset
# that quietly keeps the previous setting.
EE_LABELS = {
    ("filter", "type"): {"Low-pass", "High-pass", "Low-shelf", "High-shelf", "Bell", "Band-pass", "Notch"},
    ("filter", "mode"): {"RLC (BT)", "RLC (MT)", "BWC (BT)", "BWC (MT)", "LRX (BT)", "LRX (MT)", "APO (DR)"},
    ("filter", "equal-mode"): {"IIR", "FIR", "FFT", "SPM"},
    ("filter", "slope"): {"x1", "x2", "x3", "x4", "x6", "x8", "x12", "x16"},
    ("gate", "mode"): {"Downward", "Upward", "Boosting"},
    ("compressor", "mode"): {"Downward", "Upward", "Boosting"},
    ("equalizer", "mode"): {"IIR", "FIR", "FFT", "SPM"},
    ("deesser", "detection"): {"RMS", "Peak"},
    ("deesser", "mode"): {"Wide", "Split"},
    ("limiter", "mode"): {"Herm Thin", "Herm Wide", "Herm Tail", "Herm Duck", "Exp Thin", "Exp Wide"},
    ("limiter", "oversampling"): {"None"},
    ("limiter", "dithering"): {"None"},
}
EE_BAND_TYPES = {"Off", "Bell", "Hi-pass", "Hi-shelf", "Lo-pass", "Lo-shelf", "Notch"}
EE_BAND_MODES = EE_LABELS[("filter", "mode")]


def test_voice_presets_are_valid_easy_effects_presets() -> None:
    presets = sorted((SHARE / "easyeffects/input").glob("*.json"))
    assert DEFAULTS["STREAMSTACK_VOICE_PRESET"] in [p.stem for p in presets]
    for path in presets:
        pipeline = json.loads(path.read_text())["input"]
        order = pipeline["plugins_order"]
        assert order and len(order) == len(set(order)), path.name
        for instance in order:
            assert re.fullmatch(r"[a-z_]+#\d+", instance), instance
            assert instance in pipeline, f"{path.name}: {instance} has no settings"
            base = instance.split("#")[0]
            for (plugin, key), labels in EE_LABELS.items():
                if plugin == base and key in pipeline[instance]:
                    assert pipeline[instance][key] in labels, f"{path.name}: {instance}.{key}"
        eq = pipeline["equalizer#0"]
        for channel in ("left", "right"):
            assert len(eq[channel]) == eq["num-bands"]
            for band in eq[channel].values():
                assert band["type"] in EE_BAND_TYPES and band["mode"] in EE_BAND_MODES
        # Bass and treble are the shelving bands, so they are always adjustable.
        types = [b["type"] for b in eq["left"].values()]
        assert "Lo-shelf" in types and "Hi-shelf" in types
        assert "pitch#0" in order and -12 <= pipeline["pitch#0"]["semitones"] <= 12
        assert order[-1] == "limiter#0" and pipeline["limiter#0"]["threshold"] < 0


def test_obs_scene_collection_is_consistent_once_rendered() -> None:
    text = (SHARE / "obs/scenes.json").read_text()
    for token in ("@CAMERA_DEVICE@", "@CAMLINK_DEVICE@", "@WIDTH@", "@HEIGHT@"):
        assert token in text
    numeric = {
        "@WIDTH@": "1920",
        "@HEIGHT@": "1080",
        "@HALF_WIDTH@": "960",
        "@HALF_HEIGHT@": "540",
        "@QUARTER_HEIGHT@": "270",
    }
    for token, value in numeric.items():
        text = text.replace(token, value)
    doc = json.loads(text)
    sources = {s["name"]: s for s in doc["sources"]}
    assert sources["Canon EOS 50D"]["settings"]["device_id"] == "@CAMERA_DEVICE@"
    assert sources["Voice (Easy Effects)"]["settings"]["device_id"] == "easyeffects_source"
    for scene in doc["scene_order"]:
        for item in sources[scene["name"]]["settings"]["items"]:
            assert item["name"] in sources
    ini = (SHARE / "obs/basic.ini").read_text()
    assert "OutputCX=@WIDTH@" in ini and "FPSInt=@FPS@" in ini


def test_auto_mode_filters_exist_on_the_camera_sources() -> None:
    from streamstack.obs import COLOR_FILTER, LUT_FILTER, SHARPEN_FILTER

    text = (SHARE / "obs/scenes.json").read_text()
    for name in (COLOR_FILTER, LUT_FILTER, SHARPEN_FILTER):
        assert f'"{name}"' in text, name


def test_every_panel_icon_ships_as_svg_and_png() -> None:
    for state in ("off", "raw", "live", "warn"):
        for suffix in ("svg", "png"):
            assert (SHARE / f"icons/streamstack-{state}.{suffix}").is_file()
