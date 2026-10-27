"""What a stream is: encoder settings, destinations, secret references, and how a settings change is classified.

Nothing here runs a process. Every value that comes from a person (or from the Console) is validated into one of
these types, with an error message that says what is wrong and what is allowed; the rest of the package can then
trust them.
"""

from __future__ import annotations

import itertools
import os
import re
import shlex
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

CODECS = ("av1", "hevc", "h264")  # best first: this order is the selection policy
CODEC_RANK = {codec: rank for rank, codec in enumerate(CODECS)}
RATE_CONTROLS = ("cbr", "vbr", "cqp")
TIERS: dict[str, tuple[int, int]] = {
    "480p": (854, 480),
    "720p": (1280, 720),
    "900p": (1600, 900),
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "2160p": (3840, 2160),
}
KINDS = ("record", "srt", "rtmp", "whip")
LIVE_KINDS = ("srt", "rtmp", "whip")
SRT_MODES = ("caller", "listener")
RTMP_MODES = ("auto", "enhanced", "classic")
RECORD_FORMATS = ("mkv", "mp4")
DEST_ID = re.compile(r"[a-z][a-z0-9_-]{0,31}")

# What each transport can carry without a special receiver. SRT carries MPEG-TS, and ffmpeg (hence most receivers)
# cannot demux AV1 out of it, so AV1 over SRT is only chosen when the destination asks for it by name.
KIND_CODECS: dict[str, tuple[str, ...]] = {
    "record": ("av1", "hevc", "h264"),
    "srt": ("hevc", "h264"),
    "rtmp": ("av1", "hevc", "h264"),  # Enhanced RTMP; "classic" narrows this to h264
    "whip": ("h264",),
}
# kbps for one codec at 30 fps, by tier height. AV1 and HEVC reach the same picture at far fewer bits.
BITRATE_TABLE: dict[str, dict[int, int]] = {
    "h264": {480: 1800, 720: 4000, 900: 5200, 1080: 6000, 1440: 9000, 2160: 14000},
    "hevc": {480: 1200, 720: 2800, 900: 3600, 1080: 4500, 1440: 6500, 2160: 10000},
    "av1": {480: 1000, 720: 2400, 900: 3000, 1080: 3800, 1440: 5500, 2160: 8500},
}
MAX_BITRATE_KBPS = 100_000


class SettingsError(ValueError):
    """A setting is out of range or unknown. The message is shown to the user as it is."""


def _number(name: str, value: Any, low: float, high: float, integer: bool = True) -> float:
    if isinstance(value, bool):
        raise SettingsError(f"{name} must be a number.")
    try:
        number = int(value) if integer else float(value)
    except (TypeError, ValueError):
        raise SettingsError(f"{name} must be a number.") from None
    if not low <= number <= high:
        raise SettingsError(f"{name} must be between {low:g} and {high:g}.")
    return number


