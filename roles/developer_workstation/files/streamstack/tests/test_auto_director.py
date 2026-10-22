"""The director inside auto mode: observation, the prompt sent to Claude, privacy, overrides, easing, status."""

from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest
from test_auto import FakeOBS, journal

from streamstack import auto, config, system
from streamstack.director import audiofeatures, imagefeatures, personas, prompt, settings, status
from streamstack.obs import COLOR_FILTER

STATS = {"YAVG": 118.0, "YHIGH": 230.0, "YMIN": 16, "YMAX": 235}
FRAME = {
    "luma_mean": 118.0,
    "luma_p5": 30.0,
    "luma_p50": 116.0,
    "luma_p95": 190.0,
    "luma_p99": 215.0,
    "clipped_highlights": 0.002,
    "crushed_shadows": 0.01,
    "contrast_spread": 160.0,
    "warm_index": 0.02,
    "tint_index": 0.0,
    "saturation": 0.2,
    "sharpness": 100.0,
    "noise": 1.0,
    "centre_luma": 120.0,
    "edge_luma": 110.0,
    "histogram": [0.0625] * 16,
    "skin": {"area": 0.07, "luma": 125.0, "redness": 1.4, "hue_deg": 18.0, "saturation": 0.35},
    "subject": {"box": [0.4, 0.2, 0.2, 0.3], "area": 0.07, "headroom": 0.2, "off_centre": 0.0},
    "backlit": False,
    "cast": "neutral",
}
FEATURES = {
    "seconds": 6.0,
    "enough_speech": True,
    "speech_ratio": 0.6,
    "peak_db": -8.0,
    "speech_db": -20.0,
    "noise_floor_db": -65.0,
    "snr_db": 45.0,
    "lufs": -22.0,
    "rt60_s": 0.3,
    "room": "dry",
    "f0_hz": 120.0,
    "pitch_band": "medium-low",
    "scores": {"pitch": 0.2, "depth": 0.7, "clarity": 0.5, "sibilance": 0.3, "clean": 0.75},
}
REPLY = {
    "video": {"warmth": 0.1},
    "confidence": 0.9,
    "notes": "Warmer.",
    "reasons": {"warmth": "Skin looked cool."},
    "assessment": {"mood_match": 0.8, "observed_mood": ["calm"], "lighting": "mixed", "issues": ["Window glare"]},
}


