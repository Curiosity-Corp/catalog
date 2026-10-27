"""Resolution: a snapshot plus the profiles gives a `Plan` (ADR 0003, section 3).

`resolve(snapshot, catalogue, overrides)` is a pure function: the same inputs give the same Plan, nothing is read from
the machine, and no host name exists anywhere in it. Precedence is explicit settings (`overrides`), then the rig
profile, then the device profiles, then generic Linux defaults (any VA-API encoder else the CPU, the default PipeWire
source and sink, any UVC capture node, 48000 Hz).

The Plan names what the rest of StreamStack should use: the render node for each GPU role, the encoder, the audio
interface with its rate, bit depth and the call microphone and monitor nodes, the capture card and its modes, the
loopback nodes and which controls are present. Consumers read it through accessors (`plan.gpu("encode").node_path`,
`plan.audio_interface()`, `plan.capture_card()`), never by probing again, and `plan.diff(other)` says which sections a
hot-plug event changed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from streamstack import camlink, config

from .model import AlsaCard, PipewireCard, PipewireNode, RenderNode, Snapshot, V4l2Format, V4l2Node
from .schema import GPU_ROLES, Catalogue, DeviceProfile, EncoderSpec, RigProfile

CHAIN_RATE = 48000  # the conferencing path and the chain's preferred graph rate
# Formats in the chain's order of preference, with PipeWire's name and the bit depth.
FORMAT_PREFERENCE = (
    ("S24_3LE", "s24le", 24),
    ("S24_LE", "s24_32le", 24),
    ("S32_LE", "s32le", 32),
    ("S16_LE", "s16le", 16),
)
CODEC_RANK = {"av1": 3, "hevc": 2, "h264": 1}
GENERIC_ENCODERS = (
    EncoderSpec("av1", "av1_vaapi", "", ""),
    EncoderSpec("hevc", "hevc_vaapi", "", ""),
    EncoderSpec("h264", "h264_vaapi", "", ""),
)
VOICE_SOURCE = "streamstack_mic"
DUMMY_SINK = "auto_null"
REQUIRED_WEIGHT = 3
OPTIONAL_WEIGHT = 1
USABLE = (None, "rw")  # a node access that is not "denied"


@dataclass(frozen=True)
class Notice:
    id: str
    level: str  # "info" | "warn"
    text: str


@dataclass(frozen=True)
class RigChoice:
    id: str
    label: str
    via: str  # "env" | "overlap" | "identity" | "fallback"
    score: int
    candidates: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class DeviceRow:
    id: str  # the device profile id
    label: str
    device_class: str
    required: bool
    expected: bool  # named by the selected rig
    state: str  # "present" | "absent" | "denied"
    via: str = ""  # "usb" | "pci" | "name"
    detail: str = ""


@dataclass(frozen=True)
class GpuPlan:
    pci_id: str
    profile_id: str | None
    name: str
    driver: str
    node_path: str
    access: str | None
    encode_codecs: tuple[str, ...]
    decode_codecs: tuple[str, ...]
    probed: bool  # whether `vainfo` answered for this node
    roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class GpuRole:
    role: str
    node_path: str
    pci_id: str
    profile_id: str | None
    name: str
    via: str  # "override" | "rig" | "profile" | "generic"


@dataclass(frozen=True)
class EncoderPlan:
    codec: str
    ffmpeg: str
    hardware: bool
    node_path: str | None  # the render node the encoder runs on; None on the CPU
    pci_id: str | None
    verified: bool  # `vainfo` confirmed the profile on the node (False: taken from the GPU profile)
    reason: str = ""  # why the CPU, when it is the CPU


@dataclass(frozen=True)
class AudioInterfacePlan:
    profile_id: str
    label: str
    driver: str
    usb_id: str | None
    alsa_card: int | None
    alsa_id: str | None
    pipewire_card: str | None
    card_off: bool  # PipeWire exposes no nodes for it (profile off)
    via: str  # "override" | "rig" | "profile"


@dataclass(frozen=True)
class AudioPlan:
    interface: AudioInterfacePlan | None
    rate: int  # the graph sample rate
    format: str | None  # the graph sample format in PipeWire's name ("s24le"); None: the graph's own default
    bits: int | None
    native_rate: int | None  # the interface's own rate; differs from the conferencing rate when it must be resampled
    conferencing_rate: int
    resamples: bool
    capture_channels: int | None
    microphone: str | None  # PipeWire source node name
    monitor: str | None  # PipeWire sink node name
    source: str  # "override" | "interface" | "default" | "none"
    rates: tuple[int, ...] = ()  # the rates the interface offers (both directions when it has both)
    rate_reason: str = ""  # why `rate` is the rate
    quantum: int | None = None  # the profile's node.latency for the voice chain, in frames at the graph rate
    card_profile: str = ""  # the PipeWire card profile that exposes the interface's source and sink
    expected: str = ""  # the rig's preferred interface(s) by label while none of them is attached; "" otherwise


@dataclass(frozen=True)
class CaptureCardPlan:
    profile_id: str
    label: str
    state: str  # the device row's state
    node_path: str | None
    usb_id: str | None
    modes: tuple[V4l2Format, ...]
    best_mode: V4l2Format | None
    modes_state: str  # "ok" | "denied" | "unknown" (no node, so no modes)


@dataclass(frozen=True)
class LoopbackPlan:
    """The two virtual cameras. Nodes are found by card label, never by number; `*_nr` is the number the rig's
    profile declares for creating them, so a missing node can be created where the profile says."""

    camera_path: str | None
    camera_label: str
    program_path: str | None
    program_label: str
    camera_nr: int | None = None
    program_nr: int | None = None
    exclusive_caps: bool = True
    module_loaded: bool = False
    options: str = ""  # the modprobe options the rig's profile asks for ("devices=2 video_nr=42,43 ...")
    options_match: bool | None = None  # None: no options file was read; else do the installed ones equal `options`

    def declared_path(self, role: str) -> str | None:
        nr = self.camera_nr if role == "camera" else self.program_nr
        return None if nr is None else f"/dev/video{nr}"


@dataclass(frozen=True)
class Plan:
    rig: RigChoice
    devices: tuple[DeviceRow, ...]
    gpus: tuple[GpuPlan, ...]
    gpu_roles: tuple[GpuRole, ...]
    encoder: EncoderPlan
    audio: AudioPlan
    capture: CaptureCardPlan | None
    loopback: LoopbackPlan
    deck: bool
    lights: bool
    notices: tuple[Notice, ...] = ()

    # -- accessors the audio chain, the engine, the camera setup and doctor read ---------------------------------

    def gpu(self, role: str) -> GpuRole | None:
        """The GPU that takes this role ("grade", "encode", "decode", "blur"), or None when no GPU can."""
        return next((g for g in self.gpu_roles if g.role == role), None)

    def gpu_by_node(self, path: str) -> GpuPlan | None:
        """What the probe knows about the GPU behind this render node right now."""
        return next((g for g in self.gpus if g.node_path == path), None)

    def audio_interface(self) -> AudioInterfacePlan | None:
        return self.audio.interface

    def capture_card(self) -> CaptureCardPlan | None:
        return self.capture

    def device(self, ident: str) -> DeviceRow | None:
        return next((d for d in self.devices if d.id == ident), None)

    def has(self, ident: str) -> bool:
        row = self.device(ident)
        return row is not None and row.state != "absent"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def diff(self, other: Plan) -> tuple[str, ...]:
        """The sections that differ between two plans, so a hot-plug event applies only what changed."""
        names = ("rig", "devices", "gpus", "gpu_roles", "encoder", "audio", "capture", "loopback", "deck", "lights")
        return tuple(n for n in names if getattr(self, n) != getattr(other, n))


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------


def _hint(text: str, hints: tuple[str, ...]) -> bool:
    lowered = text.replace("_", " ").lower()
    return any(h.lower() in lowered for h in hints)


def _detect(snap: Snapshot, profile: DeviceProfile) -> tuple[str, str, str]:
    """(state, via, detail) for one device profile: found by USB id, then PCI id, then (only where a source has no
    ids to offer) by a name hint."""
    usb = snap.status("usb")
    for usb_id in profile.usb:
        if usb_id in snap.usb_ids():
            return _capture_access(snap, profile, "present", "usb", f"USB {usb_id}")
    for pci_id in profile.pci:
        node = snap.render_node_for(pci_id)
        if node is not None:
            if node.access == "denied":
                return "denied", "pci", f"{node.path} cannot be opened by this user"
            return "present", "pci", f"PCI {pci_id}"
        if pci_id in {d.pci_id for d in snap.pci}:
            return "present", "pci", f"PCI {pci_id}"
    ids_known = usb is not None and usb.ok and bool(profile.usb)
    if profile.names and not ids_known and not profile.pci:
        if any(_hint(c.name, profile.names) for c in snap.alsa):
            return "present", "name", "found by its sound card name"
        if any(_hint(n.label, profile.names) for n in snap.v4l2):
            return "present", "name", "found by its video device name"
        if any(_hint(c.name, profile.names) for c in snap.pipewire.cards):
            return "present", "name", "found by its PipeWire card name"
    return "absent", "", ""


def _capture_access(snap: Snapshot, profile: DeviceProfile, state: str, via: str, detail: str) -> tuple[str, str, str]:
    """A capture card or camera that is on the bus but whose video node this user cannot open is `denied`."""
    if profile.device_class != "capture-card":
        return state, via, detail
    nodes = _nodes_of(snap, profile)
    if nodes and any(n.access == "denied" or n.formats_state == "denied" for n in nodes if n.kind != "metadata"):
        return "denied", via, f"{detail}; its video node cannot be opened by this user"
    return state, via, detail


def _nodes_of(snap: Snapshot, profile: DeviceProfile) -> tuple[V4l2Node, ...]:
    return tuple(
        n
        for n in snap.v4l2
        if not n.loopback
        and ((n.usb_id and n.usb_id in profile.usb) or (not n.usb_id and _hint(n.label, profile.names)))
    )


# ---------------------------------------------------------------------------
# Rig
# ---------------------------------------------------------------------------


def _score(rig: RigProfile, present: frozenset[str]) -> int:
    score = 0
    for expect in rig.expects:
        if expect.id in present:
            score += REQUIRED_WEIGHT if expect.required else OPTIONAL_WEIGHT
        elif expect.required:
            score -= REQUIRED_WEIGHT
    return score


def _eligible(rig: RigProfile, present: frozenset[str]) -> bool:
    """More than half of a rig's required devices must be there for the rig to describe this machine."""
    required = [e.id for e in rig.expects if e.required]
    return bool(required) and 2 * sum(1 for i in required if i in present) > len(required)


