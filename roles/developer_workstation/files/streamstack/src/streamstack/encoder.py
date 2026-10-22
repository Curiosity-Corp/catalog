"""Pick the video encoder OBS records and streams with, from what this machine can actually do.

Nothing here is tied to one machine. The GPU side comes from `vainfo` run against every DRM render node
(never a hardcoded path), and the OBS side from the encoder list OBS itself writes to its startup log, so an
encoder is only chosen when both agree. The order is AV1 VA-API, AV1 Quick Sync, H.264 VA-API, H.264 Quick
Sync, then x264 on the CPU.

OBS encoder ids (verified against obs-studio 32.1.0, plugins/obs-ffmpeg/obs-ffmpeg-vaapi.c and
plugins/obs-qsv11/obs-qsv11.c, and against the "Available Encoders" list of a real OBS 32.1.0 log):

    av1_ffmpeg_vaapi_tex   FFmpeg VAAPI AV1      (the texture variant is the one OBS lists in Advanced output)
    ffmpeg_vaapi_tex       FFmpeg VAAPI H.264
    obs_qsv11_av1          Quick Sync AV1        (only where OBS ships the obs-qsv11 plugin)
    obs_qsv11_v2           Quick Sync H.264
    obs_x264               x264

The VA-API encoders choose their render node themselves (`vaapi_device` defaults to the first node that can
encode the codec), so the profile never names one.
"""

from __future__ import annotations

import configparser
import io
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from streamstack import config, system

OBS_PROFILE = "StreamStack"
ENCODE_ENTRYPOINTS = ("EncSlice", "EncSliceLP")
VAINFO_TIMEOUT = 20.0
VAINFO_TTL = 300.0  # the GPU does not change mid-session; Re-check does not need to ask the driver again
LOG_HEAD_BYTES = 262144  # the encoder list is printed during startup, near the top of the log
LOG_TRIES = 5
X264_ID = "obs_x264"


class EncoderError(Exception):
    """The encoder settings cannot be read or written (the message says why and what to do)."""


@dataclass(frozen=True)
class Candidate:
    key: str
    obs_id: str
    label: str
    codec: str  # av1 | h264
    backend: str  # vaapi | qsv | x264
    va_profiles: tuple[str, ...] = ()


# Best first. This is the whole selection policy.
CANDIDATES = (
    Candidate("av1-vaapi", "av1_ffmpeg_vaapi_tex", "AV1 on the GPU (VA-API)", "av1", "vaapi", ("AV1Profile0",)),
    Candidate("av1-qsv", "obs_qsv11_av1", "AV1 on the GPU (Quick Sync)", "av1", "qsv", ("AV1Profile0",)),
    Candidate(
        "h264-vaapi",
        "ffmpeg_vaapi_tex",
        "H.264 on the GPU (VA-API)",
        "h264",
        "vaapi",
        ("H264High", "H264Main", "H264ConstrainedBaseline"),
    ),
    Candidate(
        "h264-qsv",
        "obs_qsv11_v2",
        "H.264 on the GPU (Quick Sync)",
        "h264",
        "qsv",
        ("H264High", "H264Main", "H264ConstrainedBaseline"),
    ),
    Candidate("x264", X264_ID, "x264 on the CPU", "h264", "x264"),
)
BY_OBS_ID = {c.obs_id: c for c in CANDIDATES}


@dataclass
class VaProfile:
    entrypoints: set[str] = field(default_factory=set)
    rate_controls: dict[str, set[str]] = field(default_factory=dict)  # entrypoint -> VA_RC_* names


@dataclass
class VaNode:
    path: str
    driver: str = ""
    profiles: dict[str, VaProfile] = field(default_factory=dict)

    def encodes(self, names: tuple[str, ...]) -> str | None:
        """The first of these VA profiles this node can encode, or None."""
        for name in names:
            profile = self.profiles.get(name)
            if profile and profile.entrypoints & set(ENCODE_ENTRYPOINTS):
                return name
        return None


@dataclass(frozen=True)
class Choice:
    """An encoder that works here, with what the settings need to know about the hardware under it."""

    candidate: Candidate
    node: str = ""
    driver: str = ""
    low_power_only: bool = False  # the GPU offers only the low-power (fixed-function) entrypoint
    rate_controls: frozenset[str] = frozenset()

    @property
    def obs_id(self) -> str:
        return self.candidate.obs_id

    @property
    def label(self) -> str:
        return self.candidate.label


