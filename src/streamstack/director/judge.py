"""Local findings: what the measurements say about the picture and the voice, against a persona and the hour.

Everything here is deterministic and explainable. The findings go into the prompt as facts for Claude to weigh,
and onto the Auto page as plain sentences, so a change can always be traced to a number. Thresholds are
heuristics for steering a call, not broadcast standards; each finding names the measurement behind it.
"""

from __future__ import annotations

import statistics

NUMERIC_FRAME_KEYS = (
    "luma_mean", "luma_p5", "luma_p50", "luma_p95", "luma_p99", "clipped_highlights", "crushed_shadows",
    "contrast_spread", "warm_index", "tint_index", "saturation", "sharpness", "noise", "centre_luma", "edge_luma",
)  # fmt: skip
# Standard-deviation-free tolerances for "matches the persona".
WARM_TOLERANCE = 0.09
NOISE_WARN = 3.0
SOFT_WARN = 15.0
# A frame whose 99th-percentile luma is below this is a dark room, not a picture: colour, saturation, contrast and
# focus cannot be judged from it (measured on the Canon feed at night, docs/auto-mode.md, "Picture calibration").
DARK_P99 = 40.0


def aggregate_frames(frames: list[dict]) -> dict:
    """The median of every numeric measurement across the burst, plus the skin and subject of the middle frame."""
    if not frames:
        return {}
    out: dict = {}
    for key in NUMERIC_FRAME_KEYS:
        values = [f[key] for f in frames if isinstance(f.get(key), (int, float))]
        if values:
            out[key] = round(statistics.median(values), 3)
    histograms = [f["histogram"] for f in frames if isinstance(f.get("histogram"), list)]
    if histograms and len({len(h) for h in histograms}) == 1:
        out["histogram"] = [round(statistics.median(column), 4) for column in zip(*histograms, strict=True)]
    skins = [f["skin"] for f in frames if f.get("skin")]
    if skins:
        out["skin"] = {k: round(statistics.median(s[k] for s in skins), 3) for k in skins[0]}
    middle = frames[len(frames) // 2]
    out["subject"] = middle.get("subject")
    out["backlit"] = sum(1 for f in frames if f.get("backlit")) > len(frames) / 2
    out["cast"] = middle.get("cast")
    return out


def finding(fid: str, area: str, state: str, text: str, measure: str = "") -> dict:
    return {"id": fid, "area": area, "state": state, "text": text, "measure": measure}


def expected_warm_index(kelvin: float) -> float:
    """The grey-world warm index a scene at `kelvin` should read as (neutral at 6500 K)."""
    return round(max(-0.1, min(0.3, (6500.0 - kelvin) / 3000.0 * 0.2)), 3)


def picture_findings(picture: dict, burst: dict, persona: dict, sun: dict | None = None) -> list[dict]:
    """Findings about exposure, colour, sharpness and stability against the persona's picture targets."""
    target = persona["picture"]
    out: list[dict] = []
    if not picture:
        return [finding("no-picture", "picture", "note", "No picture could be measured this pass.")]
    skin = picture.get("skin")
    if skin:
        low, high = target["face_luma"]
        luma = skin["luma"]
        state = "ok" if low <= luma <= high else "warn"
        word = "in range" if state == "ok" else "too dark" if luma < low else "too bright"
        out.append(
            finding(
                "face-exposure",
                "picture",
                state,
                f"Face exposure is {word} for {persona['name']}.",
                f"face luma {luma:.0f}, target {low:.0f}-{high:.0f}",
            )
        )
    else:
        low, high = target["frame_luma"]
        luma = picture["luma_mean"]
        state = "ok" if low <= luma <= high else "warn"
        word = "in range" if state == "ok" else "too dark" if luma < low else "too bright"
        out.append(
            finding(
                "frame-exposure",
                "picture",
                state,
                f"Frame exposure is {word} (no face found to meter).",
                f"frame luma {luma:.0f}, target {low:.0f}-{high:.0f}",
            )
        )
    clipped = picture.get("clipped_highlights", 0.0)
    if clipped > target["max_clipped"]:
        out.append(
            finding(
                "clipping",
                "picture",
                "warn",
                "Highlights are clipping (windows, lamps or the forehead).",
                f"{clipped * 100:.1f}% of pixels at white, limit {target['max_clipped'] * 100:.1f}%",
            )
        )
    if picture.get("crushed_shadows", 0.0) > 0.15:
        out.append(
            finding(
                "crushed",
                "picture",
                "warn",
                "Shadows are crushed to black.",
                f"{picture['crushed_shadows'] * 100:.0f}% of pixels at black",
            )
        )
    if picture.get("backlit"):
        out.append(
            finding(
                "backlight",
                "picture",
                "warn",
                "The subject is darker than the edges of the frame (backlight).",
                f"centre {picture.get('centre_luma', 0):.0f} against edges {picture.get('edge_luma', 0):.0f}",
            )
        )
    dark = picture.get("luma_p99", 255.0) < DARK_P99
    if dark:
        out.append(
            finding(
                "too-dark-to-judge",
                "picture",
                "note",
                "The frame is almost black, so colour, contrast and focus are not judged until there is light.",
                f"brightest 1% of pixels at luma {picture.get('luma_p99', 0):.0f}, below {DARK_P99:.0f}",
            )
        )
    warm = None if dark else picture.get("warm_index")
    if warm is not None:
        wanted = expected_warm_index(target["warmth_k"])
        gap = warm - wanted
        if abs(gap) > WARM_TOLERANCE:
            side = "warmer" if gap > 0 else "cooler"
            out.append(
                finding(
                    "white-balance",
                    "picture",
                    "warn",
                    f"The picture reads {side} than the {persona['name']} look ({target['warmth_k']:.0f} K).",
                    f"warm index {warm:+.2f}, wanted about {wanted:+.2f}",
                )
            )
        else:
            out.append(
                finding(
                    "white-balance",
                    "picture",
                    "ok",
                    "White balance is close to the persona's colour temperature.",
                    f"warm index {warm:+.2f}",
                )
            )
    tint = None if dark else picture.get("tint_index")
    if tint is not None and abs(tint) > 0.06:
        out.append(
            finding(
                "tint",
                "picture",
                "warn",
                f"There is a {'green' if tint > 0 else 'magenta'} tint.",
                f"tint index {tint:+.2f}",
            )
        )
    saturation = None if dark else picture.get("saturation")
    if saturation is not None:
        low, high = target["saturation"]
        if not low <= saturation <= high:
            out.append(
                finding(
                    "saturation",
                    "picture",
                    "note",
                    f"Colour is {'muted' if saturation < low else 'strong'} for this persona.",
                    f"saturation {saturation:.2f}, target {low:.2f}-{high:.2f}",
                )
            )
    spread = None if dark else picture.get("contrast_spread")
    if spread is not None:
        low, high = target["contrast_spread"]
        if not low <= spread <= high:
            out.append(
                finding(
                    "contrast",
                    "picture",
                    "note",
                    f"Contrast is {'flat' if spread < low else 'harsh'} for this persona.",
                    f"p5-p95 spread {spread:.0f}, target {low:.0f}-{high:.0f}",
                )
            )
    if not dark and (picture.get("sharpness", SOFT_WARN + 1) < SOFT_WARN or burst.get("blurred_frames")):
        out.append(
            finding(
                "focus",
                "picture",
                "warn",
                "The picture looks soft or some frames are blurred.",
                f"sharpness {picture.get('sharpness', 0):.0f}, blurred frames {burst.get('blurred_frames') or 'none'}",
            )
        )
    if picture.get("noise", 0.0) > NOISE_WARN:
        out.append(
            finding(
                "noise",
                "picture",
                "warn",
                "The picture is grainy (low light or high ISO).",
                f"noise sigma {picture['noise']:.1f}",
            )
        )
    if burst.get("flicker"):
        out.append(
            finding(
                "flicker",
                "picture",
                "warn",
                "Brightness pulses between frames (flickering light or exposure hunting).",
                f"{burst['luma_swing'] * 100:.0f}% swing across the burst",
            )
        )
    subject = picture.get("subject")
    if subject:
        if subject.get("headroom", 0.0) > 0.3:
            out.append(
                finding(
                    "headroom",
                    "picture",
                    "note",
                    "There is a lot of space above the head.",
                    f"head top at {subject.get('headroom', 0.0) * 100:.0f}% of the frame (estimated)",
                )
            )
        if subject.get("off_centre", 0.0) > 0.25:
            out.append(
                finding(
                    "off-centre",
                    "picture",
                    "note",
                    "The speaker is well off to one side.",
                    f"{subject.get('off_centre', 0.0) * 100:.0f}% from centre (estimated)",
                )
            )
    if not dark and sun and sun.get("slot") == "night" and picture.get("cast") and "cool" in picture["cast"]:
        out.append(
            finding(
                "screen-glow",
                "picture",
                "note",
                "Cool light at night often means screen glow on the face.",
                picture["cast"],
            )
        )
    return out


def voice_findings(audio: dict | None, persona: dict, reason: str = "") -> list[dict]:
    """Findings about level, tone, noise and room against the persona's voice targets."""
    voice = persona["voice"]
    if not audio:
        return [finding("no-audio", "voice", "note", reason or "No voice measurement this pass.")]
    out: list[dict] = []
    if not audio.get("enough_speech"):
        out.append(
            finding(
                "little-speech",
                "voice",
                "note",
                "There was not enough speech in the clip to judge the tone.",
                f"speech {audio.get('speech_ratio', 0) * 100:.0f}% of the clip, SNR {audio.get('snr_db', 0):.0f} dB",
            )
        )
    peak = audio.get("peak_db")
    if peak is not None and peak > -1.0:
        out.append(finding("clipping-voice", "voice", "warn", "The microphone is clipping.", f"peak {peak:.1f} dBFS"))
    loudness = audio.get("lufs")
    if loudness is not None and audio.get("enough_speech"):
        low, high = voice["lufs"]
        if not low <= loudness <= high:
            out.append(
                finding(
                    "loudness",
                    "voice",
                    "note",
                    f"Voice is {'quiet' if loudness < low else 'loud'} for this persona.",
                    f"{loudness:.1f} LUFS, target {low:.0f} to {high:.0f}",
                )
            )
    if audio.get("snr_db") is not None and audio["snr_db"] < voice["min_snr_db"] and audio.get("enough_speech"):
        out.append(
            finding(
                "noise-voice",
                "voice",
                "warn",
                "The room is noisy compared with the voice.",
                (
                    f"SNR {audio['snr_db']:.0f} dB, wanted {voice['min_snr_db']:.0f}+; "
                    f"floor {audio.get('noise_floor_db', 0):.0f} dBFS"
                ),
            )
        )
    rt60 = audio.get("rt60_s")
    if rt60 is not None and rt60 > voice["max_rt60_s"]:
        out.append(
            finding(
                "room",
                "voice",
                "warn",
                f"The room sounds {audio.get('room', 'live')}.",
                f"estimated decay {rt60:.2f} s, limit {voice['max_rt60_s']:.2f} s",
            )
        )
    scores = audio.get("scores") or {}
    for key, thin, heavy, label in (("depth", "thin", "boomy", "body"), ("clarity", "dull", "harsh", "clarity")):
        value, span = scores.get(key), voice["targets"].get(key)
        if value is None or span is None or not audio.get("enough_speech"):
            continue
        if value < span[0]:
            out.append(
                finding(
                    f"{key}-low",
                    "voice",
                    "warn",
                    f"The voice sounds {thin} for this persona.",
                    f"{label} {value:.2f}, target {span[0]:.2f}-{span[1]:.2f}",
                )
            )
        elif value > span[1]:
            out.append(
                finding(
                    f"{key}-high",
                    "voice",
                    "warn",
                    f"The voice sounds {heavy} for this persona.",
                    f"{label} {value:.2f}, target {span[0]:.2f}-{span[1]:.2f}",
                )
            )
    sibilance, span = scores.get("sibilance"), voice["targets"].get("sibilance")
    if sibilance is not None and span is not None and sibilance > span[1] and audio.get("enough_speech"):
        out.append(
            finding(
                "sibilance-high",
                "voice",
                "warn",
                "S sounds are harsh.",
                f"sibilance {sibilance:.2f}, limit {span[1]:.2f}",
            )
        )
    if not any(f["state"] == "warn" for f in out) and audio.get("enough_speech"):
        out.append(finding("voice-ok", "voice", "ok", "The voice is within the persona's targets."))
    return out


def preset_hint(findings: list[dict], current: str | None, persona: dict, available: list[str]) -> str | None:
    """A voice preset the findings point to, when it exists and differs from the current one."""
    ids = {f["id"] for f in findings}
    wanted = None
    if "clarity-low" in ids or "depth-high" in ids:
        wanted = "StreamStack Voice Bright"
    elif "depth-low" in ids and "clarity-high" not in ids:
        wanted = "StreamStack Voice Warm"
    if wanted and wanted in available and wanted != current:
        return wanted
    return None
