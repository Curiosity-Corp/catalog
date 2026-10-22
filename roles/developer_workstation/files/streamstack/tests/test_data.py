"""Shipped presets, OBS templates and icons."""

from __future__ import annotations

import json
import re

from streamstack.config import DEFAULTS, data_dir

SHARE = data_dir()

# Enum labels Easy Effects 8 accepts in presets (src/contents/kcfg/*.kcfg).
# The limiter lists are copied from src/contents/kcfg/easyeffects_db_limiter.kcfg (modeLabels, oversamplingLabels,
# ditheringLabels): https://github.com/wwmm/easyeffects/blob/master/src/contents/kcfg/easyeffects_db_limiter.kcfg
# An unknown label is silently ignored on load, so a typo would ship a preset
# that quietly keeps the previous setting.
LIMITER_KINDS = ("Thin", "Wide", "Tail", "Duck")
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
    ("limiter", "mode"): {f"{shape} {kind}" for shape in ("Herm", "Exp", "Line") for kind in LIMITER_KINDS},
    ("limiter", "oversampling"): {
        "None",
        *(f"{scope} x{n}/{bits} bit" for scope in ("Half", "Full") for n in (2, 3, 4, 6, 8) for bits in (16, 24)),
        "True Peak/16 bit",
        "True Peak/24 bit",
    },
    ("limiter", "dithering"): {"None", "7bit", "8bit", "11bit", "12bit", "15bit", "16bit", "23bit", "24bit"},
}
# The Deep noise remover (DeepFilterNet) keys and ranges, from easyeffects_db_deepfilternet.kcfg at tag v8.1.2
# (https://github.com/wwmm/easyeffects/blob/v8.1.2/src/contents/kcfg/easyeffects_db_deepfilternet.kcfg); the kebab-case
# names are the ones the 8.1.2 binary carries. A wrong key is silently ignored on load, a value out of range is clamped.
EE_DEEPFILTERNET_KEYS = {
    "bypass": bool,
    "input-gain": (-36.0, 36.0),
    "output-gain": (-36.0, 36.0),
    "attenuation-limit": (0.0, 100.0),
    "min-processing-threshold": (-15.0, 35.0),
    "max-erb-processing-threshold": (-15.0, 35.0),
    "max-df-processing-threshold": (-15.0, 35.0),
    "min-processing-buffer": (0, 10),
    "post-filter-beta": (0.0, 0.05),
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
        assert order[-1] == "limiter#0"


def test_limiter_label_sets_match_the_kcfg_counts() -> None:
    assert len(EE_LABELS[("limiter", "mode")]) == 12
    assert len(EE_LABELS[("limiter", "oversampling")]) == 23
    assert len(EE_LABELS[("limiter", "dithering")]) == 9


def test_every_limiter_oversamples_to_a_true_peak_ceiling() -> None:
    """The limiter is the last stage, so its threshold is the ceiling of the voice that leaves the chain."""
    presets = sorted((SHARE / "easyeffects/input").glob("*.json"))
    assert presets
    for path in presets:
        limiter = json.loads(path.read_text())["input"]["limiter#0"]
        assert limiter["oversampling"] == "True Peak/24 bit", path.name
        assert limiter["threshold"] == -1.0, path.name  # -1 dBTP, the usual streaming headroom
        assert limiter["output-gain"] <= 0, path.name  # make-up gain after the limiter would break the ceiling


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
    assert "OutputCX=@WIDTH@" in ini and "FPSNum=@FPS_NUM@" in ini and "FPSDen=@FPS_DEN@" in ini


def test_auto_mode_filters_exist_on_the_camera_sources() -> None:
    from streamstack.obs import COLOR_FILTER, LUT_FILTER, SHARPEN_FILTER

    text = (SHARE / "obs/scenes.json").read_text()
    for name in (COLOR_FILTER, LUT_FILTER, SHARPEN_FILTER):
        assert f'"{name}"' in text, name


def test_every_panel_icon_ships_as_svg_and_png() -> None:
    for state in ("off", "raw", "live", "warn"):
        for suffix in ("svg", "png"):
            assert (SHARE / f"icons/streamstack-{state}.{suffix}").is_file()


def test_page_stylesheets_are_scoped_to_their_page() -> None:
    # Page stylesheets stay loaded after navigation, so an unscoped rule leaks into every later page
    # (audio.css once stacked the Live page's voice presets into one column and squashed the mic strip).
    pages = SHARE / "console" / "pages"
    routes = re.findall(r"id: '([a-z]+)'", (pages / "index.js").read_text())
    assert routes
    for route in routes:
        css = (pages / f"{route}.css").read_text()
        body = re.sub(r"/\*.*?\*/", "", css, flags=re.S).strip()
        opener = f'@scope ([data-page="{route}"]) {{'
        assert body.startswith(opener), f"{route}.css must open with {opener}"
        depth, end = 0, None
        for i, ch in enumerate(body):
            depth += {"{": 1, "}": -1}.get(ch, 0)
            if depth == 0 and ch == "}":
                end = i
                break
        assert end is not None, f"{route}.css: unbalanced braces"
        # after the scope block only rules aimed outside the page root may follow
        for rule in re.findall(r"^\S[^{]*", body[end + 1 :], flags=re.M):
            assert rule.startswith(("@keyframes", "@media")), f"{route}.css: unscoped rule {rule.strip()!r}"
            if rule.startswith("@media"):
                assert "#main:has(" in body[end + 1 :], f"{route}.css: unscoped @media rule"


def test_every_shipped_persona_is_a_valid_document_named_after_its_id() -> None:
    import json

    from streamstack.director import personas

    files = sorted(personas.shipped_dir().glob("*.json"))
    assert len(files) >= 4
    for path in files:
        saved = personas.validate(json.loads(path.read_text()))
        assert saved["id"] == path.stem, path.name
        assert len(saved["mood"]) >= 3 and saved["voice"]["preset"]


def test_a_page_template_never_reuses_a_data_ref() -> None:
    # Pages collect elements into one map by data-ref, so a second element with the same ref silently takes the
    # first one's event handlers (the Auto page's "Ask Claude now" button once went dead this way).
    import collections
    import re

    for path in sorted((SHARE / "console" / "pages").glob("*.js")):
        refs = collections.Counter(re.findall(r'(?<!\[)data-ref="([A-Za-z0-9_-]+)"', path.read_text()))
        assert [r for r, n in refs.items() if n > 1] == [], path.name


def test_background_blur_filter_ships_disabled_on_both_cameras_with_the_low_latency_settings() -> None:
    from streamstack import blur

    text = (SHARE / "obs/scenes.json").read_text()
    for token in ("@WIDTH@", "@HEIGHT@", "@HALF_WIDTH@", "@HALF_HEIGHT@", "@QUARTER_HEIGHT@"):
        text = text.replace(token, "1")
    doc = json.loads(text)
    sources = {s["name"]: s for s in doc["sources"]}
    for camera in ("Canon EOS 50D", "Cam Link 4K"):
        found = [f for f in sources[camera]["filters"] if f["name"] == blur.FILTER_NAME]
        assert len(found) == 1, camera
        assert found[0]["id"] == blur.FILTER_KIND
        assert found[0]["enabled"] is False  # opt-in: costs CPU, so never on by default
        assert found[0]["settings"] == blur.LOW_LATENCY
        assert sources[camera]["filters"][-1] is found[0]  # after the colour chain it sits on the graded picture


def test_blur_stylesheet_is_scoped_to_the_pages_that_mount_it() -> None:
    css = (SHARE / "console" / "pages" / "blur.css").read_text()
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S).strip()
    assert body.startswith('@scope ([data-page="camera"], [data-page="live"]) {')
    assert body.endswith("}")