@dataclass(frozen=True)
class EncoderSettings:
    """The picture and sound that leave the studio. `0` and `auto` mean "decide from the host and the source"."""

    codec: str = "auto"  # auto | av1 | hevc | h264 (auto: best the host and the destinations allow)
    encoder: str = "auto"  # auto, or one ffmpeg encoder name from the probe (av1_vaapi, libx264, ...)
    tier: str = "source"  # source | 480p | 720p | 900p | 1080p | 1440p | 2160p
    fps: int = 0  # 0 follows the source
    bitrate_kbps: int = 0  # 0 picks a default for the codec and tier
    rate_control: str = "cbr"
    keyint_s: float = 2.0
    quality: int = 0  # the quantiser for rate_control "cqp" (lower is better); 0 picks a default
    audio_bitrate_kbps: int = 128

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None, base: EncoderSettings | None = None) -> EncoderSettings:
        """`base` with the known keys of `data` replaced, validated. Unknown keys are an error, never ignored."""
        current = base or cls()
        data = dict(data or {})
        unknown = sorted(set(data) - {f for f in cls.__dataclass_fields__})
        if unknown:
            raise SettingsError(f"Unknown stream setting {unknown[0]!r}.")
        values: dict[str, Any] = {}
        if "codec" in data:
            if data["codec"] not in ("auto", *CODECS):
                raise SettingsError(f"codec must be auto or one of {', '.join(CODECS)}.")
            values["codec"] = data["codec"]
        if "encoder" in data:
            encoder = str(data["encoder"])
            if encoder != "auto" and not re.fullmatch(r"[a-z0-9_]{3,24}", encoder):
                raise SettingsError("encoder must be auto or an ffmpeg encoder name such as av1_vaapi.")
            values["encoder"] = encoder
        if "tier" in data:
            if data["tier"] != "source" and data["tier"] not in TIERS:
                raise SettingsError(f"tier must be source or one of {', '.join(TIERS)}.")
            values["tier"] = data["tier"]
        if "fps" in data:
            values["fps"] = int(_number("fps", data["fps"], 0, 120))
            if values["fps"] and values["fps"] < 5:
                raise SettingsError("fps must be 0 (follow the source) or at least 5.")
        if "bitrate_kbps" in data:
            values["bitrate_kbps"] = int(_number("bitrate_kbps", data["bitrate_kbps"], 0, MAX_BITRATE_KBPS))
            if 0 < values["bitrate_kbps"] < 100:
                raise SettingsError("bitrate_kbps must be 0 (automatic) or at least 100.")
        if "rate_control" in data:
            if data["rate_control"] not in RATE_CONTROLS:
                raise SettingsError(f"rate_control must be one of {', '.join(RATE_CONTROLS)}.")
            values["rate_control"] = data["rate_control"]
        if "keyint_s" in data:
            values["keyint_s"] = float(_number("keyint_s", data["keyint_s"], 0.5, 10, integer=False))
        if "quality" in data:
            values["quality"] = int(_number("quality", data["quality"], 0, 63))
        if "audio_bitrate_kbps" in data:
            values["audio_bitrate_kbps"] = int(_number("audio_bitrate_kbps", data["audio_bitrate_kbps"], 32, 320))
        return replace(current, **values)

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    def height(self, source_height: int) -> int:
        return TIERS[self.tier][1] if self.tier in TIERS else source_height

    def size(self, source: tuple[int, int]) -> tuple[int, int]:
        """The encoded picture size: the tier's, scaled to the source's aspect ratio, even in both directions."""
        if self.tier not in TIERS:
            return source
        wide, high = TIERS[self.tier]
        src_w, src_h = source
        if src_w * high == src_h * wide:
            return wide, high
        if src_w * high > src_h * wide:  # wider than the tier: keep the width
            return wide, max(2, round(wide * src_h / src_w / 2) * 2)
        return max(2, round(high * src_w / src_h / 2) * 2), high

    def bitrate_for(self, codec: str, size: tuple[int, int], fps: float) -> int:
        """The kbps to encode with: the explicit one, or the table's figure scaled by frame rate and pixel count."""
        if self.bitrate_kbps:
            return self.bitrate_kbps
        table = BITRATE_TABLE.get(codec, BITRATE_TABLE["h264"])
        heights = sorted(table)
        height = size[1]
        nearest = min(heights, key=lambda h: abs(h - height))
        base = table[nearest] * (size[0] * size[1]) / (nearest * nearest * 16 / 9)
        factor = max(0.5, min(2.0, (fps or 30) / 30)) ** 0.6  # twice the frame rate needs about 1.5 times the bits
        return max(300, round(base * factor / 50) * 50)


# The classes of change, cheapest first. Which one a change belongs to decides how the stream is touched.
SEAMLESS = {"bitrate_kbps", "rate_control", "keyint_s", "quality", "encoder"}  # new encoder, keyframe-aligned cut
GEOMETRY = {"tier", "fps"}  # the picture format changes: every sink reconnects once, recordings roll to a new file
CODEC = {"codec"}  # a different chain: every destination moves, and some reconnect
AUDIO = {"audio_bitrate_kbps"}  # the audio encoder is swapped at an Opus frame boundary


def classify_change(old: EncoderSettings, new: EncoderSettings) -> str:
    """ "none" | "audio" | "seamless" | "geometry" | "codec": the most invasive class among the changed fields."""
    changed = {name for name in EncoderSettings.__dataclass_fields__ if getattr(old, name) != getattr(new, name)}
    if not changed:
        return "none"
    if changed & CODEC:
        return "codec"
    if changed & GEOMETRY:
        return "geometry"
    if changed & SEAMLESS:
        return "seamless"
    return "audio"


