"""The question the director asks Claude, and how its answer is read back.

The prompt carries the persona (what "good" means tonight), the time of day at the property, what was measured
locally (picture statistics for every still in the burst, voice features computed on this machine) and what the
local findings concluded. Claude sees the contact sheet of stills, never audio: the voice reaches it only as the
numbers in this text. Nothing here talks to the network; `auto.ask_claude` sends the result.
"""

from __future__ import annotations

import json
import math

from streamstack.director import limits

MOODS = ("daylight", "artificial", "mixed", "unclear")
TEXT_MAX = 200

PROMPT = """You are the cinematographer and audio engineer for a live video call or stream.

THE LOOK TO ACHIEVE
Persona "{persona_name}" ({persona_tagline}). {persona_description}
Mood words: {mood}. Avoid: {avoid}.
Target picture: face luma {face_luma} (8-bit Y), frame luma {frame_luma}, white balance about {warmth_k} K,
saturation {saturation}, contrast spread {contrast_spread}, at most {max_clipped} of pixels clipped.
Target voice: {voice_targets}.

THE MOMENT
{when}. {sun_note}
{learned}

WHAT YOU SEE
The image {image} is a contact sheet of {frames} stills of the graded camera ({source}), taken about
{spacing} s apart, read left to right then top to bottom. Use all of them: a good frame is steady, sharp and the
subject looks natural in it, so judge flicker, movement and blur across the burst, not one frame.
Say whether the room is lit by daylight, artificial light or a mix, and whether the picture reads as the persona's
mood.

WHAT WAS MEASURED HERE (computed locally from the stills; medians across the burst)
Picture: {picture}
Burst: {burst}
Findings from the local checks (facts to weigh, not orders):
{findings}

THE VOICE (a short clip analysed on this machine; the audio itself is never sent, only these numbers)
{voice}

WHAT YOU MAY CHANGE
Current "Lighting & Color" settings (OBS): {look}
Allowed ranges: {limits}
Largest change per pass: {steps}
Voice presets available (current: {preset}): {presets}
{extra}
Prefer small, explained changes. Leave out anything that already suits the persona. A local loop already handles
exposure with brightness and gamma every few seconds; change those only for a clear problem. Respect that the
operator may have adjusted things by hand: values listed under "held by the operator" must not be proposed.
Suggest another voice preset only when the voice numbers clearly call for it.

If a person is in the frame, set subject.box to their face as [x, y, width, height] fractions of the frame
(0-1 from the top left of a single still, not the sheet) and subject.target_luma to the average 8-bit luma
their face should have for this persona and their skin tone. Leave subject out when no face is visible.

Video values are absolute targets, not deltas. confidence is 0-1. notes is one short sentence for the operator.
reasons maps each setting you change (for example "warmth" or "voice_preset") to one plain sentence saying why.
assessment.mood_match is 0-1 for how well the picture and voice match the persona now, assessment.observed_mood
is up to five words, assessment.lighting is {moods}, and assessment.issues lists what the operator should fix by
hand (light, framing, background, microphone position)."""

ASSESSMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "mood_match": {"type": "number", "minimum": 0, "maximum": 1},
        "observed_mood": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "lighting": {"type": "string", "enum": list(MOODS)},
        "issues": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
    },
    "additionalProperties": False,
}
REASONS_SCHEMA = {"type": "object", "additionalProperties": {"type": "string"}}


def schema(base: dict, extra: dict | None = None) -> dict:
    """`base` (the proposal schema) with the assessment, the reasons and any registered actuator's section."""
    merged = {**base, "properties": {**base["properties"], "assessment": ASSESSMENT_SCHEMA, "reasons": REASONS_SCHEMA}}
    if extra:
        merged["properties"].update(extra)
    return merged


def _range(values: object, unit: str = "") -> str:
    if isinstance(values, (list, tuple)) and len(values) == 2:
        return f"{values[0]:g} to {values[1]:g}{unit}"
    return "any"


def _numbers(data: dict | None) -> str:
    """Compact JSON of a measurement dict with its floats rounded and nested lists kept short."""

    def tidy(value: object) -> object:
        if isinstance(value, float):
            return round(value, 3) if math.isfinite(value) else None
        if isinstance(value, dict):
            return {k: tidy(v) for k, v in value.items()}
        if isinstance(value, list):
            return [tidy(v) for v in value[:16]]
        return value

    return json.dumps(tidy(data or {}), separators=(",", ":"))


