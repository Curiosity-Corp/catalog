"""Shipped presets and icons."""

from __future__ import annotations

import json
import re

from streamstack.config import DEFAULTS, data_dir

SHARE = data_dir()


def test_voice_presets_are_valid_chain_presets() -> None:
    from streamstack.audiochain import presets, stages

    files = sorted((SHARE / "audio/presets").glob("*.json"))
    assert DEFAULTS["STREAMSTACK_VOICE_PRESET"] in [p.stem for p in files]
    assert {p.stem for p in files} == {
        "StreamStack Voice",
        "StreamStack Voice Warm",
        "StreamStack Voice Bright",
        "StreamStack Voice Deep",
        "StreamStack Voice Minimal",
        "StreamStack Voice DeepFilterNet",
    }
    for path in files:
        doc = json.loads(path.read_text())
        preset = presets.parse(doc, user=False, source=path.name)  # raises on an unknown key or an out of range value
        assert preset.name == path.stem and preset.blurb, path.name
        resolved = preset.resolved()
        # the limiter is the last stage and stays on: nothing may leave the chain above the true peak ceiling
        assert resolved["limiter_on"] is True, path.name
        assert (
            resolved["limiter_ceiling_db"] <= stages.TRUE_PEAK_CEILING_DB + 0.01
            or resolved["limiter_ceiling_db"] <= -1.0
        ), path.name
        assert -12 <= resolved["bass_db"] <= 12, path.name


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


def test_blur_stylesheet_is_scoped_to_the_pages_that_mount_it() -> None:
    css = (SHARE / "console" / "pages" / "blur.css").read_text()
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S).strip()
    assert body.startswith('@scope ([data-page="camera"], [data-page="live"]) {')
    assert body.endswith("}")


def test_deepfilternet_preset_requires_its_engine_and_the_rest_are_flexible() -> None:
    from streamstack.audiochain import presets

    shipped = presets.load_all(SHARE / "audio/presets", SHARE / "audio/none")
    assert shipped["StreamStack Voice DeepFilterNet"].strict_engine is True
    assert shipped["StreamStack Voice DeepFilterNet"].engine == "deepfilternet"
    assert not any(p.strict_engine for n, p in shipped.items() if "DeepFilterNet" not in n)


def test_minimal_preset_leaves_only_high_pass_compression_and_the_limiter() -> None:
    from streamstack.audiochain import presets

    minimal = presets.load_all(SHARE / "audio/presets", SHARE / "audio/none")["StreamStack Voice Minimal"].resolved()
    assert [minimal[f"{s}_on"] for s in ("gate", "denoise", "deess")] == [False, False, False]
    assert minimal["hp_on"] and minimal["comp_on"] and minimal["limiter_on"]