def _select_rig(
    snap: Snapshot, catalogue: Catalogue, present: frozenset[str], wanted: str, notices: list[Notice]
) -> RigChoice:
    wanted = wanted.strip().lower()
    scores = tuple((r.id, _score(r, present)) for r in catalogue.rigs if not r.fallback)
    if wanted and wanted != "auto":
        rig = catalogue.rig(wanted)
        if rig is not None:
            return RigChoice(rig.id, rig.label, "env", _score(rig, present), scores)
        notices.append(
            Notice(
                "rig-unknown",
                "warn",
                f"STREAMSTACK_RIG names {wanted!r}, which is not a rig profile; choosing by overlap",
            )
        )
    eligible = [r for r in catalogue.rigs if not r.fallback and _eligible(r, present)]
    if not eligible:
        rig = catalogue.fallback_rig()
        return RigChoice(rig.id, rig.label, "fallback", 0, scores)
    best = max(_score(r, present) for r in eligible)
    tied = [r for r in eligible if _score(r, present) == best]
    via = "overlap"
    if len(tied) > 1 and snap.machine:
        known = [r for r in tied if snap.machine in r.machine_ids]
        if known:
            tied, via = known, "identity"
    rig = tied[0]
    return RigChoice(rig.id, rig.label, via, best, scores)


