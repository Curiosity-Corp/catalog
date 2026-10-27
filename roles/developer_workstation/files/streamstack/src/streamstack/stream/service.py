"""The stream service: one capture-and-encode pipeline per codec, fanned out to recordings and live destinations.

What runs while something is recording or streaming:

    audio encoder (ffmpeg, Opus) ------------------------------+----------------+
    video encoder generation(s) (ffmpeg, one codec) -> Chain A -> sinks          |
    video encoder generation(s) (ffmpeg, another codec) -> Chain B -> sinks <----+

Every encoder is an ffmpeg process writing Matroska on stdout on a shared wall-clock axis (see `ffcmd.py`); the
`Chain` (see `relay.py`) cuts between encoder generations at a keyframe and hands the bytes to sinks. That is what
makes a bitrate, rate-control, keyframe or encoder change glitch-free: a new encoder starts beside the old one and the
stream moves over at a planned keyframe, without any destination reconnecting. See docs/adr/0001-*.md.

Everything here is driven from three kinds of thread: Console action threads (`start_stream`, `set`, ...), one reader
thread per child process (blocks arrive there), and a supervisor thread that ticks once per 250 ms (swap timeouts,
crashed encoders, retired generations, the RTMP fallback). Lock order is always service lock, then chain lock, then
sink lock; callbacks that run under a chain lock never take the service lock.
"""

from __future__ import annotations

import dataclasses
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from streamstack import store
from streamstack.hardware import camera as hwcamera
from streamstack.hardware import roles
from streamstack.stream import ffcmd, mkv
from streamstack.stream.ffcmd import AudioSource, EncoderChoice, Source
from streamstack.stream.model import (
    AssignError,
    Destination,
    EncoderSettings,
    SecretError,
    SettingsError,
    assign_codec,
    classify_change,
    destinations_from,
    edit_destinations,
    plan_chains,
    redact,
    resolve_secret,
)
from streamstack.stream.probe import (
    EncoderProbe,
    HostProbe,
    ProbeCache,
    default_runner,
    probe_host,
    render_signature,
    software_ok,
    source_info,
)
from streamstack.stream.proc import Child, ProgressParser
from streamstack.stream.relay import Chain
from streamstack.stream.sinks import FileSink, PipeSink
from streamstack.stream.usage import UsageMeter

if TYPE_CHECKING:
    from streamstack.stream.whip.sink import WhipSink

Notify = Callable[[str, str], None]
Spawn = Callable[..., Child]

TICK_SECONDS = 0.25
SWAP_LEAD_S = {"hardware": 1.0, "software": 1.6}  # how far ahead of now the new encoder's keyframe is planned
AUDIO_HEADER_WAIT_S = 4.0
RESTART_BACKOFF = (1.0, 2.0, 4.0, 8.0)
ENCODER_FAILURES = 3  # fast failures of one encoder before the next one for the codec is tried
FAST_FAIL_S = 20.0
TRANSPORT_LATENCY_MS = {"record": 0, "rtmp": 250, "whip": 150}  # typical ingest buffering; SRT uses its own setting
WHIP_PLAYOUT_MS = 100  # a typical server or player jitter buffer; added to the measured half round trip (not measured)
EVENT_LIMIT = 30


class StreamError(Exception):
    """A request the stream service cannot carry out; `status` is the HTTP-like code for the Console."""

    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class Gen:
    """One encoder process and the demuxer reading its Matroska output."""

    id: int
    kind: str  # video | audio
    child: Child
    started_at: float
    codec: str = ""
    choice: EncoderChoice | None = None
    settings: EncoderSettings = field(default_factory=EncoderSettings)
    demux: mkv.Demuxer = field(default_factory=mkv.Demuxer)
    progress: ProgressParser = field(default_factory=ProgressParser)
    header_seen: threading.Event = field(default_factory=threading.Event)
    fps: float = 0.0
    encoder_drops: int = 0
    speed: float = 0.0
    dead: bool = False


@dataclass
class Pending:
    """A video swap in flight: what the stream becomes if it completes."""

    gen: int
    settings: EncoderSettings
    choice: EncoderChoice
    attempt: int = 1


@dataclass
class Run:
    """The video pipeline for one codec: a chain, its encoder generations, and what is in force."""

    codec: str
    chain: Chain
    settings: EncoderSettings
    choice: EncoderChoice
    gens: dict[int, Gen] = field(default_factory=dict)
    pending: Pending | None = None
    failures: int = 0
    bad_encoders: set[str] = field(default_factory=set)
    next_restart: float = 0.0
    dead_at: float = 0.0
    failed: str = ""


@dataclass
class Active:
    """A destination that is running."""

    dest: Destination
    codec: str
    sink: PipeSink | FileSink | WhipSink
    started_at: float
    path: Path | None = None  # recordings
    moved: bool = False  # already fell back from Enhanced RTMP


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:60] or "recording"


