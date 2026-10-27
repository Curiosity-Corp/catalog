"""The `hardware` group of `streamstack doctor`: the live snapshot against its resolved rig profile (ADR 0003 s.5).

`checks(plan, snapshot, catalogue)` is pure. It returns doctor rows (the dicts `cli.check` makes: id, group, state,
title, detail, hint) and every row that is not `ok` names the fix. The rows, in order:

* `hw-rig`        "rig: <id> (selected by <reason>)"
* `hw-device-*`   one per device the rig expects: present, absent (required: fail, optional: fine) or denied
* `hw-gpu-*`      one per GPU role (grade, encode, decode, blur) with the render node it resolved to, and `hw-encoder`
* `hw-audio-*`    the interface at its rate and format, the call microphone, the monitor output
* `hw-capture`    the capture card's modes; `hw-loopback-*` the two v4l2loopback devices by label
* `hw-source-*`   a probe source that was denied or failed, with the fix; `hw-notice-*` any other Plan warning

An audio interface the rig expects that is not enumerating on USB (the Scarlett 2i2 on the deck studio) is a **warning**
that names the interface and the fix: re-seat it in a direct rear USB port with a short known-good cable. It is not a
failure, because nothing on the host is broken and a failing doctor would block every install verification; and it is
not green, because the host has no call microphone.
"""

from __future__ import annotations

from typing import Any

from streamstack.audiochain import graph

from .found import Found
from .model import Snapshot
from .plan import DUMMY_SINK, DeviceRow, GpuRole, Plan
from .redact import redact, redact_text
from .schema import GPU_ROLES, Catalogue
from .voice import ABSENT, CARD_OFF, GENERIC, INTERFACE, OVERRIDE, VoiceTarget, voice_target

GROUP = "hardware"
VIA_WORDS = {
    "env": "STREAMSTACK_RIG",
    "overlap": "overlap",
    "identity": "machine identity",
    "fallback": "fallback, no rig matched",
}
# Plan notices that a row of this group already says in its own words.
COVERED_NOTICES = ("audio-fallback", "audio-dummy-sink", "interface-card-off", "render-node-denied")
COVERED_PREFIXES = ("missing-", "source-")

USB_RESEAT_FIX = (
    "an owner action at the machine: re-seat the interface in a direct rear USB port (no hub, no front panel) with a "
    "short known-good cable, then run `streamstack doctor` again"
)
RENDER_FIX = "add your user to the render group (sudo usermod -aG render $USER) and log in again"
VIDEO_FIX = (
    "log in at the machine's seat, or add your user to the video group (sudo usermod -aG video $USER) and log in again"
)
GAP_FIX = {
    "audio-interface": "plug it into a USB 3 port on the computer and check that `lsusb` lists it; "
    "try another cable or port",
    "capture-card": "plug it straight into a USB 3 port on the computer; `streamstack doctor` names a port that "
    "never started",
    "gpu": "check that `lspci` lists the card; reseat it and look in the BIOS",
}
DEFAULT_GAP_FIX = "check the USB cable and power, and that `lsusb` lists it"
SOURCE_FIX = {
    "v4l2": VIDEO_FIX,
    "drm": RENDER_FIX,
    "vaapi": RENDER_FIX,
    "usb": "check that lsusb works for this user and that /sys/bus/usb is readable",
    "alsa": "add your user to the audio group (sudo usermod -aG audio $USER) and log in again",
    "pipewire": "run the Console from your own desktop session, so it can reach your PipeWire",
}
DEFAULT_SOURCE_FIX = "run `streamstack hardware --json` and read the source's error"


def row(state: str, cid: str, title: str, detail: str = "", hint: str = "") -> dict[str, str]:
    return {"id": cid, "group": GROUP, "state": state, "title": title, "detail": detail, "hint": hint}


def rig_text(plan: Plan) -> str:
    return f"rig: {plan.rig.id} (selected by {plan.rig.via})"


def rig_header(plan: Plan) -> dict[str, Any]:
    return {
        "id": plan.rig.id,
        "label": plan.rig.label,
        "via": plan.rig.via,
        "reason": VIA_WORDS.get(plan.rig.via, plan.rig.via),
        "text": rig_text(plan),
    }