# ---------------------------------------------------------------------------
# GPUs and the encoder
# ---------------------------------------------------------------------------


def _gpu_profile(catalogue: Catalogue, node: RenderNode) -> DeviceProfile | None:
    return next((d for d in catalogue.of_class("gpu") if node.pci_id and node.pci_id in d.pci), None)


def _node_can(spec: EncoderSpec, node: RenderNode) -> bool:
    if node.va is None or not node.va.ok:
        return False
    if spec.va_profile:
        return node.va.can_encode(spec.va_profile, spec.entrypoint)
    return spec.codec in node.va.encode_codecs()


def _rank(node: RenderNode, profile: DeviceProfile | None) -> int:
    """How good a node is at encoding: the best codec it offers (probed), else what its profile promises."""
    if node.va is not None and node.va.ok:
        return max((CODEC_RANK.get(c, 0) for c in node.va.encode_codecs()), default=0)
    if profile is not None and profile.gpu is not None:
        return max((CODEC_RANK.get(e.codec, 0) for e in profile.gpu.encoders), default=0)
    return 0


def _usable(node: RenderNode) -> bool:
    return node.access in USABLE


def _assign_roles(
    snap: Snapshot,
    catalogue: Catalogue,
    rig: RigProfile,
    overrides: Mapping[str, str],
    notices: list[Notice],
) -> tuple[GpuRole, ...]:
    nodes = snap.render_nodes
    explicit = {
        "grade": overrides.get("STREAMSTACK_ENGINE_RENDER_NODE", "").strip(),
        "blur": overrides.get("STREAMSTACK_ENGINE_RENDER_NODE", "").strip(),
        "decode": overrides.get("STREAMSTACK_CANON_RENDER_NODE", "").strip(),
        "encode": overrides.get("STREAMSTACK_STREAM_RENDER_NODE", "").strip(),
    }
    roles: list[GpuRole] = []
    warned: set[str] = set()
    for role in GPU_ROLES:
        chosen: tuple[RenderNode, str] | None = None
        wanted = explicit.get(role, "")
        if wanted:
            node = next((n for n in nodes if n.path == wanted), None)
            if node is not None:
                chosen = (node, "override")
            elif wanted not in warned:
                warned.add(wanted)
                notices.append(
                    Notice(
                        "render-node-unknown",
                        "warn",
                        f"{wanted} is not a render node on this machine; choosing by profile",
                    )
                )
        if chosen is None:
            for ident in rig.gpu_candidates(role):
                profile = catalogue.device(ident)
                node = next((n for n in nodes if profile and n.pci_id in profile.pci and _usable(n)), None)
                if node is not None:
                    chosen = (node, "rig")
                    break
        if chosen is None:
            fit = [
                (n, p)
                for n in nodes
                if _usable(n)
                and (p := _gpu_profile(catalogue, n)) is not None
                and p.gpu is not None
                and role in p.gpu.roles
            ]
            if fit:
                best = max(fit, key=lambda np: (_rank(*np), -int(np[0].path.rsplit("D", 1)[-1] or 0)))
                chosen = (best[0], "profile")
        if chosen is None:
            unknown = [n for n in nodes if _usable(n) and _gpu_profile(catalogue, n) is None]
            if role == "encode":
                unknown = [n for n in unknown if _rank(n, None) > 0]
            if unknown:
                best_node = max(unknown, key=lambda n: (_rank(n, None), -int(n.path.rsplit("D", 1)[-1] or 0)))
                chosen = (best_node, "generic")
        if chosen is None:
            continue
        node, via = chosen
        profile = _gpu_profile(catalogue, node)
        roles.append(
            GpuRole(
                role,
                node.path,
                node.pci_id,
                profile.id if profile else None,
                node.name or (profile.label if profile else ""),
                via,
            )
        )
    for node in nodes:
        if node.access == "denied":
            notices.append(
                Notice("render-node-denied", "warn", f"{node.path} ({node.pci_id}) cannot be opened by this user")
            )
    return tuple(roles)


