"""Hardware profiles as data: versioned TOML under `streamstack/data/hardware/`, validated when loaded (ADR 0003).

Two kinds of profile live in the same files, each a `[[device]]` or `[[rig]]` table:

* a **device profile** says what a device can do and how to recognise it (`match`: USB ids, PCI ids, name hints);
* a **rig profile** says what a studio is expected to contain (`expects`, each `required` or optional) and which
  device takes which role (`preferences`).

Loading is strict: the file's `schema` must be the one this code reads, unknown keys are rejected at every level,
types and ranges are checked, ids are unique across all files, and every id a rig mentions must exist (with the
right class). A profile file that fails validation stops the load with a `ProfileError` naming the file and the key,
so a typo never silently turns a feature off.
"""

from __future__ import annotations

import functools
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
CLASSES = ("audio-interface", "capture-card", "gpu", "light", "deck", "camera")
GPU_ROLES = ("grade", "encode", "decode", "blur")
CODECS = ("av1", "hevc", "h264", "vp9")
AUDIO_TIERS = ("usb", "onboard")
GPU_KINDS = ("discrete", "integrated")
LOOPBACK_ROLES = ("camera", "program")

_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_USB_ID = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{4}$")
_ALSA_FORMAT = re.compile(r"^[A-Z0-9_]+$")
_FOURCC = re.compile(r"^[A-Za-z0-9]{4}$")
_MACHINE = re.compile(r"^[0-9a-f]{32}$")


class ProfileError(ValueError):
    """A hardware profile file is not valid. The message names the file and the key."""


@dataclass(frozen=True)
class EncoderSpec:
    codec: str
    ffmpeg: str  # the ffmpeg encoder name
    va_profile: str  # what `vainfo` must list on the node
    entrypoint: str


@dataclass(frozen=True)
class AudioSpec:
    driver: str = ""  # the hardware/profiles.py driver family; empty for interfaces the Console does not drive
    generation: str = ""
    tier: str = "usb"
    playback_rates: tuple[int, ...] = ()
    capture_rates: tuple[int, ...] = ()
    formats: tuple[str, ...] = ()  # ALSA format names, in the interface's own order of preference
    playback_channels: int | None = None
    capture_channels: int | None = None
    preferred_rate: int | None = None
    rate_note: str = ""  # why the preferred rate is the one the voice chain runs at (shown by doctor)
    quantum: int | None = None  # node.latency of the voice chain, in frames at the graph rate
    card_profile: str = ""  # the PipeWire card profile that exposes the sound card's source and sink
    clock: str = ""


@dataclass(frozen=True)
class GpuSpec:
    kind: str
    roles: tuple[str, ...]
    encoders: tuple[EncoderSpec, ...] = ()


@dataclass(frozen=True)
class CaptureSpec:
    max_height: int = 1080
    max_height_4k: int = 2160
    formats: tuple[str, ...] = ()  # cheapest first
    microphone: bool = False  # whether its audio may be the call microphone


@dataclass(frozen=True)
class DeviceProfile:
    id: str
    device_class: str
    label: str
    summary: str = ""
    usb: tuple[str, ...] = ()
    pci: tuple[str, ...] = ()
    names: tuple[str, ...] = ()  # name hints for sources that carry no id (a recorded ALSA or V4L2 label)
    audio: AudioSpec | None = None
    gpu: GpuSpec | None = None
    capture: CaptureSpec | None = None


@dataclass(frozen=True)
class LoopbackDevice:
    """One v4l2loopback node a rig wants: what it is for, the number it is created with and the name apps show."""

    role: str  # "camera" (the raw camera bridge) | "program" (what calls read)
    video_nr: int
    card_label: str
    exclusive_caps: bool = True


