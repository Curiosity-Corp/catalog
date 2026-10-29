"""Validation for every preset document: looks, platforms, bandwidth tiers and profiles.

Everything raises `PresetError` (a ValueError) whose message names the offending path, so a bad hand-edited
file or a bad import says exactly what to fix. Nothing here touches the disk or the rig.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

# --- the look parameters the engine supports (the single source; console/video.py imports these) -------------

LOOK_LIMITS: dict[str, tuple[float, float]] = {
    "brightness": (-0.5, 0.5),
    "contrast": (-1.0, 1.0),
    "gamma": (-1.0, 1.0),
    "saturation": (-1.0, 1.0),
    "warmth": (-1.0, 1.0),
    "sharpen_amount": (0.0, 1.0),
    "lut_amount": (0.0, 1.0),
}
LOOK_FLAGS = ("sharpen_enabled", "lut_enabled")
LOOK_NEUTRAL: dict[str, float | bool] = {
    "brightness": 0.0,
    "contrast": 0.0,
    "gamma": 0.0,
    "saturation": 0.0,
    "warmth": 0.0,
    "sharpen_enabled": False,
    "sharpen_amount": 0.08,
    "lut_enabled": False,
    "lut_amount": 0.35,
}

CODECS = ("av1", "hevc", "h264")
RATE_CONTROLS = ("cbr", "vbr", "cqp", "none")
TRANSPORTS = ("enhanced-rtmp", "rtmp", "srt", "whip", "virtualcam", "file")
AUDIO_CODECS = ("aac", "opus", "pcm")
RTMP_AUDIO_CODECS = ("aac", "opus")  # what an RTMP ingest can be asked to carry (Opus is Enhanced RTMP)
GROUPS = ("live", "call", "archive")
ORIENTATIONS = ("landscape", "portrait")
TIER_POLICIES = ("ladder", "ignore")
VERIFIED = ("official", "community", "uncertain")
# Mirrors streamstack.framing.engine.MODES (a test keeps the two equal; this module must import without numpy).
FRAMING_MODES = ("off", "gentle", "tight", "follow")

PROFILE_SCHEMA = "streamstack.profile/1"
PROFILES_SCHEMA = "streamstack.profiles/1"
MAX_NAME = 40
MAX_BLURB = 120
MAX_IMPORT_PROFILES = 50

ID = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
ICON = re.compile(r"[a-z][a-z0-9-]{0,23}")


class PresetError(ValueError):
    """A preset document that is not valid; the message says where."""


def _fail(path: str, message: str) -> PresetError:
    return PresetError(f"{path}: {message}")


def _obj(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail(path, "must be an object")
    return value


def _only(value: Mapping[str, Any], allowed: set[str], required: set[str], path: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise _fail(path, f"unknown field {unknown[0]!r}")
    for key in sorted(required):
        if key not in value:
            raise _fail(path, f"{key!r} is required")


def _text(value: object, path: str, maximum: int, *, blank_ok: bool = False) -> str:
    if not isinstance(value, str):
        raise _fail(path, "must be text")
    text = value.strip()
    if not text and not blank_ok:
        raise _fail(path, "must not be empty")
    if len(text) > maximum:
        raise _fail(path, f"is at most {maximum} characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise _fail(path, "must not contain control characters")
    return text


def _choice(value: object, options: tuple[str, ...], path: str) -> str:
    if not isinstance(value, str) or value not in options:
        raise _fail(path, f"must be one of {', '.join(options)}")
    return value


def _number(value: object, path: str, low: float, high: float, *, integer: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _fail(path, "must be a number")
    if integer and int(value) != value:
        raise _fail(path, "must be a whole number")
    if not low <= value <= high:
        raise _fail(path, f"must be between {low:g} and {high:g}")
    return int(value) if integer else float(value)


def _id(value: object, path: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise _fail(path, "must be lower-case letters, digits and hyphens (at most 32)")
    return value


def _url(value: object, path: str) -> str:
    text = _text(value, path, 300)
    if not text.startswith("https://"):
        raise _fail(path, "must be an https:// URL")
    return text


# --- looks ----------------------------------------------------------------------------------------------------


def validate_look_values(values: object, path: str = "values") -> dict[str, float | bool]:
    """A partial look: only the engine's parameters, inside their limits."""
    data = _obj(values, path)
    clean: dict[str, float | bool] = {}
    for key, value in data.items():
        if key in LOOK_FLAGS:
            if not isinstance(value, bool):
                raise _fail(f"{path}.{key}", "must be true or false")
            clean[key] = value
        elif key in LOOK_LIMITS:
            low, high = LOOK_LIMITS[key]
            clean[key] = round(_number(value, f"{path}.{key}", low, high), 4)
        else:
            raise _fail(path, f"unknown look setting {key!r}")
    return clean