def _encoder(snap: Snapshot, catalogue: Catalogue, roles: tuple[GpuRole, ...]) -> EncoderPlan:
    encoders = snap.ffmpeg.encoders if snap.ffmpeg is not None else None
    role = next((r for r in roles if r.role == "encode"), None)
    reason = "no GPU can encode"
    if role is not None:
        node = next(n for n in snap.render_nodes if n.path == role.node_path)
        profile = catalogue.device(role.profile_id) if role.profile_id else None
        specs = profile.gpu.encoders if profile and profile.gpu else GENERIC_ENCODERS
        probed = node.va is not None and node.va.ok
        for spec in specs:
            if probed and not _node_can(spec, node):
                continue
            if encoders is not None and spec.ffmpeg not in encoders:
                continue
            return EncoderPlan(spec.codec, spec.ffmpeg, True, node.path, node.pci_id, probed)
        reason = (
            f"{node.path} offers no usable hardware encoder" if probed else f"{node.path} was not probed for VA-API"
        )
    if encoders is not None and "libsvtav1" in encoders:
        return EncoderPlan("av1", "libsvtav1", False, None, None, True, reason)
    return EncoderPlan("h264", "libx264", False, None, None, encoders is not None and "libx264" in encoders, reason)


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------


def _alsa_card(snap: Snapshot, profile: DeviceProfile, usb_id: str | None) -> AlsaCard | None:
    if usb_id:
        found = next((c for c in snap.alsa if c.usb_id == usb_id), None)
        if found is not None:
            return found
    return next((c for c in snap.alsa if not c.usb_id and profile.names and _hint(c.name, profile.names)), None)


