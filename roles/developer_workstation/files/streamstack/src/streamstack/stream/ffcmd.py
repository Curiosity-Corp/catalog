"""The ffmpeg command lines: one video encoder, one audio encoder, and one sink per destination.

Pure functions from settings to `argv` lists; nothing here starts a process, so every flag is unit-tested.

The two encoder commands share one idea. Every frame and every audio block is stamped with the wall-clock time it
was captured at, in seconds since the stream's epoch (`setpts=(PTS*TB-E)/TB`). All encoder generations therefore
speak the same clock, which is what lets the relay cut from one video encoder to the next without rewriting a
single timestamp. Output is Matroska on stdout (`-live 1 -cluster_time_limit 1 -flush_packets 1` so each block is
written as soon as it exists).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from streamstack.stream.model import Destination, EncoderSettings

CAPTURE_FLAGS = ["-fflags", "nobuffer", "-flags", "low_delay"]
MKV_PIPE = ["-copyts", "-f", "matroska", "-live", "1", "-cluster_time_limit", "1", "-flush_packets", "1", "pipe:1"]
QUIET = ["-hide_banner", "-nostdin", "-loglevel", "warning", "-nostats", "-progress", "pipe:2", "-stats_period", "1"]
SINK_QUIET = QUIET
SINK_INPUT = ["-fflags", "nobuffer", "-flags", "low_delay", "-probesize", "65536", "-analyzeduration", "0"]
# Enhanced RTMP (Veovera E-RTMP) names a codec by FourCC. ffmpeg 6.1 and later carry HEVC and AV1 video, ffmpeg 8 also
# Opus audio; `-rtmp_enhanced_codecs` lists the ones the muxer may announce. H.264 and AAC need no entry.
ENHANCED_FOURCC = {"hevc": "hvc1", "av1": "av01"}
ENHANCED_AUDIO_FOURCC = {"opus": "Opus"}


@dataclass(frozen=True)
class Source:
    """Where the program picture comes from. `lavfi` is for tests and measurements only."""

    kind: str = "v4l2"  # v4l2 | lavfi
    device: str = "/dev/video43"
    width: int = 1920
    height: int = 1080
    fps: float = 30.0
    lavfi: str = "testsrc2"  # the lavfi source name for kind "lavfi" (size and rate are added)

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height


@dataclass(frozen=True)
class AudioSource:
    kind: str = "pulse"  # pulse (PipeWire's pulse server) | lavfi (tests)
    name: str = "default"
    offset_ms: int = 0  # positive delays the sound against the picture (lip-sync trim)
    tone_hz: int = 440  # lavfi only: the test tone, so tests can tell two tracks apart


@dataclass(frozen=True)
class EncoderChoice:
    name: str  # ffmpeg encoder name
    codec: str  # av1 | hevc | h264
    backend: str  # vaapi | qsv | nvenc | software
    device: str = ""  # DRM render node for vaapi and qsv
    label: str = ""


def _seconds(value: float) -> str:
    return f"{value:.6f}"


def timeline_filter(source_kind: str, epoch_s: float) -> str:
    """Stamp frames with capture time since the epoch. A lavfi source is paced and stamped by the wall clock."""
    if source_kind == "lavfi":
        return f"realtime,setpts=(RTCTIME-{round(epoch_s * 1e6)})/1000000/TB"
    return f"setpts=(PTS*TB-{_seconds(epoch_s)})/TB"


def audio_timeline_filter(source_kind: str, epoch_s: float, offset_ms: int = 0) -> str:
    shift = f"+{offset_ms / 1000:.3f}/TB" if offset_ms else ""
    if source_kind == "lavfi":
        return f"arealtime,asetpts=(RTCTIME-{round(epoch_s * 1e6)})/1000000/TB{shift}"
    return f"asetpts=(PTS*TB-{_seconds(epoch_s)})/TB{shift}"


def rate_args(settings: EncoderSettings, kbps: int, backend: str) -> list[str]:
    """Bitrate arguments common to every encoder: CBR holds `kbps`, VBR averages it, CQP ignores it."""
    if settings.rate_control == "cqp":
        return []
    if settings.rate_control == "vbr":
        return ["-b:v", f"{kbps}k", "-maxrate", f"{round(kbps * 1.5)}k", "-bufsize", f"{kbps * 2}k"]
    return ["-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps}k"]


def encoder_args(choice: EncoderChoice, settings: EncoderSettings, kbps: int, keyint: int) -> list[str]:
    """The `-c:v` block for one encoder, tuned for low latency: no B-frames, no look-ahead, one frame in flight."""
    codec, backend, name = choice.codec, choice.backend, choice.name
    cqp = settings.rate_control == "cqp"
    qp = settings.quality or {"av1": 100, "hevc": 28, "h264": 24}[codec]
    args = ["-c:v", name]
    if backend == "vaapi":
        mode = {"cbr": "CBR", "vbr": "VBR", "cqp": "CQP"}[settings.rate_control]
        args += ["-rc_mode", mode, *rate_args(settings, kbps, backend)]
        if cqp:
            args += ["-qp", str(min(qp, 255) if codec == "av1" else min(qp, 51))]
        args += ["-g", str(keyint), "-async_depth", "1"]
        args += ["-b_depth", "1"] if codec == "av1" else ["-bf", "0"]
        args += {"h264": ["-profile:v", "high"], "hevc": ["-profile:v", "main"]}.get(codec, [])
    elif backend == "qsv":
        args += rate_args(settings, kbps, backend)
        if cqp:
            args += ["-global_quality", str(min(qp, 51))]
        args += ["-g", str(keyint), "-bf", "0", "-async_depth", "1", "-forced_idr", "1", "-preset", "veryfast"]
        args += {"h264": ["-profile:v", "high"], "hevc": ["-profile:v", "main"]}.get(codec, [])
    elif backend == "nvenc":
        mode = {"cbr": "cbr", "vbr": "vbr", "cqp": "constqp"}[settings.rate_control]
        args += ["-preset", "p4", "-tune", "ll", "-rc", mode, *rate_args(settings, kbps, backend)]
        if cqp:
            args += ["-qp", str(min(qp, 51))]
        args += ["-g", str(keyint), "-bf", "0", "-forced-idr", "1", "-zerolatency", "1"]
        args += {"h264": ["-profile:v", "high"], "hevc": ["-profile:v", "main"]}.get(codec, [])
    elif name == "libx264":
        args += ["-preset", "veryfast", "-tune", "zerolatency", "-bf", "0", "-sc_threshold", "0", "-g", str(keyint)]
        args += ["-crf", str(min(qp, 51))] if cqp else rate_args(settings, kbps, backend)
        args += ["-profile:v", "high"]
    elif name == "libx265":
        params = f"keyint={keyint}:min-keyint={keyint}:scenecut=0:bframes=0:repeat-headers=1"
        args += ["-preset", "veryfast", "-tune", "zerolatency", "-x265-params", params, "-g", str(keyint)]
        args += ["-crf", str(min(qp, 51))] if cqp else rate_args(settings, kbps, backend)
    elif name == "libsvtav1":
        args += ["-preset", "10", "-g", str(keyint), "-svtav1-params", "tune=0:scd=0:lookahead=0"]
        args += ["-crf", str(min(qp, 63))] if cqp else rate_args(settings, kbps, backend)
    else:
        raise ValueError(f"no argument recipe for encoder {name!r}")
    return args


def hw_device_args(choice: EncoderChoice) -> list[str]:
    if choice.backend == "vaapi":
        return ["-init_hw_device", f"vaapi=va:{choice.device}", "-filter_hw_device", "va"]
    if choice.backend == "qsv":
        return [
            "-init_hw_device",
            f"vaapi=va:{choice.device}",
            "-init_hw_device",
            "qsv=qs@va",
            "-filter_hw_device",
            "qs",
        ]
    return []


def video_filter(
    source: Source, choice: EncoderChoice, settings: EncoderSettings, epoch_s: float, out_size: tuple[int, int]
) -> str:
    """Timeline, optional frame-rate change, then upload and scale on the encoder's side of the machine."""
    chain = [timeline_filter(source.kind, epoch_s)]
    if settings.fps and settings.fps != round(source.fps):
        chain.append(f"fps={settings.fps}")
    scaling = out_size != source.size
    width, height = out_size
    if choice.backend == "vaapi":
        chain += ["format=nv12", "hwupload"]
        if scaling:
            chain.append(f"scale_vaapi=w={width}:h={height}:format=nv12")
    elif choice.backend == "qsv":
        chain += ["format=nv12", "hwupload=extra_hw_frames=16"]
        if scaling:
            chain.append(f"vpp_qsv=w={width}:h={height}:format=nv12")
    else:
        if scaling:
            chain.append(f"scale={width}:{height}:flags=bilinear")
        chain.append("format=nv12" if choice.backend == "nvenc" else "format=yuv420p")
    return ",".join(chain)