@dataclass(frozen=True)
class LoopbackSpec:
    """The v4l2loopback module configuration a rig wants. `modprobe_options()` is the one place the line is built."""

    devices: tuple[LoopbackDevice, ...]
    max_buffers: int = 2

    def device(self, role: str) -> LoopbackDevice | None:
        return next((d for d in self.devices if d.role == role), None)

    def option_pairs(self) -> tuple[tuple[str, str], ...]:
        """The module options in the order they are written: `key=value` pairs, the label list unquoted."""
        return (
            ("devices", str(len(self.devices))),
            ("video_nr", ",".join(str(d.video_nr) for d in self.devices)),
            ("card_label", ",".join(d.card_label for d in self.devices)),
            ("exclusive_caps", ",".join("1" if d.exclusive_caps else "0" for d in self.devices)),
            ("max_buffers", str(self.max_buffers)),
        )

    def modprobe_options(self) -> str:
        """What follows `options v4l2loopback` in a modprobe.d file for these devices (the label list is quoted)."""
        return " ".join(f'{k}="{v}"' if k == "card_label" else f"{k}={v}" for k, v in self.option_pairs())


@dataclass(frozen=True)
class Expect:
    id: str
    required: bool


@dataclass(frozen=True)
class RigProfile:
    id: str
    label: str
    summary: str = ""
    expects: tuple[Expect, ...] = ()
    audio_interface: tuple[str, ...] = ()  # candidate interfaces, best first
    gpu: tuple[tuple[str, tuple[str, ...]], ...] = ()  # role -> candidate GPU profile ids, best first
    machine_ids: tuple[str, ...] = ()  # tie-breaker only
    fallback: bool = False
    loopback: LoopbackSpec | None = None  # the virtual camera nodes this rig wants (None: the packaged defaults)

    def gpu_candidates(self, role: str) -> tuple[str, ...]:
        return next((ids for r, ids in self.gpu if r == role), ())


@dataclass(frozen=True)
class Catalogue:
    devices: tuple[DeviceProfile, ...]
    rigs: tuple[RigProfile, ...]

    def device(self, ident: str) -> DeviceProfile | None:
        return next((d for d in self.devices if d.id == ident), None)

    def rig(self, ident: str) -> RigProfile | None:
        return next((r for r in self.rigs if r.id == ident), None)

    def fallback_rig(self) -> RigProfile:
        return next(r for r in self.rigs if r.fallback)

    def of_class(self, device_class: str) -> tuple[DeviceProfile, ...]:
        return tuple(d for d in self.devices if d.device_class == device_class)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


class _Reader:
    """Typed access to one TOML table that tracks which keys were used, so the leftovers are rejected."""

    def __init__(self, table: Any, where: str) -> None:
        if not isinstance(table, Mapping):
            raise ProfileError(f"{where}: expected a table")
        self.table, self.where, self.used = table, where, set[str]()

    def fail(self, key: str, why: str) -> ProfileError:
        return ProfileError(f"{self.where}.{key}: {why}" if key else f"{self.where}: {why}")

    def has(self, key: str) -> bool:
        return key in self.table

    def _get(self, key: str, kind: type | tuple[type, ...], label: str, default: Any, required: bool) -> Any:
        self.used.add(key)
        if key not in self.table:
            if required:
                raise self.fail(key, "is required")
            return default
        value = self.table[key]
        if isinstance(value, bool) and kind is not bool:
            raise self.fail(key, f"must be {label}")
        if not isinstance(value, kind):
            raise self.fail(key, f"must be {label}")
        return value

    def string(self, key: str, default: str = "", required: bool = False) -> str:
        value = self._get(key, str, "a string", default, required)
        if required and not value.strip():
            raise self.fail(key, "must not be empty")
        return value

    def boolean(self, key: str, default: bool = False, required: bool = False) -> bool:
        return self._get(key, bool, "true or false", default, required)

    def integer(
        self, key: str, default: int | None = None, low: int = 1, high: int = 1_000_000, required: bool = False
    ) -> int | None:
        value = self._get(key, int, "a whole number", default, required)
        if value is not None and not low <= value <= high:
            raise self.fail(key, f"must be between {low} and {high}")
        return value

    def strings(
        self, key: str, pattern: re.Pattern[str] | None = None, choices: tuple[str, ...] | None = None
    ) -> tuple[str, ...]:
        raw = self._get(key, list, "a list of strings", [], False)
        out = []
        for item in raw:
            if not isinstance(item, str):
                raise self.fail(key, "must be a list of strings")
            if pattern and not pattern.match(item):
                raise self.fail(key, f"{item!r} is not in the expected form")
            if choices is not None and item not in choices:
                raise self.fail(key, f"{item!r} is not one of {', '.join(choices)}")
            out.append(item)
        if len(set(out)) != len(out):
            raise self.fail(key, "has a repeated entry")
        return tuple(out)

    def integers(self, key: str, low: int = 1, high: int = 10_000_000) -> tuple[int, ...]:
        raw = self._get(key, list, "a list of whole numbers", [], False)
        for item in raw:
            if isinstance(item, bool) or not isinstance(item, int) or not low <= item <= high:
                raise self.fail(key, f"must be a list of whole numbers between {low} and {high}")
        return tuple(raw)

    def sub(self, key: str) -> _Reader | None:
        self.used.add(key)
        if key not in self.table:
            return None
        return _Reader(self.table[key], f"{self.where}.{key}")

    def need(self, key: str) -> _Reader:
        found = self.sub(key)
        if found is None:
            raise self.fail(key, "is required")
        return found

    def tables(self, key: str) -> list[_Reader]:
        self.used.add(key)
        raw = self.table.get(key, [])
        if not isinstance(raw, list):
            raise self.fail(key, "must be an array of tables")
        return [_Reader(item, f"{self.where}.{key}[{i}]") for i, item in enumerate(raw)]

    def done(self) -> None:
        extra = sorted(set(self.table) - self.used)
        if extra:
            raise self.fail(extra[0], "is not a known key")