def _pipewire_card(snap: Snapshot, profile: DeviceProfile, usb_id: str | None) -> PipewireCard | None:
    if usb_id:
        found = next((c for c in snap.pipewire.cards if c.usb_id == usb_id), None)
        if found is not None:
            return found
    return next(
        (c for c in snap.pipewire.cards if not c.usb_id and profile.names and _hint(c.name, profile.names)), None
    )


def _choose_interface(
    snap: Snapshot,
    catalogue: Catalogue,
    rig: RigProfile,
    present: frozenset[str],
    wanted: str,
    notices: list[Notice],
) -> tuple[DeviceProfile, str] | None:
    wanted = wanted.strip().lower()
    interfaces = [d for d in catalogue.of_class("audio-interface") if d.audio and d.audio.driver]
    if wanted == "off":
        return None
    if wanted and wanted != "auto":
        match = next((d for d in interfaces if wanted in (d.id, *d.usb) and d.id in present), None)
        if match is not None:
            return match, "override"
        notices.append(
            Notice("interface-override", "warn", f"STREAMSTACK_INTERFACE={wanted} is not attached; choosing by profile")
        )
    for ident in rig.audio_interface:
        profile = catalogue.device(ident)
        if profile is not None and profile.id in present:
            return profile, "rig"
    profile = next((d for d in interfaces if d.id in present), None)
    return (profile, "profile") if profile else None


def _format_of(card: AlsaCard | None, profile: DeviceProfile) -> tuple[str, str, int] | None:
    offered = set(card.formats("capture") + card.formats("playback")) if card else set()
    if not offered and profile.audio:
        offered = set(profile.audio.formats)
    return next((f for f in FORMAT_PREFERENCE if f[0] in offered), None)


def _graph_rate(rates: tuple[int, ...]) -> int:
    if not rates or CHAIN_RATE in rates:
        return CHAIN_RATE
    above = [r for r in rates if r > CHAIN_RATE]
    return min(above) if above else max(rates)


def _interface_rate(rates: tuple[int, ...], preferred: int | None, note: str) -> tuple[int, str]:
    """The interface's rate and why: the profile's preferred rate when the interface offers it, else the nearest
    to the conferencing rate that it does offer."""
    if preferred is not None and (not rates or preferred in rates):
        return preferred, note or f"the interface profile's preferred rate ({preferred} Hz)"
    rate = _graph_rate(rates)
    if not rates:
        return rate, "no rates known for this interface; the conferencing rate"
    if rate == CHAIN_RATE:
        return rate, "the interface offers the conferencing rate"
    return rate, f"the interface offers only {', '.join(str(r) for r in rates)} Hz; calls are resampled from it"


def _capture_card_cards(snap: Snapshot, catalogue: Catalogue) -> set[str]:
    """PipeWire card names of capture cards whose audio must never be the call microphone."""
    names: set[str] = set()
    for profile in catalogue.of_class("capture-card"):
        if profile.capture is not None and profile.capture.microphone:
            continue
        for card in snap.pipewire.cards:
            if (card.usb_id and card.usb_id in profile.usb) or (not card.usb_id and _hint(card.name, profile.names)):
                names.add(card.name)
    return names