def video_generation_argv(
    ffmpeg: str,
    source: Source,
    choice: EncoderChoice,
    settings: EncoderSettings,
    epoch_s: float,
    cut_s: float | None = None,
) -> list[str]:
    """One video encoder generation: capture, encode, Matroska on stdout, progress on stderr.

    `cut_s` is the stream time (seconds since the epoch) of the keyframe this generation must start the live
    stream with; it is forced there and then every `keyint_s` after it, so the relay can cut over on it.
    """
    out_size = settings.size(source.size)
    fps = settings.fps or source.fps
    keyint = max(1, round(settings.keyint_s * fps))
    kbps = settings.bitrate_for(choice.codec, out_size, fps)
    argv = [ffmpeg, *QUIET, *hw_device_args(choice)]
    if source.kind == "lavfi":
        argv += ["-f", "lavfi", "-i", f"{source.lavfi}=size={source.width}x{source.height}:rate={source.fps:g}"]
    else:
        argv += [
            *CAPTURE_FLAGS,
            "-f", "v4l2",
            "-use_wallclock_as_timestamps", "1",
            "-thread_queue_size", "16",
            "-framerate", f"{source.fps:g}",
            "-video_size", f"{source.width}x{source.height}",
            "-i", source.device,
        ]  # fmt: skip
    argv += ["-vf", video_filter(source, choice, settings, epoch_s, out_size), "-an", "-map", "0:v:0"]
    argv += encoder_args(choice, settings, kbps, keyint)
    if cut_s is not None:
        argv += ["-force_key_frames", f"expr:gte(t,{_seconds(cut_s)}+n_forced*{settings.keyint_s:g})"]
    argv += ["-fps_mode", "passthrough", *MKV_PIPE]
    return argv


