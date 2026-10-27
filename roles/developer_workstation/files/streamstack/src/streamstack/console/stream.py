"""Recording and streaming in the Console: the `stream.*` and `record.*` actions and the `stream` state.

The work is done by `streamstack.stream.service.StreamService` (encoders, relay, sinks); this module is the thin
adapter the rig talks to: it turns actions into service calls, service errors into `ActionError`, and hands the rig
a state dictionary whose schema is documented in docs/stream.md.

    stream.start   {destinations?: [id]}      start the enabled live destinations (or the named ones)
    stream.stop    {destination?: id}         stop all live destinations (or one); recordings keep going
    stream.set     {codec?, encoder?, tier?, fps?, bitrate_kbps?, rate_control?, keyint_s?, quality?,
                    audio_bitrate_kbps?, destinations?: [...]}   change settings, live where the encoder allows
    stream.probe   {refresh?: bool}           what this host can encode and send, with the reason for every "no"
    record.start   {format?: mkv|mp4, name?}  record the program to a file
    record.stop

`DemoStream` is the same surface without a camera, an encoder or a network, for `--demo` and the e2e suite.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

from streamstack.console.errors import ActionError
from streamstack.hardware.plan import Plan
from streamstack.stream.model import (
    CODECS,
    RATE_CONTROLS,
    TIERS,
    Destination,
    EncoderSettings,
    SettingsError,
    classify_change,
    edit_destinations,
)
from streamstack.stream.service import StreamError, StreamService, default_state_path

Notify = Callable[[str, str], None]


def _ids(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ActionError("destinations must be a list of destination ids.", 400)
    return value


def _flag(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ActionError(f"{name} must be true or false.", 400)
    return value


def plan_to_settings(plan: object) -> dict[str, Any]:
    """The stream settings that a Presets plan (`streamstack.presets.plan.resolve`) asks for.

    `stream.set` also arrives as `{"platform": id, "tier": bandwidth-tier id, "plan": {...}}` from the Presets page. The
    plan's `tier` is a bandwidth tier, not a resolution tier, so it is never copied; the resolution comes from the
    plan's picture size (the largest tier whose short side fits), the rest from the plan's rate control, key frame
    interval, first codec's bitrate or quality, and audio bitrate. A plan with several codecs leaves the choice to the
    planner (`codec: auto`); a plan with one pins it.
    """
    if not isinstance(plan, dict) or not isinstance(plan.get("video"), dict):
        raise ActionError("plan must be the object the Presets page resolves (with a video section).", 400)
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


def settings_params(params: dict) -> dict:
    """`stream.set` params as plain settings: a Presets contract (`plan`) is translated, `platform` is dropped."""
    if "plan" not in params:
        return params
    for key in ("platform", "tier"):
        if key in params and not isinstance(params[key], str):
            raise ActionError(f"{key} must be an id.", 400)
    rest = {k: v for k, v in params.items() if k not in ("plan", "platform", "tier")}
    return plan_to_settings(params["plan"]) | rest


class StreamController:
    """The live implementation: a `StreamService` behind the rig's controller interface."""

    def __init__(
        self,
        cfg: Mapping[str, str],
        service: StreamService | None = None,
        plan: Callable[[], Plan | None] | None = None,
    ) -> None:
        self.service = service or StreamService(cfg, state_path=default_state_path(), plan=plan)
        self._notify: Notify | None = None
        self.service.notify = self._toast

    @property
    def notify(self) -> Notify | None:
        return self._notify

    @notify.setter
    def notify(self, hook: Notify | None) -> None:
        self._notify = hook

    def _toast(self, level: str, text: str) -> None:
        if self._notify:
            self._notify(level, text)

    def start(self) -> None:
        self.service.start()

    def stop(self) -> None:
        self.service.shutdown()

    def act(self, action: str, params: dict) -> str | None:
        try:
            return self._act(action, params)
        except StreamError as exc:
            raise ActionError(exc.message, exc.status) from None
        except SettingsError as exc:
            raise ActionError(str(exc), 400) from None

    def _act(self, action: str, params: dict) -> str | None:
        svc = self.service
        if action == "stream.start":
            return svc.start_stream(_ids(params.get("destinations")))
        if action == "stream.stop":
            destination = params.get("destination")
            if destination is not None and not isinstance(destination, str):
                raise ActionError("destination must be a destination id.", 400)
            return svc.stop_stream(destination)
        if action == "stream.set":
            if not params:
                raise ActionError("Nothing to change: pass the settings to set.", 400)
            return svc.set(settings_params(params))
        if action == "stream.probe":
            refresh = _flag(params["refresh"], "refresh") if "refresh" in params else False
            svc.host(refresh)
            return "Probed this host."
        if action == "record.start":
            fmt = params.get("format", "mkv")
            name = params.get("name", "")
            if not isinstance(fmt, str) or not isinstance(name, str):
                raise ActionError("format and name must be text.", 400)
            return svc.start_record(fmt, name)
        if action == "record.stop":
            return svc.stop_record()
        raise ActionError(f"unknown action: {action}", 400)

    def state(self) -> dict[str, Any]:
        return self.service.state()