@dataclass
class Detection:
    nodes: list[VaNode]
    obs_ids: set[str] | None  # None: OBS has not started yet, so its encoder list is unknown
    available: list[Choice]

    @property
    def best(self) -> Choice:
        return self.available[0]


# ---------------------------------------------------------------------------
# What the GPU can encode
# ---------------------------------------------------------------------------

_PROFILE_BLOCK = re.compile(r"^VAProfile(\w+)/VAEntrypoint(\w+)\s*$")
_DRIVER = re.compile(r"^vainfo: Driver version:\s*(.+?)\s*$", re.M)
_RC = re.compile(r"\bVA_RC_(\w+)")


def parse_vainfo(text: str, path: str = "") -> VaNode:
    """Parse `vainfo -a`: the driver, and for each profile/entrypoint the rate controls it offers."""
    node = VaNode(path)
    if match := _DRIVER.search(text):
        node.driver = match.group(1).removesuffix(" ()")
    block: tuple[str, str] | None = None
    in_rc = False
    for line in text.splitlines():
        if match := _PROFILE_BLOCK.match(line):
            name, entry = match.groups()
            profile = node.profiles.setdefault(name, VaProfile())
            profile.entrypoints.add(entry)
            profile.rate_controls.setdefault(entry, set())
            block, in_rc = (name, entry), False
        elif block and line.startswith("    VAConfigAttribRateControl"):
            in_rc = True
            node.profiles[block[0]].rate_controls[block[1]].update(_RC.findall(line))
        elif block and in_rc and line.startswith(" " * 8) and "VAConfigAttrib" not in line:
            node.profiles[block[0]].rate_controls[block[1]].update(_RC.findall(line))
        elif line.strip():
            in_rc = False
    return node


def render_nodes() -> list[str]:
    """Every DRM render node present, whatever it is called on this machine."""
    return sorted(str(p) for p in Path("/dev/dri").glob("renderD*"))


_vainfo_cache: dict[str, tuple[float, VaNode | None]] = {}


def probe_node(path: str) -> VaNode | None:
    """Ask the VA-API driver on one render node what it can do (None when it cannot be queried)."""
    now = time.monotonic()
    cached = _vainfo_cache.get(path)
    if cached and now - cached[0] < VAINFO_TTL:
        return cached[1]
    result = system.run(["vainfo", "--display", "drm", "--device", path, "-a"], timeout=VAINFO_TIMEOUT)
    node = parse_vainfo(result.stdout, path) if result and result.returncode == 0 else None
    if node is not None and not node.profiles:
        node = None
    _vainfo_cache[path] = (now, node)
    return node


def probe_nodes() -> list[VaNode]:
    if not system.have("vainfo"):
        return []
    return [node for path in render_nodes() if (node := probe_node(path))]


# ---------------------------------------------------------------------------
# What OBS can encode
# ---------------------------------------------------------------------------

_ENCODER_LINE = re.compile(r"^\S+:\s+-\s+(\S+)\s+\(")


def parse_obs_log(text: str) -> set[str] | None:
    """The video encoder ids from the "Available Encoders" block of an OBS log (None when it has none)."""
    found: set[str] = set()
    section = ""
    for line in text.splitlines():
        body = line.split(": ", 1)[-1].strip()
        if body == "Available Encoders:":
            section = "start"
        elif section and body == "Video Encoders:":
            section = "video"
        elif section and body == "Audio Encoders:":
            break
        elif section == "video" and (match := _ENCODER_LINE.match(line)):
            found.add(match.group(1))
    return found if section == "video" else None


def obs_log_dir() -> Path:
    return config._xdg("XDG_CONFIG_HOME", ".config") / "obs-studio/logs"


def obs_encoder_ids(log_dir: Path | None = None) -> set[str] | None:
    """The video encoders OBS reported in its newest log that lists them, or None if there is no such log."""
    directory = log_dir or obs_log_dir()
    try:
        logs = sorted((p for p in directory.glob("*.txt") if p.is_file()), reverse=True)
    except OSError:
        return None
    for log in logs[:LOG_TRIES]:
        try:
            with log.open("rb") as handle:
                head = handle.read(LOG_HEAD_BYTES).decode("utf-8", "replace")
        except OSError:
            continue
        if (ids := parse_obs_log(head)) is not None:
            return ids
    return None