def _audio(reader: _Reader) -> AudioSpec:
    spec = AudioSpec(
        driver=reader.string("driver"),
        generation=reader.string("generation"),
        tier=reader.string("tier", "usb"),
        playback_rates=reader.integers("playback_rates", 8000, 768000),
        capture_rates=reader.integers("capture_rates", 8000, 768000),
        formats=reader.strings("formats", _ALSA_FORMAT),
        playback_channels=reader.integer("playback_channels", None, 1, 64),
        capture_channels=reader.integer("capture_channels", None, 1, 64),
        preferred_rate=reader.integer("preferred_rate", None, 8000, 768000),
        rate_note=reader.string("rate_note"),
        quantum=reader.integer("quantum", None, 16, 8192),
        card_profile=reader.string("card_profile"),
        clock=reader.string("clock"),
    )
    reader.done()
    if spec.tier not in AUDIO_TIERS:
        raise reader.fail("tier", f"{spec.tier!r} is not one of {', '.join(AUDIO_TIERS)}")
    rates = spec.playback_rates + spec.capture_rates
    if spec.preferred_rate and rates and spec.preferred_rate not in rates:
        raise reader.fail("preferred_rate", "is not one of the interface's rates")
    return spec


def _gpu(reader: _Reader) -> GpuSpec:
    kind = reader.string("kind", required=True)
    if kind not in GPU_KINDS:
        raise reader.fail("kind", f"{kind!r} is not one of {', '.join(GPU_KINDS)}")
    roles = reader.strings("roles", choices=GPU_ROLES)
    if not roles:
        raise reader.fail("roles", "must name at least one role")
    encoders = []
    for enc in reader.tables("encoder"):
        codec = enc.string("codec", required=True)
        if codec not in CODECS:
            raise enc.fail("codec", f"{codec!r} is not one of {', '.join(CODECS)}")
        va_profile = enc.string("va_profile", required=True)
        entrypoint = enc.string("entrypoint", required=True)
        if not va_profile.startswith("VAProfile"):
            raise enc.fail("va_profile", "must be a VAProfile name")
        if not entrypoint.startswith("VAEntrypointEnc"):
            raise enc.fail("entrypoint", "must be an encode entrypoint (VAEntrypointEnc...)")
        encoders.append(EncoderSpec(codec, enc.string("ffmpeg", required=True), va_profile, entrypoint))
        enc.done()
    if len({e.codec for e in encoders}) != len(encoders):
        raise reader.fail("encoder", "lists a codec twice")
    reader.done()
    return GpuSpec(kind, roles, tuple(encoders))


