"""The voice chain's target, read from the Plan (#118, ADR 0003 section 3).

`voice_target(plan, engine)` says which raw microphone the chain reads, which output monitors it, the sample rate the
graph runs at and why, the sample format, and the quantum the interface's profile asks for. The microphone and the
monitor are the nodes of the interface the Plan found by its **USB id**, never a node picked by a name, a card number or
a description; when there is no interface profile the Plan's generic answer (the default PipeWire source and sink, never
a capture card, never StreamStack's own source) is used and the target says so.

The module is pure apart from `current_plan` and `activate_card`, which read the machine and run `pactl`.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass

from streamstack import config, system
from streamstack.audiochain import graph

from .found import probe_now
from .plan import Plan
from .redact import redact

log = logging.getLogger("streamstack.hardware.voice")

INTERFACE = "interface"  # the Plan's interface profile supplies the microphone
OVERRIDE = "override"  # STREAMSTACK_MICROPHONE names it
GENERIC = "generic"  # no interface profile; the default PipeWire source
CARD_OFF = "card-off"  # the interface is attached but its sound card is at profile off
ABSENT = "absent"  # the rig expects an audio interface that is not enumerating on USB
NONE = "none"  # nothing can be the call microphone

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str] | None"]


@dataclass(frozen=True)
class VoiceTarget:
    state: str
    microphone: str | None  # PipeWire source node name the chain reads
    monitor: str | None  # PipeWire sink node name for the monitor output
    interface: str | None  # the interface profile id
    rate: int  # the graph's sample rate
    rate_reason: str
    native_rate: int | None  # the interface's own rate
    format: str | None  # the interface's sample format in PipeWire's name ("s24le")
    quantum: int | None  # node.latency in frames at `rate`, from the interface profile
    activate: tuple[str, str] | None  # (PipeWire card name, card profile) that would switch the interface on
    reason: str

    def public(self) -> dict[str, object]:
        """The Console state: node names with the device serial replaced."""
        data = asdict(self)
        data["microphone"] = redact(self.microphone)
        data["monitor"] = redact(self.monitor)
        data["activate"] = None if self.activate is None else [redact(self.activate[0]), self.activate[1]]
        return data


def _rate(plan: Plan, engine: str) -> tuple[int, str]:
    """The graph rate and why: the interface's native rate when every stage supports it, else the conferencing rate."""
    audio = plan.audio
    native = audio.native_rate if plan.audio_interface() is not None else None
    rate, reason = graph.pick_rate(native, engine)
    if native is not None and audio.rate_reason:
        reason = f"{reason}; {audio.rate_reason}" if rate == native else f"{reason} ({audio.rate_reason})"
    return rate, reason


def voice_target(plan: Plan, engine: str = "deepfilternet") -> VoiceTarget:
    """What the voice chain should read and how it should run for this Plan and this denoise engine."""
    audio = plan.audio
    interface = plan.audio_interface()
    rate, rate_reason = _rate(plan, engine)
    quantum = None if audio.quantum is None else max(16, round(audio.quantum * rate / graph.RATE))
    activate = None
    if interface is not None and interface.card_off and interface.pipewire_card and audio.card_profile:
        activate = (interface.pipewire_card, audio.card_profile)

    def target(state: str, microphone: str | None, reason: str) -> VoiceTarget:
        return VoiceTarget(
            state=state,
            microphone=microphone,
            monitor=audio.monitor,
            interface=interface.profile_id if interface else None,
            rate=rate,
            rate_reason=rate_reason,
            native_rate=audio.native_rate if interface else None,
            format=audio.format,
            quantum=quantum,
            activate=activate,
            reason=reason,
        )

    if audio.microphone is not None:
        if audio.source == "interface" and interface is not None:
            return target(INTERFACE, audio.microphone, f"{interface.label} (USB {interface.usb_id})")
        if audio.source == "override":
            return target(OVERRIDE, audio.microphone, "chosen by STREAMSTACK_MICROPHONE")
        return target(GENERIC, audio.microphone, "the default PipeWire source; no audio interface profile matched")
    if interface is not None:
        if interface.card_off:
            fix = (
                f"switching its card to {audio.card_profile}"
                if activate
                else "its profile names no card profile to switch on"
            )
            return target(CARD_OFF, None, f"{interface.label} is attached but its sound card is at profile off; {fix}")
        return target(NONE, None, f"{interface.label} is attached but PipeWire shows no source for it")
    if audio.expected:
        return target(ABSENT, None, f"{audio.expected} is not enumerating on USB")
    return target(NONE, None, "no audio interface and no usable PipeWire source")


def call_microphone(cfg: Mapping[str, str] | None = None) -> str:
    """The raw call microphone for a command line: from the Plan, else (only when the probe cannot run) the legacy
    name rule in `system.pick_microphone`."""
    found = probe_now(cfg)
    if found is None:
        return system.legacy_microphone(dict(cfg) if cfg is not None else config.load_settings())
    return found.plan.audio.microphone or ""


def activate_card(card: str, profile: str, runner: Runner | None = None) -> bool:
    """Switch a PipeWire card to a profile (`pactl set-card-profile`); True when pactl accepted it."""
    run = runner or (lambda argv: system.run(argv, timeout=10))
    result = run(["pactl", "set-card-profile", card, profile])
    if result is None or result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() if result is not None else "pactl did not run"
        log.warning("cannot switch %s to %s: %s", redact(card), profile, detail)
        return False
    return True
