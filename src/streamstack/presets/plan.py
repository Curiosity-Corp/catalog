"""Turn a platform and a bandwidth tier into one concrete stream plan.

The plan is what the pending `stream.set` action carries to the stream backend (see docs/presets.md): one
resolution and frame rate, the codecs in preference order with the bitrate (or quality) for each, rate control,
keyframe interval, audio and transport. The tier supplies the ladder, the platform supplies the ceilings and the
house rules, and the result never exceeds either.
"""

from __future__ import annotations

from typing import Any

from streamstack.presets.schema import PresetError

ASPECT = (16, 9)


def frame_size(short_side: int, orientation: str) -> tuple[int, int]:
    """Width and height (both even) for a short side in the platform's orientation."""
    long_side = round(short_side * ASPECT[0] / ASPECT[1] / 2) * 2
    short = short_side + short_side % 2
    return (long_side, short) if orientation == "landscape" else (short, long_side)


def pick_rung(platform: dict[str, Any], tier: dict[str, Any]) -> int:
    """Index of the first rung (largest first) the platform accepts. Every codec ladder has the same shape."""
    reference = tier["ladders"]["h264"]
    for index, rung in enumerate(reference):
        if rung["short_side"] <= platform["max_short_side"] and rung["fps"] <= platform["max_fps"]:
            return index
    raise PresetError(f"tier {tier['id']!r} has no rung {platform['id']!r} accepts")


def resolve(platform: dict[str, Any], tier: dict[str, Any]) -> dict[str, Any]:
    """The stream plan for one platform and one tier. Pure: same inputs, same plan."""
    audio = dict(platform["audio"])
    if platform["tier_policy"] == "ignore":
        short, fps, index = platform["max_short_side"], platform["max_fps"], None
    else:
        index = pick_rung(platform, tier)
        rung = tier["ladders"]["h264"][index]
        short, fps = rung["short_side"], rung["fps"]
    width, height = frame_size(short, platform["orientation"])
    codecs: list[dict[str, Any]] = []
    for codec in platform["codecs"]:
        if platform["rate_control"] == "cqp":
            codecs.append({"codec": codec, "quality": platform["quality"][codec]})
            continue
        bitrate = tier["ladders"][codec][index]["bitrate_kbps"] if index is not None else None
        cap = platform["bitrate_cap_kbps"]
        if bitrate is not None and cap is not None:
            bitrate = min(bitrate, cap)
        codecs.append({"codec": codec, "bitrate_kbps": bitrate})
    keyframe = platform["keyframe_interval_s"]
    video: dict[str, Any] = {
        "width": width,
        "height": height,
        "fps": fps,
        "rate_control": platform["rate_control"],
        "codecs": codecs,
        "keyframe_interval_s": keyframe,
        "gop_frames": round(fps * keyframe) if keyframe else None,
    }
    for extra in ("h264_profile", "container"):
        if extra in platform:
            video[extra] = platform[extra]
    first = next((c.get("bitrate_kbps") for c in codecs if c.get("bitrate_kbps")), None)
    uplink = None
    if first and audio["bitrate_kbps"]:
        # the first codec's bitrate plus audio, with 50 percent headroom for retransmits and other traffic
        uplink = round((first + audio["bitrate_kbps"]) * 1.5 / 1000, 1)
    return {
        "platform": platform["id"],
        "tier": tier["id"],
        "tier_applies": platform["tier_policy"] == "ladder",
        "video": video,
        "audio": audio,
        "transport": list(platform["transport"]),
        "uplink_mbps_needed": uplink,
        "uplink_mbps_tier": tier["min_uplink_mbps"],
    }