class StreamService:
    def __init__(
        self,
        cfg: Mapping[str, str],
        *,
        probe: ProbeCache | None = None,
        notify: Notify | None = None,
        spawn: Spawn | None = None,
        wall: Callable[[], float] = time.time,
        state_path: Path | None = None,
        source_override: Source | None = None,
        audio_override: AudioSource | None = None,
        plan: roles.PlanSource | None = None,
    ) -> None:
        self.cfg = dict(cfg)
        self.notify: Notify = notify or (lambda level, text: None)
        self._plan = plan or (lambda: None)  # the hardware Plan: which GPU encodes (ADR 0003)
        self._probe = probe or ProbeCache(probe_host, key=render_signature)
        self._spawn_child = spawn or self._default_spawn
        self._wall = wall
        self._state_path = state_path
        self._source_override = source_override
        self._audio_override = audio_override
        self._lock = threading.RLock()
        self._settings = EncoderSettings()
        self._destinations: list[Destination] = []
        self._runs: dict[str, Run] = {}
        self._active: dict[str, Active] = {}
        self._audio: Gen | None = None
        self._audio_pending: Gen | None = None
        self._audio_track: mkv.Track | None = None
        self._audio_last_ts = -1
        self._audio_failures = 0
        self._audio_next_restart = 0.0
        self._audio_failed = ""
        self._gen_ids = 0
        self._epoch = 0.0
        self._source: Source | None = None
        self._audio_source: AudioSource | None = None
        self._retired: deque[tuple[str, int]] = deque()
        self._events: deque[dict[str, Any]] = deque(maxlen=EVENT_LIMIT)
        self._supervisor: threading.Thread | None = None
        self._stop = threading.Event()  # replaced for every session so an old supervisor can never revive
        self._started_at = 0.0
        self._last_swap: dict[str, Any] | None = None
        self._probe_thread: threading.Thread | None = None
        self._usage = UsageMeter()
        self._load()

    # -- configuration and persistence ---------------------------------------------------------------------

    @property
    def settings(self) -> EncoderSettings:
        return self._settings

    @property
    def destinations(self) -> list[Destination]:
        return list(self._destinations)

    def _load(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            data = store.read_json(self._state_path, {}, validate=store.is_object)
            self._settings = EncoderSettings.from_dict(data.get("settings"))
            self._destinations = destinations_from(data.get("destinations", []))
        except (OSError, ValueError) as exc:  # SettingsError is a ValueError; a damaged file starts clean, loudly
            self._event("warn", f"Stream settings could not be read ({exc}); starting with the defaults.")
            self._settings, self._destinations = EncoderSettings(), []

    def _save(self) -> None:
        if self._state_path is None:
            return
        body = {"settings": self._settings.to_dict(), "destinations": [d.to_dict() for d in self._destinations]}
        try:
            store.write_json(self._state_path, body, mode=0o600)  # destinations hold stream keys
        except OSError as exc:
            self._event("warn", f"Stream settings could not be saved: {exc.strerror or exc}.")

    def _default_spawn(self, name: str, argv: list[str], **kwargs: Any) -> Child:
        return Child(name, argv, **kwargs)

    def _event(self, level: str, text: str) -> None:
        self._events.append({"at": round(self._wall(), 1), "level": level, "text": text})
        self.notify(level, text)

    def source(self) -> Source:
        if self._source_override is not None:
            return self._source_override
        try:
            width, height = int(self.cfg["STREAMSTACK_WIDTH"]), int(self.cfg["STREAMSTACK_HEIGHT"])
            fps = float(self.cfg["STREAMSTACK_FPS"])
        except (KeyError, ValueError):
            width, height, fps = 1920, 1080, 30.0
        device = hwcamera.device_for("program", self.cfg, self._plan())
        return Source("v4l2", device, width, height, fps)

    def audio_source(self) -> AudioSource:
        if self._audio_override is not None:
            return self._audio_override
        name = self.cfg.get("STREAMSTACK_STREAM_AUDIO") or "streamstack_mic"
        offset = int(self.cfg.get("STREAMSTACK_STREAM_AUDIO_OFFSET_MS") or 0)
        return AudioSource("pulse", name, offset)

    def record_dir(self) -> Path:
        configured = self.cfg.get("STREAMSTACK_RECORD_DIR") or "~/Videos/StreamStack"
        return Path(configured).expanduser()

    def max_chains(self) -> int:
        try:
            return max(1, min(3, int(self.cfg.get("STREAMSTACK_STREAM_MAX_ENCODES") or 2)))
        except ValueError:
            return 2

    # -- probe -----------------------------------------------------------------------------------------------

    def host(self, refresh: bool = False) -> HostProbe:
        return self._probe.get(refresh)

    def encode_node(self) -> roles.NodeChoice:
        """The GPU the stream encodes on: the setting, else the Plan's encode role, else "no preference"."""
        return roles.choose("stream", self._plan(), self.cfg.get("STREAMSTACK_STREAM_RENDER_NODE", ""))

    def warm_probe(self) -> None:
        """Probe in the background so the first Stream page view does not wait for the trial encodes."""
        if self._probe.peek() is not None or (self._probe_thread and self._probe_thread.is_alive()):
            return
        self._probe_thread = threading.Thread(target=self._probe.get, name="stream-probe", daemon=True)
        self._probe_thread.start()

    # -- lifecycle -------------------------------------------------------------------------------------------

    def start(self) -> None:
        self.warm_probe()

    @property
    def running(self) -> bool:
        return bool(self._active)

    def shutdown(self) -> None:
        """End everything: sinks first (so files are closed cleanly), then every encoder."""
        with self._lock:
            ids = list(self._active)
        for dest_id in ids:
            try:
                self._deactivate(dest_id)
            except StreamError as exc:
                self._event("warn", exc.message)
        self._teardown()

    # -- actions ---------------------------------------------------------------------------------------------

    def start_stream(self, ids: list[str] | None = None) -> str:
        """Start every enabled live destination (or the named ones). Recording is separate (`start_record`)."""
        with self._lock:
            chosen = [d for d in self._destinations if d.live() and (d.id in ids if ids else d.enabled)]
            if ids:
                missing = sorted(set(ids) - {d.id for d in self._destinations})
                if missing:
                    raise StreamError(f"No destination named {missing[0]!r}.", 400)
            if not chosen:
                raise StreamError("Add a destination (SRT, RTMP or WHIP) before starting a stream.", 400)
            started = [d for d in chosen if d.id not in self._active]
            if not started:
                return "Already streaming."
            self._bring_up(started)
        return f"Streaming to {', '.join(d.label() for d in started)}."

    def stop_stream(self, dest_id: str | None = None) -> str:
        with self._lock:
            if dest_id:
                active = self._active.get(dest_id)
                if active is None or not active.dest.live():
                    raise StreamError(f"{dest_id!r} is not streaming.", 400)
                targets = [dest_id]
            else:
                targets = [i for i, a in self._active.items() if a.dest.live()]
            if not targets:
                return "Not streaming."
            for target in targets:
                self._deactivate(target)
            self._after_change()
        return "Stream stopped."

    def start_record(self, fmt: str = "mkv", name: str = "") -> str:
        if fmt not in ("mkv", "mp4"):
            raise StreamError("Recording format must be mkv or mp4.", 400)
        with self._lock:
            if "record" in self._active:
                return "Already recording."
            stamp = datetime.fromtimestamp(self._wall()).strftime("%Y%m%d-%H%M%S")
            path = self.record_dir() / f"{slug(name or 'StreamStack')}-{stamp}.{fmt}"
            dest = Destination("record", "record", "Recording", options={"format": fmt})
            self._bring_up([dest], paths={"record": path})
        return f"Recording to {path}."

    def stop_record(self) -> str:
        with self._lock:
            if "record" not in self._active:
                return "Not recording."
            path = self._active["record"].path
            self._deactivate("record")
            self._after_change()
        return f"Recording saved to {path}."

    def set(self, patch: Mapping[str, Any]) -> str:
        """Change encoder settings and/or the destination list. Encoder changes apply live where they can."""
        patch = dict(patch)
        new_destinations = patch.pop("destinations", None)
        with self._lock:
            old = self._settings
            try:
                new = EncoderSettings.from_dict(patch, old)
                destinations, secrets = (
                    edit_destinations(new_destinations, self._destinations, self._key_ref)
                    if new_destinations is not None
                    else (None, {})
                )
                if secrets and self._key_dir() is None:
                    raise SettingsError("This rig has no private place to keep a key; give a key_ref instead.")
            except SettingsError as exc:
                raise StreamError(str(exc), 400) from None
            if destinations is not None:
                self._check_destination_edit(destinations, set(secrets))
            kind = classify_change(old, new)
            if kind in ("codec", "geometry", "seamless"):
                self._check_encoder_choice(new)
            if secrets:
                self._write_keys(secrets)
            self._settings = new
            if destinations is not None:
                self._destinations = destinations
                self._forget_keys()
            self._save()
            if not self._runs:
                return "Saved; it applies when the stream starts."
            return self._apply_change(kind, old, new)

    # -- stream keys typed into the Console: one private file each, never in stream.json or the state -------

    def _key_dir(self) -> Path | None:
        return self._state_path.parent / "keys" if self._state_path is not None else None

    def _key_ref(self, dest_id: str) -> str:
        directory = self._key_dir()
        return f"file:{directory / dest_id}" if directory is not None else ""

    def _write_keys(self, secrets: Mapping[str, str]) -> None:
        directory = self._key_dir()
        if directory is None:
            raise StreamError("This rig has no private place to keep a key.", 400)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o700)
            for dest_id, secret in secrets.items():
                tmp = directory / f".{dest_id}.tmp"
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as handle:
                    handle.write(secret)
                os.replace(tmp, directory / dest_id)
        except OSError as exc:
            raise StreamError(f"The stream key could not be stored: {exc.strerror or 'write failed'}.", 500) from None

    def _forget_keys(self) -> None:
        """Delete the key files no destination points at any more (removed destination, key cleared or replaced)."""
        directory = self._key_dir()
        if directory is None or not directory.is_dir():
            return
        keep = {d.id for d in self._destinations if d.key_ref == self._key_ref(d.id)}
        try:
            stale = [f for f in directory.iterdir() if f.is_file() and f.name not in keep]
        except OSError as exc:
            self._event("warn", f"The stored stream keys could not be listed: {exc.strerror or 'read failed'}.")
            return
        for path in stale:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                self._event("warn", f"A stored stream key could not be deleted: {exc.strerror or 'unlink failed'}.")

    def _check_destination_edit(self, destinations: list[Destination], rekeyed: AbstractSet[str] = frozenset()) -> None:
        by_id = {d.id: d for d in destinations}
        for dest_id, active in self._active.items():
            if dest_id == "record":
                continue
            if dest_id in rekeyed:
                raise StreamError(f"{active.dest.label()} is streaming; stop it before changing its key.", 409)
            replacement = by_id.get(dest_id)
            if replacement is None:
                raise StreamError(f"{active.dest.label()} is streaming; stop it before removing it.", 409)
            if replacement.to_dict() != active.dest.to_dict():
                raise StreamError(f"{active.dest.label()} is streaming; stop it before changing it.", 409)

    def _check_encoder_choice(self, settings: EncoderSettings) -> None:
        if settings.encoder == "auto":
            return
        host = self.host()
        pinned = host.find(settings.encoder)
        if pinned is None:
            enc = next((e for e in host.encoders if e.name == settings.encoder), None)
            why = enc.reason if enc and enc.reason else "this host does not offer it"
            raise StreamError(f"Encoder {settings.encoder} cannot be used: {why}.", 400)

    # -- bring-up and tear-down ----------------------------------------------------------------------------

    def _bring_up(self, dests: list[Destination], paths: dict[str, Path] | None = None) -> None:
        host = self.host()
        if not host.ffmpeg:
            raise StreamError("ffmpeg is not installed, so nothing can be recorded or streamed.", 409)
        for dest in dests:
            self._require_transport(host, dest)
        source = self._source or self.source()
        size = self._settings.size(source.size)
        available = host.available_codecs(size, self._settings.fps or source.fps)
        if not available:
            raise StreamError("No working video encoder was found on this host; see the probe for the reasons.", 409)
        first = not self._runs
        if first:
            self._open_session(host)
        added: list[str] = []
        try:
            for dest in dests:
                codec = self._codec_for(dest, available)
                self._attach(dest, codec, (paths or {}).get(dest.id))
                added.append(dest.id)
        except (StreamError, AssignError) as exc:
            for dest_id in added:
                self._deactivate(dest_id)
            self._after_change()
            raise StreamError(str(exc), 409) from None
        self._after_change()

    def _require_transport(self, host: HostProbe, dest: Destination) -> None:
        wanted = {"record": "record-mp4" if dest.options.get("format") == "mp4" else "record-mkv"}.get(
            dest.kind, dest.kind
        )
        transport = host.transport(wanted)
        if transport is not None and not transport.ok:
            raise StreamError(f"{dest.label()} cannot start: {transport.reason}.", 409)

    def _wanted_codec(self, settings: EncoderSettings) -> str:
        """The codec the settings ask for: the chosen one, or the pinned encoder's, or "auto"."""
        if settings.codec != "auto":
            return settings.codec
        pinned = next((e for e in self.host().encoders if e.name == settings.encoder and e.ok), None)
        return pinned.codec if pinned else "auto"

    def _codec_for(self, dest: Destination, available: set[str]) -> str:
        existing = {a.codec for a in self._active.values()}
        if self._active:
            return assign_codec(dest, available, existing, self._wanted_codec(self._settings), self.max_chains())
        plan = plan_chains([dest], available, self._wanted_codec(self._settings), self.max_chains())
        codec = plan.codec_of(dest.id)
        if codec is None:
            raise StreamError(plan.unserved.get(dest.id, f"{dest.label()} cannot be served."), 409)
        return codec

    def _open_session(self, host: HostProbe) -> None:
        self._source = self.source()
        self._audio_source = self.audio_source()
        self._epoch = self._wall()
        self._started_at = self._epoch
        self._audio_failed = ""
        self._audio_track = None
        self._audio_last_ts = -1
        self._stop = threading.Event()
        self._start_audio(host)
        self._supervisor = threading.Thread(
            target=self._supervise, args=(self._stop,), name="stream-supervisor", daemon=True
        )
        self._supervisor.start()

    def _after_change(self) -> None:
        """Stop the pipelines nothing needs any more; end the session when no destination is left."""
        for codec in list(self._runs):
            if not any(a.codec == codec for a in self._active.values()):
                self._stop_run(self._runs.pop(codec))
        if not self._active:
            self._teardown()

    def _teardown(self) -> None:
        self._stop.set()  # the supervisor thread sees it within one tick; it is never joined under the lock
        self._supervisor = None
        for run in list(self._runs.values()):
            self._stop_run(run)
        self._runs.clear()
        for gen in (self._audio, self._audio_pending):
            if gen is not None:
                gen.dead = True
                gen.child.stop(grace=1.0)
        self._audio = self._audio_pending = None
        self._source = None

    def _stop_run(self, run: Run) -> None:
        run.chain.close()
        for gen in list(run.gens.values()):
            gen.dead = True
            gen.child.stop(grace=1.0)
        run.gens.clear()

    # -- destinations ----------------------------------------------------------------------------------------

    def _attach(self, dest: Destination, codec: str, path: Path | None = None) -> None:
        run = self._runs.get(codec) or self._start_run(codec)
        sink, target = self._make_sink(dest, codec, path)
        self._active[dest.id] = Active(dest, codec, sink, self._wall(), target)
        # a WHIP session takes a new picture size in-band (new parameter sets at the keyframe) and stays connected
        run.chain.attach(sink, restart_on_geometry=dest.kind != "whip")
        self._event("info", f"{dest.label()} is starting ({codec.upper()}).")

    def _deactivate(self, dest_id: str) -> None:
        active = self._active.pop(dest_id, None)
        if active is None:
            return
        run = self._runs.get(active.codec)
        if run is not None:
            run.chain.detach(dest_id)
        active.sink.shutdown()
        self._event("info", f"{active.dest.label()} stopped.")

    def _make_sink(
        self, dest: Destination, codec: str, path: Path | None
    ) -> tuple[PipeSink | FileSink | WhipSink, Path | None]:
        if dest.kind == "record" and dest.options.get("format", "mkv") == "mkv":
            target = path or self.record_dir() / f"recording-{int(self._wall())}.mkv"
            return FileSink(dest.id, target), target
        if dest.kind == "whip":
            return self._whip_sink(dest), None
        target = path
        ffmpeg = self.host().ffmpeg
        audio_kbps = self._settings.audio_bitrate_kbps

        def spawn(on_exit: Callable[[int], None], on_line: Callable[[str], None]) -> Child:
            secret = ""
            if dest.key_ref:
                try:
                    secret = resolve_secret(dest.key_ref, self.cfg)
                except SecretError as exc:
                    raise OSError(str(exc)) from None
            if target is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
            argv = ffcmd.sink_argv(ffmpeg, dest, codec, audio_kbps, secret, str(target or ""))
            return self._spawn_child(
                f"sink-{dest.id}",
                argv,
                stdin=True,
                on_line=on_line,
                on_exit=on_exit,
                redact=lambda text: redact(text, [secret]),
            )

        return PipeSink(dest.id, spawn, on_event=self._sink_event), target

    def _whip_sink(self, dest: Destination) -> WhipSink:
        from streamstack.stream.whip.sink import WhipSink  # imports aiortc, which the probe has just confirmed

        def token() -> str:
            if not dest.key_ref:
                return ""
            try:
                return resolve_secret(dest.key_ref, self.cfg)
            except SecretError as exc:
                raise OSError(str(exc)) from None

        return WhipSink(dest.id, dest.url, token, on_event=self._sink_event)

    def _sink_event(self, level: str, text: str) -> None:
        self._event(level, text)

    # -- encoders --------------------------------------------------------------------------------------------

    def _choose(self, codec: str, settings: EncoderSettings, avoid: set[str] | None = None) -> EncoderChoice:
        host = self.host()
        source = self._source or self.source()
        size, fps = settings.size(source.size), settings.fps or source.fps
        wanted = self.encode_node()
        if settings.encoder != "auto":
            pinned = self._on_gpu(host, settings.encoder, wanted) or host.find(settings.encoder)
            if pinned is not None and pinned.codec == codec and pinned.name not in (avoid or set()):
                return pinned.choice()
        pool = [e for e in host.working(codec) if e.name not in (avoid or set())]
        # encoders that ran on the GPU the Plan chose come first; the others stay as the fallback, in probe order
        pool.sort(key=lambda e: not e.on(wanted.path, wanted.pci_id or ""))
        for enc in pool:
            if software_ok(enc, size, fps, host.cores):
                return enc.choice()
        why = next((e.reason for e in host.encoders if e.codec == codec and e.reason), "no encoder is available")
        raise StreamError(f"No {codec.upper()} encoder can run here: {why}.", 409)

    @staticmethod
    def _on_gpu(host: HostProbe, name: str, wanted: roles.NodeChoice) -> EncoderProbe | None:
        return next(
            (e for e in host.encoders if e.name == name and e.ok and e.on(wanted.path, wanted.pci_id or "")), None
        )

    def _next_gen(self) -> int:
        self._gen_ids += 1
        return self._gen_ids

    def _start_run(self, codec: str) -> Run:
        source = self._source or self.source()
        settings = self._settings
        choice = self._choose(codec, settings)
        fps = settings.fps or source.fps
        chain = Chain(codec, self._clock_ms, 1000.0 / max(fps, 1))
        chain.on_retire = lambda gen, c=codec: self._retired.append((c, gen))
        run = Run(codec, chain, settings, choice)
        self._runs[codec] = run
        if self._audio_track is not None:
            chain.set_audio(self._audio_track)
        elif self._audio_failed:
            chain.set_audio(None)
        gen = self._spawn_video(run, choice, settings, None)
        chain.begin_swap(gen.id, 0)  # nothing active yet: this generation becomes the source
        return run

    def _clock_ms(self) -> float:
        return (self._wall() - self._epoch) * 1000.0

    def _spawn_video(self, run: Run, choice: EncoderChoice, settings: EncoderSettings, cut_s: float | None) -> Gen:
        source = self._source or self.source()
        argv = ffcmd.video_generation_argv(self.host().ffmpeg, source, choice, settings, self._epoch, cut_s)
        gen_id = self._next_gen()
        holder: dict[str, Gen] = {}

        def on_bytes(data: bytes) -> None:
            self._video_bytes(run, holder["gen"], data)

        child = self._spawn_child(
            f"video-{run.codec}-{gen_id}",
            argv,
            on_stdout=on_bytes,
            on_line=lambda line: self._gen_line(holder["gen"], line),
            on_exit=lambda code: self._gen_exited(run, holder["gen"], code),
        )
        gen = Gen(gen_id, "video", child, self._wall(), run.codec, choice, settings)
        holder["gen"] = gen
        run.gens[gen_id] = gen
        child.start()
        return gen

    def _video_bytes(self, run: Run, gen: Gen, data: bytes) -> None:
        if gen.dead:
            return
        for event in gen.demux.feed(data):
            if isinstance(event, mkv.Header):
                track = event.track("video")
                if track is not None:
                    run.chain.generation_header(gen.id, track)
                gen.header_seen.set()
            else:
                run.chain.video_block(gen.id, event)

    def _gen_line(self, gen: Gen, line: str) -> None:
        report = gen.progress.feed(line)
        if report is None:
            return
        try:
            gen.fps = float(report.get("fps", gen.fps) or 0)
            gen.encoder_drops = int(report.get("drop_frames", gen.encoder_drops) or 0)
            speed = report.get("speed", "").rstrip("x")
            gen.speed = float(speed) if speed.replace(".", "", 1).isdigit() else gen.speed
        except ValueError:
            return

    def _gen_exited(self, run: Run, gen: Gen, code: int) -> None:
        if gen.child.stopping or gen.dead:
            return
        gen.dead = True
        with self._lock:
            reason = gen.child.last_error() or f"ffmpeg exited with status {code}"
            if run.codec in self._runs and self._runs[run.codec] is run:
                self._event("warn", f"The {gen.choice.name if gen.choice else run.codec} encoder stopped: {reason}")
                run.chain.generation_ended(gen.id)

    # -- audio -----------------------------------------------------------------------------------------------

    def _resolve_audio_name(self, source: AudioSource) -> AudioSource:
        """The configured source when PipeWire has it, otherwise the default input (said out loud, not silently)."""
        if source.kind != "pulse" or source.name == "default":
            return source
        code, listing = default_runner(["pactl", "list", "short", "sources"], 5.0)
        if code == 0 and re.search(rf"^\d+\s+{re.escape(source.name)}\s", listing, re.M):
            return source
        self._event("warn", f"Audio source {source.name!r} is not present; using the default input instead.")
        return dataclasses.replace(source, name="default")

    def _start_audio(self, host: HostProbe) -> None:
        source = self._resolve_audio_name(self._audio_source or self.audio_source())
        self._audio_source = source
        gen = self._spawn_audio(host, source, self._settings)
        self._audio = gen
        if not gen.header_seen.wait(AUDIO_HEADER_WAIT_S):
            reason = gen.child.last_error() or "no audio arrived"
            self._audio_failed = f"No sound ({source.name}): {reason}"
            self._event("warn", f"{self._audio_failed}. Streaming without audio until it is fixed.")
            for run in self._runs.values():
                run.chain.set_audio(None)

    def _spawn_audio(self, host: HostProbe, source: AudioSource, settings: EncoderSettings) -> Gen:
        argv = ffcmd.audio_generation_argv(host.ffmpeg, source, settings, self._epoch)
        gen_id = self._next_gen()
        holder: dict[str, Gen] = {}
        child = self._spawn_child(
            f"audio-{gen_id}",
            argv,
            on_stdout=lambda data: self._audio_bytes(holder["gen"], data),
            on_line=lambda line: self._gen_line(holder["gen"], line),
            on_exit=lambda code: self._audio_exited(holder["gen"], code),
        )
        gen = Gen(gen_id, "audio", child, self._wall(), settings=settings)
        holder["gen"] = gen
        child.start()
        return gen

    def _audio_bytes(self, gen: Gen, data: bytes) -> None:
        if gen.dead:
            return
        for event in gen.demux.feed(data):
            if isinstance(event, mkv.Header):
                track = event.track("audio")
                if track is not None and self._audio_track is None:
                    self._audio_track = track
                    for run in list(self._runs.values()):
                        run.chain.set_audio(track)
                gen.header_seen.set()
                continue
            if gen is self._audio_pending:
                if event.ts_ms <= self._audio_last_ts:
                    continue  # still inside what the old encoder already delivered
                self._audio_take_over(gen)
            elif gen is not self._audio:
                continue
            self._audio_last_ts = max(self._audio_last_ts, event.ts_ms)
            block = dataclasses.replace(event, track=mkv.TRACK_AUDIO)
            for run in list(self._runs.values()):
                run.chain.audio_block(block)

    def _audio_take_over(self, new: Gen) -> None:
        with self._lock:
            old, self._audio, self._audio_pending = self._audio, new, None
        if old is not None:
            old.dead = True
            threading.Thread(target=old.child.stop, args=(1.0,), name="stream-audio-retire", daemon=True).start()
        self._event("ok", f"Audio now encodes at {new.settings.audio_bitrate_kbps} kbps.")

    def _audio_exited(self, gen: Gen, code: int) -> None:
        if gen.child.stopping or gen.dead:
            return
        gen.dead = True
        with self._lock:
            if gen is self._audio_pending:
                self._audio_pending = None
                self._event("warn", "The replacement audio encoder stopped; keeping the current one.")
            elif gen is self._audio:
                self._audio_failed = gen.child.last_error() or f"ffmpeg exited with status {code}"
                self._event("warn", f"The audio encoder stopped: {self._audio_failed}")

    # -- live changes ----------------------------------------------------------------------------------------

    def _apply_change(self, kind: str, old: EncoderSettings, new: EncoderSettings) -> str:
        if kind == "none":
            return "Nothing changed."
        if kind == "audio":
            self._swap_audio(new)
            return "Audio bitrate changed live (an Opus frame boundary, no reconnect)."
        if kind == "codec":
            self._replan(new)
            return "Codec changed; destinations moved to the new encoder."
        swapped = []
        for run in list(self._runs.values()):
            if run.failed or run.pending is not None:
                if run.pending is not None:
                    raise StreamError("A change is still being applied; try again in a moment.", 409)
                continue
            self._swap_video(run, new)
            swapped.append(run.codec)
        if new.audio_bitrate_kbps != old.audio_bitrate_kbps:
            self._swap_audio(new)
        if kind == "geometry":
            return "Picture format changing: the encoder is swapped at a keyframe; destinations reconnect once."
        return "Applied live: a new encoder takes over at the next planned keyframe, destinations stay connected."

    def _swap_video(self, run: Run, settings: EncoderSettings, attempt: int = 1) -> None:
        choice = self._choose(run.codec, settings, run.bad_encoders)
        source = self._source or self.source()
        multi = self._multi_reader(source)
        if multi:
            lead = SWAP_LEAD_S["software" if choice.backend == "software" else "hardware"] * (1 + 1.5 * (attempt - 1))
            cut_s = (self._wall() - self._epoch) + lead
            gen = self._spawn_video(run, choice, settings, cut_s)
            run.pending = Pending(gen.id, settings, choice, attempt)
            run.chain.begin_swap(gen.id, round(cut_s * 1000))
            return
        # an exclusive capture device cannot be opened twice: end the old encoder, then start the new one
        for old in [g for g in run.gens.values() if not g.dead]:
            old.dead = True
            old.child.stop(grace=1.0)
            run.chain.generation_ended(old.id)
        run.gens = {i: g for i, g in run.gens.items() if not g.dead}
        gen = self._spawn_video(run, choice, settings, None)
        run.pending = Pending(gen.id, settings, choice)
        run.chain.begin_swap(gen.id, 0)

    def _multi_reader(self, source: Source) -> bool:
        if source.kind != "v4l2":
            return True
        return bool(source_info(source.device).get("multi_reader"))

    def _swap_audio(self, settings: EncoderSettings) -> None:
        if self._audio is None or self._audio_pending is not None:
            return
        host = self.host()
        self._audio_pending = self._spawn_audio(host, self._audio_source or self.audio_source(), settings)

    def _replan(self, new: EncoderSettings) -> None:
        """A codec change: recompute which codec each destination gets and move the ones that differ."""
        source = self._source or self.source()
        host = self.host()
        available = host.available_codecs(new.size(source.size), new.fps or source.fps)
        dests = [a.dest for a in self._active.values()]
        plan = plan_chains(dests, available, self._wanted_codec(new), self.max_chains())
        if plan.unserved:
            dest_id, why = next(iter(plan.unserved.items()))
            raise StreamError(f"{dest_id}: {why}.", 409)
        for active in list(self._active.values()):
            codec = plan.codec_of(active.dest.id)
            if codec is None or codec == active.codec:
                continue
            self._move(active, codec)
        for run in list(self._runs.values()):
            if run.settings != new and any(a.codec == run.codec for a in self._active.values()):
                self._swap_video(run, new)
        self._after_change()

    def _move(self, active: Active, codec: str) -> None:
        old_run = self._runs.get(active.codec)
        if old_run is not None:
            old_run.chain.detach(active.dest.id)
        active.sink.shutdown()
        self._active.pop(active.dest.id, None)
        self._attach(active.dest, codec, active.path)
        self._active[active.dest.id].moved = True

    # -- supervision -----------------------------------------------------------------------------------------

    def _supervise(self, stop: threading.Event) -> None:
        while not stop.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception as exc:  # the supervisor must outlive a bug in one pass; it is reported, not hidden
                self._event("error", f"Stream supervisor error: {type(exc).__name__}: {exc}")

    def tick(self) -> None:
        """One supervision pass (the thread calls it; tests call it directly)."""
        with self._lock:
            now = self._wall()
            for codec, gen_id in list(self._retired):
                self._retired.popleft()
                run = self._runs.get(codec)
                gen = run.gens.pop(gen_id, None) if run else None
                if gen is not None:
                    gen.dead = True
                    threading.Thread(target=gen.child.stop, args=(1.0,), name="stream-retire", daemon=True).start()
            for run in list(self._runs.values()):
                run.chain.tick()
                self._reconcile(run)
                self._revive(run, now)
            self._enhanced_fallback()

    def _reconcile(self, run: Run) -> None:
        """Commit or report a swap once the relay has decided it."""
        switch = run.chain.switch
        pending = run.pending
        if pending is None:
            return
        if switch.active == pending.gen:
            run.settings, run.choice, run.pending, run.failures = pending.settings, pending.choice, None, 0
            source = self._source or self.source()
            switch.frame_ms = 1000.0 / max(pending.settings.fps or source.fps, 1)
            swap = switch.last_swap
            self._last_swap = {
                "codec": run.codec,
                "ok": True,
                "gap_ms": swap.gap_ms if swap else 0,
                "encoder": pending.choice.name,
                "at": round(self._wall(), 1),
            }
            self._event("ok", f"{run.codec.upper()} encoder swapped live ({pending.choice.name}).")
        elif switch.pending is None and switch.active != pending.gen:
            swap = switch.last_swap
            reason = swap.reason if swap else "the new encoder did not start"
            run.pending = None
            self._last_swap = {"codec": run.codec, "ok": False, "reason": reason, "at": round(self._wall(), 1)}
            gen = run.gens.pop(pending.gen, None)
            if gen is not None:
                gen.dead = True
                threading.Thread(target=gen.child.stop, args=(1.0,), name="stream-retire", daemon=True).start()
            if pending.attempt < 2 and not run.failed:  # a busy machine starts encoders slowly: allow more headroom
                self._event("info", f"The encoder change was late ({reason}); retrying with more headroom.")
                try:
                    self._swap_video(run, pending.settings, attempt=pending.attempt + 1)
                except StreamError as exc:
                    self._event("warn", f"The encoder change could not be retried: {exc.message}")
                return
            self._event("warn", f"The encoder change could not be applied ({reason}); the old encoder keeps running.")

    def _revive(self, run: Run, now: float) -> None:
        """Restart a codec's encoder after it died: with backoff, and with the next encoder if one keeps failing."""
        switch = run.chain.switch
        if run.failed or run.pending is not None or switch.pending is not None:
            return
        if any(not g.dead for g in run.gens.values()):
            run.dead_at = 0.0
            return
        if run.dead_at == 0.0:  # just noticed: count the failure and schedule the restart
            run.dead_at = now
            last = max((g.started_at for g in run.gens.values()), default=now)
            run.failures = run.failures + 1 if now - last < FAST_FAIL_S else 1
            if run.failures > ENCODER_FAILURES:
                run.bad_encoders.add(run.choice.name)
                run.failures = 1
                self._event("warn", f"{run.choice.name} keeps failing; trying another {run.codec.upper()} encoder.")
            run.next_restart = now + RESTART_BACKOFF[min(run.failures - 1, len(RESTART_BACKOFF) - 1)]
        if now < run.next_restart:
            return
        try:
            choice = self._choose(run.codec, run.settings, run.bad_encoders)
        except StreamError as exc:
            run.failed = exc.message
            self._event("error", f"{run.codec.upper()} cannot be encoded any more: {exc.message}")
            return
        run.gens.clear()
        run.dead_at = 0.0
        run.choice = choice
        gen = self._spawn_video(run, choice, run.settings, None)
        run.chain.begin_swap(gen.id, 0)

    def _enhanced_fallback(self) -> None:
        """Enhanced RTMP that never connects: the server probably cannot take AV1 or HEVC, so use H.264."""
        for active in list(self._active.values()):
            dest = active.dest
            if dest.kind != "rtmp" or dest.options.get("mode", "auto") != "auto" or active.codec == "h264":
                continue
            sink = active.sink
            if not isinstance(sink, PipeSink) or sink.ever_live or sink.restarts < 2 or active.moved:
                continue
            available = self.host().available_codecs()
            if "h264" not in available:
                continue
            self._event(
                "warn",
                f"{dest.label()} did not accept {active.codec.upper()} (Enhanced RTMP); falling back to classic H.264.",
            )
            try:
                self._move(active, "h264")
            except StreamError as exc:
                self._event("error", f"{dest.label()} could not fall back to H.264: {exc.message}")
                continue
            self._after_change()

    # -- state -----------------------------------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        with self._lock:
            return self._state_locked()

    def _destination_state(self, dest: Destination, active: Active | None) -> dict[str, Any]:
        base: dict[str, Any] = {
            "id": dest.id,
            "kind": dest.kind,
            "name": dest.label(),
            "url": dest.url,
            "enabled": dest.enabled,
            "key": "set" if dest.key_ref else "none",
            "codec_wanted": dest.codec,
            "options": dict(dest.options),
            "active": active is not None,
            "state": "idle",
            "health": "idle",
            "codec": "",
            "bitrate_kbps": 0.0,
            "dropped_frames": 0,
            "restarts": 0,
            "latency_ms": 0,
            "last_error": "",
            "queued_bytes": 0,
            "connected_s": 0.0,
            "moved": False,
        }
        if active is None:
            return base
        run = self._runs.get(active.codec)
        status = active.sink.status()
        health = {"live": "ok", "connecting": "warn", "backoff": "warn", "failed": "fail"}.get(status["state"], "idle")
        kbps = run.chain.snapshot()["video_kbps"] + run.chain.snapshot()["audio_kbps"] if run else 0.0
        encode_ms = run.chain.latency_ms if run else 0.0
        queue_ms = status["queued_bytes"] * 8 / max(kbps, 1)  # bytes*8/kbps = ms of video waiting
        whip = status.get("whip") or {}
        transport = (
            int(dest.options.get("latency_ms", 120)) if dest.kind == "srt" else TRANSPORT_LATENCY_MS.get(dest.kind, 0)
        )
        if dest.kind == "whip" and whip.get("rtt_ms") is not None:
            transport = WHIP_PLAYOUT_MS + int(whip["rtt_ms"] / 2)  # the measured one-way delay plus a receiver buffer
        base.update(
            state=status["state"],
            health=health,
            codec=active.codec,
            bitrate_kbps=round(kbps, 1),
            dropped_frames=status["dropped_frames"],
            restarts=status["restarts"],
            latency_ms=round(encode_ms + queue_ms + transport),
            last_error=status["last_error"],
            queued_bytes=status["queued_bytes"],
            connected_s=status["connected_s"],
            moved=active.moved,
        )
        if dest.kind == "whip":
            base["whip"] = whip
        if active.path is not None:
            base["file"] = status.get("file") or str(active.path)
            base["bytes"] = status["net_bytes"]
        return base

    def _state_locked(self) -> dict[str, Any]:
        host = self._probe.peek()
        destinations = [self._destination_state(d, self._active.get(d.id)) for d in self._destinations]
        record_active = self._active.get("record")
        recording: dict[str, Any] = {"active": False, "file": "", "bytes": 0, "seconds": 0, "format": ""}
        if record_active is not None:
            info = self._destination_state(record_active.dest, record_active)
            recording = {
                "active": True,
                "file": info.get("file", ""),
                "bytes": info.get("bytes", 0),
                "seconds": round(self._wall() - record_active.started_at),
                "format": record_active.dest.options.get("format", "mkv"),
                "state": info["state"],
                "last_error": info["last_error"],
            }
        live = [d for d in destinations if d["active"]]
        states = {d["state"] for d in live}
        if not self._active:
            status = "idle"
        elif any(r.failed for r in self._runs.values()):
            status = "error"
        elif live and states == {"live"}:
            status = "live"
        elif live and "live" in states:
            status = "degraded"
        elif live:
            status = "starting" if states <= {"connecting", "idle"} else "degraded"
        else:
            status = "recording"
        runs = []
        pids: set[int] = set()
        for run in self._runs.values():
            snap = run.chain.snapshot()
            gen = next((g for g in run.gens.values() if g.id == run.chain.switch.active), None)
            pid = gen.child.pid if gen else None
            if pid is not None:
                pids.add(pid)
            usage = self._usage.sample(pid)
            runs.append(
                {
                    "codec": run.codec,
                    "encoder": run.choice.name,
                    "label": run.choice.label,
                    "backend": run.choice.backend,
                    "device": run.choice.device,
                    "bitrate_kbps": snap["video_kbps"],
                    "fps": round(gen.fps, 1) if gen else 0.0,
                    "encoder_dropped": gen.encoder_drops if gen else 0,
                    "cpu_percent": usage.cpu_percent,
                    "gpu_percent": usage.gpu_percent,
                    "latency_ms": snap["latency_ms"],
                    "swapping": snap["swapping"],
                    "swaps": snap["swaps"],
                    "failed": run.failed,
                    "destinations": [a.dest.id for a in self._active.values() if a.codec == run.codec],
                }
            )
        self._usage.forget(pids)
        cpus = [r["cpu_percent"] for r in runs if r["cpu_percent"] is not None]
        gpus = [r["gpu_percent"] for r in runs if r["gpu_percent"] is not None]
        dropped = sum(d["dropped_frames"] for d in destinations if d["active"]) + sum(
            r["encoder_dropped"] for r in runs
        )
        bitrate = sum(r["bitrate_kbps"] + (self._audio.settings.audio_bitrate_kbps if self._audio else 0) for r in runs)
        latencies = [d["latency_ms"] for d in live if d["kind"] != "record" and d["state"] == "live"]
        source = self._source or self.source()
        effective = self._settings.size(source.size)
        return {
            "status": status,
            "uptime_s": round(self._wall() - self._started_at) if self._active else 0,
            "bitrate_kbps": round(bitrate, 1),
            "dropped_frames": dropped,
            "latency_ms": max(latencies) if latencies else 0,
            "cpu_percent": round(sum(cpus), 1) if cpus else None,
            "gpu_percent": max(gpus) if gpus else None,
            "encoders": runs,
            "encoder": ", ".join(r["encoder"] for r in runs) if runs else "",
            "destinations": destinations,
            "recording": recording,
            "settings": self._settings.to_dict(),
            "output": {"width": effective[0], "height": effective[1], "fps": self._settings.fps or source.fps},
            "audio": {
                "source": (self._audio_source or self.audio_source()).name,
                "bitrate_kbps": self._audio.settings.audio_bitrate_kbps if self._audio else 0,
                "ok": self._audio is not None and not self._audio_failed,
                "error": self._audio_failed,
            },
            "last_swap": self._last_swap,
            "events": list(self._events)[-8:],
            "probe": host.to_dict() if host is not None else None,
        }


def default_state_path() -> Path:
    from streamstack import config

    return config.state_dir() / "stream.json"
