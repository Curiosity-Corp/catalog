"""StreamStack auto mode: an auto-cinematographer and auto-audio engineer.

Off until turned on (`streamstack auto on`, or the panel menu). While on it
runs two loops on the graded studio camera:

  every STREAMSTACK_AUTO_INTERVAL seconds (local, no network)
    video  measure the graded program picture (from the Console) and nudge the
           look's gamma, then brightness, towards a good
           exposure. Once Claude has located the face it meters the face
           alone (spot metering), towards the luma Claude chose for that skin
           tone, and never clips it.
    audio  measure 4 s of the raw microphone and nudge its input gain so
           speech peaks sit around -24 to -12 dBFS and never clip; steady
           room noise never raises the gain.

  every STREAMSTACK_AUTO_AI_INTERVAL seconds (Claude)
    send a contact sheet of a burst of stills (the stills go to Claude), the
    measured picture and voice numbers, the chosen persona, the time of day
    and the current settings (see streamstack.director), and apply the
    bounded changes Claude proposes:
    contrast, saturation, warmth (white balance), the cinematic LUT,
    sharpening and the voice preset. Audio never leaves the host, only the
    numbers measured from it. When Claude changes nothing and the picture holds still, the
    pass backs off to 2x and then 4x the interval; a changed picture (lights,
    camera, person) brings it back early.

Every change is clamped to fixed limits and a maximum step, ramped over a few
seconds so a call never sees a jump, and logged to
~/.local/state/streamstack/auto.jsonl. The look and mic gain from before auto
mode's first change are kept, and `streamstack auto reset` ramps back to them.

Claude backends (STREAMSTACK_AUTO_BACKEND):
  claude-cli  the signed-in Claude Code CLI (`claude -p`), so passes run on
              the user's Claude subscription; restricted to reading the one
              still, with schema-validated output
  api         the Anthropic API with ANTHROPIC_API_KEY from the environment
              or ~/.config/streamstack/auto.env (mode 600), forced tool use
  local       no Claude at all, only the local loops
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from streamstack import config, crash, store, system
from streamstack.audiochain import control as voicechain
from streamstack.consolelink import ConsoleLink
from streamstack.director import (
    actuators,
    adapters,
    audiofeatures,
    devices,
    faces,
    judge,
    limits,
    observe,
    pilot,
    prompt,
    status,
)

API_MODEL = "claude-sonnet-5-5"
LOG_MAX_BYTES = 2_000_000

# Hard limits for every setting auto mode may touch live in director.limits (shared with the actuators).
LIMITS = limits.LIMITS
AI_STEP = limits.AI_STEP
COLOR_KEYS = limits.COLOR_KEYS
MIC_GAIN_DB = limits.MIC_GAIN_DB
FRAME_BAND = limits.FRAME_BAND
FACE_TARGET = limits.FACE_TARGET
FACE_BAND = limits.FACE_BAND
SUBJECT_MAX_AGE = 1200.0
AI_MIN_GAP = 60.0
AI_MAX_BACKOFF = 4

PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "video": {
            "type": "object",
            "properties": {
                "brightness": {"type": "number"},
                "contrast": {"type": "number"},
                "gamma": {"type": "number"},
                "saturation": {"type": "number"},
                "warmth": {"type": "number"},
                "lut_enabled": {"type": "boolean"},
                "lut_amount": {"type": "number"},
                "sharpen_enabled": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        "audio": {
            "type": "object",
            "properties": {"voice_preset": {"type": ["string", "null"]}},
            "additionalProperties": False,
        },
        "subject": {
            "type": "object",
            "properties": {
                "box": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                "target_luma": {"type": "number"},
            },
            "required": ["box"],
            "additionalProperties": False,
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "notes": {"type": "string"},
    },
    "required": ["confidence", "notes"],
    "additionalProperties": False,
}


def log(event: str, **fields) -> None:
    state = config.state_dir()
    state.mkdir(parents=True, exist_ok=True)
    journal = state / "auto.jsonl"
    try:
        if journal.stat().st_size > LOG_MAX_BYTES:
            journal.replace(journal.with_suffix(".jsonl.1"))
    except OSError:
        pass
    entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
    with open(journal, "a") as handle:
        handle.write(json.dumps(entry) + "\n")
    if os.environ.get("STREAMSTACK_AUTO_VERBOSE"):
        print(json.dumps(entry), flush=True)


def notify(cfg: dict, summary: str, body: str = "") -> None:
    if config.truthy(cfg["STREAMSTACK_AUTO_NOTIFY"]) and shutil.which("notify-send"):
        subprocess.Popen(
            ["notify-send", "-a", "StreamStack", "-t", "6000", summary, body],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


# ---------------------------------------------------------------------------
# Measurements
# ---------------------------------------------------------------------------


def crop_filter(box: list[float] | None) -> str:
    if not box:
        return ""
    x, y, w, h = box
    return f"crop=iw*{w:.4f}:ih*{h:.4f}:iw*{x:.4f}:ih*{y:.4f},"


def picture_stats(image: Path, box: list[float] | None = None) -> dict[str, float]:
    """Luma average and spread, chroma balance and saturation (ffmpeg signalstats).

    With `box` (x, y, width, height as fractions of the frame) only that region is measured.
    """
    result = system.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-v",
            "error",
            "-i",
            str(image),
            "-vf",
            f"{crop_filter(box)}signalstats,metadata=mode=print:file=-",
            "-f",
            "null",
            "-",
        ],
        timeout=20,
    )
    stats = {}
    if result and result.returncode == 0:
        for key, value in re.findall(r"lavfi\.signalstats\.(\w+)=([-\d.]+)", result.stdout):
            if key in ("YAVG", "YLOW", "YHIGH", "YMIN", "YMAX", "UAVG", "VAVG", "SATAVG"):
                stats[key] = float(value)
    return stats


def no_picture(stats: dict[str, float]) -> str:
    """Why a frame is not worth exposing for: no signal, lens cap, a flat test card."""
    if "YAVG" not in stats:
        return "no measurement"
    if stats["YAVG"] < 6:
        return "black frame (camera off or covered)"
    if stats.get("YMAX", 255) - stats.get("YMIN", 0) < 12:
        return "flat frame (no camera signal)"
    return ""


def picture_changed(before: dict[str, float] | None, now: dict[str, float] | None) -> bool:
    if not before or not now or "YAVG" not in before or "YAVG" not in now:
        return False
    return (
        abs(before["YAVG"] - now["YAVG"]) > 12
        or abs(before.get("UAVG", 128) - now.get("UAVG", 128)) > 4
        or abs(before.get("VAVG", 128) - now.get("VAVG", 128)) > 4
    )


def voice_stats(source: str, seconds: float = 4.0) -> dict[str, float]:
    """Peak, loudest RMS window, average RMS, noise floor and spectral tilt of a short take, in dBFS."""
    with tempfile.TemporaryDirectory(prefix="streamstack-auto-") as tmp:
        take = Path(tmp) / "take.wav"
        if not system.record(source, seconds, str(take)):
            return {}
        result = system.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-v",
                "info",
                "-i",
                str(take),
                "-af",
                "astats=metadata=0:measure_overall=Peak_level+RMS_peak+RMS_level+Noise_floor",
                "-f",
                "null",
                "-",
            ],
            timeout=20,
        )
        tilt = {}
        for band, flt in (("low", "lowpass=f=250"), ("high", "highpass=f=4000")):
            band_result = system.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-v",
                    "info",
                    "-i",
                    str(take),
                    "-af",
                    f"{flt},astats=metadata=0:measure_overall=RMS_level",
                    "-f",
                    "null",
                    "-",
                ],
                timeout=20,
            )
            match = re.search(r"Overall.*?RMS level dB: ([-\d.inf]+)", band_result.stderr if band_result else "", re.S)
            if match and "inf" not in match.group(1):
                tilt[f"{band}_rms_db"] = float(match.group(1))
    return parse_astats(result.stderr if result else "") | tilt


def parse_astats(text: str) -> dict[str, float]:
    stats = {}
    overall = text.split("Overall", 1)[-1]
    for label, key in (
        ("Peak level dB", "peak_db"),
        ("RMS peak dB", "rms_peak_db"),
        ("RMS level dB", "rms_db"),
        ("Noise floor dB", "noise_floor_db"),
    ):
        match = re.search(rf"{label}: ([-\d.]+)", overall)
        if match:
            stats[key] = float(match.group(1))
    return stats


def source_gain_db(source: str) -> float | None:
    result = system.run(["pactl", "get-source-volume", source])
    match = re.search(r"/\s*(-?[\d.]+|-inf) dB", result.stdout if result else "")
    if not match:
        return None
    return -60.0 if match.group(1) == "-inf" else float(match.group(1))


def set_source_gain_db(source: str, db: float) -> None:
    system.run(["pactl", "set-source-volume", source, f"{db:.1f}dB"])


# ---------------------------------------------------------------------------
# Applying changes gently
# ---------------------------------------------------------------------------


def clamp(name: str, value: float) -> float:
    low, high = LIMITS[name]
    return max(low, min(high, value))


def current_look(link: ConsoleLink, source: str) -> dict:
    """The look in place on `source`, read from the Console's native engine."""
    return link.look(source)