# ---------------------------------------------------------------------------
# Choosing
# ---------------------------------------------------------------------------


def _hardware_choice(candidate: Candidate, nodes: list[VaNode]) -> Choice | None:
    for node in nodes:
        profile = node.encodes(candidate.va_profiles)
        if profile is None:
            continue
        if candidate.backend == "qsv" and "intel" not in node.driver.lower():
            continue  # Quick Sync is Intel's; other drivers only offer VA-API
        va = node.profiles[profile]
        entries = va.entrypoints & set(ENCODE_ENTRYPOINTS)
        rates = frozenset().union(*(va.rate_controls.get(e, set()) for e in entries))
        return Choice(candidate, node.path, node.driver, entries == {"EncSliceLP"}, rates)
    return None


def detect(nodes: list[VaNode] | None = None, obs_ids: set[str] | None = None, *, read_log: bool = True) -> Detection:
    """Every encoder usable here, best first; x264 is always last. The arguments let tests supply what is probed."""
    nodes = probe_nodes() if nodes is None else nodes
    ids = obs_encoder_ids() if obs_ids is None and read_log else obs_ids
    available: list[Choice] = []
    for candidate in CANDIDATES:
        if candidate.backend == "x264":
            available.append(Choice(candidate))
            continue
        # Quick Sync cannot be verified without OBS's own list (the plugin is optional); VA-API can, from vainfo.
        if ids is None and candidate.backend == "qsv":
            continue
        if ids is not None and candidate.obs_id not in ids:
            continue
        if choice := _hardware_choice(candidate, nodes):
            available.append(choice)
    return Detection(nodes, ids, available)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

KEYINT_SECONDS = 2  # what Twitch, YouTube and Teams ingest expect; also a fine seek granularity for recordings
BITS_PER_PIXEL = {"av1": 0.07, "h264": 0.10}


def stream_bitrate_kbps(codec: str, width: int, height: int, fps: float) -> int:
    """A live bitrate for this picture size: AV1 needs about 30 % fewer bits than H.264 for the same quality."""
    kbps = width * height * fps * BITS_PER_PIXEL[codec] / 1000
    return int(min(20000, max(1500, round(kbps / 500) * 500)))


def _pick_rate_control(wanted: tuple[str, ...], choice: Choice) -> str:
    """The first wanted mode the GPU offers (when the driver reported none, the first wanted one)."""
    if not choice.rate_controls:
        return wanted[0]
    return next((mode for mode in wanted if mode in choice.rate_controls), wanted[-1])


def stream_settings(choice: Choice, width: int, height: int, fps: float) -> dict[str, object]:
    """Encoder settings for going live: constant bitrate, no B-frames, a keyframe every two seconds."""
    bitrate = stream_bitrate_kbps(choice.candidate.codec, width, height, fps)
    backend = choice.candidate.backend
    if backend == "vaapi":
        settings: dict[str, object] = {
            "rate_control": _pick_rate_control(("CBR", "VBR"), choice),
            "bitrate": bitrate,
            "keyint_sec": KEYINT_SECONDS,
            "bf": 0,
        }
        settings.update(_low_power(choice))
        return settings
    if backend == "qsv":
        return {
            "rate_control": "CBR",
            "bitrate": bitrate,
            "keyint_sec": KEYINT_SECONDS,
            "latency": "ultra-low",
            "bframes": 0,
            "target_usage": "TU4",
        }
    return {"rate_control": "CBR", "bitrate": bitrate, "keyint_sec": KEYINT_SECONDS, "preset": "veryfast", "bf": 0}


def record_settings(choice: Choice, width: int, height: int, fps: float) -> dict[str, object]:
    """Encoder settings for recording: variable bitrate at about twice the live rate, so quality wins over latency."""
    bitrate = min(40000, stream_bitrate_kbps(choice.candidate.codec, width, height, fps) * 2)
    backend = choice.candidate.backend
    if backend == "vaapi":
        settings: dict[str, object] = {
            "rate_control": _pick_rate_control(("VBR", "CBR"), choice),
            "bitrate": bitrate,
            "maxrate": int(bitrate * 1.5),
            "keyint_sec": KEYINT_SECONDS,
            "bf": 0,
        }
        settings.update(_low_power(choice))
        return settings
    if backend == "qsv":
        return {
            "rate_control": "VBR",
            "bitrate": bitrate,
            "max_bitrate": int(bitrate * 1.5),
            "keyint_sec": KEYINT_SECONDS,
            "latency": "normal",
            "target_usage": "TU2",
        }
    return {"rate_control": "CRF", "crf": 20, "keyint_sec": KEYINT_SECONDS, "preset": "fast"}