def _capture(reader: _Reader) -> CaptureSpec:
    spec = CaptureSpec(
        max_height=reader.integer("max_height", 1080, 240, 4320) or 1080,
        max_height_4k=reader.integer("max_height_4k", 2160, 240, 4320) or 2160,
        formats=reader.strings("formats", _FOURCC),
        microphone=reader.boolean("microphone", False),
    )
    reader.done()
    if spec.max_height_4k < spec.max_height:
        raise reader.fail("max_height_4k", "must not be below max_height")
    return spec


_SPEC_KEYS = {"audio-interface": "audio", "gpu": "gpu", "capture-card": "capture"}


def _device(reader: _Reader) -> DeviceProfile:
    ident = reader.string("id", required=True)
    if not _ID.match(ident):
        raise reader.fail("id", f"{ident!r} must be lowercase words joined by hyphens")
    device_class = reader.string("class", required=True)
    if device_class not in CLASSES:
        raise reader.fail("class", f"{device_class!r} is not one of {', '.join(CLASSES)}")
    label = reader.string("label", required=True)
    summary = reader.string("summary")
    match = reader.need("match")
    usb = match.strings("usb", _USB_ID)
    pci = match.strings("pci", _USB_ID)
    names = match.strings("names")
    match.done()
    if not (usb or pci or names):
        raise match.fail("", "must give at least one of usb, pci or names")
    specs: dict[str, Any] = {}
    for key, build in (("audio", _audio), ("gpu", _gpu), ("capture", _capture)):
        sub = reader.sub(key)
        if sub is None:
            continue
        if _SPEC_KEYS.get(device_class) != key:
            raise reader.fail(key, f"does not apply to a {device_class}")
        specs[key] = build(sub)
    for needed in ("audio", "gpu"):
        if _SPEC_KEYS.get(device_class) == needed and needed not in specs:
            raise reader.fail(needed, f"is required for a {device_class}")
    reader.done()
    return DeviceProfile(
        ident, device_class, label, summary, usb, pci, names, specs.get("audio"), specs.get("gpu"), specs.get("capture")
    )


def _loopback(reader: _Reader) -> LoopbackSpec:
    devices = []
    for item in reader.tables("device"):
        role = item.string("role", required=True)
        if role not in LOOPBACK_ROLES:
            raise item.fail("role", f"{role!r} is not one of {', '.join(LOOPBACK_ROLES)}")
        label = item.string("card_label", required=True)
        if any(c in label for c in ',"\n\\'):
            raise item.fail("card_label", "must not contain a comma, a quote, a backslash or a line break")
        devices.append(
            LoopbackDevice(
                role,
                item.integer("video_nr", None, 0, 255, required=True) or 0,
                label,
                item.boolean("exclusive_caps", True),
            )
        )
        item.done()
    spec = LoopbackSpec(tuple(devices), reader.integer("max_buffers", 2, 2, 32) or 2)
    reader.done()
    if not devices:
        raise reader.fail("device", "must list at least one device")
    for key, values in (
        ("role", [d.role for d in devices]),
        ("video_nr", [d.video_nr for d in devices]),
        ("card_label", [d.card_label for d in devices]),
    ):
        if len(set(values)) != len(values):
            raise reader.fail("device", f"has a repeated {key}")
    return spec