# -- secrets -------------------------------------------------------------------------------------------------

SECRET_TIMEOUT = 15.0
REF = re.compile(r"(env|file|cfg|cmd):(.+)", re.S)


class SecretError(Exception):
    """A secret reference could not be resolved. The message names the reference, never the secret."""


def resolve_secret(
    ref: str,
    cfg: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
    run: Callable[[list[str]], str] | None = None,
) -> str:
    """The secret a reference points at. Forms: `env:NAME`, `file:/path`, `cfg:STREAMSTACK_KEY`, `cmd:argv...`.

    `cmd:` runs the arguments (shell-split, no shell) and takes stdout, so a vault CLI can be the source, for
    example `cmd:az keyvault secret show --vault-name V --name N --query value -o tsv`.
    """
    match = REF.fullmatch(ref.strip())
    if not match:
        raise SecretError("A key reference looks like env:NAME, file:/path, cfg:NAME or cmd:command args.")
    scheme, target = match.group(1), match.group(2).strip()
    if scheme == "env":
        value = (environ if environ is not None else os.environ).get(target, "")
    elif scheme == "cfg":
        value = (cfg or {}).get(target, "")
    elif scheme == "file":
        try:
            value = Path(target).expanduser().read_text()
        except OSError as exc:
            raise SecretError(f"Cannot read the key file {target!r}: {exc.strerror or exc}.") from None
    else:
        try:
            argv = shlex.split(target)
        except ValueError as exc:
            raise SecretError(f"The key command {target!r} cannot be parsed: {exc}.") from None
        if not argv:
            raise SecretError("The key command is empty.")
        try:
            value = (run or _run_secret_command)(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SecretError(f"The key command {argv[0]!r} failed: {type(exc).__name__}.") from None
    value = value.strip()
    if not value:
        raise SecretError(
            f"The key reference {scheme}:{target if scheme != 'cmd' else shlex.split(target)[0]} is empty."
        )
    return value


def _run_secret_command(argv: list[str]) -> str:
    done = subprocess.run(argv, capture_output=True, text=True, timeout=SECRET_TIMEOUT, check=True)
    return done.stdout


def redact(text: str, secrets: list[str]) -> str:
    """`text` with every secret (and its URL-quoted form) replaced, for logs and the Console."""
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        for form in {secret, quote(secret, safe=""), quote(secret)}:
            text = text.replace(form, "***")
    return text


# -- destinations --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Destination:
    """One place the program goes. Keys and tokens are references (`key_ref`), never values."""

    id: str
    kind: str  # record | srt | rtmp | whip
    name: str = ""
    url: str = ""  # srt://host:port, rtmp(s)://host/app, https://whip-endpoint; empty for record
    key_ref: str = ""  # rtmp stream key, whip bearer token, srt passphrase: a reference, see resolve_secret
    codec: str = "auto"  # auto, or the one codec this destination must get
    options: Mapping[str, Any] = field(default_factory=dict)
    enabled: bool = True

    def label(self) -> str:
        return self.name or self.id

    def allowed_codecs(self) -> tuple[str, ...]:
        """The codecs this destination can take, best first, honouring its own codec choice and RTMP mode."""
        base = KIND_CODECS[self.kind]
        if self.kind == "rtmp" and self.options.get("mode") == "classic":
            base = ("h264",)
        if self.kind == "srt" and self.codec == "av1":
            base = ("av1", *base)
        if self.kind == "whip" and self.codec in ("hevc", "av1"):
            base = (self.codec, *base)  # WebRTC receivers rarely take them, so only when asked for by name
        if self.codec != "auto":
            return (self.codec,) if self.codec in base else ()
        return base

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "url": self.url,
            "key_ref": self.key_ref,
            "codec": self.codec,
            "options": dict(self.options),
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Destination:
        if not isinstance(data, Mapping):
            raise SettingsError("A destination must be an object.")
        unknown = sorted(set(data) - set(cls.__dataclass_fields__))
        if unknown:
            raise SettingsError(f"Unknown destination field {unknown[0]!r}.")
        ident, kind = str(data.get("id", "")), str(data.get("kind", ""))
        if not DEST_ID.fullmatch(ident):
            raise SettingsError("A destination id is lowercase letters, digits, - and _, starting with a letter.")
        if kind not in KINDS:
            raise SettingsError(f"kind must be one of {', '.join(KINDS)}.")
        codec = str(data.get("codec", "auto"))
        if codec not in ("auto", *CODECS):
            raise SettingsError(f"codec must be auto or one of {', '.join(CODECS)}.")
        options = dict(data.get("options") or {})
        dest = cls(
            ident,
            kind,
            str(data.get("name", ""))[:60],
            str(data.get("url", "")).strip(),
            str(data.get("key_ref", "")).strip(),
            codec,
            options,
            bool(data.get("enabled", True)),
        )
        dest.validate()
        return dest

    def validate(self) -> None:
        if self.kind == "record":
            fmt = self.options.get("format", "mkv")
            if fmt not in RECORD_FORMATS:
                raise SettingsError(f"record format must be one of {', '.join(RECORD_FORMATS)}.")
            return
        parts = urlsplit(self.url)
        schemes = {"srt": ("srt",), "rtmp": ("rtmp", "rtmps"), "whip": ("https", "http")}[self.kind]
        if parts.scheme not in schemes or not parts.hostname:
            raise SettingsError(f"A {self.kind} destination needs a {' or '.join(s + '://' for s in schemes)} URL.")
        if parts.username or parts.password:
            raise SettingsError("Put credentials in key_ref, not in the URL.")
        if self.kind == "srt":
            if self.options.get("mode", "caller") not in SRT_MODES:
                raise SettingsError(f"SRT mode must be one of {', '.join(SRT_MODES)}.")
            if parts.port is None:
                raise SettingsError("An SRT URL needs a port: srt://host:port.")
            _number("SRT latency_ms", self.options.get("latency_ms", 120), 20, 8000)
        if self.kind == "rtmp" and self.options.get("mode", "auto") not in RTMP_MODES:
            raise SettingsError(f"RTMP mode must be one of {', '.join(RTMP_MODES)}.")
        if self.kind == "rtmp" and "?" in self.url:
            raise SettingsError("Put the stream key in key_ref; the URL ends at the application name.")
        if self.key_ref and not REF.fullmatch(self.key_ref):
            raise SettingsError("key_ref looks like env:NAME, file:/path, cfg:NAME or cmd:command args.")
        if not self.allowed_codecs():
            raise SettingsError(f"A {self.kind} destination cannot carry codec {self.codec}.")

    def live(self) -> bool:
        return self.kind in LIVE_KINDS


def destinations_from(data: Any) -> list[Destination]:
    if not isinstance(data, list):
        raise SettingsError("destinations must be a list.")
    out = [Destination.from_dict(item) for item in data]
    ids = [d.id for d in out]
    if len(set(ids)) != len(ids):
        raise SettingsError("Destination ids must be unique.")
    return out


MAX_KEY_CHARS = 1024


def edit_destinations(
    data: Any, existing: list[Destination], key_ref_for: Callable[[str], str]
) -> tuple[list[Destination], dict[str, str]]:
    """The destination list a `stream.set` asks for, with the keys it carries kept out of the list itself.

    The Console never receives a stream key back (the state says only `"key": "set"|"none"`), so it cannot resend
    one: an entry whose id already exists and that carries neither `key` nor `key_ref` keeps the stored reference.
    An entry may carry:

    - `key`: the secret itself, typed once. It is returned in the second value (`{id: secret}`) for the caller to
      store privately, and the destination's `key_ref` becomes `key_ref_for(id)`. An empty `key` clears the key.
    - `key_ref`: a reference (`env:NAME`, ...); an empty one clears the key.

    Errors name the field and the rule, never the value.
    """
    if not isinstance(data, list):
        raise SettingsError("destinations must be a list.")
    stored = {d.id: d.key_ref for d in existing}
    secrets: dict[str, str] = {}
    entries: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, Mapping):
            raise SettingsError("A destination must be an object.")
        entry = dict(item)
        ident = str(entry.get("id", ""))
        if "key" in entry:
            key = entry.pop("key")
            if "key_ref" in entry:
                raise SettingsError("Give a destination a key or a key_ref, not both.")
            if not isinstance(key, str):
                raise SettingsError("key must be text.")
            key = key.strip()
            if len(key) > MAX_KEY_CHARS or not key.isprintable() or " " in key:
                raise SettingsError(f"key must be one printable word of at most {MAX_KEY_CHARS} characters.")
            if key and DEST_ID.fullmatch(ident):
                secrets[ident] = key
                entry["key_ref"] = key_ref_for(ident)
            else:
                entry["key_ref"] = ""
        elif "key_ref" not in entry and ident in stored:
            entry["key_ref"] = stored[ident]
        entries.append(entry)
    return destinations_from(entries), secrets