def _low_power(choice: Choice) -> dict[str, str]:
    """`low_power=1` for FFmpeg's VA-API encoders, only where the GPU has nothing but the low-power entrypoint."""
    return {"ffmpeg_opts": "low_power=1"} if choice.low_power_only else {}


def apply_to_ini(text: str, choice: Choice, home: Path | None = None) -> str:
    """The profile's basic.ini with the output mode and encoders for `choice`; other keys are kept as they are."""
    ini = configparser.ConfigParser(interpolation=None, delimiters=("=",), strict=False)
    ini.optionxform = str  # OBS keys are case sensitive
    try:
        ini.read_string(text)
    except configparser.Error as exc:
        raise EncoderError(f"the OBS profile cannot be parsed: {exc}") from exc
    if not ini.has_section("Output"):
        ini.add_section("Output")
    if choice.candidate.backend == "x264":
        ini.set("Output", "Mode", "Simple")
    else:
        videos = (home or Path.home()) / "Videos/StreamStack"
        if not ini.has_section("AdvOut"):
            ini.add_section("AdvOut")
        ini.set("Output", "Mode", "Advanced")
        for key, value in {
            "Encoder": choice.obs_id,
            "RecType": "Standard",
            "RecFormat2": "mkv",
            "RecTracks": "1",
            "TrackIndex": "1",
            "ApplyServiceSettings": "false",  # the service must not override the low-latency settings
            "FileNameWithoutSpace": "true",
        }.items():
            ini.set("AdvOut", key, value)
        if not ini.has_option("AdvOut", "RecFilePath"):
            ini.set("AdvOut", "RecFilePath", str(videos))
        ini.set("AdvOut", "RecEncoder", choice.obs_id)  # its own settings: recordEncoder.json
    out = io.StringIO()
    ini.write(out, space_around_delimiters=False)
    return out.getvalue()


def write_json(path: Path, data: dict[str, object]) -> None:
    tmp = path.with_name(path.name + ".streamstack-tmp")
    try:
        tmp.write_text(json.dumps(data, indent=4) + "\n")
        tmp.replace(path)
    except OSError as exc:
        raise EncoderError(f"cannot write {path}: {exc}") from exc


def apply_to_profile(
    profile: Path, choice: Choice, width: int, height: int, fps: float, home: Path | None = None
) -> None:
    """Write the encoder choice into an OBS profile directory (basic.ini, streamEncoder.json, recordEncoder.json)."""
    ini_path = profile / "basic.ini"
    try:
        text = ini_path.read_text()
    except OSError as exc:
        raise EncoderError(f"cannot read {ini_path}: {exc}") from exc
    new_text = apply_to_ini(text, choice, home)
    if choice.candidate.backend != "x264":
        write_json(profile / "streamEncoder.json", stream_settings(choice, width, height, fps))
        write_json(profile / "recordEncoder.json", record_settings(choice, width, height, fps))
    tmp = ini_path.with_name(ini_path.name + ".streamstack-tmp")
    try:
        tmp.write_text(new_text)
        tmp.replace(ini_path)
    except OSError as exc:
        raise EncoderError(f"cannot write {ini_path}: {exc}") from exc


def profile_dir() -> Path:
    return config._xdg("XDG_CONFIG_HOME", ".config") / "obs-studio/basic/profiles" / OBS_PROFILE


def apply(cfg: dict[str, str], choice: Choice, *, directory: Path | None = None) -> Path:
    """Apply `choice` to this user's StreamStack profile. OBS rewrites its config on exit, so it must be closed."""
    target = directory or profile_dir()
    if system.running("obs"):
        raise EncoderError("OBS is running and would overwrite the change when it quits; close OBS and run this again")
    try:
        width, height, fps = (
            int(cfg["STREAMSTACK_WIDTH"]),
            int(cfg["STREAMSTACK_HEIGHT"]),
            float(cfg["STREAMSTACK_FPS"]),
        )
    except ValueError as exc:
        raise EncoderError(f"STREAMSTACK_WIDTH, STREAMSTACK_HEIGHT and STREAMSTACK_FPS must be numbers: {exc}") from exc
    apply_to_profile(target, choice, width, height, fps)
    return target