def _rig_row(plan: Plan) -> dict[str, str]:
    scores = ", ".join(f"{ident} {score}" for ident, score in plan.rig.candidates)
    detail = f"{plan.rig.label}; reason: {VIA_WORDS.get(plan.rig.via, plan.rig.via)}; scores: {scores or 'none'}"
    if plan.rig.via == "fallback":
        return row(
            "warn",
            "hw-rig",
            rig_text(plan),
            detail,
            "no studio profile matches what is plugged in: add a rig to src/streamstack/data/hardware/rigs.toml "
            "(docs/hardware-profiles.md) or set STREAMSTACK_RIG",
        )
    return row("ok", "hw-rig", rig_text(plan), detail)


# -- devices ---------------------------------------------------------------------------------------------------------


def _denied_fix(device: DeviceRow) -> str:
    if device.device_class == "gpu" or device.via == "pci":
        return RENDER_FIX
    if device.device_class in ("capture-card", "camera"):
        return VIDEO_FIX
    return "check the udev rules for this device (packaging/udev) and that your user may open it"


def _device_row(device: DeviceRow, rig_label: str) -> dict[str, str]:
    cid = f"hw-device-{device.id}"
    if device.state == "present":
        return row("ok", cid, f"{device.label}: present", device.detail)
    if device.state == "denied":
        return row(
            "fail" if device.required else "warn",
            cid,
            f"{device.label}: denied",
            f"{device.detail}; it is attached but this user cannot open it",
            _denied_fix(device),
        )
    if device.required:
        return row(
            "fail",
            cid,
            f"{device.label}: missing",
            f"{rig_label} requires it and the probe did not find it",
            GAP_FIX.get(device.device_class, DEFAULT_GAP_FIX),
        )
    return row("ok", cid, f"{device.label}: not present", "optional for this rig")


# -- GPUs ------------------------------------------------------------------------------------------------------------


def _role_row(role: str, found: GpuRole | None, plan: Plan, catalogue: Catalogue) -> dict[str, str]:
    cid = f"hw-gpu-{role}"
    rig = catalogue.rig(plan.rig.id)
    wanted = [catalogue.device(i) for i in (rig.gpu_candidates(role) if rig else ())]
    wanted_pci = {pci for d in wanted if d for pci in d.pci}
    wanted_names = " or ".join(d.label for d in wanted if d) or "any GPU"
    if found is None:
        return row(
            "fail" if role in ("grade", "encode") else "warn",
            cid,
            f"{role}: no GPU can take this role",
            f"{wanted_names} is expected and no usable render node was found",
            "check that the GPU is listed by `lspci` and its /dev/dri/renderD* node is readable: " + RENDER_FIX,
        )
    where = f"{found.node_path} ({found.name or found.pci_id}, {found.pci_id})"
    if found.via == "override":
        if wanted_pci and found.pci_id not in wanted_pci:
            return row(
                "warn",
                cid,
                f"{role}: {where}",
                f"set by an explicit render node setting; the rig expects {wanted_names}",
                "clear STREAMSTACK_ENGINE_RENDER_NODE / STREAMSTACK_CANON_RENDER_NODE to let the profile choose",
            )
        return row("ok", cid, f"{role}: {where}", "set by an explicit render node setting")
    if found.via in ("profile", "generic"):
        return row(
            "warn",
            cid,
            f"{role}: {where}",
            f"{wanted_names} is not usable, so {found.via} defaults chose this GPU",
            "check that the expected GPU is present and its render node is readable: " + RENDER_FIX,
        )
    return row("ok", cid, f"{role}: {where}", f"chosen by the {plan.rig.id} rig")


def _encoder_row(plan: Plan) -> dict[str, str]:
    enc = plan.encoder
    if enc.hardware:
        detail = f"VA-API on {enc.node_path}" + (
            "" if enc.verified else "; not confirmed by vainfo, from the GPU profile"
        )
        return row("ok", "hw-encoder", f"encoder: {enc.ffmpeg} ({enc.codec})", detail)
    return row(
        "warn",
        "hw-encoder",
        f"encoder: {enc.ffmpeg} on the CPU",
        enc.reason or "no hardware encoder",
        "check that the Arc is present and `vainfo --display drm --device <its render node>` lists an encode "
        "entrypoint",
    )


# -- audio -----------------------------------------------------------------------------------------------------------


def _off_cards(snapshot: Snapshot) -> str:
    cards = snapshot.pipewire.cards
    off = [c for c in cards if c.off]
    if cards and len(off) == len(cards):
        return f"all {len(cards)} sound cards are at profile off"
    return "no audio interface is attached"


