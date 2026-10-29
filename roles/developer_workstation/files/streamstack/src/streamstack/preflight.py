"""Pre-flight: judge the whole rig before a meeting and say what to fix, as a 0-100 score.

This module is pure: it turns *facts* (the Console's state snapshot, plus a few measurements) into seven checks and a
score. It touches no device, so every rule can be tested with plain dicts. The Console controller
(`streamstack.console.preflight`) gathers the facts, runs the checks in parallel with hard timeouts and offers the
fixes; `streamstack preflight` asks a running Console to do the same from a terminal.

The seven checks (one per area, so a fault lowers exactly its own check):

    camera    program camera running, frame rate, the virtual camera has a producer, privacy off, face found
    mic       node alive and not stalled, not muted, level on a spoken prompt, noise floor, SNR, clip headroom
    outputs   a default output exists, is audible, and is not the loudspeakers when headphones are plugged in
    lights    every light connected, on, and inside the persona's brightness and colour range
    network   round trip, jitter and loss (from the network monitor, #41; skipped while nothing measures it)
    headroom  CPU, GPU, dropped frames and free disk
    meeting   the call app uses StreamStack's camera and microphone (only while a call is on, see `callstate`)

A check is `ok`, `warn`, `fail` or `skip` (nothing to judge: it does not count towards the score). Each finding that
can be fixed with one existing Console action carries that action; nothing here ever runs one. Pre-flight never
opens the camera: the picture facts come from the engine's own state and the framing controller.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
STATE_ORDER = {OK: 0, SKIP: 0, WARN: 1, FAIL: 2}
STATE_VALUE = {OK: 1.0, WARN: 0.6, FAIL: 0.0}
WEIGHTS = {"camera": 22, "mic": 26, "outputs": 14, "lights": 8, "network": 14, "headroom": 10, "meeting": 6}
TITLES = {
    "camera": "Camera",
    "mic": "Microphone",
    "outputs": "Outputs",
    "lights": "Lights",
    "network": "Network",
    "headroom": "Headroom",
    "meeting": "Meeting app",
}
GROUPS = tuple(WEIGHTS)

# thresholds, all in one place so the docs and the tests quote the same numbers
FPS_WARN = 24.0
FLOOR_WARN_DB, FLOOR_FAIL_DB = -45.0, -35.0
SNR_WARN_DB, SNR_FAIL_DB = 20.0, 10.0
SPEECH_QUIET_DB = -40.0
CLIP_HEADROOM_DB = -1.0
LOUDNESS_TOLERANCE_LU = 4.0
VOLUME_LOW, VOLUME_HIGH = 10, 95
RTT_WARN_MS, RTT_FAIL_MS = 150.0, 300.0
JITTER_WARN_MS, JITTER_FAIL_MS = 30.0, 100.0
LOSS_WARN_PCT, LOSS_FAIL_PCT = 1.0, 5.0
UPLINK_WARN_MBPS = 3.0
CPU_WARN, CPU_FAIL = 85.0, 95.0
GPU_WARN, GPU_FAIL = 90.0, 98.0
DISK_WARN_GB, DISK_FAIL_GB = 5.0, 1.0
EXPOSURE_CLIPPED_WARN_PCT = 5.0
HEALTHY_VERDICT = 90  # at or above this with no failing check: "Ready"
OUTPUT_QUIET_KINDS = ("headphones", "headset")
OUTPUT_LOUD_KINDS = ("speaker", "hdmi")


@dataclass(frozen=True)
class Fix:
    """One Console action that repairs a finding. `label` is the button text."""

    action: str
    params: Mapping[str, Any]
    label: str

    def public(self) -> dict[str, Any]:
        return {"action": self.action, "params": dict(self.params), "label": self.label}


@dataclass(frozen=True)
class Finding:
    state: str
    text: str
    fix: Fix | None = None


@dataclass(frozen=True)
class Check:
    id: str
    state: str
    detail: str
    fix: Fix | None = None
    measured: Mapping[str, Any] = field(default_factory=dict)

    @property
    def title(self) -> str:
        return TITLES[self.id]

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "state": self.state,
            "detail": self.detail,
            "fix": self.fix.public() if self.fix else None,
            "measured": dict(self.measured),
        }


@dataclass(frozen=True)
class MicProbe:
    """What the microphone did while the person said their name (levels from the raw meter, in dBFS)."""

    floor_db: float | None  # the quiet between words
    speech_db: float | None  # the loud part (95th percentile of the 20 ms RMS)
    peak_db: float | None
    heard_speech: bool
    frames: int = 0

    @property
    def snr_db(self) -> float | None:
        if self.floor_db is None or self.speech_db is None or not self.heard_speech:
            return None
        return self.speech_db - self.floor_db


@dataclass(frozen=True)
class NetworkFacts:
    """From the network monitor (#41). `up_mbps` and `down_mbps` are estimates and may be unknown."""

    rtt_ms: float
    jitter_ms: float
    loss_pct: float
    up_mbps: float | None = None
    down_mbps: float | None = None


@dataclass(frozen=True)
class Facts:
    """Everything the checks may look at. Optional measurements are None when nothing could measure them."""

    state: Mapping[str, Any]  # the Console snapshot
    mic: MicProbe | None = None
    network: NetworkFacts | None = None
    face: bool | None = None  # a face is in the program picture (None: not measured)
    exposure_clipped_pct: float | None = None
    gpu_percent: float | None = None
    disk_free_gb: float | None = None  # where recordings go


def _get(data: Any, *path: str, default: Any = None) -> Any:
    for key in path:
        if not isinstance(data, Mapping) or key not in data:
            return default
        data = data[key]
    return default if data is None else data


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _resolve(check_id: str, findings: Sequence[Finding], measured: Mapping[str, Any], ok_text: str) -> Check:
    """The worst finding sets the state; every finding is said; the first fixable one is offered."""
    problems = [f for f in findings if f.state in (WARN, FAIL)]
    if not problems:
        skipped = [f for f in findings if f.state == SKIP]
        if skipped and len(skipped) == len(findings):
            return Check(check_id, SKIP, skipped[0].text, None, measured)
        return Check(check_id, OK, ok_text, None, measured)
    worst = max(problems, key=lambda f: STATE_ORDER[f.state]).state
    problems.sort(key=lambda f: -STATE_ORDER[f.state])
    fix = next((f.fix for f in problems if f.fix is not None), None)
    return Check(check_id, worst, " ".join(f.text for f in problems), fix, measured)


# ---------------------------------------------------------------------------
# the seven checks
# ---------------------------------------------------------------------------


def check_camera(facts: Facts) -> Check:
    s = facts.state
    findings: list[Finding] = []
    start = Fix("studio.start", {}, "Start the studio")
    if not _get(s, "studio", "running", default=False):
        findings.append(Finding(FAIL, "The program camera is off.", start))
    else:
        engine = _get(s, "video", "engine", default={})
        if _get(engine, "native", default=False):
            if _get(engine, "state") != "running":
                findings.append(
                    Finding(FAIL, f"The video engine is {_get(engine, 'state', default='not running')}.", start)
                )
            else:
                fps = _number(_get(engine, "fps"))
                if fps is not None and fps < FPS_WARN:
                    findings.append(Finding(WARN, f"The picture runs at {fps:.0f} fps, below {FPS_WARN:.0f}."))
        cam = _get(s, "virtual", "camera", default={})
        if cam and not _get(cam, "present", default=True):
            findings.append(Finding(FAIL, "StreamStack Camera is not there for apps to pick.", start))
        elif cam and not _get(cam, "producer", default=True):
            findings.append(Finding(FAIL, "StreamStack Camera has no picture behind it.", start))
    if _get(s, "video", "privacy", default=False):
        findings.append(
            Finding(
                WARN,
                "Privacy is on: apps see the Be Right Back picture.",
                Fix("video.privacy", {"on": False}, "Privacy off"),
            )
        )
    if facts.face is False:
        findings.append(Finding(WARN, "No face is in the picture. Check the framing and the camera angle."))
    if facts.exposure_clipped_pct is not None and facts.exposure_clipped_pct > EXPOSURE_CLIPPED_WARN_PCT:
        findings.append(
            Finding(WARN, f"{facts.exposure_clipped_pct:.0f}% of the picture is clipped (too bright or too dark).")
        )
    measured = {
        "fps": _number(_get(s, "video", "engine", "fps")),
        "face": facts.face,
        "exposure_clipped_pct": facts.exposure_clipped_pct,
    }
    return _resolve("camera", findings, measured, "The camera is running and apps can see it.")


def check_mic(facts: Facts) -> Check:
    s = facts.state
    mic = _get(s, "audio", "mic", default={})
    findings: list[Finding] = []
    if not _get(mic, "present", default=False):
        findings.append(Finding(FAIL, "No microphone found.", Fix("hardware.refresh", {}, "Look again")))
    elif _get(mic, "stalled", default=False):
        findings.append(
            Finding(
                FAIL, "The microphone has stalled (it delivers no audio).", Fix("audio.revive", {}, "Revive the mic")
            )
        )
    elif _get(mic, "muted", default=False):
        findings.append(Finding(WARN, "The microphone is muted.", Fix("audio.mute", {"on": False}, "Unmute")))
    if not _get(s, "studio", "voice_chain", default=True):
        findings.append(
            Finding(
                WARN, "StreamStack Mic (the voice chain) is not running.", Fix("studio.start", {}, "Start the studio")
            )
        )
    probe = facts.mic
    measured: dict[str, Any] = {
        "floor_db": None,
        "speech_db": None,
        "snr_db": None,
        "peak_db": None,
        "heard_speech": None,
    }
    if probe is not None and probe.frames > 0:
        measured.update(
            floor_db=_round(probe.floor_db),
            speech_db=_round(probe.speech_db),
            snr_db=_round(probe.snr_db),
            peak_db=_round(probe.peak_db),
            heard_speech=probe.heard_speech,
        )
        findings.extend(_probe_findings(probe, s))
    elif probe is not None:
        findings.append(Finding(WARN, "The microphone meter delivered no levels."))
    return _resolve("mic", findings, measured, _mic_ok(probe))


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def _mic_ok(probe: MicProbe | None) -> str:
    if probe is None or not probe.frames:
        return "The microphone is alive. Run with the spoken prompt to test its level."
    snr = probe.snr_db
    return f"Level, noise floor and headroom are fine (signal to noise {snr:.0f} dB)." if snr is not None else "Fine."


def _probe_findings(probe: MicProbe, s: Mapping[str, Any]) -> list[Finding]:
    out: list[Finding] = []
    floor = probe.floor_db
    if floor is not None and floor > FLOOR_WARN_DB:
        state = FAIL if floor > FLOOR_FAIL_DB else WARN
        out.append(
            Finding(state, f"The noise floor is high ({floor:.0f} dBFS): close a window, move the fan, lower the gain.")
        )
    if not probe.heard_speech:
        out.append(Finding(WARN, "No voice was heard on the prompt. Say your name, or check the mic is the right one."))
        return out
    snr = probe.snr_db
    if snr is not None and snr < SNR_WARN_DB:
        out.append(Finding(FAIL if snr < SNR_FAIL_DB else WARN, f"Your voice is only {snr:.0f} dB above the noise."))
    if probe.speech_db is not None and probe.speech_db < SPEECH_QUIET_DB:
        out.append(Finding(WARN, f"Your voice is quiet ({probe.speech_db:.0f} dBFS): raise the gain or move closer."))
    if probe.peak_db is not None and probe.peak_db > CLIP_HEADROOM_DB:
        out.append(
            Finding(
                WARN,
                "The voice peaks at full scale (clipping): lower the gain.",
                Fix("audio.clip_clear", {}, "Clear the clip mark"),
            )
        )
    loud = _get(s, "audio", "chain", "loudness", default={})
    level, target = _number(_get(loud, "level_lufs")), _number(_get(loud, "target_lufs"))
    if (
        _get(loud, "on", default=False)
        and level is not None
        and target is not None
        and abs(level - target) > LOUDNESS_TOLERANCE_LU
    ):
        out.append(Finding(WARN, f"The voice measures {level:.0f} LUFS against the {target:.0f} LUFS target."))
    return out


def check_outputs(facts: Facts) -> Check:
    out = _get(facts.state, "audio", "outputs", default={})
    if not out or not _get(out, "available", default=False):
        return Check("outputs", SKIP, "Audio outputs cannot be read here.", None, {})
    devices = [d for d in _get(out, "devices", default=[]) if isinstance(d, Mapping)]
    default = next((d for d in devices if d.get("name") == out.get("default")), None)
    findings: list[Finding] = []
    if not devices:
        findings.append(Finding(FAIL, "No audio output found."))
    elif default is None:
        best = next((d for d in devices if d.get("kind") in OUTPUT_QUIET_KINDS), devices[0])
        findings.append(
            Finding(
                FAIL,
                "No default output is set.",
                Fix("audio.output_default", {"name": best["name"]}, f"Use {best['label']}"),
            )
        )
    else:
        if default.get("muted"):
            findings.append(
                Finding(
                    WARN,
                    f"{default['label']} is muted.",
                    Fix("audio.output_mute", {"name": default["name"], "on": False}, "Unmute"),
                )
            )
        volume = _number(default.get("volume"))
        if volume is not None and not default.get("muted") and (volume < VOLUME_LOW or volume > VOLUME_HIGH):
            word = "very quiet" if volume < VOLUME_LOW else "at full volume"
            findings.append(
                Finding(
                    WARN,
                    f"{default['label']} is {word} ({volume:.0f}%).",
                    Fix("audio.output_volume", {"name": default["name"], "percent": 60}, "Set 60%"),
                )
            )
        quiet = next((d for d in devices if d.get("kind") in OUTPUT_QUIET_KINDS), None)
        if default.get("kind") in OUTPUT_LOUD_KINDS and quiet is not None:
            findings.append(
                Finding(
                    WARN,
                    f"The default output is {default['label']}; the call would play through the room and echo. "
                    f"{quiet['label']} is plugged in.",
                    Fix("audio.output_default", {"name": quiet["name"]}, f"Use {quiet['label']}"),
                )
            )
    result = default.get("result") if default else None
    measured = {
        "default": default.get("label") if default else None,
        "volume": default.get("volume") if default else None,
        "last_test": result,
        "devices": len(devices),
    }
    return _resolve(
        "outputs", findings, measured, f"{default['label']} is the default output and is audible." if default else ""
    )


def check_lights(facts: Facts) -> Check:
    lighting = _get(facts.state, "lighting", default={})
    lights = [x for x in _get(lighting, "lights", default=[]) if isinstance(x, Mapping)]
    expected = bool(_get(facts.state, "hardware", "lights", default=False))
    if not lights:
        if expected:
            return Check(
                "lights",
                WARN,
                "The rig has lights but none answered. Check the USB cable and rescan.",
                Fix("lighting.rescan", {}, "Rescan"),
                {"lights": 0},
            )
        return Check("lights", SKIP, "No lights on this rig.", None, {"lights": 0})
    findings: list[Finding] = []
    for light in lights:
        name = light.get("name") or light.get("id")
        if not light.get("connected", False) or light.get("error"):
            findings.append(
                Finding(
                    FAIL,
                    f"{name} is not answering ({light.get('error') or 'disconnected'}).",
                    Fix("lighting.rescan", {}, "Rescan"),
                )
            )
            continue
        if light.get("role") == "manual":
            continue
        if not light.get("power"):
            findings.append(
                Finding(WARN, f"{name} is off.", Fix("lighting.power", {"id": light["id"], "on": True}, "Turn on"))
            )
            continue
        limits = light.get("limits") or {}
        bright, kelvin = _number(light.get("brightness")), _number(light.get("kelvin"))
        lo, hi = _number(limits.get("min_brightness")), _number(limits.get("max_brightness"))
        if bright is not None and lo is not None and hi is not None and not lo <= bright <= hi:
            target = min(max(bright, lo), hi)
            findings.append(
                Finding(
                    WARN,
                    f"{name} is at {bright:.0f}% brightness, outside {lo:.0f} to {hi:.0f}%.",
                    Fix("lighting.brightness", {"id": light["id"], "percent": round(target)}, f"Set {target:.0f}%"),
                )
            )
        klo, khi = _number(limits.get("min_kelvin")), _number(limits.get("max_kelvin"))
        if kelvin is not None and klo is not None and khi is not None and not klo <= kelvin <= khi:
            target = min(max(kelvin, klo), khi)
            findings.append(
                Finding(
                    WARN,
                    f"{name} is at {kelvin:.0f} K, outside {klo:.0f} to {khi:.0f} K.",
                    Fix("lighting.temperature", {"id": light["id"], "kelvin": round(target)}, f"Set {target:.0f} K"),
                )
            )
    off = [x for x in lights if x.get("connected") and not x.get("error") and not x.get("power")]
    if len(off) > 1 and all(x.get("role") != "manual" for x in off):
        all_on = Fix("lighting.power", {"on": True}, "Turn the lights on")  # one press, every light
        findings = [
            Finding(f.state, f.text, all_on if f.fix and f.fix.action == "lighting.power" else f.fix) for f in findings
        ]
    measured = {"lights": len(lights), "on": sum(1 for x in lights if x.get("power"))}
    return _resolve(
        "lights", findings, measured, f"{len(lights)} light{'s' if len(lights) != 1 else ''} connected and in range."
    )


def check_network(facts: Facts) -> Check:
    net = facts.network
    if net is None:
        return Check("network", SKIP, "The network monitor has no measurement yet.", None, {})
    findings: list[Finding] = []
    for value, warn, fail, label, unit in (
        (net.rtt_ms, RTT_WARN_MS, RTT_FAIL_MS, "Round trip", "ms"),
        (net.jitter_ms, JITTER_WARN_MS, JITTER_FAIL_MS, "Jitter", "ms"),
        (net.loss_pct, LOSS_WARN_PCT, LOSS_FAIL_PCT, "Packet loss", "%"),
    ):
        if value > warn:
            findings.append(
                Finding(FAIL if value > fail else WARN, f"{label} is {value:.0f} {unit} (good is under {warn:.0f}).")
            )
    if net.up_mbps is not None and net.up_mbps < UPLINK_WARN_MBPS:
        findings.append(Finding(WARN, f"Uplink is about {net.up_mbps:.1f} Mbps, which is thin for HD video."))
    measured = {
        "rtt_ms": net.rtt_ms,
        "jitter_ms": net.jitter_ms,
        "loss_pct": net.loss_pct,
        "up_mbps": net.up_mbps,
        "down_mbps": net.down_mbps,
    }
    return _resolve(
        "network",
        findings,
        measured,
        f"Round trip {net.rtt_ms:.0f} ms, jitter {net.jitter_ms:.0f} ms, loss {net.loss_pct:.1f}%.",
    )


def check_headroom(facts: Facts) -> Check:
    s = facts.state
    findings: list[Finding] = []
    cpu = _number(_get(s, "system", "cpu_percent"))
    if cpu is not None and cpu > CPU_WARN:
        findings.append(Finding(FAIL if cpu > CPU_FAIL else WARN, f"The CPU is {cpu:.0f}% busy."))
    gpu = facts.gpu_percent if facts.gpu_percent is not None else _number(_get(s, "stream", "gpu_percent"))
    if gpu is not None and gpu > GPU_WARN:
        findings.append(Finding(FAIL if gpu > GPU_FAIL else WARN, f"The GPU is {gpu:.0f}% busy."))
    dropped = _number(_get(s, "stream", "dropped_frames")) or 0
    engine_dropped = _number(_get(s, "video", "engine", "dropped")) or 0
    if dropped + engine_dropped > 0:
        findings.append(Finding(WARN, f"{dropped + engine_dropped:.0f} frames were dropped."))
    if facts.disk_free_gb is not None and facts.disk_free_gb < DISK_WARN_GB:
        findings.append(
            Finding(
                FAIL if facts.disk_free_gb < DISK_FAIL_GB else WARN,
                f"Only {facts.disk_free_gb:.1f} GB is free for recordings.",
            )
        )
    measured = {
        "cpu_percent": cpu,
        "gpu_percent": gpu,
        "dropped_frames": dropped + engine_dropped,
        "disk_free_gb": facts.disk_free_gb,
    }
    return _resolve("headroom", findings, measured, "Plenty of headroom.")


def check_meeting(facts: Facts) -> Check:
    call = _get(facts.state, "call", default={})
    if not _get(call, "active", default=False):
        return Check("meeting", SKIP, "No call is on, so there is no meeting app to check.", None, {})
    seen = [x for x in _get(facts.state, "virtual", "seen", default=[]) if isinstance(x, Mapping)]
    findings: list[Finding] = []
    service = _get(call, "service", default="The meeting app")
    if _get(call, "camera", default=False) and not any(x.get("kind") == "camera" for x in seen):
        findings.append(
            Finding(
                WARN, f"{service} has a camera open, but not StreamStack Camera. Pick it in the app's video settings."
            )
        )
    if _get(call, "mic", default="none") != "none" and not any(x.get("kind") == "mic" for x in seen):
        findings.append(
            Finding(
                WARN,
                f"{service} is listening to a microphone, but not StreamStack Mic. "
                "Pick it in the app's audio settings.",
            )
        )
    return _resolve("meeting", findings, {"service": service}, f"{service} uses StreamStack Camera and Mic.")


CHECKS: tuple[tuple[str, Callable[[Facts], Check]], ...] = (
    ("camera", check_camera),
    ("mic", check_mic),
    ("outputs", check_outputs),
    ("lights", check_lights),
    ("network", check_network),
    ("headroom", check_headroom),
    ("meeting", check_meeting),
)


# ---------------------------------------------------------------------------
# score
# ---------------------------------------------------------------------------


def score(checks: Sequence[Check]) -> int:
    """0 to 100: the weighted mean of the checks that could be judged (skipped ones do not count either way)."""
    counted = [c for c in checks if c.state != SKIP]
    total = sum(WEIGHTS[c.id] for c in counted)
    if not total:
        return 0
    return round(100 * sum(WEIGHTS[c.id] * STATE_VALUE[c.state] for c in counted) / total)


def verdict(checks: Sequence[Check], points: int) -> str:
    """`ready`, `check` (something to look at) or `not-ready` (a check failed)."""
    if any(c.state == FAIL for c in checks):
        return "not-ready"
    return "ready" if points >= HEALTHY_VERDICT and all(c.state != WARN for c in checks) else "check"


def summary(checks: Sequence[Check]) -> dict[str, Any]:
    points = score(checks)
    problems = [c for c in checks if c.state in (WARN, FAIL)]
    return {
        "score": points,
        "verdict": verdict(checks, points),
        "problems": len(problems),
        "text": (
            "Everything checked is ready."
            if not problems
            else "; ".join(f"{c.title}: {c.detail}" for c in sorted(problems, key=lambda c: -STATE_ORDER[c.state]))
        ),
    }


def percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def mic_probe(levels: Sequence[tuple[float, float]], *, speech_db: float = SPEECH_QUIET_DB - 5.0) -> MicProbe:
    """A probe from the raw meter's frames, each `(rms_db, peak_db)`.

    The floor is the 10th percentile of the 20 ms RMS (the quiet between words), the voice the 95th. Speech is heard
    when the voice rises 12 dB above the floor and above `speech_db`."""
    if not levels:
        return MicProbe(None, None, None, False, 0)
    rms = [r for r, _ in levels]
    floor, voice = percentile(rms, 0.10), percentile(rms, 0.95)
    heard = voice - floor >= 12.0 and voice > speech_db
    return MicProbe(floor, voice, max(p for _, p in levels), heard, len(levels))


def run_checks(facts: Facts) -> list[Check]:
    """Every check, in order (the Console runs them in parallel with timeouts; this is the sequential form)."""
    return [fn(facts) for _, fn in CHECKS]