# -- planning ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    """Which codecs to encode and which destination takes which."""

    chains: dict[str, list[str]]  # codec -> destination ids
    unserved: dict[str, str]  # destination id -> why it cannot be served

    def codec_of(self, dest_id: str) -> str | None:
        return next((codec for codec, ids in self.chains.items() if dest_id in ids), None)


def plan_chains(
    destinations: list[Destination], available: set[str], wanted: str = "auto", max_chains: int = 2
) -> Plan:
    """Give each destination the best codec it can take and the host can encode, with at most `max_chains` encodes.

    `available` is the set of codecs with a working encoder (from the probe); `wanted` is the global codec choice.
    The set of encodes is chosen to serve the most destinations first and the best codecs second (every set of
    at most `max_chains` codecs is tried; there are only three codecs).
    """
    usable: dict[str, list[str]] = {}
    unserved: dict[str, str] = {}
    for dest in destinations:
        options = [c for c in dest.allowed_codecs() if c in available]
        if wanted != "auto" and dest.codec == "auto":
            options = [c for c in options if c == wanted] or options
        if options:
            usable[dest.id] = options
        else:
            names = ", ".join(dest.allowed_codecs()) or "none"
            unserved[dest.id] = f"no working encoder for what {dest.label()} can take ({names})"

    def pick(options: list[str], codecs: tuple[str, ...]) -> str | None:
        inside = [c for c in options if c in codecs]
        return min(inside, key=CODEC_RANK.__getitem__) if inside else None

    best: tuple[int, int] | None = None
    best_set: tuple[str, ...] = ()
    for size in range(1, max(1, max_chains) + 1):
        for codecs in itertools.combinations(sorted(available & set(CODECS), key=CODEC_RANK.__getitem__), size):
            picks = [pick(options, codecs) for options in usable.values()]
            served = [c for c in picks if c is not None]
            score = (len(served), -sum(CODEC_RANK[c] for c in served))
            if best is None or score > best:
                best, best_set = score, codecs
    chains: dict[str, list[str]] = {}
    for dest_id, options in usable.items():
        codec = pick(options, best_set)
        if codec is None:
            unserved[dest_id] = f"needs {options[0]}, and only {max_chains} encodes may run at once"
        else:
            chains.setdefault(codec, []).append(dest_id)
    return Plan(dict(sorted(chains.items(), key=lambda kv: CODEC_RANK[kv[0]])), unserved)


class AssignError(ValueError):
    """A destination cannot be given a codec chain right now (the message says why)."""


def assign_codec(
    dest: Destination, available: set[str], existing: set[str], wanted: str = "auto", max_chains: int = 2
) -> str:
    """The codec chain for one destination joining a running stream, without disturbing the chains already up.

    Quality first: the best codec the destination can take and the host can encode, even when that opens a new
    chain; but never beyond `max_chains` encodes, and a running chain is reused whenever the cap would otherwise
    be exceeded.
    """
    usable = [c for c in dest.allowed_codecs() if c in available]
    if wanted != "auto" and dest.codec == "auto":
        usable = [c for c in usable if c == wanted] or usable
    if not usable:
        takes = ", ".join(dest.allowed_codecs()) or "nothing this host offers"
        raise AssignError(f"{dest.label()} needs one of {takes}, and none has a working encoder on this host.")
    best = min(usable, key=CODEC_RANK.__getitem__)
    if best in existing or len(existing) < max_chains:
        return best
    shared = [c for c in usable if c in existing]
    if shared:
        return min(shared, key=CODEC_RANK.__getitem__)
    raise AssignError(
        f"{dest.label()} would need another encode ({best}); stop a destination first, "
        f"or raise the limit of {max_chains}."
    )