# ---------------------------------------------------------------------------
# What the profile uses now, and the health check
# ---------------------------------------------------------------------------


def configured_encoders(directory: Path | None = None) -> tuple[str, str] | None:
    """(stream encoder id, record encoder id) the StreamStack profile selects; None without a profile."""
    path = (directory or profile_dir()) / "basic.ini"
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise EncoderError(f"cannot read {path}: {exc}") from exc
    ini = configparser.ConfigParser(interpolation=None, delimiters=("=",), strict=False)
    ini.optionxform = str
    try:
        ini.read_string(text)
    except configparser.Error as exc:
        raise EncoderError(f"cannot parse {path}: {exc}") from exc
    if ini.get("Output", "Mode", fallback="Simple").lower() != "advanced":
        return X264_ID, X264_ID  # Simple mode without a hardware encoder choice is x264
    stream = ini.get("AdvOut", "Encoder", fallback=X264_ID)
    record = ini.get("AdvOut", "RecEncoder", fallback="none")
    return stream, stream if record.lower() == "none" else record


def name_of(obs_id: str) -> str:
    candidate = BY_OBS_ID.get(obs_id)
    return candidate.label if candidate else obs_id


def describe(choice: Choice) -> str:
    if choice.candidate.backend == "x264":
        return "software encoder, uses the CPU while you are on a call"
    where = choice.node or "GPU"
    driver = f", {choice.driver}" if choice.driver else ""
    return f"{where}{driver}{', low-power mode' if choice.low_power_only else ''}"


def health_check(cfg: dict[str, str], detection: Detection | None = None) -> dict[str, str]:
    """The doctor / Console check "OBS encoder" (same shape as cli.check)."""
    group, cid = "video", "encoder"
    try:
        detection = detection or detect()
        used = configured_encoders()
    except EncoderError as exc:
        return {
            "id": cid,
            "group": group,
            "state": "warn",
            "title": "OBS encoder cannot be checked",
            "detail": str(exc),
            "hint": "",
        }
    best = detection.best
    note = "" if detection.obs_ids is not None else " (OBS has not listed its encoders yet; start it once to confirm)"
    if used is None:
        return {
            "id": cid,
            "group": group,
            "state": "warn",
            "title": "OBS profile StreamStack is not set up",
            "detail": f"best encoder here: {best.label}{note}",
            "hint": "streamstack setup",
        }
    stream, record = used
    available = {c.obs_id: c for c in detection.available}
    if stream == best.obs_id and record == best.obs_id:
        return {
            "id": cid,
            "group": group,
            "state": "ok",
            "title": f"OBS encodes with {best.label}",
            "detail": f"{best.obs_id}: {describe(best)}{note}",
            "hint": "",
        }
    missing = [i for i in (stream, record) if i not in available]
    if missing:
        title = f"OBS is set to {name_of(missing[0])}, which this machine cannot use"
    else:
        title = f"OBS encodes with {name_of(stream)} but {best.label} is available"
    return {
        "id": cid,
        "group": group,
        "state": "warn",
        "title": title,
        "detail": f"best encoder here: {best.label} ({describe(best)}){note}",
        "hint": "close OBS, then: streamstack encoder --apply",
    }


def summary_lines(detection: Detection, used: tuple[str, str] | None) -> list[str]:
    """Text for `streamstack encoder`."""
    lines = []
    for node in detection.nodes:
        lines.append(f"render node {node.path}: {node.driver or 'unknown driver'}")
    if not detection.nodes:
        lines.append("no VA-API render node answered (vainfo missing or no GPU encoder)")
    lines.append(
        "OBS encoder list: " + (", ".join(sorted(detection.obs_ids)) if detection.obs_ids else "not available yet")
    )
    lines.append("usable, best first:")
    lines.extend(f"  {c.obs_id:22} {c.label} ({describe(c)})" for c in detection.available)
    lines.append("profile uses: " + (" / ".join(dict.fromkeys(name_of(i) for i in used)) if used else "no profile yet"))
    return lines