def test_deepfilternet_preset_uses_valid_deep_noise_remover_keys() -> None:
    pipeline = json.loads((SHARE / "easyeffects/input/StreamStack Voice DeepFilterNet.json").read_text())["input"]
    settings = pipeline["deepfilternet#0"]
    assert set(settings) == set(EE_DEEPFILTERNET_KEYS)
    for key, rule in EE_DEEPFILTERNET_KEYS.items():
        if rule is bool:
            assert isinstance(settings[key], bool), key
        else:
            low, high = rule
            assert not isinstance(settings[key], bool) and low <= settings[key] <= high, key
    assert isinstance(settings["min-processing-buffer"], int)
    assert not any(instance.startswith("rnnoise#") for instance in pipeline), "one noise removal engine per preset"


def test_deepfilternet_preset_is_the_default_voice_with_only_the_denoiser_swapped() -> None:
    base = json.loads((SHARE / "easyeffects/input/StreamStack Voice.json").read_text())["input"]
    deep = json.loads((SHARE / "easyeffects/input/StreamStack Voice DeepFilterNet.json").read_text())["input"]
    assert deep["plugins_order"] == ["deepfilternet#0" if i == "rnnoise#0" else i for i in base["plugins_order"]]
    assert {k: v for k, v in deep.items() if k not in ("deepfilternet#0", "plugins_order")} == {
        k: v for k, v in base.items() if k not in ("rnnoise#0", "plugins_order")
    }


def test_rnnoise_stays_the_default_denoiser() -> None:
    default = DEFAULTS["STREAMSTACK_VOICE_PRESET"]
    pipeline = json.loads((SHARE / "easyeffects/input" / f"{default}.json").read_text())["input"]
    assert "rnnoise#0" in pipeline and "deepfilternet#0" not in pipeline
    others = [p for p in (SHARE / "easyeffects/input").glob("*.json") if "DeepFilterNet" not in p.stem]
    assert others
    for path in others:
        assert "rnnoise#0" in json.loads(path.read_text())["input"], path.name