def voice_summary(audio: dict | None, reason: str) -> str:
    if not audio:
        return reason or "not measured this pass"
    keep = (
        "seconds",
        "enough_speech",
        "speech_ratio",
        "peak_db",
        "speech_db",
        "noise_floor_db",
        "snr_db",
        "lufs",
        "f0_hz",
        "f0_range_st",
        "pitch_band",
        "centroid_hz",
        "tilt_db_oct",
        "clarity_db",
        "sibilance_db",
        "rt60_s",
        "room",
        "scores",
    )
    return _numbers({k: audio[k] for k in keep if k in audio})


def findings_text(findings: list[dict]) -> str:
    if not findings:
        return "- none"
    return "\n".join(
        f"- [{f['state']}] {f['text']}" + (f" ({f['measure']})" if f.get("measure") else "") for f in findings
    )


def build(
    *,
    image: object,
    source: str,
    frames: int,
    spacing: float,
    persona: dict,
    sun: dict,
    picture: dict,
    burst: dict,
    findings: list[dict],
    audio: dict | None,
    audio_reason: str,
    look: dict,
    presets: list[str],
    preset: str | None,
    learned: dict | None,
    held: list[str],
    extra_actuators: list[str] | None = None,
) -> str:
    """The full prompt for one pass. Every value is data; nothing is evaluated."""
    pic, voice = persona["picture"], persona["voice"]
    targets = ", ".join(f"{name} {_range(span)}" for name, span in voice["targets"].items())
    voice_targets = (
        f"{targets}; speech peaks {_range(voice['speech_peak_db'], ' dBFS')};"
        f" loudness {_range(voice['lufs'], ' LUFS')};"
        f" SNR at least {voice['min_snr_db']:g} dB; room reverb under {voice['max_rt60_s']:g} s"
    )
    if learned:
        learned_text = (
            f"At this hour ({sun['slot_label'].lower()}) the operator last kept this look: "
            f"{_numbers(learned.get('look'))} (seen {learned.get('n', 1)} times)."
        )
    else:
        learned_text = "No look has been learned for this hour yet."
    if held:
        learned_text += f" Held by the operator (changed by hand recently): {', '.join(held)}."
    return PROMPT.format(
        persona_name=persona["name"],
        persona_tagline=persona["tagline"],
        persona_description=persona["description"],
        mood=", ".join(persona["mood"]),
        avoid=", ".join(persona["avoid"]) or "nothing in particular",
        face_luma=_range(pic["face_luma"]),
        frame_luma=_range(pic["frame_luma"]),
        warmth_k=pic["warmth_k"],
        saturation=_range(pic["saturation"]),
        contrast_spread=_range(pic["contrast_spread"]),
        max_clipped=f"{pic['max_clipped'] * 100:g}%",
        voice_targets=voice_targets,
        when=f"{sun['label']} (sun {sun['elevation']:+.0f} degrees, local time {sun.get('clock', 'unknown')})",
        sun_note=sun["note"],
        learned=learned_text,
        image=image,
        frames=frames,
        source=source,
        spacing=f"{spacing:g}",
        picture=_numbers(picture),
        burst=_numbers(burst),
        findings=findings_text(findings),
        voice=voice_summary(audio, audio_reason),
        look=json.dumps(look),
        limits=json.dumps(limits.LIMITS),
        steps=json.dumps(limits.AI_STEP),
        preset=preset or "unknown",
        presets=json.dumps(presets),
        extra="\n".join(extra_actuators or []),
        moods=", ".join(MOODS),
    )


def _text(value: object, longest: int = TEXT_MAX) -> str:
    return " ".join(value.split())[:longest] if isinstance(value, str) else ""


def reasons_from(proposal: dict) -> dict[str, str]:
    """Claude's per-setting reasons, trimmed; anything that is not text is ignored."""
    raw = proposal.get("reasons")
    if not isinstance(raw, dict):
        return {}
    return {str(k)[:40]: text for k, v in raw.items() if (text := _text(v))}


def assessment_from(proposal: dict) -> dict | None:
    """Claude's read of the mood and lighting, validated; None when it gave none."""
    raw = proposal.get("assessment")
    if not isinstance(raw, dict):
        return None
    match = raw.get("mood_match")
    moods = raw.get("observed_mood")
    issues = raw.get("issues")
    out: dict = {
        "mood_match": round(max(0.0, min(1.0, float(match))), 2)
        if isinstance(match, (int, float)) and not isinstance(match, bool) and math.isfinite(match)
        else None,
        "observed_mood": [t for m in (moods if isinstance(moods, list) else [])[:5] if (t := _text(m, 40))],
        "lighting": raw.get("lighting") if raw.get("lighting") in MOODS else "unclear",
        "issues": [t for i in (issues if isinstance(issues, list) else [])[:6] if (t := _text(i))],
    }
    return out