class DemoStream:
    """A believable simulation with the same state schema: nothing is probed, encoded or sent."""

    PROBE: ClassVar[dict[str, Any]] = {
        "ffmpeg": "demo",
        "version": "demo",
        "cores": 8,
        "render_nodes": ["/dev/dri/renderD128"],
        "encoders": [
            {
                "name": "av1_vaapi",
                "codec": "av1",
                "backend": "vaapi",
                "label": "AV1 on the GPU (VA-API)",
                "device": "/dev/dri/renderD128",
                "ok": True,
                "reason": "",
                "trial_ms": 120,
            },
            {
                "name": "hevc_vaapi",
                "codec": "hevc",
                "backend": "vaapi",
                "label": "HEVC on the GPU (VA-API)",
                "device": "/dev/dri/renderD128",
                "ok": True,
                "reason": "",
                "trial_ms": 110,
            },
            {
                "name": "h264_vaapi",
                "codec": "h264",
                "backend": "vaapi",
                "label": "H.264 on the GPU (VA-API)",
                "device": "/dev/dri/renderD128",
                "ok": True,
                "reason": "",
                "trial_ms": 90,
            },
            {
                "name": "libx264",
                "codec": "h264",
                "backend": "software",
                "label": "H.264 on the CPU (x264)",
                "device": "",
                "ok": True,
                "reason": "",
                "trial_ms": 60,
            },
            {
                "name": "av1_nvenc",
                "codec": "av1",
                "backend": "nvenc",
                "label": "AV1 on the GPU (NVENC)",
                "device": "",
                "ok": False,
                "reason": "no NVIDIA GPU or driver (NVENC cannot load)",
                "trial_ms": 40,
            },
        ],
        "transports": [
            {"id": "record-mkv", "label": "Recording (Matroska)", "ok": True, "reason": "", "detail": ""},
            {"id": "srt", "label": "SRT (caller and listener)", "ok": True, "reason": "", "detail": ""},
            {"id": "rtmp-enhanced", "label": "Enhanced RTMP (AV1, HEVC)", "ok": True, "reason": "", "detail": ""},
            {
                "id": "whip",
                "label": "WHIP (WebRTC)",
                "ok": True,
                "reason": "",
                "detail": "built-in client on aiortc (demo); H.264 + Opus, HEVC and AV1 when asked for by name",
            },
        ],
        "codecs": ["av1", "hevc", "h264"],
        "problems": [],
        "age_s": 0.0,
    }

    def __init__(self, notify: Notify | None = None, destinations: list[Destination] | None = None) -> None:
        self._notify = notify
        self._lock = threading.RLock()
        self._settings = EncoderSettings()
        self._destinations: list[Destination] = (
            destinations
            if destinations is not None
            else [
                Destination.from_dict(
                    {"id": "twitch", "kind": "rtmp", "name": "Twitch", "url": "rtmp://live.demo/app"}
                ),
                Destination.from_dict(
                    {"id": "backup", "kind": "srt", "name": "Backup SRT", "url": "srt://demo.test:9000"}
                ),
            ]
        )
        self._streaming: set[str] = set()
        self._recording = False
        self._began = 0.0
        self._rec_began = 0.0
        self._rec_format = "mkv"
        self._swap_until = 0.0
        self._swaps = 0
        self._restarts = 0
        self._last_swap: dict[str, Any] | None = None
        self._events: list[dict[str, Any]] = []

    def _event(self, level: str, text: str) -> None:
        self._events.append({"at": round(time.time(), 1), "level": level, "text": text})
        del self._events[:-8]

    @property
    def notify(self) -> Notify | None:
        return self._notify

    @notify.setter
    def notify(self, hook: Notify | None) -> None:
        self._notify = hook

    def _applied(self, kind: str) -> str:
        """What the real service answers for each class of change; the demo also shows a swap in flight."""
        if kind == "none":
            return "Nothing changed."
        if not (self._streaming or self._recording):
            return "Saved; it applies when the stream starts."
        now = time.time()
        if kind == "audio":
            return "Audio bitrate changed live (an Opus frame boundary, no reconnect)."
        if kind == "codec":
            self._restarts += 1
            return "Codec changed; destinations moved to the new encoder."
        self._swaps += 1
        self._swap_until = now + 1.2
        self._event("info", "Encoder swapped at a keyframe.")
        self._last_swap = {"codec": "av1", "ok": True, "at": round(now, 1), "gap_ms": 0, "encoder": "av1_vaapi"}
        if kind == "geometry":
            self._restarts += 1
            return "Picture format changing: the encoder is swapped at a keyframe; destinations reconnect once."
        return "Applied live: a new encoder takes over at the next planned keyframe, destinations stay connected."

    def start(self) -> None:
        """Nothing runs in the background."""

    def stop(self) -> None:
        """Nothing to stop."""

    def act(self, action: str, params: dict) -> str | None:
        with self._lock:
            if action == "stream.start":
                ids = _ids(params.get("destinations"))
                chosen = [d.id for d in self._destinations if (d.id in ids if ids else d.enabled)]
                if not chosen:
                    raise ActionError("Add a destination (SRT, RTMP or WHIP) before starting a stream.", 400)
                if not self._streaming and not self._recording:
                    self._began = time.time()
                self._streaming.update(chosen)
                self._event("info", f"Streaming to {', '.join(chosen)}.")
                return "Streaming."
            if action == "stream.stop":
                destination = params.get("destination")
                if destination is not None and destination not in self._streaming:
                    raise ActionError(f"{destination!r} is not streaming.", 400)
                self._streaming.discard(destination) if destination else self._streaming.clear()
                self._event("info", f"Stopped {destination or 'the stream'}.")
                return "Stream stopped."
            if action == "stream.set":
                if not params:
                    raise ActionError("Nothing to change: pass the settings to set.", 400)
                patch = dict(settings_params(params))
                destinations = patch.pop("destinations", None)
                try:
                    settings = EncoderSettings.from_dict(patch, self._settings)
                    edited = (
                        edit_destinations(destinations, self._destinations, lambda i: f"file:/demo/keys/{i}")[0]
                        if destinations is not None
                        else None
                    )
                except SettingsError as exc:
                    raise ActionError(str(exc), 400) from None
                if settings.encoder != "auto" and settings.encoder not in {
                    e["name"] for e in self.PROBE["encoders"] if e["ok"]
                }:
                    raise ActionError(f"Encoder {settings.encoder} cannot be used on this host.", 400)
                kind = classify_change(self._settings, settings)
                self._settings = settings
                if edited is not None:
                    self._destinations = edited
                    self._streaming &= {d.id for d in edited}
                return self._applied(kind)
            if action == "stream.probe":
                return "Probed this host."
            if action == "record.start":
                fmt = params.get("format", "mkv")
                if fmt not in ("mkv", "mp4"):
                    raise ActionError("Recording format must be mkv or mp4.", 400)
                if not self._streaming and not self._recording:
                    self._began = time.time()
                self._recording, self._rec_began, self._rec_format = True, time.time(), fmt
                self._event("info", f"Recording to a {fmt} file.")
                return "Recording."
            if action == "record.stop":
                self._recording = False
                self._event("info", "Recording saved.")
                return "Recording saved."
        raise ActionError(f"unknown action: {action}", 400)

    @staticmethod
    def _demo_whip(on: bool, flaky: bool, kbps: float, now: float) -> dict[str, Any]:
        """What a connected WHIP session reports, so the Stream page can be exercised without a server."""
        if not on or flaky:
            return {}
        return {
            "ice_state": "completed",
            "dtls_state": "connected",
            "rtt_ms": round(18 + (int(now) % 5) * 0.4, 1),
            "loss_percent": 0.0,
            "jitter_ms": 2.0,
            "bitrate_kbps": round(kbps + 128, 1),
            "nacks": 0,
            "plis": 0,
            "resource": "whip.demo.test/live/session",
            "codec": "H.264",
            "trickle": True,
        }

    def state(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            active = bool(self._streaming or self._recording)
            kbps = float(self._settings.bitrate_kbps or 3800)
            wobble = 1 + 0.04 * ((int(now) % 7) - 3) / 3
            destinations = []
            for dest in self._destinations:
                on = dest.id in self._streaming
                flaky = on and "flaky" in dest.url  # a demo endpoint that never stays connected
                row = {
                    "id": dest.id,
                    "kind": dest.kind,
                    "name": dest.label(),
                    "url": dest.url,
                    "enabled": dest.enabled,
                    "key": "set" if dest.key_ref else "none",
                    "codec_wanted": dest.codec,
                    "options": dict(dest.options),
                    "active": on,
                    "state": ("backoff" if flaky else "live") if on else "idle",
                    "health": ("warn" if flaky else "ok") if on else "idle",
                    "codec": (
                        (dest.codec if dest.codec != "auto" else "h264")
                        if on and dest.kind == "whip"
                        else "av1"
                        if on and dest.kind == "rtmp"
                        else "hevc"
                        if on
                        else ""
                    ),
                    "bitrate_kbps": round(kbps * wobble + 128, 1) if on else 0.0,
                    "dropped_frames": 0,
                    "restarts": 3 + self._restarts if flaky else self._restarts if on else 0,
                    "latency_ms": (
                        (120 + 60 if dest.kind == "srt" else 190 if dest.kind == "whip" else 420)
                        if on and not flaky
                        else 0
                    ),
                    "last_error": "Connection refused by flaky.demo.test; retrying in 4 s." if flaky else "",
                    "queued_bytes": 0,
                    "connected_s": round(now - self._began, 1) if on else 0.0,
                    "moved": False,
                }
                if dest.kind == "whip":
                    row["whip"] = self._demo_whip(on, flaky, kbps * wobble, now)
                destinations.append(row)
            live = [d for d in destinations if d["active"]]
            states = {d["state"] for d in live}
            status = "recording" if self._recording else "idle"
            if live:
                status = "live" if states == {"live"} else "degraded" if states - {"connecting", "idle"} else "starting"
            cpu = round(6.5 + 2.0 * wobble, 1) if active else None
            gpu = round(5.0 + 1.5 * wobble, 1) if active else None
            return {
                "status": status,
                "uptime_s": round(now - self._began) if active else 0,
                "bitrate_kbps": round(kbps * wobble + 128, 1) if active else 0.0,
                "dropped_frames": 0,
                "latency_ms": max((d["latency_ms"] for d in live), default=0),
                "cpu_percent": cpu,
                "gpu_percent": gpu,
                "encoders": (
                    [
                        {
                            "codec": "av1",
                            "encoder": "av1_vaapi",
                            "label": "AV1 on the GPU (VA-API)",
                            "backend": "vaapi",
                            "device": "/dev/dri/renderD128",
                            "bitrate_kbps": round(kbps * wobble, 1),
                            "fps": 30.0,
                            "encoder_dropped": 0,
                            "cpu_percent": cpu,
                            "gpu_percent": gpu,
                            "latency_ms": 58.0,
                            "swapping": now < self._swap_until,
                            "swaps": self._swaps,
                            "failed": "",
                            "destinations": [d["id"] for d in live],
                        }
                    ]
                    if active
                    else []
                ),
                "encoder": "av1_vaapi" if active else "",
                "destinations": destinations,
                "recording": {
                    "active": self._recording,
                    "file": f"/home/demo/Videos/StreamStack/demo.{self._rec_format}" if self._recording else "",
                    "bytes": round((now - self._rec_began) * kbps * 125) if self._recording else 0,
                    "seconds": round(now - self._rec_began) if self._recording else 0,
                    "format": self._rec_format if self._recording else "",
                    "state": "live" if self._recording else "idle",
                    "last_error": "",
                },
                "settings": self._settings.to_dict(),
                "output": {"width": 1920, "height": 1080, "fps": self._settings.fps or 30.0},
                "audio": {"source": "demo", "bitrate_kbps": self._settings.audio_bitrate_kbps, "ok": True, "error": ""},
                "last_swap": self._last_swap,
                "events": list(self._events),
                "probe": dict(self.PROBE),
            }


def empty_state() -> dict[str, Any]:
    """The state of a rig without recording and streaming (older rigs and tests)."""
    return DemoStream(destinations=[]).state() | {"status": "unavailable", "probe": None, "audio": {}}