def ramp(
    link: ConsoleLink,
    source: str,
    start: dict,
    target: dict,
    seconds: float = 3.0,
    steps: int = 12,
    cancel: threading.Event | None = None,
) -> None:
    """Move the colour from start to target in small steps, so no frame jumps.

    Each step is one `video.look` write: the engine applies it between two frames, with no restart.
    Setting `cancel` stops after the current step.
    """
    keys = [k for k in COLOR_KEYS if k in target and k in start]
    if not any(target[k] != start[k] for k in keys):
        return
    for step in range(1, steps + 1):
        if cancel is not None and cancel.is_set():
            return
        mix = step / steps
        mix = mix * mix * (3 - 2 * mix)  # smoothstep
        link.set_look(source, {key: round(start[key] + (target[key] - start[key]) * mix, 4) for key in keys})
        if cancel is not None:
            if cancel.wait(seconds / steps):
                return
        else:
            time.sleep(seconds / steps)


def apply_look(link: ConsoleLink, source: str, look: dict, target: dict, seconds: float = 4.0) -> bool:
    """Ramp the colour, then switch the LUT and Sharpen and set the LUT amount. True when anything changed."""
    changed = any(target.get(k) != look.get(k) for k in target)
    ramp(link, source, look, target, seconds=seconds, steps=max(4, int(seconds * 4)))
    extras = {
        key: target[key]
        for key in ("lut_enabled", "sharpen_enabled", "lut_amount")
        if key in target and target[key] != look.get(key)
    }
    if extras:
        link.set_look(source, extras)
    return changed


def exposure_target(stats: dict, look: dict, band: tuple[float, float] = FRAME_BAND) -> dict | None:
    """Local auto-exposure: keep the metered luma inside `band`, never clip highlights."""
    if "YAVG" not in stats:
        return None
    low, high = band
    average, top = stats["YAVG"], stats.get("YHIGH", stats["YAVG"])
    change = 0.0
    if top >= 245 or average > high:
        change = -0.02
    elif average < low and top < 235:
        change = 0.02 if average < low - 15 else 0.01
    if not change:
        return None
    target = dict(look)
    # Gamma lifts mid-tones (faces) without greying blacks; brightness only finishes the job.
    if change > 0 and look["gamma"] < LIMITS["gamma"][1]:
        target["gamma"] = clamp("gamma", look["gamma"] + change * 4)
    else:
        target["brightness"] = clamp("brightness", look["brightness"] + change)
    return target if target != look else None


def subject_from(proposal: dict) -> dict | None:
    """A sane face box and luma target from Claude's proposal, or None."""
    subject = proposal.get("subject")
    if not isinstance(subject, dict):
        return None
    box = subject.get("box")
    if not (isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box)):
        return None
    x, y, w, h = (float(v) for v in box)
    if not all(math.isfinite(v) for v in (x, y, w, h)):
        return None
    x, y = max(0.0, min(0.95, x)), max(0.0, min(0.95, y))
    w, h = min(1.0 - x, w), min(1.0 - y, h)
    if w < 0.04 or h < 0.04:
        return None
    target = subject.get("target_luma")
    if isinstance(target, (int, float)) and math.isfinite(target):
        target = max(FACE_TARGET[0], min(FACE_TARGET[1], float(target)))
    else:
        target = None
    return {"box": [round(v, 4) for v in (x, y, w, h)], "target": target}