def _rig(reader: _Reader) -> RigProfile:
    ident = reader.string("id", required=True)
    if not _ID.match(ident):
        raise reader.fail("id", f"{ident!r} must be lowercase words joined by hyphens")
    expects = []
    for item in reader.tables("expects"):
        expects.append(Expect(item.string("id", required=True), item.boolean("required", required=True)))
        item.done()
    if len({e.id for e in expects}) != len(expects):
        raise reader.fail("expects", "lists a device twice")
    audio_interface: tuple[str, ...] = ()
    gpu: list[tuple[str, tuple[str, ...]]] = []
    prefs = reader.sub("preferences")
    if prefs is not None:
        audio_interface = prefs.strings("audio_interface")
        gpu_prefs = prefs.sub("gpu")
        if gpu_prefs is not None:
            gpu = [(role, gpu_prefs.strings(role)) for role in GPU_ROLES if gpu_prefs.has(role)]
            gpu_prefs.done()
        prefs.done()
    loopback_reader = reader.sub("loopback")
    rig = RigProfile(
        id=ident,
        label=reader.string("label", required=True),
        summary=reader.string("summary"),
        expects=tuple(expects),
        audio_interface=audio_interface,
        gpu=tuple(gpu),
        machine_ids=reader.strings("machine_ids", _MACHINE),
        fallback=reader.boolean("fallback", False),
        loopback=_loopback(loopback_reader) if loopback_reader is not None else None,
    )
    reader.done()
    if rig.fallback and (rig.expects or rig.machine_ids):
        raise reader.fail("fallback", "the fallback rig expects nothing and has no machine ids")
    return rig


def parse_file(text: str, name: str) -> tuple[list[DeviceProfile], list[RigProfile]]:
    """One profile file's devices and rigs. `name` is only used in messages."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ProfileError(f"{name}: not valid TOML: {exc}") from exc
    top = _Reader(raw, name)
    version = top.table.get("schema")
    top.used.add("schema")
    if version != SCHEMA_VERSION:
        raise ProfileError(f"{name}: schema must be {SCHEMA_VERSION}, found {version!r}")
    devices = [_device(r) for r in top.tables("device")]
    rigs = [_rig(r) for r in top.tables("rig")]
    top.done()
    return devices, rigs


def validate(devices: list[DeviceProfile], rigs: list[RigProfile]) -> Catalogue:
    """Cross-file rules: unique ids, rigs point at real devices of the right class, exactly one fallback rig."""
    seen: dict[str, str] = {}
    for kind, items in (("device", devices), ("rig", rigs)):
        for item in items:
            if item.id in seen:
                raise ProfileError(f"{kind} id {item.id!r} is already used by a {seen[item.id]}")
            seen[item.id] = kind
    by_id = {d.id: d for d in devices}

    def need(rig: RigProfile, ident: str, device_class: str | None, where: str) -> None:
        device = by_id.get(ident)
        if device is None:
            raise ProfileError(f"rig {rig.id}: {where} names {ident!r}, which is not a device profile")
        if device_class and device.device_class != device_class:
            raise ProfileError(f"rig {rig.id}: {where} names {ident!r}, a {device.device_class}, not a {device_class}")

    for rig in rigs:
        for expect in rig.expects:
            need(rig, expect.id, None, "expects")
        for ident in rig.audio_interface:
            need(rig, ident, "audio-interface", "preferences.audio_interface")
        for role, ids in rig.gpu:
            for ident in ids:
                need(rig, ident, "gpu", f"preferences.gpu.{role}")
                gpu = by_id[ident].gpu
                if gpu and role not in gpu.roles:
                    raise ProfileError(
                        f"rig {rig.id}: preferences.gpu.{role} names {ident!r}, which is not fit for {role}"
                    )
    fallbacks = [r.id for r in rigs if r.fallback]
    if len(fallbacks) != 1:
        raise ProfileError(f"exactly one rig must be the fallback, found {len(fallbacks)}")
    return Catalogue(tuple(devices), tuple(rigs))


def load(directory: Path | None = None) -> Catalogue:
    """Every `*.toml` in the profile directory (default: the packaged data), validated as one catalogue."""
    if directory is None:
        from streamstack.config import data_dir

        directory = data_dir() / "hardware"
    files = sorted(directory.glob("*.toml"))
    if not files:
        raise ProfileError(f"{directory}: no hardware profiles found")
    devices: list[DeviceProfile] = []
    rigs: list[RigProfile] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ProfileError(f"{path.name}: cannot be read: {exc}") from exc
        found_devices, found_rigs = parse_file(text, path.name)
        devices.extend(found_devices)
        rigs.extend(found_rigs)
    return validate(devices, rigs)


@functools.cache
def packaged() -> Catalogue:
    """The catalogue shipped in the package, loaded and validated once per process."""
    return load()
