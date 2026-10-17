"""StreamStack auto mode: an auto-cinematographer and auto-audio engineer.

Off until turned on (`streamstack auto on`, or the panel menu). While on it
runs two loops on the graded studio camera:

  every STREAMSTACK_AUTO_INTERVAL seconds (local, no network)
    video  measure the picture through obs-websocket and nudge the
           "Lighting & Color" filter's gamma, then brightness, towards a good
           exposure. Once Claude has located the face it meters the face
           alone (spot metering), towards the luma Claude chose for that skin
           tone, and never clips it.
    audio  measure 4 s of the raw microphone and nudge its input gain so
           speech peaks sit around -24 to -12 dBFS and never clip; steady
           room noise never raises the gain.

  every STREAMSTACK_AUTO_AI_INTERVAL seconds (Claude)
    send one still of the camera, the measured picture and voice statistics
    and the current settings, and apply the bounded changes Claude proposes:
    contrast, saturation, warmth (white balance), the cinematic LUT,
    sharpening and the voice preset. Audio never leaves the host, only its
    statistics. When Claude changes nothing and the picture holds still, the
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
import time
import urllib.request
from pathlib import Path

from streamstack import config, system
from streamstack.obs import COLOR_FILTER, LUT_FILTER, OBS, SHARPEN_FILTER, ensure_websocket

API_MODEL = "claude-sonnet-5-5"
LOG_MAX_BYTES = 2_000_000

# Hard limits for every setting auto mode may touch, whoever proposes it.
LIMITS = {
    "brightness": (-0.15, 0.2),
    "contrast": (-0.3, 0.4),
    "gamma": (-0.5, 0.6),
    "saturation": (-0.3, 0.35),
    "warmth": (-0.5, 0.5),
    "lut_amount": (0.0, 0.6),
}
# Largest change one Claude pass may make.
AI_STEP = {"brightness": 0.08, "contrast": 0.15, "gamma": 0.2, "saturation": 0.15, "warmth": 0.25, "lut_amount": 0.2}
COLOR_KEYS = ("brightness", "contrast", "gamma", "saturation", "warmth")
# Relative to the gain auto mode found at start; boosting a quiet mic in
# software also raises its noise, so the hardware gain knob comes first.
MIC_GAIN_DB = (-12.0, 6.0)
# Whole-frame exposure band (8-bit luma average) when no face is known.
FRAME_BAND = (100.0, 140.0)
# Face luma targets Claude may choose, and the band kept around them.
FACE_TARGET = (70.0, 180.0)
FACE_BAND = 15.0
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


def warmth_to_multiply(warmth: float) -> int:
    """Warmth -1..1 as an OBS colour multiply (0xBBGGRR): warm lifts red, lowers blue."""
    red = 1.0 if warmth >= 0 else 1.0 + warmth * 0.12
    blue = 1.0 if warmth <= 0 else 1.0 - warmth * 0.12
    green = 1.0 - abs(warmth) * 0.02
    return (round(blue * 255) << 16) | (round(green * 255) << 8) | round(red * 255)


def multiply_to_warmth(value: int | None) -> float:
    if value is None:
        return 0.0
    red, blue = value & 0xFF, (value >> 16) & 0xFF
    if blue < 255:
        return round((1 - blue / 255) / 0.12, 3)
    if red < 255:
        return round(-(1 - red / 255) / 0.12, 3)
    return 0.0


def current_look(obs: OBS, source: str) -> dict:
    _, color = obs.filter_settings(source, COLOR_FILTER)
    look = {
        "brightness": float(color.get("brightness", 0.0)),
        "contrast": float(color.get("contrast", 0.0)),
        "gamma": float(color.get("gamma", 0.0)),
        "saturation": float(color.get("saturation", 0.0)),
        "warmth": multiply_to_warmth(color.get("color_multiply")),
    }
    try:
        enabled, lut = obs.filter_settings(source, LUT_FILTER)
        look.update(lut_enabled=enabled, lut_amount=float(lut.get("clut_amount", 1.0)))
    except RuntimeError:
        pass
    try:
        look["sharpen_enabled"] = obs.filter_settings(source, SHARPEN_FILTER)[0]
    except RuntimeError:
        pass
    return look


def ramp(obs: OBS, source: str, start: dict, target: dict, seconds: float = 3.0, steps: int = 12) -> None:
    """Move the colour filter from start to target in small steps, so no frame jumps."""
    keys = [k for k in COLOR_KEYS if k in target and k in start]
    if not any(target[k] != start[k] for k in keys):
        return
    for step in range(1, steps + 1):
        mix = step / steps
        mix = mix * mix * (3 - 2 * mix)  # smoothstep
        values = {}
        for key in keys:
            value = start[key] + (target[key] - start[key]) * mix
            if key == "warmth":
                values["color_multiply"] = warmth_to_multiply(value)
            else:
                values[key] = round(value, 4)
        obs.set_filter(source, COLOR_FILTER, values)
        time.sleep(seconds / steps)


def apply_look(obs: OBS, source: str, look: dict, target: dict, seconds: float = 4.0) -> bool:
    """Ramp the colour, then switch filters and the LUT amount. True when anything changed."""
    changed = any(target.get(k) != look.get(k) for k in target)
    ramp(obs, source, look, target, seconds=seconds, steps=max(4, int(seconds * 4)))
    for key, name in (("lut_enabled", LUT_FILTER), ("sharpen_enabled", SHARPEN_FILTER)):
        if key in target and target[key] != look.get(key):
            obs.enable_filter(source, name, target[key])
    if "lut_amount" in target and target["lut_amount"] != look.get("lut_amount"):
        obs.set_filter(source, LUT_FILTER, {"clut_amount": target["lut_amount"]})
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
    try:
        return json.loads(baseline_path().read_text())
    except (OSError, ValueError):
        return {}


def remember(kind: str, key: str, value) -> None:
    """Record the value from before auto mode's first change (never overwritten)."""
    baseline = load_baseline()
    section = baseline.setdefault(kind, {})
    if key in section:
        return
    section[key] = value
    path = baseline_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(baseline, indent=2))
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

