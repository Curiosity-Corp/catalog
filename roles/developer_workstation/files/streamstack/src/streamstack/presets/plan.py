"""Turn a platform and a bandwidth tier into one concrete stream plan.

The plan is what the pending `stream.set` action carries to the stream backend (see docs/presets.md): one
resolution and frame rate, the codecs in preference order with the bitrate (or quality) for each, rate control,
keyframe interval, audio and transport. The tier supplies the ladder, the platform supplies the ceilings and the
house rules, and the result never exceeds either.
"""

from __future__ import annotations

from collections.abc import Sequence
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


MIN_STEP = 0.95  # a lower rung must save at least 5 percent of the bitrate, or it is not a step down


def ladder_tier(platform: dict[str, Any], tiers: Sequence[dict[str, Any]], tier_id: str) -> dict[str, Any]:
    """The chosen tier with its ladders continued into the lower tiers: the steps automatic downshift walks (#98).

    Rung 0 is exactly what `resolve(platform, tier)` picks, the ceiling the person chose. The rungs after it are the
    rest of that tier's ladder and then the lower tiers', each kept only when the platform accepts it and it is a real
    step down: less picture (pixels per second) than the rung before and, with a bitrate to compare, at least 5 percent
    less of it (after the platform's cap, on the H.264 reference ladder). A platform that
    ignores tiers has a fixed picture and gets a single rung. The result has the shape of a tier, so
    `resolve(platform, ladder_tier(...), index=n)` is the plan for step n.
    """
    ordered = sorted(tiers, key=lambda t: -t["min_uplink_mbps"])
    chosen = next((t for t in ordered if t["id"] == tier_id), None)
    if chosen is None:
        raise PresetError(f"unknown tier {tier_id!r}")
    if platform["tier_policy"] == "ignore":
        return {**chosen, "steps": [{"tier": chosen["id"], "index": None}]}
    steps: list[dict[str, Any]] = []
    previous: tuple[float, float] | None = None  # pixel rate and capped bitrate of the last kept rung
    cap = platform["bitrate_cap_kbps"]
    constant_quality = platform["rate_control"] == "cqp"
    for tier in ordered[ordered.index(chosen) :]:
        first = pick_rung(platform, chosen) if tier is chosen else 0
        for index in range(first, len(tier["ladders"]["h264"])):
            rung = tier["ladders"]["h264"][index]
            if rung["short_side"] > platform["max_short_side"] or rung["fps"] > platform["max_fps"]:
                continue
            pixel_rate = rung["short_side"] ** 2 * rung["fps"]
            kbps = min(rung["bitrate_kbps"], cap) if cap else rung["bitrate_kbps"]
            if previous is not None and (
                pixel_rate >= previous[0] or (not constant_quality and kbps >= previous[1] * MIN_STEP)
            ):
                continue
            steps.append({"tier": tier["id"], "index": index})
            previous = (pixel_rate, kbps)
    ladders = {
        codec: [next(t for t in tiers if t["id"] == s["tier"])["ladders"][codec][s["index"]] for s in steps]
        for codec in chosen["ladders"]
    }
    return {**chosen, "ladders": ladders, "steps": steps}


def resolve(platform: dict[str, Any], tier: dict[str, Any], index: int | None = None) -> dict[str, Any]:
    """The stream plan for one platform and one tier. Pure: same inputs, same plan.

    `index` names a rung of the tier's ladders instead of the first one the platform accepts; automatic downshift uses
    it with `ladder_tier` to ask for the plan of a lower step.
    """
    audio = dict(platform["audio"])
    if platform["tier_policy"] == "ignore":
        short, fps, index = platform["max_short_side"], platform["max_fps"], None
    else:
        if index is None:
            index = pick_rung(platform, tier)
        elif not 0 <= index < len(tier["ladders"]["h264"]):
            raise PresetError(f"tier {tier['id']!r} has no rung {index}")
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
    plan = {
        "platform": platform["id"],
        "tier": tier["id"],
        "tier_applies": platform["tier_policy"] == "ladder",
        "video": video,
        "audio": audio,
        "transport": list(platform["transport"]),
        "uplink_mbps_needed": uplink,
        "uplink_mbps_tier": tier["min_uplink_mbps"],
    }
    if {"rtmp", "enhanced-rtmp"} & set(platform["transport"]):
        # how an RTMP destination for this platform is set up: TLS where the ingest takes it, AAC unless the
        # platform is known to take Opus (none is documented to yet)
        plan["rtmp"] = {"tls": platform.get("rtmps", False), "audio": list(platform.get("rtmp_audio", ["aac"]))}
    return plan