def validate_look(entry: object, index: int = 0) -> dict[str, Any]:
    path = f"looks[{index}]"
    data = _obj(entry, path)
    _only(data, {"name", "category", "blurb", "values", "lut"}, {"name", "category", "blurb", "values"}, path)
    out: dict[str, Any] = {
        "name": _text(data["name"], f"{path}.name", MAX_NAME),
        "category": _id(data["category"], f"{path}.category"),
        "blurb": _text(data["blurb"], f"{path}.blurb", MAX_BLURB),
        "values": validate_look_values(data["values"], f"{path}.values"),
        "lut": None,
    }
    if data.get("lut") is not None:
        out["lut"] = _id(data["lut"], f"{path}.lut")
        if not out["values"].get("lut_enabled"):
            raise _fail(path, "a look with a lut must set lut_enabled")
    return out


# --- platforms and tiers ---------------------------------------------------------------------------------------


def _audio(value: object, path: str) -> dict[str, Any]:
    data = _obj(value, path)
    keys = {"codec", "bitrate_kbps", "sample_rate", "channels", "loudness_lufs", "true_peak_dbtp"}
    _only(data, keys, keys, path)
    bitrate = data["bitrate_kbps"]
    loudness = data["loudness_lufs"]
    return {
        "codec": _choice(data["codec"], AUDIO_CODECS, f"{path}.codec"),
        "bitrate_kbps": None if bitrate is None else _number(bitrate, f"{path}.bitrate_kbps", 16, 512, integer=True),
        "sample_rate": _number(data["sample_rate"], f"{path}.sample_rate", 8000, 96000, integer=True),
        "channels": _number(data["channels"], f"{path}.channels", 1, 2, integer=True),
        "loudness_lufs": None if loudness is None else _number(loudness, f"{path}.loudness_lufs", -30, -6),
        "true_peak_dbtp": _number(data["true_peak_dbtp"], f"{path}.true_peak_dbtp", -6, 0),
    }