@pytest.fixture(autouse=True)
def fast(monkeypatch) -> None:
    monkeypatch.setattr(auto.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(auto, "notify", lambda *a, **k: None)
    monkeypatch.setattr(auto, "picture_stats", lambda frame, box=None: dict(STATS))
    monkeypatch.setattr(system, "ee_presets", lambda: ["Voice", "Broadcast"])
    monkeypatch.setattr(system, "easyeffects_preset", lambda: "Voice")
    monkeypatch.setattr(system, "run", lambda argv, timeout=10: None)


def make(
    monkeypatch, replies, backend: str = "claude-cli", mic: str = "", **cfg
) -> tuple[auto.Auto, FakeOBS, list[dict]]:
    sent: list[dict] = []

    def fake_claude(settings_, image, context):
        sent.append(
            {"image": image, "context": context, "bytes": Path(image).read_bytes() if Path(image).exists() else None}
        )
        return next(replies)

    monkeypatch.setattr(auto, "ask_claude", fake_claude)
    monkeypatch.setattr(
        imagefeatures,
        "analyze_stills",
        lambda paths: {
            "frames": [dict(FRAME) for _ in paths],
            "burst": {"flicker": False, "frames": len(paths)},
            "problems": [],
        },
    )

    def sheet(paths, out, timeout=30.0):
        out.write_bytes(b"SHEET" + b"x" * 2000)
        return out

    monkeypatch.setattr(imagefeatures, "contact_sheet", sheet)
    settings_ = {
        **config.DEFAULTS,
        "STREAMSTACK_AUTO_AUDIO": "false",
        "STREAMSTACK_AUTO_BACKEND": backend,
        "STREAMSTACK_AUTO_BURST": "4",
        **cfg,
    }
    obs = FakeOBS({"brightness": 0.0, "contrast": 0.0, "gamma": 0.0, "saturation": 0.0})
    a = auto.Auto(settings_)
    a.obs = obs
    if mic:
        a.mic, a.mic_start_db = mic, 0.0
    return a, obs, sent


def test_claude_is_shown_one_contact_sheet_and_a_prompt_about_the_persona_and_the_hour(monkeypatch) -> None:
    settings.set_persona("executive")
    a, _obs, sent = make(monkeypatch, iter([REPLY]))
    a.ai_tick(force=True)
    (call,) = sent
    assert call["bytes"].startswith(b"SHEET")  # the composed sheet, not a single frame
    text = call["context"]["prompt"]
    for expected in (
        "Executive",
        "contact sheet of 4 stills",
        "warm",
        a.pilot.sun.label,
        "Findings from the local checks",
    ):
        assert expected in text
    schema = call["context"]["schema"]
    assert {"assessment", "reasons", "video", "audio", "subject"} <= set(schema["properties"])
    assert auto.PROPOSAL_SCHEMA["properties"].keys() == {
        "video",
        "audio",
        "subject",
        "confidence",
        "notes",
    }  # untouched


def test_the_persona_changes_what_claude_is_asked(monkeypatch) -> None:
    prompts = {}
    for persona in ("executive", "late-night-calm"):
        settings.set_persona(persona)
        a, _obs, sent = make(monkeypatch, iter([REPLY]))
        a.ai_tick(force=True)
        prompts[persona] = sent[0]["context"]["prompt"]
    assert "Executive" in prompts["executive"] and "Executive" not in prompts["late-night-calm"]
    assert personas.load_library().get("late-night-calm")["name"] in prompts["late-night-calm"]


def test_audio_never_leaves_the_machine_only_its_features_do(monkeypatch, tmp_path) -> None:
    a, _obs, sent = make(monkeypatch, iter([REPLY]), mic="mic")
    clips: list[Path] = []

    def record(source, seconds, path, **kw):
        with wave.open(path, "wb") as clip:
            clip.setnchannels(1)
            clip.setsampwidth(2)
            clip.setframerate(24000)
            clip.writeframes(b"\x00\x00" * 24000)
        clips.append(Path(path))
        return True

    monkeypatch.setattr(system, "record", record)
    monkeypatch.setattr(audiofeatures, "analyze_wav", lambda path: dict(FEATURES))
    a.ai_tick(force=True)
    assert clips and not any(c.exists() for c in clips)  # the clip is deleted as soon as it is analysed
    context = sent[0]["context"]
    assert str(clips[0]) not in context["prompt"] and ".wav" not in json.dumps(context)
    assert "f0_hz" in context["prompt"] and "never sent" in context["prompt"]
    assert sent[0]["image"].suffix == ".jpg"  # the only file Claude is pointed at is a picture


def test_a_pass_without_a_microphone_still_works_and_says_so(monkeypatch) -> None:
    a, _obs, sent = make(monkeypatch, iter([REPLY]))
    a.ai_tick(force=True)
    assert "no microphone is being watched" in sent[0]["context"]["prompt"]
    assert status.read()["audio"] == {"features": None, "reason": "no microphone is being watched"}


def test_the_pass_is_written_for_the_console_and_each_change_has_a_reason(monkeypatch) -> None:
    a, obs, _sent = make(monkeypatch, iter([REPLY]), mic="mic")
    monkeypatch.setattr(system, "record", lambda *a, **k: False)
    a.ai_tick(force=True)
    report = status.read()
    assert report["persona"] == a.pilot.persona["id"] and report["slot"] == a.pilot.slot
    assert report["observation"]["frames"] == 4 and report["observation"]["picture"]["luma_mean"] == 118.0
    assert report["claude"]["assessment"]["lighting"] == "mixed" and report["claude"]["confidence"] == 0.9
    (change,) = [c for c in report["changes"] if c["key"] == "warmth"]
    assert change["reason"] == "Skin looked cool." and change["source"] == "claude" and change["actuator"] == "obs-look"
    assert {row["id"] for row in report["actuators"]} >= {"obs-look", "mic-gain", "voice-preset", "face-finder"}
    entries = [e for e in journal() if e["event"] == "change"]
    assert entries and entries[0]["reason"] == "Skin looked cool."
    sheet = settings.sheet_path()
    assert sheet.read_bytes().startswith(b"SHEET") and sheet.stat().st_mode & 0o077 == 0
    assert settings.status_path().stat().st_mode & 0o077 == 0
    assert obs.filters[COLOR_FILTER][1]["color_multiply"] != 0xFFFFFF


def test_a_look_the_person_changed_by_hand_is_left_alone(monkeypatch) -> None:
    a, obs, _sent = make(monkeypatch, iter([REPLY, REPLY]))
    a.ai_tick(force=True)
    warmth = auto.multiply_to_warmth(obs.filters[COLOR_FILTER][1]["color_multiply"])
    obs.filters[COLOR_FILTER][1]["color_multiply"] = auto.warmth_to_multiply(-0.3)  # turned cooler by hand
    writes = len(obs.writes)
    a.ai_tick(force=True)
    assert auto.multiply_to_warmth(obs.filters[COLOR_FILTER][1]["color_multiply"]) == pytest.approx(-0.3, abs=0.05)
    assert len(obs.writes) == writes and warmth > 0
    assert any(e["event"] == "override" and "warmth" in e["keys"] for e in journal())
    assert "warmth" in status.read()["held"]


def test_what_was_kept_is_learned_for_this_hour(monkeypatch) -> None:
    from streamstack.director import learning

    a, obs, _ = make(monkeypatch, iter([REPLY]))
    obs.filters[COLOR_FILTER][1]["contrast"] = 0.12
    a.ai_tick(force=True)
    learned = learning.get(a.pilot.place, a.pilot.persona["id"], a.pilot.slot)
    assert learned["look"]["contrast"] == 0.12 and learned["n"] == 1


def test_the_local_backend_observes_and_reports_but_never_calls_claude(monkeypatch) -> None:
    a, _obs, sent = make(monkeypatch, iter([]), backend="local")
    assert a.ai_tick(force=True) is None
    assert sent == []
    report = status.read()
    assert report["claude"] is None and report["observation"]["frames"] == 4


def test_a_failed_later_still_shortens_the_burst_instead_of_failing_the_pass(monkeypatch) -> None:
    a, obs, sent = make(monkeypatch, iter([REPLY]))
    original = obs.screenshot
    calls = {"n": 0}

    def flaky(source, path, width=768):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("OBS dropped the connection")
        return original(source, path, width)

    monkeypatch.setattr(obs, "screenshot", flaky)
    a.ai_tick(force=True)
    assert len(sent) == 1 and status.read()["observation"]["frames"] == 2
    assert any("OBS dropped" in p for p in status.read()["observation"]["problems"])


def test_without_ffmpeg_the_single_middle_still_goes_to_claude(monkeypatch) -> None:
    a, _obs, sent = make(monkeypatch, iter([REPLY]))

    def broken(paths, out, timeout=30.0):
        raise imagefeatures.ImageAnalysisError("ffmpeg is not installed")

    monkeypatch.setattr(imagefeatures, "contact_sheet", broken)
    a.ai_tick(force=True)
    assert sent[0]["bytes"] == b"jpeg"
    assert any("ffmpeg" in p for p in status.read()["observation"]["problems"])


def test_between_passes_the_look_eases_towards_the_persona_in_small_steps(monkeypatch) -> None:
    settings.set_persona("executive")
    a, obs, _ = make(monkeypatch, iter([]))
    a.video_tick()
    changes = [e for e in journal() if e["event"] == "change"]
    assert changes and all(c["source"] in ("persona", "baseline") and "Executive" in c["reason"] for c in changes)
    assert all(abs(c["after"] - c["before"]) <= auto.limits.START_STEP[c["key"]] + 1e-6 for c in changes)
    before = len(obs.writes)
    for _ in range(30):
        a.video_tick()
    assert len(obs.writes) > before
    final = auto.current_look(obs, "Camera")
    assert final["warmth"] == pytest.approx(
        personas.load_library().get("executive")["picture"]["look"]["warmth"], abs=0.05
    )
    settled = len(obs.writes)
    a.video_tick()
    assert len(obs.writes) == settled  # nothing left to ease


def test_easing_can_be_switched_off(monkeypatch) -> None:
    a, obs, _ = make(monkeypatch, iter([]), STREAMSTACK_AUTO_PERSONA_LOOK="false")
    a.video_tick()
    assert obs.writes == []


def test_exposure_follows_the_persona_band_and_explains_itself(monkeypatch) -> None:
    settings.set_persona("late-night-calm")
    a, obs, _ = make(monkeypatch, iter([]), STREAMSTACK_AUTO_PERSONA_LOOK="false")
    low, _high = a.pilot.frame_band()
    monkeypatch.setattr(
        auto, "picture_stats", lambda frame, box=None: {"YAVG": low - 30, "YHIGH": 200.0, "YMIN": 16, "YMAX": 235}
    )
    a.video_tick()
    event = next(e for e in journal() if e["event"] == "exposure")
    assert "late-night" in event["reason"].lower() or "Late" in event["reason"]
    assert str(int(low)) in event["reason"]
    assert obs.filters[COLOR_FILTER][1]["gamma"] > 0


def test_a_new_persona_applies_within_one_tick(monkeypatch) -> None:
    a, _obs, _ = make(monkeypatch, iter([]))
    settings.set_persona("bright-podcast")
    a.pilot.refresh()
    assert a.pilot.persona["id"] == "bright-podcast"


def test_the_prompt_builder_and_readers_are_defensive() -> None:
    assert prompt.reasons_from({"reasons": "nope"}) == {} and prompt.reasons_from(
        {"reasons": {"a": 3, "b": " fine  "}}
    ) == {"b": "fine"}
    assert prompt.assessment_from({}) is None
    assessed = prompt.assessment_from(
        {"assessment": {"mood_match": 7, "observed_mood": "x", "lighting": "neon", "issues": [1, "a"]}}
    )
    assert assessed == {"mood_match": 1.0, "observed_mood": [], "lighting": "unclear", "issues": ["a"]}
    assert prompt.assessment_from({"assessment": {"mood_match": float("nan")}})["mood_match"] is None