def _real_source(node: PipewireNode, excluded_cards: set[str]) -> bool:
    return (
        node.kind == "source"
        and node.name != VOICE_SOURCE
        and node.card not in excluded_cards
        and (node.card is not None or node.name.startswith(("alsa_input.", "bluez_input.")))
    )


def _audio(
    snap: Snapshot,
    catalogue: Catalogue,
    rig: RigProfile,
    present: frozenset[str],
    overrides: Mapping[str, str],
    notices: list[Notice],
) -> AudioPlan:
    pw = snap.pipewire
    excluded = _capture_card_cards(snap, catalogue)
    chosen = _choose_interface(snap, catalogue, rig, present, overrides.get("STREAMSTACK_INTERFACE", ""), notices)
    interface: AudioInterfacePlan | None = None
    card: AlsaCard | None = None
    pcard: PipewireCard | None = None
    profile: DeviceProfile | None = None
    if chosen is not None:
        profile, via = chosen
        usb_id = next((u for u in profile.usb if u in snap.usb_ids()), None)
        card = _alsa_card(snap, profile, usb_id)
        pcard = _pipewire_card(snap, profile, usb_id)
        interface = AudioInterfacePlan(
            profile.id,
            profile.label,
            profile.audio.driver if profile.audio else "",
            usb_id,
            card.number if card else None,
            card.id if card else None,
            pcard.name if pcard else None,
            pcard.off if pcard else False,
            via,
        )
    # rate and format: the interface's own, in the chain's order of preference
    rates: tuple[int, ...] = ()
    if card is not None:
        both = sorted(set(card.rates("capture")) & set(card.rates("playback")))
        rates = tuple(both) or card.rates("capture") or card.rates("playback")
    if not rates and profile is not None and profile.audio is not None:
        rates = (
            tuple(sorted(set(profile.audio.capture_rates) & set(profile.audio.playback_rates)))
            or profile.audio.capture_rates
            or profile.audio.playback_rates
        )
    if profile is not None and profile.audio is not None:
        rate, rate_reason = _interface_rate(rates, profile.audio.preferred_rate, profile.audio.rate_note)
    elif profile is not None:
        rate, rate_reason = _graph_rate(rates), "the interface offers the conferencing rate"
    else:
        rate, rate_reason = CHAIN_RATE, "no audio interface profile: the conferencing rate"
    fmt = _format_of(card, profile) if profile is not None else None
    channels = None
    if card is not None and card.channels("capture"):
        channels = max(card.channels("capture"))
    elif profile is not None and profile.audio is not None:
        channels = profile.audio.capture_channels
    # the call microphone and the monitor
    microphone = monitor = None
    source = "none"
    wanted = overrides.get("STREAMSTACK_MICROPHONE", "").strip().lower().replace("_", " ")
    if wanted:
        microphone = next(
            (n.name for n in pw.nodes if _real_source(n, excluded) and wanted in n.name.lower().replace("_", " ")), None
        )
        if microphone:
            source = "override"
        else:
            notices.append(
                Notice(
                    "microphone-override",
                    "warn",
                    f"STREAMSTACK_MICROPHONE={wanted!r} matches no audio source; choosing by profile",
                )
            )
    if microphone is None and interface is not None and pcard is not None:
        microphone = next((n.name for n in pw.nodes_of(pcard.name, "source")), None)
        if microphone:
            source = "interface"
        elif pcard.off:
            notices.append(
                Notice(
                    "interface-card-off", "warn", f"{interface.label} is attached but its sound card is at profile off"
                )
            )
    if interface is not None and pcard is not None:
        monitor = next((n.name for n in pw.nodes_of(pcard.name, "sink")), None)
    if interface is None or (microphone is None and source == "none"):
        if microphone is None:
            candidate = next((n for n in pw.nodes if n.name == pw.default_source and _real_source(n, excluded)), None)
            candidate = candidate or next((n for n in pw.nodes if _real_source(n, excluded)), None)
            if candidate is not None:
                microphone, source = candidate.name, "default"
        if monitor is None:
            sink = next((n for n in pw.nodes if n.kind == "sink" and n.name == pw.default_sink), None)
            sink = sink or next((n for n in pw.nodes if n.kind == "sink"), None)
            monitor = sink.name if sink else None
    if interface is None and not _any_external(present, catalogue):
        off = [c for c in pw.cards if c.off]
        detail = (
            f"every sound card is at profile off ({len(off)} cards)"
            if off and len(off) == len(pw.cards)
            else "none is attached"
        )
        if microphone is None:
            notices.append(
                Notice("audio-fallback", "warn", f"No audio interface ({detail}); there is no call microphone")
            )
        else:
            notices.append(
                Notice("audio-fallback", "info", "No audio interface from a profile; using the default source")
            )
    if monitor == DUMMY_SINK:
        notices.append(Notice("audio-dummy-sink", "warn", "PipeWire is on its Dummy Output: nothing is audible"))
    return AudioPlan(
        interface=interface,
        rate=rate,
        format=fmt[1] if fmt else None,
        bits=fmt[2] if fmt else None,
        native_rate=rate if interface is not None else None,
        conferencing_rate=CHAIN_RATE,
        resamples=rate != CHAIN_RATE,
        capture_channels=channels,
        microphone=microphone,
        monitor=monitor,
        source=source,
        rates=rates,
        rate_reason=rate_reason,
        quantum=profile.audio.quantum if profile is not None and profile.audio is not None else None,
        card_profile=profile.audio.card_profile if profile is not None and profile.audio is not None else "",
        expected=_expected_interfaces(catalogue, rig) if interface is None else "",
    )