def validate_platform(entry: object, index: int = 0) -> dict[str, Any]:
    path = f"platforms[{index}]"
    data = _obj(entry, path)
    required = {
        "id", "name", "group", "orientation", "max_short_side", "max_fps", "codecs", "rate_control",
        "bitrate_cap_kbps", "platform_max_kbps", "keyframe_interval_s", "audio", "transport", "tier_policy",
        "verified", "rationale", "source", "uncertain",
    }  # fmt: skip
    _only(data, required | {"quality", "h264_profile", "container", "rtmps", "rtmp_audio"}, required, path)
    codecs = data["codecs"]
    if not isinstance(codecs, list) or len(set(codecs)) != len(codecs):
        raise _fail(f"{path}.codecs", "must be a list of distinct codecs, most preferred first")
    transports = data["transport"]
    if not isinstance(transports, list) or not transports or len(set(transports)) != len(transports):
        raise _fail(f"{path}.transport", "must be a non-empty list of distinct transports, most preferred first")
    rate_control = _choice(data["rate_control"], RATE_CONTROLS, f"{path}.rate_control")
    cap, platform_max = data["bitrate_cap_kbps"], data["platform_max_kbps"]
    keyframe = data["keyframe_interval_s"]
    out: dict[str, Any] = {
        "id": _id(data["id"], f"{path}.id"),
        "name": _text(data["name"], f"{path}.name", MAX_NAME + 20),
        "group": _choice(data["group"], GROUPS, f"{path}.group"),
        "orientation": _choice(data["orientation"], ORIENTATIONS, f"{path}.orientation"),
        "max_short_side": _number(data["max_short_side"], f"{path}.max_short_side", 240, 4320, integer=True),
        "max_fps": _number(data["max_fps"], f"{path}.max_fps", 10, 120, integer=True),
        "codecs": [_choice(c, CODECS, f"{path}.codecs[{i}]") for i, c in enumerate(codecs)],
        "rate_control": rate_control,
        "bitrate_cap_kbps": None
        if cap is None
        else _number(cap, f"{path}.bitrate_cap_kbps", 100, 200000, integer=True),
        "platform_max_kbps": None
        if platform_max is None
        else _number(platform_max, f"{path}.platform_max_kbps", 100, 200000, integer=True),
        "keyframe_interval_s": None if keyframe is None else _number(keyframe, f"{path}.keyframe_interval_s", 0.5, 10),
        "audio": _audio(data["audio"], f"{path}.audio"),
        "transport": [_choice(t, TRANSPORTS, f"{path}.transport[{i}]") for i, t in enumerate(transports)],
        "tier_policy": _choice(data["tier_policy"], TIER_POLICIES, f"{path}.tier_policy"),
        "verified": _choice(data["verified"], VERIFIED, f"{path}.verified"),
        "rationale": _text(data["rationale"], f"{path}.rationale", 700),
        "source": _url(data["source"], f"{path}.source"),
        "uncertain": [_text(u, f"{path}.uncertain[{i}]", 300) for i, u in enumerate(data["uncertain"] or [])],
    }
    if out["codecs"] and rate_control == "none":
        raise _fail(path, "a platform with codecs needs a rate control")
    if out["bitrate_cap_kbps"] and out["platform_max_kbps"] and out["bitrate_cap_kbps"] > out["platform_max_kbps"]:
        raise _fail(path, "bitrate_cap_kbps must not exceed platform_max_kbps")
    if rate_control == "cqp":
        quality = _obj(data.get("quality"), f"{path}.quality")
        out["quality"] = {
            _choice(c, CODECS, f"{path}.quality"): _number(q, f"{path}.quality.{c}", 0, 63, integer=True)
            for c, q in quality.items()
        }
        if set(out["quality"]) != set(out["codecs"]):
            raise _fail(f"{path}.quality", "needs a value for every codec")
    elif "quality" in data:
        raise _fail(path, "quality only applies to cqp rate control")
    if "h264_profile" in data:
        out["h264_profile"] = _choice(data["h264_profile"], ("baseline", "main", "high"), f"{path}.h264_profile")
    if "container" in data:
        out["container"] = _choice(data["container"], ("mkv", "mp4", "mov"), f"{path}.container")
    rtmp_transport = bool({"rtmp", "enhanced-rtmp"} & set(out["transport"]))
    if "rtmps" in data:
        if not isinstance(data["rtmps"], bool):
            raise _fail(f"{path}.rtmps", "must be true or false")
        if not rtmp_transport:
            raise _fail(f"{path}.rtmps", "only applies to a platform with an rtmp transport")
        out["rtmps"] = data["rtmps"]
    if "rtmp_audio" in data:
        accepted = data["rtmp_audio"]
        if not isinstance(accepted, list) or not accepted or len(set(accepted)) != len(accepted):
            raise _fail(f"{path}.rtmp_audio", "must be a non-empty list of distinct audio codecs, most preferred first")
        if not rtmp_transport:
            raise _fail(f"{path}.rtmp_audio", "only applies to a platform with an rtmp transport")
        out["rtmp_audio"] = [_choice(c, RTMP_AUDIO_CODECS, f"{path}.rtmp_audio[{i}]") for i, c in enumerate(accepted)]
        if "opus" in out["rtmp_audio"] and "enhanced-rtmp" not in out["transport"]:
            raise _fail(f"{path}.rtmp_audio", "opus needs the enhanced-rtmp transport")
    if "file" in out["transport"] and "container" not in out:
        raise _fail(path, "a file transport needs a container")
    return out


