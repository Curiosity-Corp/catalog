"""Recording and streaming in the Console: the `stream.*` and `record.*` actions and the `stream` state.

The work is done by `streamstack.stream.service.StreamService` (encoders, relay, sinks); this module is the thin
adapter the rig talks to: it turns actions into service calls, service errors into `ActionError`, and hands the rig
a state dictionary whose schema is documented in docs/stream.md.

    stream.start   {destinations?: [id]}      start the enabled live destinations (or the named ones)
    stream.stop    {destination?: id}         stop all live destinations (or one); recordings keep going
    stream.set     {codec?, encoder?, tier?, fps?, bitrate_kbps?, rate_control?, keyint_s?, quality?,
                    audio_bitrate_kbps?, destinations?: [...], adapt?: bool, plan?, platform?, tier?}
                                              change settings, live where the encoder allows; `adapt` turns automatic
                                              downshift on or off
    stream.probe   {refresh?: bool}           what this host can encode and send, with the reason for every "no"
    record.start   {format?: mkv|mp4, name?}  record the program to a file
    record.stop
    replay.start   {seconds?: 5-600}          keep the last N seconds in memory (instant replay)
    replay.save    {seconds?, name?}          write the last N seconds to a clip in the recordings folder
    replay.stop                               stop the replay buffer and free its memory

`DemoStream` is the same surface without a camera, an encoder or a network, for `--demo` and the e2e suite.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

from streamstack.console.errors import ActionError
from streamstack.hardware.plan import Plan
from streamstack.presets import catalog
from streamstack.presets.schema import PresetError
from streamstack.stream import adapt, planmap
from streamstack.stream.model import (
    Destination,
    EncoderSettings,
    SettingsError,
    classify_change,
    edit_destinations,
)
from streamstack.stream.replay import DEFAULT_MAX_MB, DEFAULT_SECONDS, MAX_SECONDS, MIN_SECONDS, idle_stats
from streamstack.stream.service import IDLE_INGEST, StreamError, StreamService, default_state_path

Notify = Callable[[str, str], None]


def _ids(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ActionError("destinations must be a list of destination ids.", 400)
    return value


def _whole(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise ActionError(f"{name} must be a whole number.", 400)
    return int(value)


def _flag(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ActionError(f"{name} must be true or false.", 400)
    return value


def plan_to_settings(plan: object) -> dict[str, Any]:
    """The stream settings that a Presets plan asks for (`streamstack.stream.planmap`), as a 400 on a bad plan."""
    try:
        return planmap.plan_to_settings(plan)
    except planmap.PlanError as exc:
        raise ActionError(str(exc), 400) from None


def settings_params(params: dict) -> dict:
    """`stream.set` params as plain settings.

    A Presets contract (`plan`) is translated; its `platform` and bandwidth `tier` ids become `ladder`, the plan that
    automatic downshift steps down from (docs/stream.md). A bare `ladder: null` forgets it.
    """
    if "plan" not in params:
        return params
    for key in ("platform", "tier"):
        if key in params and not isinstance(params[key], str):
            raise ActionError(f"{key} must be an id.", 400)
    rest = {k: v for k, v in params.items() if k not in ("plan", "platform", "tier")}
    ladder = (
        {"ladder": {"platform": params["platform"], "tier": params["tier"]}}
        if {"platform", "tier"} <= set(params)
        else {}
    )
    return plan_to_settings(params["plan"]) | ladder | rest


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
        if action == "ingest.start":
            port, latency, name, key_ref = _ingest_params(params)
            return svc.start_ingest(port, name, latency, key_ref)
        if action == "ingest.stop":
            return svc.stop_ingest()
        if action == "replay.start":
            return svc.start_replay(_whole(params.get("seconds", DEFAULT_SECONDS), "seconds"))
        if action == "replay.save":
            name = params.get("name", "")
            if not isinstance(name, str):
                raise ActionError("name must be text.", 400)
            seconds = _whole(params["seconds"], "seconds") if params.get("seconds") is not None else None
            result = svc.save_replay(seconds, name)
            return f"Saved {result['seconds']} s to {result['file']}." + (
                " The buffer was not that long yet." if result["short"] else ""
            )
        if action == "replay.stop":
            return svc.stop_replay()
        raise ActionError(f"unknown action: {action}", 400)

    def state(self) -> dict[str, Any]:
        return self.service.state()


def _ingest_params(params: Mapping[str, Any]) -> tuple[int, int, str, str]:
    port, latency = params.get("port"), params.get("latency_ms", 200)
    name, key_ref = params.get("name", ""), params.get("key_ref", "")
    if isinstance(port, bool) or not isinstance(port, int):
        raise ActionError("port must be a number from 1024 to 65535.", 400)
    if isinstance(latency, bool) or not isinstance(latency, int):
        raise ActionError("latency_ms must be a number of milliseconds.", 400)
    if not isinstance(name, str) or not isinstance(key_ref, str):
        raise ActionError("name and key_ref must be text.", 400)
    return port, latency, name, key_ref


class DemoStream:
    """A believable simulation with the same state schema: nothing is probed, encoded or sent."""

    ADAPT: ClassVar[adapt.AdaptConfig] = adapt.AdaptConfig(  # the same rules, on a clock a test or a demo can watch
        eval_interval_s=0.5,
        down_after=2,
        cooldown_s=1.5,
        min_gap_s=1.0,
        up_hold_s=3.0,
        flap_window_s=20.0,
        reset_calm_s=60.0,
    )

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
        self._ingest: dict[str, Any] | None = None  # SRT input: the port it listens on and when it began
        self._replay_began = 0.0
        self._replay_seconds = 0
        self._clips: list[str] = []
        self._swap_until = 0.0
        self._swaps = 0
        self._restarts = 0
        self._last_swap: dict[str, Any] | None = None
        self._events: list[dict[str, Any]] = []
        self._effective = self._settings
        self._ladder: dict[str, str] | None = None
        self._adapt: adapt.AdaptController | None = None
        self._adapt_on = True
        self._adapt_next = 0.0
        self._adapt_last = adapt.Verdict()
        self._adapt_judge = adapt.Judge(self.ADAPT)
        self._sim_loss = 0.0  # percent packet loss the demo pretends the uplink has (`stream.simulate`)

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
                ladder_given = "ladder" in patch
                ladder_req = patch.pop("ladder", None)
                adapt_req = patch.pop("adapt", None)
                if adapt_req is not None and not isinstance(adapt_req, bool):
                    raise ActionError("adapt must be true or false.", 400)
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
                reset = self._plan_ladder(patch, ladder_given, ladder_req, adapt_req)
                effective = (
                    settings
                    if reset or self._adapt is None
                    else adapt.overlay(settings, self._adapt.steps[self._adapt.index])
                )
                kind = classify_change(self._effective, effective)
                self._settings, self._effective = settings, effective
                if adapt_req is not None:
                    self._adapt_on = adapt_req
                if edited is not None:
                    self._destinations = edited
                    self._streaming &= {d.id for d in edited}
                return self._applied(kind)
            if action == "stream.probe":
                return "Probed this host."
            if action == "stream.simulate":
                loss = params.get("loss_percent", 0)
                if isinstance(loss, bool) or not isinstance(loss, (int, float)) or not 0 <= loss <= 100:
                    raise ActionError("loss_percent must be a number from 0 to 100.", 400)
                self._sim_loss = float(loss)
                return f"The demo uplink now loses {self._sim_loss:g}% of packets."
            if action == "record.start":
                fmt = params.get("format", "mkv")
                if fmt not in ("mkv", "mp4"):
                    raise ActionError("Recording format must be mkv or mp4.", 400)
                if not self._streaming and not self._recording:
                    self._began = time.time()
                self._recording, self._rec_began, self._rec_format = True, time.time(), fmt
                self._event("info", f"Recording to a {fmt} file.")
                return "Recording."
            if action == "replay.start":
                seconds = _whole(params.get("seconds", DEFAULT_SECONDS), "seconds")
                if not MIN_SECONDS <= seconds <= MAX_SECONDS:
                    raise ActionError(f"The replay length must be from {MIN_SECONDS} to {MAX_SECONDS} seconds.", 400)
                if not self._replay_seconds and not self._streaming and not self._recording:
                    self._began = time.time()
                self._replay_began, self._replay_seconds = time.time(), seconds
                self._event("info", f"Instant replay is keeping the last {seconds} s.")
                return f"Instant replay is keeping the last {seconds} s in memory."
            if action == "replay.save":
                if not self._replay_seconds:
                    raise ActionError("Instant replay is not armed.", 409)
                depth = min(time.time() - self._replay_began, float(self._replay_seconds))
                asked = (
                    _whole(params["seconds"], "seconds") if params.get("seconds") is not None else self._replay_seconds
                )
                if not MIN_SECONDS <= asked <= self._replay_seconds:
                    raise ActionError(f"A clip can be from {MIN_SECONDS} to {self._replay_seconds} seconds.", 400)
                name = f"/home/demo/Videos/StreamStack/Replay-{len(self._clips) + 1}.mkv"
                self._clips.append(name)
                self._event("info", f"Replay clip saved to {name}.")
                return f"Saved {round(min(depth, asked), 1)} s to {name}."
            if action == "replay.stop":
                self._replay_seconds = 0
                self._event("info", "Instant replay stopped.")
                return "Instant replay stopped and its memory freed."
            if action == "record.stop":
                self._recording = False
                self._event("info", "Recording saved.")
                return "Recording saved."
            if action == "ingest.start":
                port, latency, _name, _key = _ingest_params(params)
                self._ingest = {"port": port, "latency_ms": latency, "began": time.time()}
                self._event("info", f"Listening for an SRT sender on port {port}.")
                return f"Listening for an SRT sender on port {port}."
            if action == "ingest.stop":
                self._ingest = None
                return "SRT input stopped."
        raise ActionError(f"unknown action: {action}", 400)

    def _plan_ladder(self, patch: dict, given: bool, request: object, adapt_req: bool | None) -> bool:
        """The same rules as `StreamService`: returns whether the controller starts again at the top step."""
        if given and request is not None:
            if not isinstance(request, dict) or not all(isinstance(request.get(k), str) for k in ("platform", "tier")):
                raise ActionError("ladder must be {platform: id, tier: id} or null.", 400)
            try:
                cat = catalog.load()
                steps = adapt.build_steps(cat.platform(request["platform"]), cat.tiers, request["tier"])
            except PresetError as exc:
                raise ActionError(f"Bandwidth ladder: {exc}", 400) from None
            self._ladder = {"platform": request["platform"], "tier": request["tier"]}
            self._adapt = adapt.AdaptController(steps, self.ADAPT)
            self._adapt_judge.forget()
            return True
        if given or any(key in patch for key in adapt.ADAPT_KEYS):
            self._ladder, self._adapt = None, None
            return True
        if adapt_req is not None and self._adapt is not None:
            self._adapt.reset()
            self._adapt_judge.forget()
            return True
        return False

    def _adapt_step(self, now: float) -> None:
        """One evaluation of the downshift controller against the simulated uplink (called from `state`)."""
        controller = self._adapt
        if controller is None:
            return
        if not self._streaming:
            if controller.index:
                controller.reset()
                self._adapt_judge.forget()
                self._effective = self._settings
                self._event("info", f"Picture back to {controller.steps[0].label}: the stream stopped.")
            return
        if not self._adapt_on or now < self._adapt_next:
            return
        self._adapt_next = now + self.ADAPT.eval_interval_s
        sample = adapt.LinkSample(dest="demo", name="the demo uplink", loss_percent=self._sim_loss, connected_s=60)
        self._adapt_last = self._adapt_judge.judge(sample)
        shift = controller.evaluate(now, self._adapt_last)
        if shift is None:
            return
        old, self._effective = self._effective, adapt.overlay(self._settings, controller.steps[shift.to])
        self._applied(classify_change(old, self._effective))
        controller.commit(now, shift)
        label = controller.steps[shift.to].label
        if shift.direction == "down":
            self._event("warn", f"The link is struggling ({shift.reason}); sending {label} until it recovers.")
        else:
            self._event("ok", f"The link has been steady ({shift.reason}); sending {label}.")

    def _output(self) -> dict[str, Any]:
        width, height = self._effective.size((1920, 1080))
        return {"width": width, "height": height, "fps": self._effective.fps or 30.0}

    def _adapt_state(self, now: float) -> dict[str, Any]:
        controller = self._adapt
        body: dict[str, Any] = {"enabled": self._adapt_on, "ladder": self._ladder, "available": controller is not None}
        if controller is not None:
            body.update(controller.snapshot(now))
            body["verdict"] = {"level": self._adapt_last.level, "reason": self._adapt_last.reason}
        return body

    @staticmethod
    def _demo_srt(dest: Destination, on: bool, now: float) -> dict[str, Any]:
        """What a measured SRT path reports: a 28 ms round trip that drifts a little."""
        auto = dest.options.get("latency_ms", 120) == "auto"
        rtt = round(28.0 + 3.0 * ((int(now) % 5) - 2) / 2, 1)
        recommended = max(80, round(rtt * 4))
        configured = None if auto else int(dest.options.get("latency_ms", 120))
        applied = recommended if auto else configured
        hint = ""
        if on and configured is not None and configured < recommended * 0.75:
            hint = f"Latency {configured} ms is under 4 round trips of {rtt:.0f} ms: a lost packet may arrive too late."
        return {
            "mode": str(dest.options.get("mode", "caller")),
            "auto": auto,
            "measurable": True,
            "latency_ms": applied if on else None,
            "configured_ms": configured,
            "recommended_ms": recommended if on else None,
            "rtt_ms": rtt if on else None,
            "rtt_min_ms": round(rtt - 4, 1) if on else None,
            "rtt_max_ms": round(rtt + 6, 1) if on else None,
            "probes": 30 if on else 0,
            "probe_loss_percent": 0.0,
            "age_s": 2.0 if on else None,
            "hint": hint,
        }

    def _demo_ingest(self, now: float) -> dict[str, Any]:
        """What a listening SRT input reports: waiting for a sender for 3 s, then receiving."""
        ingest = self._ingest
        if ingest is None:
            return dict(IDLE_INGEST)
        receiving = now - ingest["began"] > 3
        return {
            **IDLE_INGEST,
            "active": True,
            "state": "receiving" if receiving else "waiting",
            "port": ingest["port"],
            "latency_ms": ingest["latency_ms"],
            "file": "/home/demo/Videos/StreamStack/SRT-backup-demo.mkv" if receiving else "",
            "bytes": round((now - ingest["began"] - 3) * 480_000) if receiving else 0,
            "callers": 1 if receiving else 0,
        }

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

    def _replay_state(self, now: float, kbps: float) -> dict[str, Any]:
        cap = DEFAULT_MAX_MB * 1024 * 1024
        if not self._replay_seconds:
            return idle_stats(cap)
        depth = min(now - self._replay_began, float(self._replay_seconds))
        held = round(depth * (kbps + 128) * 125)
        return idle_stats(cap, self._replay_seconds) | {
            "active": True,
            "state": "live",
            "depth_s": round(depth, 1),
            "bytes": held,
            "peak_bytes": held,
            "groups": int(depth // 2) + 1,
            "clips": self._clips[-5:],
        }

    def state(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            self._adapt_step(now)
            active = bool(self._streaming or self._recording or self._replay_seconds)
            kbps = float(self._effective.bitrate_kbps or 3800)
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
                        (
                            self._demo_srt(dest, True, now)["latency_ms"] + 60
                            if dest.kind == "srt"
                            else 190
                            if dest.kind == "whip"
                            else 420
                        )
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
                if dest.kind == "srt" and on:  # as live: the path block exists while the destination runs
                    row["srt"] = self._demo_srt(dest, not flaky, now)
                destinations.append(row)
            live = [d for d in destinations if d["active"]]
            states = {d["state"] for d in live}
            status = "recording" if self._recording else "replay" if self._replay_seconds else "idle"
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
                "ingest": self._demo_ingest(now),
                "recording": {
                    "active": self._recording,
                    "file": f"/home/demo/Videos/StreamStack/demo.{self._rec_format}" if self._recording else "",
                    "bytes": round((now - self._rec_began) * kbps * 125) if self._recording else 0,
                    "seconds": round(now - self._rec_began) if self._recording else 0,
                    "format": self._rec_format if self._recording else "",
                    "state": "live" if self._recording else "idle",
                    "last_error": "",
                },
                "replay": self._replay_state(now, kbps * wobble),
                "settings": self._settings.to_dict(),
                "effective": self._effective.to_dict(),
                "adapt": self._adapt_state(now),
                "output": self._output(),
                "audio": {"source": "demo", "bitrate_kbps": self._settings.audio_bitrate_kbps, "ok": True, "error": ""},
                "last_swap": self._last_swap,
                "events": list(self._events),
                "probe": dict(self.PROBE),
            }


def empty_state() -> dict[str, Any]:
    """The state of a rig without recording and streaming (older rigs and tests)."""
    return DemoStream(destinations=[]).state() | {"status": "unavailable", "probe": None, "audio": {}}