def _audio_rows(plan: Plan, snapshot: Snapshot, target: VoiceTarget) -> list[dict[str, str]]:
    audio = plan.audio
    iface = plan.audio_interface()
    rows: list[dict[str, str]] = []
    if iface is None:
        if target.state == ABSENT:
            rows.append(
                row(
                    "warn",
                    "hw-audio-interface",
                    f"{audio.expected} is not enumerating on USB",
                    f"{plan.rig.label} expects it, and it is not among the USB devices; {_off_cards(snapshot)} and "
                    "PipeWire is on its Dummy Output, so there is no call microphone and the voice chain does not "
                    "start",
                    USB_RESEAT_FIX,
                )
            )
        elif target.state == GENERIC:
            rows.append(
                row(
                    "warn",
                    "hw-audio-interface",
                    "no audio interface profile matched; using the default source",
                    target.reason,
                    "if this is a studio interface, add its USB id to "
                    "src/streamstack/data/hardware/audio-interfaces.toml",
                )
            )
        else:
            rows.append(
                row(
                    "warn",
                    "hw-audio-interface",
                    "no audio interface and no microphone",
                    f"{_off_cards(snapshot)}; there is nothing to read",
                    "plug in a USB audio interface or microphone, then run `streamstack doctor` again",
                )
            )
    else:
        cid = "hw-audio-interface"
        card = redact(iface.pipewire_card) or "no PipeWire card"
        if target.state == CARD_OFF:
            fix = (
                f"StreamStack switches it on itself while the Console runs; by hand: pactl set-card-profile "
                f"{card} {audio.card_profile}"
                if audio.card_profile
                else "switch the card to a profile with an input in pavucontrol (Configuration)"
            )
            rows.append(row("fail", cid, f"{iface.label}: its sound card is at profile off", card, fix))
        elif target.microphone is None:
            rows.append(
                row(
                    "fail",
                    cid,
                    f"{iface.label}: attached, but PipeWire shows no microphone for it",
                    f"{card}; {target.reason}",
                    "unplug and replug it; if it stays silent run `streamstack mic-revive`",
                )
            )
        else:
            fmt = f" {audio.format}" if audio.format else ""
            channels = f", {audio.capture_channels} ch capture" if audio.capture_channels else ""
            detail = f"{target.rate_reason}; quantum {target.quantum or 'default'}"
            if target.rate != target.native_rate:
                rows.append(
                    row(
                        "warn",
                        cid,
                        f"{iface.label}: {target.native_rate} Hz native, voice chain at {target.rate} Hz{fmt}",
                        detail,
                        "calls are resampled; set `preferred_rate` in the interface profile only after the stages "
                        "support it (docs/audio-chain.md)",
                    )
                )
            else:
                rows.append(row("ok", cid, f"{iface.label}: {target.rate} Hz{fmt}{channels}, no resampling", detail))
    if target.microphone is not None:
        label = {INTERFACE: "the interface's", OVERRIDE: "the configured", GENERIC: "the default"}.get(
            target.state, "the"
        )
        rows.append(
            row(
                "ok",
                "hw-audio-mic",
                f"call microphone: {redact(target.microphone)}",
                f"{label} source; {target.reason}",
            )
        )
    if audio.monitor is None:
        rows.append(
            row(
                "warn",
                "hw-audio-monitor",
                "no monitor output",
                "PipeWire has no sink",
                "connect headphones or an interface, or check `wpctl status`",
            )
        )
    elif audio.monitor == DUMMY_SINK:
        rows.append(
            row(
                "warn",
                "hw-audio-monitor",
                "monitor output: PipeWire's Dummy Output, nothing is audible",
                "no real sink exists",
                "connect headphones or an interface"
                + (" (its card is at profile off)" if target.state in (ABSENT, CARD_OFF) else ""),
            )
        )
    else:
        rows.append(row("ok", "hw-audio-monitor", f"monitor output: {redact(audio.monitor)}"))
    return rows


# -- video inputs ----------------------------------------------------------------------------------------------------


def _capture_row(plan: Plan) -> dict[str, str] | None:
    card = plan.capture
    if card is None:
        return None
    if card.modes:
        best = card.best_mode
        offered = ", ".join(f"{m.width}x{m.height}@{m.fps:g} {m.fourcc}" for m in card.modes)
        if best is None:
            return row(
                "fail",
                "hw-capture",
                f"{card.label}: no usable mode",
                f"offers {offered}",
                "check the HDMI source's output format (1080p or lower, 8 bit) or pin STREAMSTACK_CAMLINK_MODE",
            )
        return row(
            "ok",
            "hw-capture",
            f"{card.label}: {best.width}x{best.height}@{best.fps:g} {best.fourcc} on {card.node_path}",
            f"offers {offered}",
        )
    if card.modes_state == "denied":
        return row(
            "warn",
            "hw-capture",
            f"{card.label}: its modes cannot be read (denied)",
            "the card is on the bus but this user cannot open its video node",
            VIDEO_FIX,
        )
    return row(
        "warn",
        "hw-capture",
        f"{card.label}: no video node, so no modes",
        "the card is listed on USB but no capture node was found",
        "unplug and replug it in a USB 3 port, and check `v4l2-ctl --list-devices`",
    )