PROMPT = """You are the cinematographer and audio engineer for a live video call or stream.
The still {image} is the graded camera picture viewers see right now ({source}).

Measured picture (ffmpeg signalstats, 8-bit, whole frame): {picture}
Current "Lighting & Color" settings (OBS color correction): {look}
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
    backend = cfg["STREAMSTACK_AUTO_BACKEND"]
    prompt = PROMPT.format(image=image, **context)
    if backend == "api":
        return _ask_api(cfg, image, prompt)
    if backend == "claude-cli":
        return _ask_cli(cfg, image, prompt)
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


def cli_argv(claude: str, image: Path, prompt: str, model: str = "") -> list[str]:
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
        json.dumps(PROPOSAL_SCHEMA),
        "--tools",
        "Read",
        "--restricted",
        "--strict-mcp-config",
        "--no-session-persistence",
    ]
    if model:
        argv += ["--model", model]
    return argv


def _ask_cli(cfg: dict, image: Path, prompt: str) -> dict:
    claude = claude_cli()
    if not claude:
        raise RuntimeError("the claude CLI is not installed; install Claude Code and sign in, or use the api backend")
    argv = cli_argv(claude, image, f"Read the image {image}, then answer.\n\n{prompt}", cfg["STREAMSTACK_AUTO_MODEL"])
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


def api_request(cfg: dict, image: Path, prompt: str) -> dict:
    return {
        "model": cfg["STREAMSTACK_AUTO_MODEL"] or API_MODEL,
        "max_tokens": 1024,
        "tools": [
            {
                "name": "propose_adjustments",
                "description": "Propose bounded picture and voice adjustments for the live call.",
                "input_schema": PROPOSAL_SCHEMA,
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


def _ask_api(cfg: dict, image: Path, prompt: str) -> dict:
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(api_request(cfg, image, prompt)).encode(),
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
        self.obs: OBS | None = None
        self.mic = system.microphone(cfg) if config.truthy(cfg["STREAMSTACK_AUTO_AUDIO"]) else ""
        self.mic_start_db = source_gain_db(self.mic) if self.mic else None
        self.last_ai = 0.0
        self.last_preset_change = 0.0
        self.last_voice: dict = {}
        self.frame_stats: dict = {}
        self.ai_reference: dict = {}
        self.ai_backoff = 1
        self.subject: dict | None = None
        self.skipping = ""
        self.cache = config.cache_dir() / "auto"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.cache.chmod(0o700)

    def connect(self) -> OBS | None:
        if self.obs:
            return self.obs
        if not system.running("obs"):
            return None
        try:
            self.obs = OBS()
        except Exception as exc:
            log("obs-unavailable", error=str(exc))
            self.obs = None
        return self.obs

    def drop(self) -> None:
        if self.obs:
            self.obs.close()
        self.obs = None

    def _skip(self, reason: str) -> None:
        if reason != self.skipping:
            log("video-paused", reason=reason)
        self.skipping = reason

    def video_tick(self) -> None:
        obs = self.connect()
        if not obs:
            return
        try:
            sources = obs.camera_sources()
            for index, source in enumerate(sources):
                frame = obs.screenshot(source, self.cache / "local.jpg", width=320)
                whole = picture_stats(frame)
                reason = no_picture(whole)
                if reason:
                    self._skip(f"{source}: {reason}")
                    continue
                self.skipping = ""
                if index == 0:
                    self.frame_stats = whole
                stats, band, metering = whole, FRAME_BAND, "frame"
                subject = self.subject if index == 0 else None
                if subject and time.monotonic() - subject["time"] < SUBJECT_MAX_AGE:
                    face = picture_stats(frame, subject["box"])
                    if "YAVG" in face:
                        stats, metering = face, "face"
                        if subject["target"] is not None:
                            band = (subject["target"] - FACE_BAND, subject["target"] + FACE_BAND)
                look = current_look(obs, source)
                target = exposure_target(stats, look, band)
                if target:
                    log(
                        "exposure",
                        source=source,
                        metering=metering,
                        stats=stats,
                        band=band,
                        before=look,
                        after=target,
                        dry_run=self.dry_run,
                    )
                    if not self.dry_run:
                        remember("look", source, look)
                        ramp(obs, source, look, target, seconds=1.5, steps=6)
        except Exception as exc:
            log("video-error", error=str(exc))
            self.drop()

    def audio_tick(self) -> None:
        if not self.mic:
            return
        stats = voice_stats(self.mic)
        self.last_voice = stats
        gain = source_gain_db(self.mic)
        if not stats or gain is None:
            return
        change = gain_change(stats)
        if not change:
            return
        base = self.mic_start_db if self.mic_start_db is not None else gain
        new = max(base + MIC_GAIN_DB[0], min(base + MIC_GAIN_DB[1], gain + change))
        if abs(new - gain) < 0.25:
            return
        log("mic-gain", source=self.mic, stats=stats, before_db=gain, after_db=new, dry_run=self.dry_run)
        if not self.dry_run:
            remember("mic", self.mic, gain if self.mic_start_db is None else self.mic_start_db)
            set_source_gain_db(self.mic, new)

    def ai_due(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        elapsed = now - self.last_ai
        interval = float(self.cfg["STREAMSTACK_AUTO_AI_INTERVAL"]) * self.ai_backoff
        if elapsed >= interval:
            return True
        # Lights, camera or person changed: look again soon instead of waiting out a backoff.
        return elapsed >= AI_MIN_GAP and picture_changed(self.ai_reference, self.frame_stats)

    def ai_tick(self, force: bool = False) -> dict | None:
        if self.cfg["STREAMSTACK_AUTO_BACKEND"] == "local":
            return None
        if not force and not self.ai_due():
            return None
        self.last_ai = time.monotonic()
        obs = self.connect()
        if not obs:
            log("ai-skipped", reason="OBS is not running")
            return None
        try:
            sources = obs.camera_sources()
            if not sources:
                log("ai-skipped", reason="no camera with the StreamStack filters in the program scene")
                return None
            source = sources[0]
            with tempfile.TemporaryDirectory(prefix="ai-", dir=self.cache) as tmp:
                frame = obs.screenshot(source, Path(tmp) / "frame.jpg")
                stats = picture_stats(frame)
                reason = no_picture(stats)
                if reason:
                    log("ai-skipped", reason=f"{source}: {reason}")
                    return None
                look = current_look(obs, source)
                presets = system.ee_presets()
                preset = system.easyeffects_preset()
                context = {
                    "source": source,
                    "picture": json.dumps(stats),
                    "look": json.dumps(look),
                    "limits": json.dumps(LIMITS),
                    "steps": json.dumps(AI_STEP),
                    "voice": json.dumps(self.last_voice or "not measured"),
                    "preset": preset or "unknown",
                    "presets": json.dumps(presets),
                }
                started = time.monotonic()
                proposal = ask_claude(self.cfg, frame, context)
                shutil.copy2(frame, self.cache / "last-ai.jpg")
            target = bounded(proposal, look)
            confidence = float(proposal.get("confidence", 0) or 0)
            notes = str(proposal.get("notes", ""))[:300]
            subject = subject_from(proposal)
            self.subject = dict(subject, time=time.monotonic(), source=source) if subject else None
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
                dry_run=self.dry_run,
            )
            if self.dry_run or confidence < 0.5:
                self.ai_backoff = min(AI_MAX_BACKOFF, self.ai_backoff * 2)
                return proposal
            if any(target.get(k) != look.get(k) for k in target):
                remember("look", source, look)
            changed = apply_look(obs, source, look, target)
            wanted = (proposal.get("audio") or {}).get("voice_preset")
            if (
                wanted
                and wanted in presets
                and wanted != preset
                and confidence >= 0.7
                and time.monotonic() - self.last_preset_change > 600
            ):
                if preset:
                    remember("preset", "input", preset)
                system.run(["easyeffects", "-l", wanted], timeout=15)
                self.last_preset_change = time.monotonic()
                notes += f" (voice: {wanted})"
                changed = True
            # Nothing to change in a still picture: look less often.
            self.ai_backoff = 1 if changed else min(AI_MAX_BACKOFF, self.ai_backoff * 2)
            if notes:
                notify(self.cfg, "Auto-cinematographer", notes)
            return proposal
        except Exception as exc:
            log("ai-error", error=str(exc))
            notify(self.cfg, "Auto-cinematographer paused a pass", str(exc)[:200])
            self.drop()
            return None


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
    if ensure_websocket() == "restart-obs":
        log("waiting", reason="restart OBS once to enable its local websocket")
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
        auto.ai_tick()
        while not stop["now"] and time.monotonic() - started < interval:
            time.sleep(0.5)
    auto.drop()
    log("stop")
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    cfg = config.load_settings()
    state = ensure_websocket()
    if state != "ready":
        print(
            "OBS's websocket was just enabled; restart OBS (streamstack stop; streamstack studio) and retry."
            if state == "edited"
            else "Restart OBS once so its local websocket comes up, then retry."
        )
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
        obs = OBS() if ensure_websocket() == "ready" and system.running("obs") else None
        if obs is None:
            print("Start OBS (streamstack studio) to undo the picture changes.")
            problems += 1
        else:
            for source, look in baseline["look"].items():
                try:
                    apply_look(obs, source, current_look(obs, source), look, seconds=2.0)
                    print(f"{source}: look restored")
                except RuntimeError as exc:
                    print(f"{source}: {exc}")
                    problems += 1
            obs.close()
    for source, gain in baseline.get("mic", {}).items():
        set_source_gain_db(source, float(gain))
        print(f"{source}: input gain back to {float(gain):+.1f} dB")
    preset = baseline.get("preset", {}).get("input")
    if preset and system.easyeffects_running():
        system.run(["easyeffects", "-l", preset], timeout=15)
        print(f"voice preset back to {preset}")
    if not problems:
        baseline_path().unlink(missing_ok=True)
    log("reset", restored=baseline, complete=not problems)
    return 1 if problems else 0


def cmd_check(args: argparse.Namespace) -> int:
    """Can auto mode reach OBS and Claude? Prints one line per piece."""
    cfg = config.load_settings()
    problems = 0
    state = ensure_websocket()
    print(
        {
            "ready": "OBS websocket: enabled",
            "edited": "OBS websocket: enabled now; restart OBS once",
            "restart-obs": "OBS websocket: off; quit OBS and run this again to enable it",
        }[state]
    )
    problems += state != "ready"
    try:
        import websocket  # noqa: F401
    except ImportError:
        print("python3-websocket: missing")
        problems += 1
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
    sub.add_parser("check", help="check OBS, the Claude backend and the microphone").set_defaults(func=cmd_check)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
