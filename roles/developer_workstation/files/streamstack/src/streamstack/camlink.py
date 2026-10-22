"""Elgato Cam Link: read the modes its HDMI input offers, pick the best one and tune OBS to it.

A Cam Link has no modes of its own: it advertises whatever the HDMI source sends (a camera in 1080p59.94 offers
1080p59.94, a laptop in 4K offers 4K30, a switched-off camera offers nothing). So the best mode is chosen from
the live listing every time, never from a fixed table, and the card is found by its USB id so the same gear works
at every property whatever /dev/videoN it lands on.

Preference, highest first: the biggest picture the setup allows (1080p, or 4K with STREAMSTACK_CAMLINK_4K), then
the highest frame rate (1080p60 > 1080p50 > 1080p30), then the format that costs the least latency (raw YUYV,
UYVY or NV12 before MJPEG and H.264, which need a decoder).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from streamstack import config, system
from streamstack.system import Mode

SOURCE_NAME = "Cam Link 4K"  # the OBS source the scene collection ships
CAP_HEIGHT = 1080
CAP_HEIGHT_4K = 2160
# Lower is better. Raw formats need no decoder; anything unlisted sits between raw and MJPEG.
FORMAT_RANK = {"YUYV": 0, "UYVY": 0, "NV12": 0, "YU12": 1, "MJPG": 3, "H264": 4}
DEFAULT_RANK = 2
PIN = re.compile(r"(\d+)x(\d+)(?:@([\d.]+))?(?:\s+(\w{4}))?")


class CamlinkError(Exception):
    """Something about the Cam Link or OBS the caller should report to the person, not a bug."""


def fps_fraction(fps: float) -> tuple[int, int]:
    """59.94 -> (60000, 1001), 30 -> (30, 1): the exact rate OBS and v4l2 use for a printed frame rate."""
    nominal = round(fps)
    if nominal > 0 and abs(fps - nominal * 1000 / 1001) < 0.02:
        return nominal * 1000, 1001
    return max(nominal, 1), 1


def short_label(mode: Mode) -> str:
    fourcc, _, height, fps = mode
    return f"{height}p{fps:g} {fourcc}"


def parse_pin(text: str) -> tuple[int, int, float | None, str | None]:
    """STREAMSTACK_CAMLINK_MODE as (width, height, fps or None, fourcc or None); ValueError when malformed."""
    match = PIN.fullmatch(text.strip())
    if not match:
        raise ValueError(f"{text!r} is not WIDTHxHEIGHT[@FPS] [FOURCC] (or auto, or off)")
    width, height, fps, fourcc = match.groups()
    return int(width), int(height), float(fps) if fps else None, fourcc.upper() if fourcc else None


def rank(mode: Mode) -> tuple[int, int, int, float]:
    """Sort key, bigger is better: picture size, whole frame rate, lowest-latency format, exact frame rate."""
    fourcc, width, height, fps = mode
    return width * height, round(fps), -FORMAT_RANK.get(fourcc, DEFAULT_RANK), fps


def choose_mode(modes: list[Mode], *, allow_4k: bool = False, pin: str = "auto") -> Mode | None:
    """The best of the offered `modes`, or the pinned one when the source offers it.

    Without 4K the choice is capped at 1080 lines. A source that only offers more than the cap (a 4K laptop)
    still gets its smallest offered size, which OBS scales down, rather than nothing.
    """
    if not modes:
        return None
    wanted = pin.strip()
    if wanted.lower() not in ("", "auto", "off"):
        try:
            width, height, fps, fourcc = parse_pin(wanted)
        except ValueError:
            pass  # a typo in the settings file must not leave the studio without a picture; fall back to auto
        else:
            matches = [
                m
                for m in modes
                if (m[1], m[2]) == (width, height)
                and (fps is None or abs(m[3] - fps) < 0.02)
                and (fourcc is None or m[0] == fourcc)
            ]
            if matches:
                return max(matches, key=rank)
    cap = CAP_HEIGHT_4K if allow_4k else CAP_HEIGHT
    allowed = [m for m in modes if m[2] <= cap]
    if allowed:
        return max(allowed, key=rank)
    smallest = min(m[1] * m[2] for m in modes)
    return max((m for m in modes if m[1] * m[2] == smallest), key=rank)


def pack(a: int, b: int) -> int:
    """OBS stores a pair of numbers (width and height, or a frame interval) in one integer setting."""
    return (a << 16) | b


def fourcc_int(fourcc: str) -> int:
    code = fourcc.encode("ascii")
    return code[0] | code[1] << 8 | code[2] << 16 | code[3] << 24


def obs_input_settings(mode: Mode) -> dict[str, int]:
    """The v4l2_input (OBS "Video Capture Device") settings that select `mode`."""
    fourcc, width, height, fps = mode
    num, den = fps_fraction(fps)
    return {"pixelformat": fourcc_int(fourcc), "resolution": pack(width, height), "framerate": pack(den, num)}


def video_size(cfg: dict[str, str], mode: Mode | None) -> tuple[int, int, int, int]:
    """(width, height, fps numerator, fps denominator) of the OBS canvas.

    With a Cam Link mode (and STREAMSTACK_CAMLINK_MODE not "off") the canvas matches it, so the picture is neither
    scaled nor does it drop or repeat frames. Otherwise it is STREAMSTACK_WIDTH, _HEIGHT and _FPS.
    """
    if mode is not None and cfg["STREAMSTACK_CAMLINK_MODE"].strip().lower() != "off":
        _, width, height, fps = mode
        num, den = fps_fraction(fps)
        return width, height, num, den
    return int(cfg["STREAMSTACK_WIDTH"]), int(cfg["STREAMSTACK_HEIGHT"]), int(cfg["STREAMSTACK_FPS"]), 1


@dataclass
class Probe:
    device: str = ""  # "" when no Cam Link is plugged in
    offered: list[Mode] = field(default_factory=list)
    chosen: Mode | None = None
    active: Mode | None = None  # what the driver is set to now (OBS's pick while it holds the card)
    applied: Mode | None = None  # the mode this tool last tuned OBS to
    audio: str = ""  # description of the HDMI audio source, "" when there is none
    note: str = ""

    @property
    def present(self) -> bool:
        return bool(self.device)

    @property
    def signal(self) -> bool:
        return bool(self.offered)

    @property
    def labels(self) -> list[str]:
        return [short_label(m) for m in self.offered]

    @property
    def retune(self) -> bool:
        """The HDMI source changed mode since OBS was tuned (a camera switched to 60 fps, say)."""
        return bool(self.chosen and self.applied and rank(self.chosen)[:3] != rank(self.applied)[:3])

    def public(self) -> dict:
        """The snapshot the Console renders."""
        state, mode = "off", ""
        if self.signal and self.chosen:
            state, mode = ("info" if self.retune else "ok"), short_label(self.chosen)
        active = ""
        if self.active:
            fourcc, width, height, fps = self.active
            active = f"{width}x{height} {fourcc}" + (f" @ {fps:g} fps" if fps else "")
        return {
            "present": self.present,
            "device": self.device,
            "signal": self.signal,
            "state": state,
            "mode": mode,
            "offered": self.labels,
            "active": active,
            "audio": self.audio,
            "retune": self.retune,
            "note": self.note,
        }


def probe(cfg: dict[str, str] | None = None, sources: dict[str, str] | None = None) -> Probe:
    """Look at the Cam Link now. Read-only: v4l2 listings and the audio source list, nothing is opened for capture."""
    cfg = cfg or config.load_settings()
    device = system.find_camlink()
    if not device:
        return Probe()
    offered = system.camera_modes(device)
    note = ""
    pin = cfg["STREAMSTACK_CAMLINK_MODE"]
    if pin.strip().lower() not in ("", "auto", "off"):
        try:
            parse_pin(pin)
        except ValueError as exc:
            note = f"STREAMSTACK_CAMLINK_MODE ignored: {exc}"
    chosen = choose_mode(offered, allow_4k=config.truthy(cfg["STREAMSTACK_CAMLINK_4K"]), pin=pin)
    audio_name, audio_desc = system.capture_card_audio(sources if sources is not None else system.audio_sources())
    return Probe(
        device=device,
        offered=offered,
        chosen=chosen,
        active=system.current_mode(device),
        applied=load_applied(),
        audio=audio_desc or audio_name,
        note=note,
    )


# -- what OBS was last tuned to ---------------------------------------------------------------


def applied_file() -> Path:
    return config.state_dir() / "camlink.json"


def load_applied() -> Mode | None:
    try:
        data = json.loads(applied_file().read_text())
        return str(data["fourcc"]), int(data["width"]), int(data["height"]), float(data["fps"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_applied(mode: Mode) -> None:
    fourcc, width, height, fps = mode
    path = applied_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"fourcc": fourcc, "width": width, "height": height, "fps": fps}))
        os.replace(tmp, path)
    except OSError as exc:
        raise CamlinkError(f"could not record the applied mode in {path}: {exc}") from exc


# -- OBS --------------------------------------------------------------------------------------


def patch_scene_collection(text: str, mode: Mode, source: str = SOURCE_NAME) -> str:
    """A rendered scenes.json with the Cam Link source set to `mode`."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise CamlinkError(f"the OBS scene collection is not valid JSON: {exc}") from exc
    for item in data.get("sources", []):
        if item.get("id") == "v4l2_input" and item.get("name") == source:
            item.setdefault("settings", {}).update(obs_input_settings(mode))
    return json.dumps(data, indent=4) + "\n"


