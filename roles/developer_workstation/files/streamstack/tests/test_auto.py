"""Auto mode: bounded changes, metering, pacing, undo and the Claude backends."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from streamstack import auto, config, system

LOOK = {"brightness": 0.0, "contrast": 0.0, "gamma": 0.0, "saturation": 0.0, "warmth": 0.0}


class FakeLink:
    """Just enough of the Console link: the look in place on one camera, a log of writes, a still."""

    def __init__(self, look: dict | None = None) -> None:
        self.values = {
            "brightness": 0.0,
            "contrast": 0.0,
            "gamma": 0.0,
            "saturation": 0.0,
            "warmth": 0.0,
            "lut_enabled": False,
            "lut_amount": 0.35,
            "sharpen_enabled": False,
            **(look or {}),
        }
        self.writes: list[dict] = []

    def camera_sources(self) -> list[str]:
        return ["Camera"]

    def look(self, source: str) -> dict:
        return dict(self.values)

    def set_look(self, source: str, values: dict) -> None:
        self.values.update(values)
        self.writes.append(dict(values))

    def screenshot(self, source: str, path: Path, width: int = 768) -> Path:
        path.write_bytes(b"jpeg")
        os.chmod(path, 0o600)
        return path

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch) -> None:
    monkeypatch.setattr(auto.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(auto, "notify", lambda *a, **k: None)


def quiet(**cfg) -> auto.Auto:
    settings = dict(config.DEFAULTS, STREAMSTACK_AUTO_AUDIO="false", STREAMSTACK_AUTO_NOTIFY="false", **cfg)
    return auto.Auto(settings)


def journal() -> list[dict]:
    return [json.loads(line) for line in (config.state_dir() / "auto.jsonl").read_text().splitlines()]


# -- limits -----------------------------------------------------------------


def test_limits_hold_whatever_claude_proposes() -> None:
    look = dict(LOOK, saturation=0.35, lut_enabled=False, lut_amount=0.35)
    proposal = {
        "video": {
            "contrast": 5,
            "saturation": 1.0,
            "warmth": float("nan"),
            "gamma": "high",
            "brightness": -1,
            "lut_enabled": "yes",
            "sharpen_enabled": True,
            "lut_amount": True,
        }
    }
    target = auto.bounded(proposal, look)
    assert target["contrast"] == auto.AI_STEP["contrast"]  # one step at most
    assert target["saturation"] == auto.LIMITS["saturation"][1]  # never past the limit
    assert target["brightness"] == -auto.AI_STEP["brightness"]
    assert target["warmth"] == 0.0 and target["gamma"] == 0.0  # junk is ignored
    assert target["lut_amount"] == 0.35  # booleans are not numbers
    assert target["lut_enabled"] is False  # only real booleans switch filters
    assert "sharpen_enabled" not in target  # nor filters the scene does not have


def test_a_ramp_writes_smooth_steps_ending_exactly_on_the_target() -> None:
    link = FakeLink()
    auto.ramp(link, "Camera", auto.current_look(link, "Camera"), {"gamma": 0.2, "warmth": 0.4}, seconds=1.0, steps=5)
    assert len(link.writes) == 5
    gammas = [w["gamma"] for w in link.writes]
    assert gammas == sorted(gammas) and gammas[-1] == 0.2 and link.values["warmth"] == 0.4


def test_a_ramp_with_nothing_to_change_writes_nothing_and_a_cancelled_one_stops() -> None:
    link = FakeLink()
    auto.ramp(link, "Camera", dict(link.values), {"gamma": 0.0}, seconds=1.0, steps=4)
    assert link.writes == []
    cancel = auto.threading.Event()
    cancel.set()
    auto.ramp(link, "Camera", dict(link.values), {"gamma": 0.2}, seconds=1.0, steps=4, cancel=cancel)
    assert link.writes == []


def test_apply_look_ramps_the_colour_then_sets_the_filters_in_one_write() -> None:
    link = FakeLink()
    look = auto.current_look(link, "Camera")
    changed = auto.apply_look(link, "Camera", look, {"gamma": 0.1, "lut_enabled": True, "lut_amount": 0.5}, seconds=1.0)
    assert changed
    assert link.writes[-1] == {"lut_enabled": True, "lut_amount": 0.5}
    assert link.values["gamma"] == 0.1
    assert not auto.apply_look(link, "Camera", dict(link.values), dict(link.values), seconds=1.0)


def test_exposure_lifts_mid_tones_first_and_never_clips() -> None:
    dark = auto.exposure_target({"YAVG": 40, "YHIGH": 120}, LOOK)
    assert dark["gamma"] > 0 and dark["brightness"] == 0.0
    maxed = auto.exposure_target({"YAVG": 40, "YHIGH": 120}, dict(LOOK, gamma=auto.LIMITS["gamma"][1]))
    assert maxed["brightness"] > 0
    assert auto.exposure_target({"YAVG": 90, "YHIGH": 250}, LOOK)["brightness"] < 0
    assert auto.exposure_target({"YAVG": 120, "YHIGH": 220}, LOOK) is None
    assert auto.exposure_target({}, LOOK) is None
    # A face band replaces the frame band.
    assert auto.exposure_target({"YAVG": 120, "YHIGH": 200}, LOOK, band=(140, 170))["gamma"] > 0


def test_frames_without_a_picture_are_never_exposed_for() -> None:
    assert auto.no_picture({}) == "no measurement"
    assert "black" in auto.no_picture({"YAVG": 2, "YMIN": 0, "YMAX": 20})
    assert "flat" in auto.no_picture({"YAVG": 120, "YMIN": 118, "YMAX": 124})
    assert auto.no_picture({"YAVG": 120, "YMIN": 16, "YMAX": 235}) == ""


def test_subject_box_is_sanitised() -> None:
    assert auto.subject_from({}) is None
    assert auto.subject_from({"subject": {"box": [0.1, 0.1, 0.01, 0.5]}}) is None  # too small
    assert auto.subject_from({"subject": {"box": [0.1, "a", 0.2, 0.2]}}) is None
    assert auto.subject_from({"subject": {"box": [0.1, float("inf"), 0.2, 0.2]}}) is None
    subject = auto.subject_from({"subject": {"box": [0.9, -0.5, 0.5, 0.3], "target_luma": 250}})
    assert subject == {"box": [0.9, 0.0, 0.1, 0.3], "target": auto.FACE_TARGET[1]}
    assert auto.crop_filter(subject["box"]) == "crop=iw*0.1000:ih*0.3000:iw*0.9000:ih*0.0000,"


# -- audio ------------------------------------------------------------------


def test_audio_levels_speech_but_never_room_noise(monkeypatch) -> None:
    a = quiet()
    a.mic, a.mic_start_db = "mic", 0.0
    gains: list[float] = []
    current = {"db": 0.0}
    monkeypatch.setattr(auto, "source_gain_db", lambda source: current["db"])
    monkeypatch.setattr(auto, "set_source_gain_db", lambda source, db: (gains.append(db), current.update(db=db)))

    def take(**stats):
        monkeypatch.setattr(auto, "voice_stats", lambda source: stats)
        a.audio_tick()

    take(peak_db=-30, rms_peak_db=-35.7, rms_db=-38.5)  # steady room noise
    assert gains == []
    take(peak_db=-20, rms_peak_db=-30, rms_db=-42)  # quiet speech
    assert gains == [1.0]
    take(peak_db=-0.2, rms_peak_db=-8, rms_db=-20)  # clipping
    assert gains[-1] == -1.0
    current["db"] = 5.5
    a.governor.committed("mic-gain", "mic", {"gain_db": 5.5}, ["gain_db"])  # as if auto mode had set it
    take(peak_db=-20, rms_peak_db=-30, rms_db=-42)
    assert gains[-1] == auto.MIC_GAIN_DB[1]  # boost capped relative to the start
    assert auto.load_baseline()["mic"] == {"mic": 0.0}


def test_a_gain_the_person_set_by_hand_is_left_alone(monkeypatch) -> None:
    a = quiet()
    a.mic, a.mic_start_db = "mic", 0.0
    current = {"db": 0.0}
    gains: list[float] = []
    monkeypatch.setattr(auto, "source_gain_db", lambda source: current["db"])
    monkeypatch.setattr(auto, "set_source_gain_db", lambda source, db: (gains.append(db), current.update(db=db)))
    monkeypatch.setattr(auto, "voice_stats", lambda source: {"peak_db": -20, "rms_peak_db": -30, "rms_db": -42})
    a.audio_tick()
    assert gains == [1.0]
    current["db"] = -4.0  # the person turned it down
    a.audio_tick()
    a.audio_tick()
    assert gains == [1.0]
    assert any(e["event"] == "override" and e["keys"] == ["gain_db"] for e in journal())


def test_astats_parsing_reads_the_overall_section() -> None:
    text = (
        "[Parsed_astats_0 @ 0x1] Channel: 1\nPeak level dB: -1.0\n"
        "[Parsed_astats_0 @ 0x1] Overall\nPeak level dB: -3.5\nRMS peak dB: -14.2\n"
        "RMS level dB: -22.0\nNoise floor dB: -61.3\n"
    )
    assert auto.parse_astats(text) == {"peak_db": -3.5, "rms_peak_db": -14.2, "rms_db": -22.0, "noise_floor_db": -61.3}


# -- Claude passes ----------------------------------------------------------


def _claude_pass(monkeypatch, replies, stats=None):
    link = FakeLink()
    monkeypatch.setattr(
        auto, "picture_stats", lambda frame, box=None: stats or {"YAVG": 118.0, "YHIGH": 230.0, "YMIN": 16, "YMAX": 235}
    )
    monkeypatch.setattr(auto.voicechain, "preset_names", lambda: ["Voice", "Broadcast"])
    monkeypatch.setattr(auto.voicechain, "current_preset", lambda: "Voice")
    loaded: list[str] = []
    monkeypatch.setattr(auto.voicechain, "load_preset", lambda name: loaded.append(name) or True)
    monkeypatch.setattr(system, "run", lambda argv, timeout=10: None)
    monkeypatch.setattr(auto, "ask_claude", lambda cfg, frame, context: next(replies))
    a = quiet()
    a.link = link
    return a, link, loaded


def test_a_confident_proposal_is_applied_bounded_and_can_be_undone(monkeypatch) -> None:
    replies = iter(
        [
            {
                "video": {"contrast": 0.1, "warmth": 0.4, "lut_enabled": True},
                "audio": {"voice_preset": "Broadcast"},
                "subject": {"box": [0.4, 0.2, 0.2, 0.3], "target_luma": 130},
                "confidence": 0.9,
                "notes": "Warmer.",
            },
            {"video": {"contrast": 0.4}, "confidence": 0.3, "notes": "Unsure."},
        ]
    )
    a, link, loaded = _claude_pass(monkeypatch, replies)
    a.ai_tick(force=True)
    color = link.values
    assert color["contrast"] == 0.1
    assert abs(color["warmth"] - auto.AI_STEP["warmth"]) < 0.05
    assert link.values["lut_enabled"] is True
    assert loaded == ["Broadcast"]
    assert a.subject["box"] == [0.4, 0.2, 0.2, 0.3] and a.subject["target"] == 130
    assert a.ai_backoff == 1
    baseline = auto.load_baseline()
    assert baseline["look"]["Camera"]["contrast"] == 0.0 and baseline["preset"] == {"input": "Voice"}
    writes = len(link.writes)
    a.ai_tick(force=True)  # low confidence: logged, not applied, and the next pass waits longer
    assert len(link.writes) == writes and color["contrast"] == 0.1
    assert a.ai_backoff == 2
    events = [e["event"] for e in journal()]
    assert events.count("ai") == 2 and set(events) == {"ai", "change"}
    reasons = {(e["actuator"], e["key"]): e["reason"] for e in journal() if e["event"] == "change"}
    assert reasons[("program-look", "contrast")] and reasons[("voice-preset", "voice_preset")]
    cache = config.cache_dir() / "auto"
    assert (cache / "last-ai.jpg").is_file() and cache.stat().st_mode & 0o077 == 0
    assert (cache / "last-ai.jpg").stat().st_mode & 0o077 == 0


def test_remember_keeps_the_first_value() -> None:
    auto.remember("mic", "m", -3.0)
    auto.remember("mic", "m", 4.0)
    assert auto.load_baseline() == {"mic": {"m": -3.0}}


def test_a_black_frame_is_never_sent_to_claude(monkeypatch) -> None:
    def never(*a):
        raise AssertionError("called Claude")

    a, _link, _ = _claude_pass(monkeypatch, iter([]), stats={"YAVG": 1.0, "YMIN": 0, "YMAX": 3})
    monkeypatch.setattr(auto, "ask_claude", never)
    assert a.ai_tick(force=True) is None
    assert journal()[-1]["event"] == "ai-skipped"


def test_local_backend_never_calls_claude(monkeypatch) -> None:
    monkeypatch.setattr(auto, "ask_claude", lambda *a: (_ for _ in ()).throw(AssertionError("called")))
    a = quiet(STREAMSTACK_AUTO_BACKEND="local")
    a.link = FakeLink()
    assert a.ai_tick(force=True) is None


def test_pacing_backs_off_and_returns_early_when_the_picture_changes() -> None:
    a = quiet(STREAMSTACK_AUTO_AI_INTERVAL="300")
    a.last_ai = 1000.0
    assert not a.ai_due(now=1200.0)
    assert a.ai_due(now=1300.0)
    a.ai_backoff = 4
    assert not a.ai_due(now=1300.0)
    a.ai_reference = {"YAVG": 120.0, "UAVG": 128.0, "VAVG": 128.0}
    a.frame_stats = {"YAVG": 70.0, "UAVG": 128.0, "VAVG": 128.0}  # the lights went down
    assert not a.ai_due(now=1000.0 + auto.AI_MIN_GAP - 1)
    assert a.ai_due(now=1000.0 + auto.AI_MIN_GAP)


def test_cli_backend_is_restricted_to_reading_the_still() -> None:
    argv = auto.cli_argv("/bin/claude", Path("/x/frame.jpg"), "prompt", model="opus")
    assert argv[:3] == ["/bin/claude", "-p", "prompt"]
    assert argv[argv.index("--tools") + 1] == "Read"
    for flag in ("--restricted", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in argv
    assert json.loads(argv[argv.index("--json-schema") + 1]) == auto.PROPOSAL_SCHEMA
    assert argv[-2:] == ["--model", "opus"]
    assert "--model" not in auto.cli_argv("/bin/claude", Path("/x/frame.jpg"), "prompt")


def test_cli_replies_prefer_structured_output() -> None:
    proposal = {"confidence": 0.8, "notes": "ok"}
    reply = json.dumps({"type": "result", "is_error": False, "result": "", "structured_output": proposal})
    assert auto.parse_cli_reply(0, reply, "") == proposal
    fenced = json.dumps({"is_error": False, "result": "Here:\n```json\n" + json.dumps(proposal) + "\n```"})
    assert auto.parse_cli_reply(0, fenced, "") == proposal
    with pytest.raises(RuntimeError, match="Not logged in"):
        auto.parse_cli_reply(1, json.dumps({"is_error": True, "result": "Not logged in"}), "")
    with pytest.raises(RuntimeError, match="boom"):
        auto.parse_cli_reply(2, "", "boom")


def test_cli_backend_runs_in_the_frame_directory_without_the_parent_session(tmp_path, monkeypatch) -> None:
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs, argv=argv)
        out = json.dumps({"is_error": False, "structured_output": {"confidence": 1, "notes": ""}})
        return auto.subprocess.CompletedProcess(argv, 0, out, "")

    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"x")
    monkeypatch.setattr(auto, "claude_cli", lambda: "/bin/claude")
    monkeypatch.setattr(auto.subprocess, "run", fake_run)
    monkeypatch.setenv("CLAUDECODE", "1")
    auto._ask_cli(dict(config.DEFAULTS), frame, "prompt")
    assert seen["cwd"] == tmp_path
    assert "CLAUDECODE" not in seen["env"]
    assert seen["stdin"] == auto.subprocess.DEVNULL


def test_api_backend_forces_the_schema_tool(tmp_path) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"\xff\xd8jpeg")
    request = auto.api_request(dict(config.DEFAULTS), frame, "prompt")
    assert request["model"] == auto.API_MODEL
    assert request["tool_choice"] == {"type": "tool", "name": "propose_adjustments"}
    assert request["tools"][0]["input_schema"] == auto.PROPOSAL_SCHEMA
    assert request["messages"][0]["content"][0]["source"]["media_type"] == "image/jpeg"


def test_api_key_file_must_be_private() -> None:
    path = config.secrets_file()
    path.parent.mkdir(parents=True)
    path.write_text("# key\nANTHROPIC_API_KEY='sk-test'\n")
    path.chmod(0o644)
    with pytest.raises(RuntimeError, match="chmod 600"):
        auto.api_key()
    path.chmod(0o600)
    assert auto.api_key() == "sk-test"


def test_reset_refuses_while_auto_mode_runs_and_restores_the_mic(monkeypatch, capsys) -> None:
    auto.remember("mic", "mic", -2.0)
    monkeypatch.setattr(system, "user_unit_active", lambda unit: True)
    assert auto.cmd_reset(None) == 1
    monkeypatch.setattr(system, "user_unit_active", lambda unit: False)
    gains = []
    monkeypatch.setattr(auto, "set_source_gain_db", lambda source, db: gains.append((source, db)))
    assert auto.cmd_reset(None) == 0
    assert gains == [("mic", -2.0)]
    assert auto.load_baseline() == {}
    capsys.readouterr()
    assert auto.cmd_reset(None) == 0
    assert "Nothing to undo" in capsys.readouterr().out


def test_journal_rotates(monkeypatch) -> None:
    monkeypatch.setattr(auto, "LOG_MAX_BYTES", 200)
    for _ in range(10):
        auto.log("tick", padding="x" * 50)
    state = config.state_dir()
    assert (state / "auto.jsonl.1").is_file()
    assert (state / "auto.jsonl").stat().st_size <= 400


def test_the_program_look_actuator_needs_the_console_and_says_so() -> None:
    from streamstack.director.actuators import ProgramLookActuator

    actuator = ProgramLookActuator(
        connect=lambda: None,
        read_look=lambda link, target: {},
        apply_look=lambda link, target, current, wanted, seconds: None,
        remember=lambda *args: None,
        sources=lambda link: [],
    )
    availability = actuator.availability()
    assert not availability.ok and "Console is not running" in availability.reason
    assert actuator.targets() == []
    with pytest.raises(RuntimeError, match="Console is not running"):
        actuator.read("program")
    with pytest.raises(RuntimeError, match="Console is not running"):
        actuator.apply("program", {}, {"contrast": 1.1}, 1.0)