def validate_tier(entry: object, index: int = 0) -> dict[str, Any]:
    path = f"tiers[{index}]"
    data = _obj(entry, path)
    _only(
        data,
        {"id", "name", "blurb", "min_uplink_mbps", "ladders"},
        {"id", "name", "blurb", "min_uplink_mbps", "ladders"},
        path,
    )
    ladders = _obj(data["ladders"], f"{path}.ladders")
    if set(ladders) != set(CODECS):
        raise _fail(f"{path}.ladders", f"needs exactly {', '.join(CODECS)}")
    clean: dict[str, list[dict[str, int]]] = {}
    for codec in CODECS:
        rungs = ladders[codec]
        if not isinstance(rungs, list) or not rungs:
            raise _fail(f"{path}.ladders.{codec}", "must be a non-empty list of rungs")
        rows = []
        for i, rung in enumerate(rungs):
            where = f"{path}.ladders.{codec}[{i}]"
            item = _obj(rung, where)
            _only(item, {"short_side", "fps", "bitrate_kbps"}, {"short_side", "fps", "bitrate_kbps"}, where)
            rows.append(
                {
                    "short_side": _number(item["short_side"], f"{where}.short_side", 240, 4320, integer=True),
                    "fps": _number(item["fps"], f"{where}.fps", 10, 120, integer=True),
                    "bitrate_kbps": _number(item["bitrate_kbps"], f"{where}.bitrate_kbps", 100, 200000, integer=True),
                }
            )
        order = [(r["short_side"], r["fps"]) for r in rows]
        if order != sorted(order, reverse=True) or len(set(order)) != len(order):
            raise _fail(f"{path}.ladders.{codec}", "rungs must run from largest to smallest, without repeats")
        clean[codec] = rows
    shapes = {codec: [(r["short_side"], r["fps"]) for r in rows] for codec, rows in clean.items()}
    if len({tuple(v) for v in shapes.values()}) != 1:
        raise _fail(f"{path}.ladders", "every codec needs the same resolution and frame-rate rungs")
    return {
        "id": _id(data["id"], f"{path}.id"),
        "name": _text(data["name"], f"{path}.name", MAX_NAME),
        "blurb": _text(data["blurb"], f"{path}.blurb", MAX_BLURB),
        "min_uplink_mbps": _number(data["min_uplink_mbps"], f"{path}.min_uplink_mbps", 0.5, 10000),
        "ladders": clean,
    }


# --- profiles -----------------------------------------------------------------------------------------------


def validate_profile(
    entry: object,
    *,
    platforms: set[str] | None = None,
    tiers: set[str] | None = None,
    path: str = "profile",
) -> dict[str, Any]:
    """One profile. Look, lighting and voice are names looked up when the profile is applied (the user may have
    saved their own); platform and tier are checked against the catalog when it is given. A part may be None: apply
    leaves that part of the rig as it is."""
    data = _obj(entry, path)
    parts = ("look", "platform", "tier", "lighting", "voice", "framing")
    _only(data, {"id", "name", "blurb", "icon", *parts}, {"name"}, path)
    out: dict[str, Any] = {
        "name": _text(data["name"], f"{path}.name", MAX_NAME),
        "blurb": _text(data.get("blurb") or "", f"{path}.blurb", MAX_BLURB, blank_ok=True),
        "icon": None,
    }
    if "id" in data:
        out["id"] = _id(data["id"], f"{path}.id")
    if data.get("icon") is not None:
        icon = data["icon"]
        if not isinstance(icon, str) or not ICON.fullmatch(icon):
            raise _fail(f"{path}.icon", "must be an icon name")
        out["icon"] = icon
    for part in ("look", "lighting", "voice"):
        value = data.get(part)
        out[part] = None if value is None else _text(value, f"{path}.{part}", MAX_NAME)
    for part, known in (("platform", platforms), ("tier", tiers)):
        value = data.get(part)
        if value is None:
            out[part] = None
            continue
        value = _id(value, f"{path}.{part}")
        if known is not None and value not in known:
            raise _fail(f"{path}.{part}", f"unknown {part} {value!r}")
        out[part] = value
    framing = data.get("framing")
    out["framing"] = None if framing is None else _choice(framing, FRAMING_MODES, f"{path}.framing")
    if (out["platform"] is None) != (out["tier"] is None):
        raise _fail(path, "platform and tier go together: set both or neither")
    return out


def validate_profile_document(
    document: object, *, platforms: set[str] | None = None, tiers: set[str] | None = None
) -> list[dict[str, Any]]:
    """An import or export file: one profile (`streamstack.profile/1`) or a list (`streamstack.profiles/1`)."""
    data = _obj(document, "document")
    schema = data.get("schema")
    if schema == PROFILE_SCHEMA:
        _only(data, {"schema", "profile"}, {"profile"}, "document")
        return [validate_profile(data["profile"], platforms=platforms, tiers=tiers)]
    if schema == PROFILES_SCHEMA:
        _only(data, {"schema", "profiles"}, {"profiles"}, "document")
        items = data["profiles"]
        if not isinstance(items, list) or not items:
            raise _fail("document.profiles", "must be a non-empty list")
        if len(items) > MAX_IMPORT_PROFILES:
            raise _fail("document.profiles", f"holds at most {MAX_IMPORT_PROFILES} profiles")
        return [
            validate_profile(item, platforms=platforms, tiers=tiers, path=f"profiles[{i}]")
            for i, item in enumerate(items)
        ]
    raise _fail("document.schema", f"must be {PROFILE_SCHEMA!r} or {PROFILES_SCHEMA!r}")