def apply_to_obs(cfg: dict[str, str], mode: Mode, conn=None) -> list[str]:
    """Tune the running OBS to `mode` through obs-websocket; returns what was done, one line per step.

    The Cam Link source is always updated. The canvas (base and output size, frame rate) follows only while
    nothing is on air, because OBS refuses to change it under an active output and a resize mid-call would
    freeze the virtual camera. Raises CamlinkError when OBS cannot be reached or refuses.
    """
    from streamstack.obs import OBS

    opened = conn is None
    try:
        conn = conn or OBS()
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        raise CamlinkError(f"cannot reach OBS (is it running with its WebSocket server on?): {exc}") from exc
    try:
        steps = []
        label = short_label(mode)
        try:
            conn.call("SetInputSettings", inputName=SOURCE_NAME, inputSettings=obs_input_settings(mode), overlay=True)
        except RuntimeError as exc:
            raise CamlinkError(f"OBS would not set the {SOURCE_NAME} source: {exc}") from exc
        steps.append(f"{SOURCE_NAME} source set to {label}")
        if cfg["STREAMSTACK_CAMLINK_MODE"].strip().lower() == "off":
            steps.append("canvas left alone (STREAMSTACK_CAMLINK_MODE=off)")
        else:
            busy = [
                name
                for name, request, key in (
                    ("virtual camera", "GetVirtualCamStatus", "outputActive"),
                    ("recording", "GetRecordStatus", "outputActive"),
                    ("stream", "GetStreamStatus", "outputActive"),
                )
                if _output_active(conn, request, key)
            ]
            if busy:
                steps.append(f"canvas not changed while the {' and '.join(busy)} is on; run this again when it is off")
            else:
                width, height, num, den = video_size(cfg, mode)
                try:
                    conn.call(
                        "SetVideoSettings",
                        baseWidth=width,
                        baseHeight=height,
                        outputWidth=width,
                        outputHeight=height,
                        fpsNumerator=num,
                        fpsDenominator=den,
                    )
                except RuntimeError as exc:
                    raise CamlinkError(f"OBS would not change the canvas: {exc}") from exc
                steps.append(f"canvas set to {width}x{height} at {f'{num / den:.2f}'.rstrip('0').rstrip('.')} fps")
        save_applied(mode)
        return steps
    finally:
        if opened:
            conn.close()


def _output_active(conn, request: str, key: str) -> bool:
    try:
        return bool(conn.call(request).get(key))
    except RuntimeError:
        return False  # an OBS without that output type cannot have it on