def _loopback_rows(plan: Plan, snapshot: Snapshot) -> list[dict[str, str]]:
    status = snapshot.status("loopback")
    unreadable = status is not None and not status.ok
    rows = []
    for key, label, path in (
        ("camera", plan.loopback.camera_label, plan.loopback.camera_path),
        ("program", plan.loopback.program_label, plan.loopback.program_path),
    ):
        cid = f"hw-loopback-{key}"
        if path:
            rows.append(row("ok", cid, f"{label}: {path}", "v4l2loopback device found by its label"))
        elif unreadable and status is not None:
            rows.append(
                row(
                    "warn",
                    cid,
                    f"{label}: cannot tell ({status.state})",
                    redact_text(status.error) or "",
                    SOURCE_FIX.get("v4l2", DEFAULT_SOURCE_FIX),
                )
            )
        else:
            rows.append(
                row(
                    "fail",
                    cid,
                    f"{label}: no v4l2loopback device carries this label",
                    "the label must match exactly (card_label in the module options)",
                    "load v4l2loopback with the StreamStack options (packaging/modprobe.d) or reboot after installing",
                )
            )
    return rows


# -- sources and notices ---------------------------------------------------------------------------------------------


def _source_rows(snapshot: Snapshot) -> list[dict[str, str]]:
    rows = []
    for source in snapshot.sources:
        if source.ok or source.state == "absent":
            continue
        cid = f"hw-source-{source.source}"
        error = redact_text(source.error) or ""
        if source.state == "denied":
            rows.append(
                row(
                    "warn",
                    cid,
                    f"{source.source}: denied",
                    error,
                    SOURCE_FIX.get(source.source, "give this user read access to what the probe reads"),
                )
            )
        else:
            rows.append(
                row(
                    "warn",
                    cid,
                    f"{source.source}: {source.state}",
                    error,
                    SOURCE_FIX.get(source.source, DEFAULT_SOURCE_FIX)
                    if source.state == "error"
                    else DEFAULT_SOURCE_FIX,
                )
            )
    return rows


def _notice_rows(plan: Plan) -> list[dict[str, str]]:
    return [
        row(
            "warn" if notice.level == "warn" else "ok",
            f"hw-notice-{notice.id}",
            redact_text(notice.text) or notice.id,
            "",
            "run `streamstack hardware` for the whole Plan",
        )
        for notice in plan.notices
        if notice.id not in COVERED_NOTICES and not notice.id.startswith(COVERED_PREFIXES)
    ]


def default_engine() -> str:
    """The denoise engine the voice chain would be built with here (decides the graph rate)."""
    return graph.pick_engine("deepfilternet", graph.available_engines())


def checks(plan: Plan, snapshot: Snapshot, catalogue: Catalogue, engine: str | None = None) -> list[dict[str, str]]:
    """Every hardware row for this Plan and snapshot."""
    target = voice_target(plan, engine or default_engine())
    rig = catalogue.rig(plan.rig.id)
    rig_label = rig.label if rig else plan.rig.id
    rows = [_rig_row(plan)]
    rows += [_device_row(d, rig_label) for d in plan.devices if d.expected]
    rows += [_role_row(role, plan.gpu(role), plan, catalogue) for role in GPU_ROLES]
    rows.append(_encoder_row(plan))
    rows += _audio_rows(plan, snapshot, target)
    if capture := _capture_row(plan):
        rows.append(capture)
    rows += _loopback_rows(plan, snapshot)
    rows += _source_rows(snapshot)
    rows += _notice_rows(plan)
    return rows


def failed_row() -> dict[str, str]:
    return row(
        "warn",
        "hw-probe",
        "the hardware probe could not run",
        "the profiles are invalid or a probe tool crashed; see the log",
        "run `streamstack hardware` in a terminal to see the error",
    )


def report(found: Found | None, engine: str | None = None) -> dict[str, Any]:
    """The `hardware` section of `streamstack doctor --json`: the rig header and the rows."""
    if found is None:
        return {"rig": None, "checks": [failed_row()]}
    return {"rig": rig_header(found.plan), "checks": checks(found.plan, found.snapshot, found.catalogue, engine)}