def bounded(proposal: dict, look: dict) -> dict:
    """Clamp Claude's proposal to the hard limits and the per-pass step."""
    target = dict(look)
    video = proposal.get("video") or {}
    for key, step in AI_STEP.items():
        value = video.get(key)
        if key in look and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            wanted = max(look[key] - step, min(look[key] + step, float(value)))
            target[key] = round(clamp(key, wanted), 4)
    for key in ("lut_enabled", "sharpen_enabled"):
        if key in video and key in look and isinstance(video[key], bool):
            target[key] = video[key]
    return target


# ---------------------------------------------------------------------------
# What auto mode changed, so it can be undone
# ---------------------------------------------------------------------------


def baseline_path() -> Path:
    return config.state_dir() / "baseline.json"


def load_baseline() -> dict:
    return store.read_json(baseline_path(), {}, validate=store.is_object)


def baseline_gain(target: str) -> float | None:
    """The preamp gain an interface control had before auto mode first moved it, when recorded."""
    value = (load_baseline().get("gain") or {}).get(target)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def remember(kind: str, key: str, value) -> None:
    """Record the value from before auto mode's first change (never overwritten)."""
    baseline = load_baseline()
    section = baseline.setdefault(kind, {})
    if key in section:
        return
    section[key] = value
    path = baseline_path()
    store.write_json(path, baseline, sort_keys=False)


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

PROMPT = """You are the cinematographer and audio engineer for a live video call or stream.
The still {image} is the graded camera picture viewers see right now ({source}).

Measured picture (ffmpeg signalstats, 8-bit, whole frame): {picture}
Current look settings (the native engine's colour grade): {look}
Allowed ranges: {limits}
Largest change per pass: {steps}
Measured raw microphone over 4 s, dBFS: {voice}
Voice presets available (current: {preset}): {presets}

Judge skin tones, exposure on the face, white balance, contrast and colour
for a natural, flattering, professional look. A local loop already handles
exposure with brightness/gamma every few seconds; change those only for a
clear problem. Prefer small changes; leave out anything that already looks
good. For audio, suggest another preset only when the statistics clearly call
for it (for example a strong low-frequency excess or a dull, muffled voice).

If a person is in the frame, set subject.box to their face as
[x, y, width, height] fractions of the frame (0-1, from the top left), and
subject.target_luma to the average 8-bit luma (Y, 0-255) their face should
have for a natural exposure of their skin tone (usually 90-160). The local
loop meters that box. Leave subject out when no face is visible.

Video values are absolute targets, not deltas. confidence is 0-1. notes is one
short sentence for the operator: what you changed and why, or what they should
fix by hand (light, framing, background)."""


def ask_claude(cfg: dict, image: Path, context: dict) -> dict:
    """One Claude pass about `image`. `context["prompt"]` and `context["schema"]` replace the built-in ones."""
    backend = cfg["STREAMSTACK_AUTO_BACKEND"]
    text = context["prompt"] if "prompt" in context else PROMPT.format(image=image, **context)
    schema = context.get("schema", PROPOSAL_SCHEMA)
    if backend == "api":
        return _ask_api(cfg, image, text, schema)
    if backend == "claude-cli":
        return _ask_cli(cfg, image, text, schema)
    raise RuntimeError(f"backend {backend!r} does not call Claude")


def claude_cli() -> str:
    """The Claude Code CLI; user services do not see a login shell's PATH."""
    for candidate in (
        os.environ.get("STREAMSTACK_AUTO_CLAUDE"),
        shutil.which("claude"),
        str(Path.home() / ".local/bin/claude"),
        str(Path.home() / ".claude/local/claude"),
        str(Path.home() / ".npm-global/bin/claude"),
    ):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return ""


def json_from(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise RuntimeError("Claude's reply had no JSON object")
    return json.loads(match.group(0))


def cli_argv(claude: str, image: Path, prompt: str, model: str = "", schema: dict | None = None) -> list[str]:
    # --restricted drops every tool that runs code and confines Read to the
    # working directory, which holds only this still; --tools leaves Read
    # alone. No MCP servers, no saved session, schema-checked output.
    argv = [
        claude,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema or PROPOSAL_SCHEMA),
        "--tools",
        "Read",
        "--restricted",
        "--strict-mcp-config",
        "--no-session-persistence",
    ]
    if model:
        argv += ["--model", model]
    return argv


def _ask_cli(cfg: dict, image: Path, prompt: str, schema: dict | None = None) -> dict:
    claude = claude_cli()
    if not claude:
        raise RuntimeError("the claude CLI is not installed; install Claude Code and sign in, or use the api backend")
    argv = cli_argv(
        claude, image, f"Read the image {image}, then answer.\n\n{prompt}", cfg["STREAMSTACK_AUTO_MODEL"], schema
    )
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
            cwd=image.parent,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except OSError as exc:
        raise RuntimeError(f"{claude} cannot run ({exc.strerror}); reinstall Claude Code") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("claude -p took longer than 4 minutes") from exc
    return parse_cli_reply(result.returncode, result.stdout, result.stderr)


def parse_cli_reply(code: int, stdout: str, stderr: str) -> dict:
    try:
        reply = json.loads(stdout)
    except ValueError:
        reply = None
    if not isinstance(reply, dict):
        if code != 0:
            raise RuntimeError(f"claude -p failed: {(stderr or stdout).strip()[:300]}")
        return json_from(stdout)
    if code != 0 or reply.get("is_error"):
        raise RuntimeError(f"claude -p failed: {str(reply.get('result') or stderr).strip()[:300]}")
    structured = reply.get("structured_output")
    if isinstance(structured, dict):
        return structured
    return json_from(str(reply.get("result", "")))


def api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    path = config.secrets_file()
    if not key and path.exists():
        if path.stat().st_mode & 0o077:
            raise RuntimeError(f"{path} is readable by others; chmod 600 it")
        for line in path.read_text().splitlines():
            parts = shlex.split(line, comments=True)
            if parts and parts[0].startswith("ANTHROPIC_API_KEY="):
                key = parts[0].split("=", 1)[1]
    if not key:
        raise RuntimeError(f"no ANTHROPIC_API_KEY in the environment or {path}")
    return key


def api_request(cfg: dict, image: Path, prompt: str, schema: dict | None = None) -> dict:
    return {
        "model": cfg["STREAMSTACK_AUTO_MODEL"] or API_MODEL,
        "max_tokens": 1800,
        "tools": [
            {
                "name": "propose_adjustments",
                "description": "Propose bounded picture and voice adjustments for the live call.",
                "input_schema": schema or PROPOSAL_SCHEMA,
            }
        ],
        "tool_choice": {"type": "tool", "name": "propose_adjustments"},
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64.b64encode(image.read_bytes()).decode(),
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }


def _ask_api(cfg: dict, image: Path, prompt: str, schema: dict | None = None) -> dict:
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(api_request(cfg, image, prompt, schema)).encode(),
        method="POST",
        headers={"x-api-key": api_key(), "anthropic-version": "2023-06-01", "content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        reply = json.loads(response.read())
    for part in reply.get("content", []):
        if part.get("type") == "tool_use" and isinstance(part.get("input"), dict):
            return part["input"]
    return json_from("".join(part.get("text", "") for part in reply.get("content", [])))


# ---------------------------------------------------------------------------
# The loops
# ---------------------------------------------------------------------------


class Auto:
    def __init__(self, cfg: dict, dry_run: bool = False) -> None:
        self.cfg = cfg
        self.dry_run = dry_run
        self.link: ConsoleLink | None = None
        self.mic = system.microphone(cfg) if config.truthy(cfg["STREAMSTACK_AUTO_AUDIO"]) else ""
        self.mic_start_db = source_gain_db(self.mic) if self.mic else None
        self.last_ai = 0.0
        self.last_voice: dict = {}
        self.frame_stats: dict = {}
        self.ai_reference: dict = {}
        self.ai_backoff = 1
        self.subject: dict | None = None
        self.skipping = ""
        self.cache = config.cache_dir() / "auto"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.cache.chmod(0o700)
        self.pilot = pilot.Pilot(cfg)
        self.governor = actuators.Governor()
        self.registry = self._registry()
        self.faces, self.faces_row = faces.install(cfg)
        self.last_error = ""
        self.report: dict = {}

    def _registry(self) -> actuators.Registry:
        """Everything auto mode can drive, behind one interface (see director.actuators)."""
        registry = actuators.Registry()
        registry.register(
            actuators.ProgramLookActuator(
                connect=self.connect,
                read_look=lambda link, source: current_look(link, source),
                apply_look=lambda link, source, look, target, seconds: apply_look(link, source, look, target, seconds),
                remember=lambda kind, key, value: remember(kind, key, value),
                sources=lambda link: link.camera_sources(),
            )
        )
        registry.register(
            actuators.MicGainActuator(
                source=lambda: self.mic,
                read_gain=lambda source: source_gain_db(source),
                write_gain=lambda source, db: set_source_gain_db(source, db),
                remember=lambda kind, key, value: remember(kind, key, value),
                start_gain=lambda: self.mic_start_db,
            )
        )
        registry.register(
            actuators.VoicePresetActuator(
                presets=lambda: voicechain.preset_names(),
                current=lambda: voicechain.current_preset(),
                load=lambda name: voicechain.load_preset(name),
                remember=lambda kind, key, value: remember(kind, key, value),
            )
        )
        # Hardware: each is registered per its setting and says "unavailable, because ..." until its device shows.
        for actuator in adapters.hardware_actuators(self.cfg, remember, baseline_gain):
            registry.register(actuator)
        return registry

    def connect(self) -> ConsoleLink | None:
        """The running Console (which owns the video engine), or None while it is not up."""
        if self.link:
            return self.link
        self.link = ConsoleLink.find()
        return self.link

    def drop(self) -> None:
        if self.link:
            self.link.close()
        self.link = None

    def _skip(self, reason: str) -> None:
        if reason != self.skipping:
            log("video-paused", reason=reason)
        self.skipping = reason

    # -- one change, whoever asked for it ----------------------------------------------

    def watch(self, actuator_id: str, target: str, current: dict) -> list[str]:
        """Note values the person moved by hand since the director last set them; those are left alone."""
        actuator = self.registry.get(actuator_id)
        eps = actuator.override_eps()
        self.governor.remember_state(actuator_id, target, current)
        moved = self.governor.sync(actuator_id, target, current, eps)
        if moved:
            log("override", actuator=actuator_id, target=target, keys=moved, hold_minutes=int(self.governor.hold // 60))
        return [f"{actuator.label}: {key}" for key in moved]

    def held(self, actuator_id: str, target: str, current: dict) -> list[str]:
        return [key for key in current if self.governor.is_held(actuator_id, target, key)]

    def drive(
        self,
        actuator_id: str,
        target: str,
        current: dict,
        wanted: dict,
        reasons: dict[str, str],
        source: str,
        default_reason: str = "",
        seconds: float = 4.0,
    ) -> list[actuators.Change]:
        """Apply `wanted` to one actuator through the governor: bounded, rate-limited, explained and journalled."""
        actuator = self.registry.get(actuator_id)
        values, changes = self.governor.plan(actuator, target, current, wanted, reasons, source, default_reason)
        if not changes:
            return []
        for change in changes:
            log("change", dry_run=self.dry_run, **change.public())
        if self.dry_run:
            return changes
        try:
            actuator.remember(target, current)
            actuator.apply(target, current, values, seconds)
        except actuators.ActuatorError as exc:
            self.last_error = str(exc)
            log("change-failed", actuator=actuator_id, target=target, error=str(exc), keys=[c.key for c in changes])
            return []
        self.governor.committed(actuator_id, target, values, [c.key for c in changes])
        return changes

    # -- the local loops -----------------------------------------------------------------

    def video_tick(self) -> None:
        link = self.connect()
        if not link:
            return
        try:
            self.pilot.refresh()
            sources = link.camera_sources()
            for index, source in enumerate(sources):
                frame = link.screenshot(source, self.cache / "local.jpg", width=320)
                whole = picture_stats(frame)
                reason = no_picture(whole)
                if reason:
                    self._skip(f"{source}: {reason}")
                    continue
                self.skipping = ""
                if index == 0:
                    self.frame_stats = whole
                stats, band, metering = whole, self.pilot.frame_band(), "frame"
                subject = self.subject if index == 0 else None
                if subject and time.monotonic() - subject["time"] < SUBJECT_MAX_AGE:
                    face = picture_stats(frame, subject["box"])
                    if "YAVG" in face:
                        stats, metering = face, "face"
                        if subject["target"] is not None:
                            band = (subject["target"] - FACE_BAND, subject["target"] + FACE_BAND)
                        else:
                            band = self.pilot.face_band()
                look = current_look(link, source)
                self.watch("program-look", source, look)
                target = exposure_target(stats, look, band)
                if target and index == 0 and self.canon_exposure(look, target, stats, band, metering):
                    continue
                if target:
                    self._expose(link, source, look, target, stats, band, metering)
                elif index == 0 and config.truthy(self.cfg["STREAMSTACK_AUTO_PERSONA_LOOK"]):
                    self._ease(source, look)
        except Exception as exc:
            log("video-error", error=str(exc))
            self.drop()

    def _expose(
        self, link: ConsoleLink, source: str, look: dict, target: dict, stats: dict, band: tuple, metering: str
    ) -> None:
        keys = [k for k in ("brightness", "gamma") if target.get(k) != look.get(k)]
        if all(self.governor.is_held("program-look", source, k) for k in keys):
            return
        average = stats.get("YAVG", 0.0)
        side = "above" if average > band[1] else "below"
        reason = (
            f"The {metering} was {average:.0f} luma, {side} the {band[0]:.0f}-{band[1]:.0f} band for "
            f"{self.pilot.persona['name']}"
        )
        log(
            "exposure",
            source=source,
            metering=metering,
            stats=stats,
            band=band,
            before=look,
            after=target,
            reason=reason,
            dry_run=self.dry_run,
        )
        if self.dry_run:
            return
        wanted = {k: target[k] for k in keys}
        values, changes = self.governor.plan(
            self.registry.get("program-look"), source, look, wanted, {}, "local", reason
        )
        if not changes:
            return
        remember("look", source, look)
        ramp(link, source, look, values, seconds=1.5, steps=6)
        self.governor.committed("program-look", source, values, [c.key for c in changes])

    def canon(self) -> tuple[devices.CanonActuator, str, dict] | None:
        """(the Canon actuator, its target, the controls the director may change now), or None without a usable one."""
        actuator = self.registry.find("canon-exposure")
        if not isinstance(actuator, devices.CanonActuator) or not actuator.availability().ok:
            return None
        targets = actuator.targets()
        current = actuator.read(targets[0]) if targets else {}
        return (actuator, targets[0], current) if targets and current else None

    def canon_exposure(self, look: dict, target: dict, stats: dict, band: tuple, metering: str) -> bool:
        """Let the camera's own exposure compensation do the exposing; True when it took this pass.

        The Canon is the first choice because it works on the sensor, before the picture is compressed: raising the
        exposure in the picture afterwards lifts noise too. The program look finishes the job when the camera is at its
        limit, locked, or the person set it by hand (see the governor's hold), and always when there is no Canon.
        """
        found = self.canon()
        if found is None or "exposure_comp" not in found[2]:
            return False
        actuator, name, current = found
        self.watch("canon-exposure", name, current)
        direction = 1 if (target["gamma"] - look["gamma"]) + (target["brightness"] - look["brightness"]) > 0 else -1
        if self.governor.is_held("canon-exposure", name, "exposure_comp") or not actuator.headroom(current, direction):
            return False
        average = stats.get("YAVG", 0.0)
        side = "above" if average > band[1] else "below"
        reason = (
            f"The {metering} was {average:.0f} luma, {side} the {band[0]:.0f}-{band[1]:.0f} band for "
            f"{self.pilot.persona['name']}; the camera's own exposure compensation moves first"
        )
        step = actuator.param("exposure_comp").step or 0.34
        self.last_error = ""
        self.drive(
            "canon-exposure",
            name,
            current,
            {"exposure_comp": current["exposure_comp"] + direction * step},
            {},
            "local",
            reason,
        )
        return not self.last_error

    def canon_white_balance(self, picture: dict) -> None:
        """One step of the camera's Kelvin towards the persona's colour temperature, judged on the measured picture."""
        found = self.canon()
        warm = picture.get("warm_index")
        if found is None or "kelvin" not in found[2] or not isinstance(warm, (int, float)):
            return
        actuator, name, current = found
        self.watch("canon-exposure", name, current)
        wanted_k = float(self.pilot.persona["picture"]["warmth_k"])
        gap = warm - judge.expected_warm_index(wanted_k)
        if abs(gap) <= judge.WARM_TOLERANCE:
            return
        step = actuator.param("kelvin").step or 400.0
        # A warmer picture than wanted: tell the camera the light is cooler (a lower Kelvin), and the other way round.
        direction = -1 if gap > 0 else 1
        side = "warmer" if gap > 0 else "cooler"
        reason = (
            f"The picture read {side} than {self.pilot.persona['name']}'s {wanted_k:.0f} K "
            f"(warm index {warm:+.2f}, wanted about {judge.expected_warm_index(wanted_k):+.2f})"
        )
        self.drive(
            "canon-exposure", name, current, {"kelvin": current["kelvin"] + direction * step}, {}, "local", reason
        )

    def light_tick(self) -> None:
        """Ease the Litra lights towards the persona's light for this hour (brightness and colour temperature)."""
        actuator = self.registry.find("litra")
        if not isinstance(actuator, devices.LitraActuator) or not actuator.availability().ok:
            return
        self.pilot.refresh()
        goal = self.pilot.light_goal()
        if goal is None:
            return
        fraction, kelvin, origin, why = goal
        readings = {t: r for t in actuator.targets() if (r := actuator.read(t))}
        for light, current in readings.items():
            self.watch("litra", light, current)
        for light, wanted in actuator.plan_group(fraction, kelvin, readings).items():
            current = readings[light]
            wanted = {
                k: v
                for k, v in wanted.items()
                if abs(v - current[k]) >= (devices.LIGHT_EPS_LUMEN if k == "lumen" else devices.LIGHT_EPS_KELVIN)
            }
            if wanted:
                self.drive("litra", light, current, wanted, {}, origin, f"Easing towards {why}")

    def _ease(self, source: str, look: dict) -> None:
        """A small step towards the persona's look for this hour, when nothing needs exposing."""
        wanted, origin, reason = self.pilot.ease(look)
        if "warmth" in wanted:
            found = self.canon()
            if found is not None and "kelvin" in found[2]:
                del wanted["warmth"]  # the camera's own Kelvin is doing the white balance
        if wanted:
            self.drive("program-look", source, look, wanted, {}, origin, reason, seconds=2.0)

    def audio_tick(self) -> None:
        if not self.mic:
            return
        stats = voice_stats(self.mic)
        self.last_voice = stats
        if stats and self.hardware_gain(stats):
            return
        gain = source_gain_db(self.mic)
        if not stats or gain is None:
            return
        self.watch("mic-gain", self.mic, {"gain_db": gain})
        if self.governor.is_held("mic-gain", self.mic, "gain_db"):
            return
        change = gain_change(stats)
        if not change:
            return
        base = self.mic_start_db if self.mic_start_db is not None else gain
        new = max(base + MIC_GAIN_DB[0], min(base + MIC_GAIN_DB[1], gain + change))
        if abs(new - gain) < 0.25:
            return
        reason = (
            "The microphone was clipping."
            if stats.get("peak_db", -90) > -1.0
            else "Speech was too loud."
            if change < 0
            else "Speech was quiet against the room."
        )
        self.drive("mic-gain", self.mic, {"gain_db": gain}, {"gain_db": new}, {}, "local", reason)

    def hardware_gain(self, stats: dict) -> bool:
        """Level the voice with the interface's own preamp gain; False when there is none (use the software gain)."""
        actuator = self.registry.find("interface-gain")
        if not isinstance(actuator, devices.InterfaceGainActuator) or not actuator.availability().ok:
            return False
        targets = actuator.targets()
        current = actuator.read(targets[0]) if targets else {}
        if not current:
            return False
        name, gain = targets[0], current["gain_db"]
        self.watch("interface-gain", name, current)
        if self.governor.is_held("interface-gain", name, "gain_db"):
            return True  # the person has a hand on the preamp; the software gain stays out of it too
        change = gain_change(stats)
        window = actuator.window(name)
        if not change or window is None:
            return True
        new = max(window[0], min(window[1], gain + change))
        if abs(new - gain) < 0.25:
            return True
        reason = (
            "The microphone was clipping."
            if stats.get("peak_db", -90) > -1.0
            else "Speech was too loud."
            if change < 0
            else "Speech was quiet against the room."
        )
        self.last_error = ""
        self.drive("interface-gain", name, current, {"gain_db": new}, {}, "local", reason)
        return not self.last_error

    # -- looking and listening for a Claude pass ----------------------------------------------

    def listen(self) -> tuple[dict | None, str]:
        """Record a short clip, reduce it to features on this machine and delete it. (features, why-not)."""
        if not self.mic:
            return None, "no microphone is being watched"
        seconds = max(3.0, min(15.0, float(self.cfg["STREAMSTACK_AUTO_CLIP_SECONDS"] or 6)))
        with tempfile.TemporaryDirectory(prefix="clip-", dir=self.cache) as tmp:
            clip = Path(tmp) / "clip.wav"
            if not system.record(self.mic, seconds, str(clip)):
                return None, "the microphone gave no audio"
            try:
                return audiofeatures.analyze_wav(clip), ""
            except audiofeatures.AudioAnalysisError as exc:
                return None, f"the clip could not be analysed ({exc})"

    def ai_due(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        elapsed = now - self.last_ai
        interval = float(self.cfg["STREAMSTACK_AUTO_AI_INTERVAL"]) * self.ai_backoff
        if elapsed >= interval:
            return True
        # Lights, camera or person changed: look again soon instead of waiting out a backoff.
        return elapsed >= AI_MIN_GAP and picture_changed(self.ai_reference, self.frame_stats)

    def ai_tick(self, force: bool = False) -> dict | None:
        asks_claude = self.cfg["STREAMSTACK_AUTO_BACKEND"] != "local"
        if not force and not self.ai_due():
            return None
        self.last_ai = time.monotonic()
        link = self.connect()
        if not link:
            log("ai-skipped", reason="the Console is not running (streamstack console)")
            return None
        try:
            sources = link.camera_sources()
            if not sources:
                log("ai-skipped", reason="the video engine has no camera on air")
                return None
            source = sources[0]
            self.pilot.refresh()
            with tempfile.TemporaryDirectory(prefix="ai-", dir=self.cache) as tmp:
                seen = observe.observe(
                    source,
                    lambda path: link.screenshot(source, path),
                    Path(tmp),
                    observe.burst_size(self.cfg),
                    observe.burst_spacing(self.cfg),
                    sleep=lambda seconds: time.sleep(seconds),
                )
                frame = seen.middle
                stats = picture_stats(frame)
                reason = no_picture(stats)
                if reason:
                    log("ai-skipped", reason=f"{source}: {reason}")
                    return None
                look = current_look(link, source)
                self.watch("program-look", source, look)
                held = self.held("program-look", source, look)
                audio, audio_reason = self.listen()
                findings = self.assess(seen, audio, audio_reason)
                self.canon_white_balance(judge.aggregate_frames((seen.analysis or {}).get("frames", [])))
                self.keep(source, look, held)
                presets = voicechain.preset_names()
                preset = voicechain.current_preset() or ""
                context = self.context(seen, source, stats, look, held, findings, audio, audio_reason, presets, preset)
                status.publish_sheet(seen.sheet)
                self.report = self.snapshot(seen, findings, audio, audio_reason, held)
                if not asks_claude:
                    self.subject = self.local_subject(seen, source, None)
                    self.finish_report(None, [])
                    return None
                started = time.monotonic()
                proposal = ask_claude(self.cfg, seen.sheet, context)
                shutil.copy2(seen.sheet, self.cache / "last-ai.jpg")
            return self.apply_proposal(proposal, link, source, look, stats, presets, preset, started, seen)
        except Exception as exc:
            log("ai-error", error=str(exc))
            notify(self.cfg, "Auto-cinematographer paused a pass", str(exc)[:200])
            self.drop()
            return None

    def local_subject(self, seen: observe.Observation, source: str, claude: dict | None) -> dict | None:
        """The face to meter: the face detector's box when it found one, else Claude's; Claude keeps the luma target."""
        found = judge.aggregate_frames((seen.analysis or {}).get("frames", [])).get("subject")
        box = found["box"] if isinstance(found, dict) and found.get("source") == "yunet" else None
        if box is None and claude is None:
            return None
        chosen = {"box": box or claude["box"], "target": claude["target"] if claude else None}
        return dict(chosen, time=time.monotonic(), source=source)

    def assess(self, seen: observe.Observation, audio: dict | None, audio_reason: str) -> list[dict]:
        """Local findings (picture and voice) against the persona and the hour."""
        persona = self.pilot.persona
        frames = (seen.analysis or {}).get("frames", [])
        picture = judge.aggregate_frames(frames)
        burst = (seen.analysis or {}).get("burst", {})
        sun = self.pilot.sun_public()
        return judge.picture_findings(picture, burst, persona, sun) + judge.voice_findings(audio, persona, audio_reason)

    def keep(self, source: str, look: dict, held: list[str]) -> None:
        """What is in place at the start of a pass is what the person kept since the last one; learn it."""
        if self.dry_run:
            return
        gain = source_gain_db(self.mic) if self.mic else None
        try:
            self.pilot.remember_kept(
                look,
                bool(held),
                (self.mic, gain) if self.mic and gain is not None else None,
                self.kept_light(),
            )
        except OSError as exc:
            log("learn-error", error=str(exc))

    def kept_light(self) -> tuple[float, float] | None:
        """The (brightness fraction, kelvin) the person left the lights at, only when they moved them by hand.

        What the director set itself is not "kept": learning it would make the first night pass settle on whatever
        the lights were at and never raise them for the dark hours.
        """
        actuator = self.registry.find("litra")
        if not isinstance(actuator, devices.LitraActuator) or not actuator.availability().ok:
            return None
        fractions: list[float] = []
        kelvins: list[float] = []
        for light in actuator.targets():
            current = actuator.read(light)
            if not current or not self.held("litra", light, current):
                continue
            fraction = actuator.fraction(light, current["lumen"])
            if fraction is not None:
                fractions.append(fraction)
                kelvins.append(current["kelvin"])
        return (sum(fractions) / len(fractions), sum(kelvins) / len(kelvins)) if fractions else None

    def context(
        self,
        seen: observe.Observation,
        source: str,
        stats: dict,
        look: dict,
        held: list[str],
        findings: list[dict],
        audio: dict | None,
        audio_reason: str,
        presets: list[str],
        preset: str,
    ) -> dict:
        analysis = seen.analysis or {}
        text = prompt.build(
            image=seen.sheet,
            source=source,
            frames=len(seen.stills),
            spacing=seen.spacing,
            persona=self.pilot.persona,
            sun=self.pilot.sun_public(),
            picture={k: v for k, v in judge.aggregate_frames(analysis.get("frames", [])).items() if k != "histogram"}
            or stats,
            burst=analysis.get("burst", {}),
            findings=findings,
            audio=audio,
            audio_reason=audio_reason,
            look=look,
            presets=presets,
            preset=preset or None,
            learned=self.pilot.learned(),
            held=held,
        )
        return {
            "prompt": text,
            "schema": prompt.schema(PROPOSAL_SCHEMA),
            "source": source,
            "picture": json.dumps(stats),
            "look": json.dumps(look),
            "limits": json.dumps(LIMITS),
            "steps": json.dumps(AI_STEP),
            "voice": json.dumps(audio or self.last_voice or "not measured"),
            "preset": preset or "unknown",
            "presets": json.dumps(presets),
        }

    def apply_proposal(
        self,
        proposal: dict,
        link: ConsoleLink,
        source: str,
        look: dict,
        stats: dict,
        presets: list[str],
        preset: str,
        started: float,
        seen: observe.Observation,
    ) -> dict:
        target = bounded(proposal, look)
        confidence = float(proposal.get("confidence", 0) or 0)
        notes = str(proposal.get("notes", ""))[:300]
        reasons = prompt.reasons_from(proposal)
        assessment = prompt.assessment_from(proposal)
        subject = subject_from(proposal)
        self.subject = self.local_subject(seen, source, subject)
        self.ai_reference = stats
        log(
            "ai",
            source=source,
            stats=stats,
            before=look,
            after=target,
            proposal=proposal,
            confidence=confidence,
            seconds=round(time.monotonic() - started, 1),
            persona=self.pilot.persona["id"],
            slot=self.pilot.slot,
            frames=len(seen.stills),
            assessment=assessment,
            reasons=reasons,
            dry_run=self.dry_run,
        )
        changes: list[actuators.Change] = []
        if self.dry_run or confidence < 0.5:
            self.ai_backoff = min(AI_MAX_BACKOFF, self.ai_backoff * 2)
        else:
            wanted = {k: v for k, v in target.items() if v != look.get(k)}
            changes += self.drive("program-look", source, look, wanted, reasons, "claude", "Claude adjusted the look")
            self.pilot.claude_set.update(c.key for c in changes)
            name = (proposal.get("audio") or {}).get("voice_preset")
            if name and name in presets and name != preset and confidence >= 0.7:
                voice = self.drive(
                    "voice-preset",
                    "input",
                    {"voice_preset": preset or None},
                    {"voice_preset": name},
                    {"voice_preset": reasons.get("voice_preset", reasons.get("audio", ""))},
                    "claude",
                    "Claude chose another voice preset",
                )
                if voice:
                    notes += f" (voice: {name})"
                changes += voice
            # Nothing to change in a still picture: look less often.
            self.ai_backoff = 1 if changes else min(AI_MAX_BACKOFF, self.ai_backoff * 2)
            if notes:
                notify(self.cfg, "Auto-cinematographer", notes)
        self.finish_report(
            {"confidence": confidence, "notes": notes, "assessment": assessment, "reasons": reasons}, changes
        )
        return proposal

    # -- the status the Console reads -----------------------------------------------------------

    def snapshot(
        self, seen: observe.Observation, findings: list[dict], audio: dict | None, audio_reason: str, held: list[str]
    ) -> dict:
        analysis = seen.analysis or {}
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
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
            "f0_low_hz",
            "f0_high_hz",
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
        return {
            "time": now,
            "persona": self.pilot.persona["id"],
            "slot": self.pilot.slot,
            "observation": {
                "source": seen.source,
                "frames": len(seen.stills),
                "spacing": seen.spacing,
                "picture": judge.aggregate_frames(analysis.get("frames", [])),
                "burst": analysis.get("burst", {}),
                "problems": seen.problems,
            },
            "audio": {"features": {k: audio[k] for k in keep if k in audio} if audio else None, "reason": audio_reason},
            "findings": findings,
            "held": held,
            "learned": self.pilot.learned(),
        }

    def finish_report(self, answer: dict | None, changes: list[actuators.Change]) -> None:
        """Write what this pass saw, said and changed to director-status.json for the Console."""
        if not self.report:
            return
        report = dict(self.report)
        report["claude"] = answer
        report["changes"] = [c.public() for c in changes]
        report["actuators"] = [*self.registry.describe(), self.faces.describe() if self.faces else self.faces_row]
        try:
            status.write(report)
        except OSError as exc:
            log("status-error", error=str(exc))


def gain_change(stats: dict[str, float]) -> float:
    """dB to move the mic gain for one take: clipping and loud speech down, quiet speech up."""
    peak, loud = stats.get("peak_db", -90), stats.get("rms_peak_db", -90)
    # Speech comes in bursts well above the take's average; steady room
    # noise does not, and must never be "levelled up".
    speaking = loud - stats.get("rms_db", loud) >= 6
    if peak > -1.0:
        return -2.0
    if loud > -12:
        return -1.0
    if speaking and -45 < loud < -26:
        return 1.0
    return 0.0


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    cfg = config.load_settings()
    auto = Auto(cfg, dry_run=args.dry_run)
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    log("start", backend=cfg["STREAMSTACK_AUTO_BACKEND"], microphone=auto.mic, dry_run=args.dry_run)
    interval = max(5.0, float(cfg["STREAMSTACK_AUTO_INTERVAL"]))
    # The first Claude pass waits one local interval, so exposure settles first.
    auto.last_ai = time.monotonic() - float(cfg["STREAMSTACK_AUTO_AI_INTERVAL"]) + interval
    while not stop["now"]:
        started = time.monotonic()
        if config.truthy(cfg["STREAMSTACK_AUTO_VIDEO"]):
            auto.video_tick()
        if config.truthy(cfg["STREAMSTACK_AUTO_AUDIO"]):
            auto.audio_tick()
        auto.light_tick()
        auto.ai_tick()
        while not stop["now"] and time.monotonic() - started < interval:
            time.sleep(0.5)
    auto.drop()
    log("stop")
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    cfg = config.load_settings()
    if ConsoleLink.find() is None:
        print("Start the Console (streamstack console) so auto mode can see and grade the program picture, then retry.")
        return 1
    auto = Auto(cfg, dry_run=args.dry_run)
    if config.truthy(cfg["STREAMSTACK_AUTO_AUDIO"]):
        print("Measuring 4 s of the microphone - talk normally...")
        auto.audio_tick()
    print("Asking Claude about the picture...")
    proposal = auto.ai_tick(force=True)
    auto.drop()
    if proposal is None:
        print(f"No proposal; see {config.state_dir() / 'auto.jsonl'}")
        return 1
    print(json.dumps(proposal, indent=2))
    return 0


def restore_hardware(baseline: dict, cfg: dict) -> int:
    """Put the camera, lights and preamp gain back to what they were before auto mode (problems found)."""
    problems = 0
    for actuator in adapters.hardware_actuators(cfg, remember, baseline_gain):
        saved = baseline.get(actuator.baseline_kind) or {}
        for target, value in saved.items():
            try:
                actuator.restore(target, value)
            except actuators.ActuatorError as exc:
                print(f"{actuator.label} ({target}): not restored: {exc}")
                problems += 1
            else:
                print(f"{actuator.label} ({target}): restored")
    return problems


def cmd_reset(args: argparse.Namespace) -> int:
    """Ramp back to the look, mic gain and voice preset from before auto mode changed them."""
    baseline = load_baseline()
    if not baseline:
        print("Nothing to undo: auto mode has not changed anything since the last reset.")
        return 0
    if system.user_unit_active("streamstack-auto.service"):
        print("Auto mode is on and would change things again; run `streamstack auto off` first.")
        return 1
    problems = 0
    if baseline.get("look"):
        link = ConsoleLink.find()
        if link is None:
            print("Start the Console (streamstack console) to undo the picture changes.")
            problems += 1
        else:
            for source, look in baseline["look"].items():
                try:
                    apply_look(link, source, current_look(link, source), look, seconds=2.0)
                    print(f"{source}: look restored")
                except RuntimeError as exc:
                    print(f"{source}: {exc}")
                    problems += 1
            link.close()
    for source, gain in baseline.get("mic", {}).items():
        set_source_gain_db(source, float(gain))
        print(f"{source}: input gain back to {float(gain):+.1f} dB")
    problems += restore_hardware(baseline, config.load_settings())
    preset = baseline.get("preset", {}).get("input")
    if preset and voicechain.load_preset(preset):
        print(f"voice preset back to {preset}")
    if not problems:
        baseline_path().unlink(missing_ok=True)
    log("reset", restored=baseline, complete=not problems)
    return 1 if problems else 0


def cmd_check(args: argparse.Namespace) -> int:
    """Can auto mode reach the Console and Claude? Prints one line per piece."""
    cfg = config.load_settings()
    problems = 0
    reachable = ConsoleLink.find() is not None
    print("Console: reachable" if reachable else "Console: not running (streamstack console)")
    problems += not reachable
    backend = cfg["STREAMSTACK_AUTO_BACKEND"]
    if backend == "claude-cli":
        claude = claude_cli()
        try:
            result = (
                subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=30, check=False)
                if claude
                else None
            )
        except OSError as exc:
            result = subprocess.CompletedProcess([claude], 1, "", f"{claude}: {exc.strerror} (reinstall Claude Code)")
        ok = bool(result and result.returncode == 0)
        output = (result.stdout or result.stderr).strip() if result else ""
        detail = output.splitlines()[0] if output else "not installed"
        print(f"Claude CLI: {'ok ' + detail if ok else 'not working: ' + detail}")
        problems += not ok
    elif backend == "api":
        try:
            api_key()
            print("Anthropic API key: found")
        except RuntimeError as exc:
            print(f"Anthropic API key: {exc}")
            problems += 1
    else:
        print("Claude: off (local loops only)")
    for actuator in adapters.hardware_actuators(cfg, remember, baseline_gain):
        state = actuator.availability()
        print(f"{actuator.label}: {'ready' if state.ok else 'not available - ' + state.reason}")
    _, face_row = faces.install(cfg)
    print(f"Face finder: {'YuNet' if face_row['available'] else face_row['reason']}")
    mic = system.microphone(cfg)
    print(f"Microphone: {mic or 'none found'}")
    problems += config.truthy(cfg["STREAMSTACK_AUTO_AUDIO"]) and not mic
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="streamstack-auto", description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the loops until stopped (the streamstack-auto user service)")
    run.add_argument("--dry-run", action="store_true", help="measure and log, change nothing")
    run.set_defaults(func=cmd_run)
    once = sub.add_parser("once", help="one microphone measurement and one Claude pass now")
    once.add_argument("--dry-run", action="store_true", help="show the proposal, change nothing")
    once.set_defaults(func=cmd_once)
    sub.add_parser("reset", help="undo auto mode's changes to the look, mic gain and voice preset").set_defaults(
        func=cmd_reset
    )
    sub.add_parser("check", help="check the Console, the Claude backend and the microphone").set_defaults(
        func=cmd_check
    )
    args = parser.parse_args(argv)
    crash.install("auto")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