def audio_generation_argv(
    ffmpeg: str,
    source: AudioSource,
    settings: EncoderSettings,
    epoch_s: float,
) -> list[str]:
    """The audio encoder: Opus, 20 ms frames, 48 kHz stereo, Matroska on stdout."""
    argv = [ffmpeg, *QUIET]
    if source.kind == "lavfi":
        argv += ["-f", "lavfi", "-i", f"sine=frequency={source.tone_hz}:sample_rate=48000"]
    else:
        argv += [
            "-f", "pulse",
            "-use_wallclock_as_timestamps", "1",
            "-thread_queue_size", "64",
            "-sample_rate", "48000",
            "-channels", "2",
            "-i", source.name,
        ]  # fmt: skip
    argv += [
        "-vn",
        "-af", audio_timeline_filter(source.kind, epoch_s, source.offset_ms),
        "-c:a", "libopus",
        "-b:a", f"{settings.audio_bitrate_kbps}k",
        "-vbr", "on",
        "-application", "audio",
        "-frame_duration", "20",
        "-ar", "48000",
        "-ac", "2",
        *MKV_PIPE,
    ]  # fmt: skip
    return argv


# -- sinks -----------------------------------------------------------------------------------------------------


def _latency_ms(dest: Destination, override: int | None) -> int:
    if override is not None:
        return int(override)
    value = dest.options.get("latency_ms", 120)
    return 120 if value == "auto" else int(value)


def srt_url(dest: Destination, passphrase: str = "", streamid: str = "", latency_ms: int | None = None) -> str:
    """The srt:// URL. `latency_ms` is the latency in force (resolved from "auto" by the caller); without it the
    destination's own number is used, and 120 when that is "auto"."""
    parts = urlsplit(dest.url)
    query = {
        "mode": dest.options.get("mode", "caller"),
        "latency": str(_latency_ms(dest, latency_ms) * 1000),  # ffmpeg takes microseconds
        "pkt_size": "1316",
    }
    if passphrase:
        query["passphrase"] = passphrase
        query["pbkeylen"] = str(dest.options.get("pbkeylen", 16))
    if streamid:
        query["streamid"] = streamid
    host = parts.hostname or ""
    host = f"[{host}]" if ":" in host else host
    return f"srt://{host}:{parts.port}?" + "&".join(f"{k}={quote(v, safe='')}" for k, v in query.items())


