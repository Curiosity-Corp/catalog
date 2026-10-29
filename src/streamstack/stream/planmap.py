"""A Presets plan as stream settings (`stream.set`), shared by the Console and automatic downshift (#98).

`streamstack.presets.plan.resolve` turns a platform and a bandwidth tier into a plan: a picture size, a frame rate, the
codecs with their bitrates, rate control, key frame interval and audio. The stream backend works in
`EncoderSettings` instead (a resolution tier name, an fps, one bitrate), so this is the one place that maps the first
onto the second. The Console applies it when a platform and tier are chosen; the downshift controller applies it to
each step of the ladder, so a downshifted stream is always a plan the Presets page could have produced.
"""

from __future__ import annotations

from typing import Any

from streamstack.stream.model import CODECS, RATE_CONTROLS, TIERS


class PlanError(ValueError):
    """The plan is not what `streamstack.presets.plan.resolve` returns."""


def plan_to_settings(plan: object) -> dict[str, Any]:
    """The stream settings that a Presets plan (`streamstack.presets.plan.resolve`) asks for.

    `stream.set` also arrives as `{"platform": id, "tier": bandwidth-tier id, "plan": {...}}` from the Presets page. The
    plan's `tier` is a bandwidth tier, not a resolution tier, so it is never copied; the resolution comes from the
    plan's picture size (the largest tier whose short side fits), the rest from the plan's rate control, key frame
    interval, first codec's bitrate or quality, and audio bitrate. A plan with several codecs leaves the choice to the
    planner (`codec: auto`); a plan with one pins it.
    """
    if not isinstance(plan, dict) or not isinstance(plan.get("video"), dict):
        raise PlanError("plan must be the object the Presets page resolves (with a video section).")
    video = plan["video"]
    out: dict[str, Any] = {}
    short = (
        min(v for v in (video.get("width"), video.get("height")) if isinstance(v, int) and v > 0)
        if (isinstance(video.get("width"), int) and isinstance(video.get("height"), int))
        else 0
    )
    fitting = [name for name, (_w, high) in TIERS.items() if high <= short]
    if short:
        out["tier"] = fitting[-1] if fitting else next(iter(TIERS))
    if isinstance(video.get("fps"), (int, float)) and video["fps"] >= 5:
        out["fps"] = int(video["fps"])
    if video.get("rate_control") in RATE_CONTROLS:
        out["rate_control"] = video["rate_control"]
    if isinstance(video.get("keyframe_interval_s"), (int, float)) and video["keyframe_interval_s"] > 0:
        out["keyint_s"] = float(video["keyframe_interval_s"])
    codecs = [c for c in video.get("codecs") or [] if isinstance(c, dict) and c.get("codec") in CODECS]
    if codecs:
        out["codec"] = codecs[0]["codec"] if len(codecs) == 1 else "auto"
        if isinstance(codecs[0].get("bitrate_kbps"), (int, float)) and codecs[0]["bitrate_kbps"] > 0:
            out["bitrate_kbps"] = int(codecs[0]["bitrate_kbps"])
        if isinstance(codecs[0].get("quality"), (int, float)) and codecs[0]["quality"] > 0:
            out["quality"] = int(codecs[0]["quality"])
    audio = plan.get("audio")
    if isinstance(audio, dict) and isinstance(audio.get("bitrate_kbps"), (int, float)) and audio["bitrate_kbps"] > 0:
        out["audio_bitrate_kbps"] = int(audio["bitrate_kbps"])
    return out