def _expected_interfaces(catalogue: Catalogue, rig: RigProfile) -> str:
    """The labels of the audio interfaces the rig prefers, for the one that is not attached ("" when it names none)."""
    labels = []
    for ident in rig.audio_interface:
        profile = catalogue.device(ident)
        if profile is not None and profile.label not in labels:
            labels.append(profile.label)
    return " or ".join(labels)


def _any_external(present: frozenset[str], catalogue: Catalogue) -> bool:
    return any(d.id in present and d.audio and d.audio.driver for d in catalogue.of_class("audio-interface"))


# ---------------------------------------------------------------------------
# Video inputs
# ---------------------------------------------------------------------------


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def _capture(
    snap: Snapshot, catalogue: Catalogue, rows: Mapping[str, DeviceRow], overrides: Mapping[str, str]
) -> CaptureCardPlan | None:
    row = next(
        (rows[p.id] for p in catalogue.of_class("capture-card") if p.id in rows and rows[p.id].state != "absent"), None
    )
    if row is None:
        return None
    profile = catalogue.device(row.id)
    if profile is None:
        return None
    nodes = [n for n in _nodes_of(snap, profile) if n.kind in ("capture", "unknown")]
    nodes.sort(key=lambda n: int("".join(c for c in n.path if c.isdigit()) or 0))
    node = nodes[0] if nodes else None
    modes = node.formats if node else ()
    video = snap.status("v4l2")
    state = (
        node.formats_state
        if node is not None
        else ("denied" if video is not None and video.state == "denied" else "unknown")
    )
    best = None
    if modes:
        pin = overrides.get("STREAMSTACK_CAMLINK_MODE", "auto")
        picked = camlink.choose_mode(
            [(m.fourcc, m.width, m.height, m.fps) for m in modes],
            allow_4k=_truthy(overrides.get("STREAMSTACK_CAMLINK_4K", "")),
            pin=pin,
        )
        best = next((m for m in modes if picked and (m.fourcc, m.width, m.height, m.fps) == tuple(picked)), None)
    usb_id = next((u for u in profile.usb if u in snap.usb_ids()), None)
    return CaptureCardPlan(
        profile.id, profile.label, row.state, node.path if node else None, usb_id, modes, best, state
    )


