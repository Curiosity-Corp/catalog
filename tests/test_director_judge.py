"""Local findings, the learned look per hour, and the pilot that eases towards it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from streamstack.director import daylight, judge, learning, personas, pilot, settings

PERSONA = personas.load_library().get("executive")
SUN = {"slot": "morning", "label": "Morning, strong daylight", "elevation": 45.0}


def picture(**over) -> dict:
    base = {
        "luma_mean": 120.0,
        "luma_p5": 30.0,
        "luma_p50": 120.0,
        "luma_p95": 190.0,
        "luma_p99": 215.0,
        "clipped_highlights": 0.002,
        "crushed_shadows": 0.01,
        "contrast_spread": 160.0,
        "warm_index": 0.13,
        "tint_index": 0.0,
        "saturation": 0.22,
        "sharpness": 120.0,
        "noise": 1.0,
        "centre_luma": 125.0,
        "edge_luma": 110.0,
        "skin": {"area": 0.07, "luma": 130.0, "redness": 1.4, "hue_deg": 18.0, "saturation": 0.35},
        "subject": {"box": [0.4, 0.2, 0.2, 0.3], "area": 0.07},
        "backlit": False,
    }
    return {**base, **over}


def ids(findings: list[dict], state: str | None = None) -> set[str]:
    return {f["id"] for f in findings if state is None or f["state"] == state}


def test_a_good_picture_raises_no_warnings() -> None:
    findings = judge.picture_findings(picture(), {"flicker": False, "motion_word": "still"}, PERSONA, SUN)
    assert ids(findings, "warn") == set()
    assert "face-exposure" in ids(findings, "ok")


def test_each_problem_gets_its_own_plain_finding() -> None:
    cases = {
        "face-exposure": picture(skin={"area": 0.07, "luma": 60.0, "redness": 1.4, "hue_deg": 18, "saturation": 0.3}),
        "clipping": picture(clipped_highlights=0.08),
        "backlight": picture(backlit=True, centre_luma=60.0, edge_luma=190.0),
        "focus": picture(sharpness=2.0),
        "noise": picture(noise=9.0),
    }
    for expected, pic in cases.items():
        found = judge.picture_findings(pic, {"flicker": False}, PERSONA, SUN)
        assert expected in ids(found, "warn"), expected
        assert all(f["text"] and f["area"] == "picture" for f in found)
    flicker = judge.picture_findings(picture(), {"flicker": True, "luma_swing": 0.09}, PERSONA, SUN)
    assert "flicker" in ids(flicker, "warn")


def test_white_balance_is_judged_against_the_persona_not_a_fixed_number() -> None:
    cool = judge.picture_findings(picture(warm_index=-0.1), {}, PERSONA, SUN)
    assert "white-balance" in ids(cool, "warn")
    daylight_persona = personas.load_library().get("daylight-natural")
    neutral = judge.picture_findings(picture(warm_index=0.0), {}, daylight_persona, SUN)
    assert "white-balance" not in ids(neutral, "warn")


def test_no_picture_and_no_audio_are_notes() -> None:
    assert ids(judge.picture_findings({}, {}, PERSONA, SUN)) == {"no-picture"}
    note = judge.voice_findings(None, PERSONA, "the microphone gave no audio")
    assert ids(note) == {"no-audio"} and "no audio" in note[0]["text"]


def voice(**over) -> dict:
    base = {
        "enough_speech": True,
        "speech_ratio": 0.6,
        "peak_db": -8.0,
        "speech_db": -20.0,
        "snr_db": 45.0,
        "noise_floor_db": -65.0,
        "lufs": -22.0,
        "rt60_s": 0.3,
        "f0_hz": 120.0,
        "scores": {"pitch": 0.2, "depth": 0.7, "clarity": 0.5, "sibilance": 0.3, "clean": 0.75},
    }
    return {**base, **over}


def test_voice_findings_compare_numbers_with_the_persona() -> None:
    assert ids(judge.voice_findings(voice(), PERSONA), "warn") == set()
    assert "clipping-voice" in ids(judge.voice_findings(voice(peak_db=-0.3), PERSONA), "warn")
    assert "noise-voice" in ids(judge.voice_findings(voice(snr_db=18.0), PERSONA), "warn")
    assert "room" in ids(judge.voice_findings(voice(rt60_s=1.1), PERSONA), "warn")
    thin = judge.voice_findings(voice(scores={"pitch": 0.2, "depth": 0.1, "clarity": 0.5, "sibilance": 0.3}), PERSONA)
    assert "depth-low" in ids(thin)
    harsh = judge.voice_findings(voice(scores={"pitch": 0.2, "depth": 0.7, "clarity": 0.5, "sibilance": 0.95}), PERSONA)
    assert "sibilance-high" in ids(harsh)
    assert "little-speech" in ids(judge.voice_findings(voice(enough_speech=False), PERSONA))


def test_preset_hint_only_names_presets_that_exist_and_differ() -> None:
    available = ["StreamStack Voice", "StreamStack Voice Warm", "StreamStack Voice Bright"]
    dull = [{"id": "clarity-low", "state": "warn"}]
    assert judge.preset_hint(dull, "StreamStack Voice", PERSONA, available) == "StreamStack Voice Bright"
    assert judge.preset_hint(dull, "StreamStack Voice Bright", PERSONA, available) is None
    assert judge.preset_hint(dull, "x", PERSONA, ["StreamStack Voice"]) is None
    assert (
        judge.preset_hint([{"id": "depth-low", "state": "warn"}], "x", PERSONA, available) == "StreamStack Voice Warm"
    )
    assert judge.preset_hint([], "x", PERSONA, available) is None


def test_burst_medians_ignore_one_odd_frame() -> None:
    frames = [picture(luma_mean=m) | {"histogram": [0.5, 0.5]} for m in (100.0, 102.0, 240.0)]
    aggregate = judge.aggregate_frames(frames)
    assert aggregate["luma_mean"] == 102.0 and aggregate["histogram"] == [0.5, 0.5]
    assert judge.aggregate_frames([]) == {}


# -- learning ----------------------------------------------------------------------------------------


def test_the_learned_look_moves_towards_what_the_person_keeps() -> None:
    assert learning.get("p", "executive", "dawn") is None
    learning.record("p", "executive", "dawn", {"warmth": 0.2, "contrast": 0.1})
    first = learning.get("p", "executive", "dawn")
    assert first["look"]["warmth"] == 0.2 and first["n"] == 1
    learning.record("p", "executive", "dawn", {"warmth": 0.0, "contrast": 0.1})
    kept = learning.get("p", "executive", "dawn")["look"]["warmth"]
    learning.record("p", "executive", "dawn", {"warmth": 0.0, "contrast": 0.1}, manual=True)
    manual = learning.get("p", "executive", "dawn")["look"]["warmth"]
    assert 0.0 < kept < 0.2 and 0.0 <= manual < kept  # a hand adjustment is weighted more than a kept look


def test_learning_is_kept_per_persona_per_hour_and_remembers_the_mic() -> None:
    learning.record("p", "executive", "dawn", {"warmth": 0.2}, mic=("mic", -3.0))
    learning.record("p", "executive", "night", {"warmth": 0.05})
    assert learning.get("p", "executive", "night")["look"]["warmth"] == 0.05
    assert learning.get("p", "bright-podcast", "dawn") is None
    assert learning.get("p", "executive", "dawn")["mic"] == {"source": "mic", "db": -3.0}
    learning.record("p", "executive", "dawn", {"warmth": 0.1})
    assert learning.get("p", "executive", "dawn")["mic"]["db"] == -3.0


def test_the_learning_file_is_bounded_and_private() -> None:
    for index in range(learning.KEEP_ENTRIES + 20):
        learning.record("p", "executive", f"slot{index}", {"warmth": 0.1})
    assert len(learning.load()) == learning.KEEP_ENTRIES
    assert learning._path().stat().st_mode & 0o077 == 0


# -- the pilot ------------------------------------------------------------------------------------------


NOW = datetime(2026, 10, 22, 7, 30, tzinfo=timezone(timedelta(hours=-4)))


def make_pilot() -> pilot.Pilot:
    settings.set_property("Home", 40.7128, -74.006)
    settings.set_persona("executive")
    p = pilot.Pilot({})
    p.refresh(NOW)
    return p


def test_the_pilot_reads_the_persona_the_slot_and_the_bands() -> None:
    p = make_pilot()
    assert p.persona["id"] == "executive" and p.slot == "dawn"
    assert p.frame_band() == tuple(float(v) for v in PERSONA["picture"]["frame_luma"])
    assert p.face_band()[0] < p.face_band()[1]
    assert p.sun_public(NOW)["clock"] == "07:30"


def test_easing_takes_small_steps_towards_the_persona_look_and_stops() -> None:
    p = make_pilot()
    look = {"contrast": 0.0, "saturation": 0.0, "warmth": 0.0, "lut_amount": 0.35}
    goal, source, _ = p.goal()
    assert source == "persona" and goal["warmth"] == pytest.approx(PERSONA["picture"]["look"]["warmth"])
    step, _, reason = p.ease(look)
    assert 0 < step["warmth"] <= 0.06 and "Executive" in reason
    for _ in range(40):
        step, _, _ = p.ease(look)
        look.update(step)
    assert p.ease(look)[0] == {}
    assert look["warmth"] == pytest.approx(goal["warmth"], abs=0.04)


def test_the_look_you_kept_beats_the_persona_look() -> None:
    p = make_pilot()
    learning.record(p.place, "executive", "dawn", {"warmth": 0.02, "contrast": 0.0, "saturation": 0.0}, mic=None)
    goal, source, reason = p.goal()
    assert source == "baseline" and goal["warmth"] == 0.02 and "kept" in reason


def test_a_setting_claude_moved_is_not_eased_back_until_the_persona_or_the_hour_changes() -> None:
    p = make_pilot()
    p.claude_set.add("warmth")
    step, _, _ = p.ease({"contrast": 0.0, "saturation": 0.0, "warmth": 0.4, "lut_amount": 0.3})
    assert "warmth" not in step
    settings.set_persona("bright-podcast")
    p.refresh(NOW)
    assert p.claude_set == set()


def test_slot_priors_nudge_the_persona_look_only_until_something_is_learned() -> None:
    p = make_pilot()
    goal = p.goal()[0]
    assert goal["gamma"] == pytest.approx(PERSONA["picture"]["look"]["gamma"] + daylight.SLOT_PRIORS["dawn"]["gamma"])