def rtmp_url(dest: Destination, secret: str = "") -> str:
    """The URL ffmpeg publishes to: the destination's URL (application path) with the stream key as the last segment."""
    return dest.url.rstrip("/") + ("/" + quote(secret, safe="") if secret else "")


def rtmp_tls_args(dest: Destination) -> list[str]:
    """ffmpeg's TLS options for an `rtmps://` destination; none for plain `rtmp://`.

    ffmpeg's `tls_verify` defaults to off, which would send the stream key to anyone who answers on the port. The
    certificate is therefore checked, against the host in the URL, unless the destination says `tls_verify: false`;
    `ca_file` adds a CA bundle for a private ingest.
    """
    if not dest.is_tls():
        return []
    if not dest.tls_verify():
        return ["-tls_verify", "0"]
    args = ["-tls_verify", "1", "-verifyhost", urlsplit(dest.url).hostname or ""]
    ca_file = str(dest.options.get("ca_file", ""))
    return [*args, "-ca_file", ca_file] if ca_file else args


def rtmp_wants_opus(dest: Destination, legacy: bool = False) -> bool:
    """Whether this destination gets Opus (Enhanced RTMP audio) rather than AAC. Never in legacy fallback."""
    return dest.rtmp_audio() == "opus" and not legacy and dest.options.get("mode") != "classic"


def rtmp_enhanced_codecs(dest: Destination, codec: str, legacy: bool = False) -> list[str]:
    """The FourCCs the muxer may announce for this destination: the video codec's, then the audio codec's."""
    names: list[str] = []
    if not legacy and codec in ENHANCED_FOURCC:
        names.append(ENHANCED_FOURCC[codec])
    if rtmp_wants_opus(dest, legacy):
        names.append(ENHANCED_AUDIO_FOURCC["opus"])
    return names


def sink_argv(
    ffmpeg: str,
    dest: Destination,
    codec: str,
    audio_kbps: int = 128,
    secret: str = "",
    target: str = "",
    latency_ms: int | None = None,
    legacy: bool = False,
) -> list[str]:
    """The ffmpeg that reads the relay's Matroska on stdin and delivers it to `dest`. `secret` is the resolved key.

    `target` is the output path for file sinks (record to mp4). The key, when there is one, appears only in this
    argv; callers must never log it unredacted. `legacy` is the RTMP fallback: classic FLV, H.264 and AAC only,
    for a server that did not accept Enhanced RTMP.
    """
    base = [ffmpeg, *SINK_QUIET, *SINK_INPUT, "-f", "matroska", "-i", "pipe:0", "-map", "0"]
    if dest.kind == "srt":
        passphrase = secret
        streamid = str(dest.options.get("streamid", ""))
        return [
            *base, "-c", "copy",
            "-f", "mpegts", "-muxdelay", "0.1", "-muxpreload", "0.1", "-flush_packets", "1",
            srt_url(dest, passphrase, streamid, latency_ms),
        ]  # fmt: skip
    if dest.kind == "rtmp":
        if legacy and codec != "h264":
            raise ValueError("The legacy RTMP fallback carries H.264 only.")
        # the relay's audio is already Opus (48 kHz stereo, 20 ms): Enhanced RTMP forwards it as it is; classic
        # FLV has no Opus, so AAC is encoded from it
        audio = ["-c:a", "copy"] if rtmp_wants_opus(dest, legacy) else [
            "-c:a", "aac", "-b:a", f"{audio_kbps}k", "-ar", "48000", "-ac", "2",
        ]  # fmt: skip
        argv = [*base, "-c:v", "copy", *audio]
        fourccs = rtmp_enhanced_codecs(dest, codec, legacy)
        if fourccs:
            argv += ["-rtmp_enhanced_codecs", ",".join(fourccs)]
        argv += rtmp_tls_args(dest)
        return [*argv, "-flvflags", "no_duration_filesize", "-f", "flv", "-flush_packets", "1", rtmp_url(dest, secret)]
    if dest.kind == "record":
        return [
            *base, "-c", "copy",
            "-f", "mp4", "-movflags", "+frag_keyframe+empty_moov+default_base_moof+skip_trailer",
            "-frag_duration", "1000000", target,
        ]  # fmt: skip
    if dest.kind == "whip":
        raise ValueError("WHIP is published by StreamStack itself (stream.whip), not by an ffmpeg child")
    raise ValueError(f"no ffmpeg sink for destination kind {dest.kind!r}")