def _loopback(snap: Snapshot, rig: RigProfile, overrides: Mapping[str, str]) -> LoopbackPlan:
    spec = rig.loopback
    camera_dev = spec.device("camera") if spec else None
    program_dev = spec.device("program") if spec else None
    default_camera = config.DEFAULTS["STREAMSTACK_CAMERA_LABEL"]
    default_program = config.DEFAULTS["STREAMSTACK_PROGRAM_LABEL"]

    def label(key: str, default: str, declared: str | None) -> str:
        # a setting that differs from the packaged default is the person's choice and wins over the profile
        given = overrides.get(key, "")
        if given and given != default:
            return given
        return declared or given or default

    camera = label("STREAMSTACK_CAMERA_LABEL", default_camera, camera_dev.card_label if camera_dev else None)
    program = label("STREAMSTACK_PROGRAM_LABEL", default_program, program_dev.card_label if program_dev else None)

    def find(wanted: str) -> str | None:
        return next((d.path for d in snap.loopback.devices if d.label == wanted), None)

    wanted_options = spec.modprobe_options() if spec else ""
    installed = snap.loopback.options
    match: bool | None = None
    if spec is not None and installed:
        match = dict(installed) == dict(spec.option_pairs())
    return LoopbackPlan(
        find(camera),
        camera,
        find(program),
        program,
        camera_dev.video_nr if camera_dev else None,
        program_dev.video_nr if program_dev else None,
        all(d.exclusive_caps for d in spec.devices) if spec else True,
        snap.loopback.loaded,
        wanted_options,
        match,
    )


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


def resolve(snapshot: Snapshot, catalogue: Catalogue, overrides: Mapping[str, str] | None = None) -> Plan:
    """The Plan for what the snapshot found. Pure: no I/O, no environment, no host name."""
    overrides = overrides or {}
    notices: list[Notice] = []
    detected = {d.id: _detect(snapshot, d) for d in catalogue.devices}
    present = frozenset(i for i, (state, _via, _detail) in detected.items() if state != "absent")
    choice = _select_rig(snapshot, catalogue, present, overrides.get("STREAMSTACK_RIG", ""), notices)
    rig = catalogue.rig(choice.id) or catalogue.fallback_rig()
    expected = {e.id: e.required for e in rig.expects}

    rows: dict[str, DeviceRow] = {}
    order = [e.id for e in rig.expects] + [d.id for d in catalogue.devices if d.id not in expected]
    for ident in order:
        profile = catalogue.device(ident)
        if profile is None:
            continue
        state, via, detail = detected[ident]
        if ident in expected or state != "absent":
            rows[ident] = DeviceRow(
                ident,
                profile.label,
                profile.device_class,
                expected.get(ident, False),
                ident in expected,
                state,
                via,
                detail,
            )
    for row in rows.values():
        if row.required and row.state == "absent":
            notices.append(
                Notice(f"missing-{row.id}", "warn", f"{row.label} is expected by {rig.label} but not present")
            )

    roles = _assign_roles(snapshot, catalogue, rig, overrides, notices)
    by_node: dict[str, list[str]] = {}
    for role in roles:
        by_node.setdefault(role.node_path, []).append(role.role)
    gpus = tuple(
        GpuPlan(
            n.pci_id,
            p.id if (p := _gpu_profile(catalogue, n)) else None,
            n.name or (p.label if p else ""),
            n.driver,
            n.path,
            n.access,
            n.va.encode_codecs() if n.va and n.va.ok else (),
            n.va.decode_codecs() if n.va and n.va.ok else (),
            n.va is not None and n.va.ok,
            tuple(by_node.get(n.path, ())),
        )
        for n in snapshot.render_nodes
    )
    encoder = _encoder(snapshot, catalogue, roles)
    for node in snapshot.render_nodes:
        if node.va is not None and not node.va.ok and node.va.state != "denied":
            notices.append(Notice("vaapi-failed", "warn", f"VA-API could not be read on {node.path}: {node.va.error}"))
    audio = _audio(snapshot, catalogue, rig, present, overrides, notices)
    capture = _capture(snapshot, catalogue, rows, overrides)
    for source in snapshot.sources:
        if not source.ok and source.state in ("denied", "error", "timeout"):
            notices.append(
                Notice(f"source-{source.source}", "warn", f"{source.source}: {source.state}: {source.error}")
            )
    return Plan(
        rig=choice,
        devices=tuple(rows.values()),
        gpus=gpus,
        gpu_roles=roles,
        encoder=encoder,
        audio=audio,
        capture=capture,
        loopback=_loopback(snapshot, rig, overrides),
        deck=any(r.device_class == "deck" and r.state != "absent" for r in rows.values()),
        lights=any(r.device_class == "light" and r.state != "absent" for r in rows.values()),
        notices=tuple(notices),
    )
