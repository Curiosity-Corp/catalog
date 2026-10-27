"""Why a Focusrite Scarlett shows no controls: the `hw-scarlett` doctor row (#130).

The in-kernel `scarlett2` mixer driver (sound/usb/mixer_scarlett2.c, Linux master) creates **no controls at all**
when the interface is in Mass Storage Device ("Easy Start", MSD) mode, and none when its firmware is older than the
first configuration set the driver knows. It does create "Firmware Version" and "Minimum Firmware Version" first, and
"MSD Mode Switch" when MSD is on, then stops. It logs, through `usb_audio_*`:

    Focusrite <series> Mixer Driver enabled (pid=0x<pid>); report any issues to ...
    Focusrite <series> Mixer Driver disabled by modprobe options (snd_usb_audio vid=0x... pid=0x... device_setup=N)
    Focusrite <series> firmware version <have> is too old; need <need>
    Error initialising <series> Mixer Driver: <errno>
    scarlett2_... missing device entry for <vid>:<pid>

`diagnose` tells four states apart (the first of the issue's list, in order), each with its fix:

* `absent`      not on USB: re-seat it (rear port, short cable); the kernel log's USB enumeration errors are quoted
* `no-card`     on USB, but no ALSA sound card: the kernel's USB error lines are quoted
* `no-controls` a card without scarlett2 controls: MSD mode, firmware too old, driver switched off, or unknown
* `healthy`     controls present: firmware and MSD state shown

StreamStack only **reads** (ALSA controls, the kernel log) and prints the command for the owner. It never flashes
or reboots the interface and links nothing GPL: the update tool is geoffreybennett/scarlett2 (GPL-3.0), named in
the fix text only, and the firmware blobs are not bundled.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from subprocess import CompletedProcess
from typing import Any

from streamstack import system

from .alsa import Mixer, MixerControl, MixerError
from .model import AlsaCard, Snapshot, UsbNode
from .redact import redact_text
from .schema import Catalogue

GROUP = "hardware"
CHECK_ID = "hw-scarlett"
FOCUSRITE_VENDOR = "1235"

ABSENT = "absent"
NO_CARD = "no-card"
NO_CONTROLS = "no-controls"
HEALTHY = "healthy"

CAUSE_MSD = "msd"
CAUSE_OLD_FIRMWARE = "old-firmware"
CAUSE_DRIVER_OFF = "driver-off"
CAUSE_UNKNOWN = "unknown"
CAUSE_UNREADABLE = "unreadable"  # amixer could not list the card's controls, so nothing can be said

# How the kernel log was (not) read: "ok" | "absent" (no tool) | "denied" | "error" | "timeout".
LOG_OK = "ok"
EMPTY_MIXER = "exposes no mixer controls"  # alsa.Mixer.controls() on a card with an empty listing

MSD_FIX = (
    "turn MSD (Easy Start) mode off: hold the 48V button for five seconds while you power the interface on (unplug "
    "and replug the USB cable while holding it), or run `amixer -c {card} cset name='MSD Mode Switch' off` and "
    "power-cycle the interface; then run `streamstack doctor` again"
)
FIRMWARE_FIX = (
    "update the firmware with the scarlett2 tool (https://github.com/geoffreybennett/scarlett2, GPL-3.0, "
    "needs kernel 6.8 or newer): `scarlett2 update` after `scarlett2 --list` shows the interface (firmware files "
    "come from https://github.com/geoffreybennett/scarlett2-firmware; StreamStack does not flash or bundle "
    "them); then power-cycle the interface"
)
DRIVER_OFF_FIX = (
    "the scarlett2 mixer driver is switched off by a snd_usb_audio option: remove `device_setup=1` for this "
    "interface from /etc/modprobe.d/*.conf and /etc/modules-load.d, reload snd_usb_audio or reboot"
)
UNKNOWN_FIX = (
    "no scarlett2 controls and no kernel line says why: update the kernel (6.8 or newer), check `journalctl -k -b | "
    "grep -i focusrite`, and replug the interface"
)
ABSENT_FIX = (
    "an owner action at the machine: re-seat the interface in a direct rear USB port (no hub, no front panel) with a "
    "short known-good cable, then run `streamstack doctor` again"
)
NO_CARD_FIX = (
    "it is on the bus but the kernel could not start its sound card: unplug it, plug it into a direct rear USB 3 "
    "port with a short known-good cable, and read `journalctl -k -b | grep -iE 'usb|focusrite'` for the error"
)
LOG_DENIED_FIX = (
    "add your user to the systemd-journal group (sudo usermod -aG systemd-journal $USER) and log in again so the "
    "doctor can read the kernel log"
)

_DRIVER_ENABLED = re.compile(r"Focusrite (?P<series>.+?) Mixer Driver enabled \(pid=0x(?P<pid>[0-9a-fA-F]+)\)")
_DRIVER_DISABLED = re.compile(r"Focusrite (?P<series>.+?) Mixer Driver disabled by modprobe options")
_FIRMWARE_OLD = re.compile(r"Focusrite (?P<series>.+?) firmware version (?P<have>\d+) is too old; need (?P<need>\d+)")
_INIT_ERROR = re.compile(r"Error initialising (?P<series>.+?) Mixer Driver: (?P<errno>-?\d+)")
_MISSING_ENTRY = re.compile(r"missing device entry for (?P<vid>[0-9a-fA-F]{4}):(?P<pid>[0-9a-fA-F]{4})")
_USB_ERROR = re.compile(
    r"device descriptor read/\d+, error -?\d+"
    r"|device not accepting address"
    r"|unable to enumerate USB device"
    r"|Cannot enable\. Maybe the USB cable is bad"
    r"|can't set config #\d+, error -?\d+"
    r"|usb_audio.*(?:cannot get|cannot set|failed|error -?\d+)"
    r"|usb \S+: .*(?:error -?\d+|disabled by hub)",
    re.I,
)
_DENIED = re.compile(r"permission denied|not permitted|not seeing messages|insufficient permissions", re.I)
_TIMESTAMP = re.compile(r"^(?:[A-Z][a-z]{2} +\d+ [\d:]+ \S+ kernel: )?(?:\[\s*[\d.]+\]\s*)?(?:\S+ kernel: )?")


@dataclass(frozen=True)
class KernelLog:
    """What the kernel said about the interface, parsed from `journalctl -k` / `dmesg` text. Every field is the
    **last** such line, because a replug logs the whole sequence again."""

    driver_enabled: str | None = None  # the series name from "Mixer Driver enabled (pid=...)"
    driver_enabled_pid: str | None = None
    driver_disabled: str | None = None  # the line "Mixer Driver disabled by modprobe options"
    firmware_old: tuple[str, int, int] | None = None  # series, firmware found, firmware needed
    firmware_old_line: str = ""
    init_error: str | None = None
    missing_entry: str | None = None  # "1235:xxxx" with no entry in this kernel's driver
    usb_errors: tuple[str, ...] = ()  # the last few enumeration or audio start errors, verbatim

    @property
    def mentions_focusrite(self) -> bool:
        return any((self.driver_enabled, self.driver_disabled, self.firmware_old, self.init_error, self.missing_entry))


def _strip(line: str) -> str:
    return _TIMESTAMP.sub("", line.strip()).strip()


def parse_kernel_log(text: str, usb_error_lines: int = 3) -> KernelLog:
    """The scarlett2 lines and USB error lines of a kernel log. Unknown lines are ignored; the text may be a whole
    journal. A later line replaces an earlier one of the same kind, and a "Mixer Driver enabled" line after a
    "disabled" one (or the reverse) is the newer state."""
    enabled = disabled = missing = init_error = None
    enabled_pid = None
    old: tuple[str, int, int] | None = None
    old_line = ""
    usb: list[str] = []
    for raw in text.splitlines():
        line = _strip(raw)
        if not line:
            continue
        if match := _DRIVER_ENABLED.search(line):
            enabled, enabled_pid = match["series"], match["pid"].lower()
            disabled = None
            old, old_line, init_error = None, "", None  # a fresh probe: the earlier verdicts are history
        elif _DRIVER_DISABLED.search(line):
            disabled = line
            enabled = None
        elif match := _FIRMWARE_OLD.search(line):
            old = (match["series"], int(match["have"]), int(match["need"]))
            old_line = line
        elif match := _INIT_ERROR.search(line):
            init_error = f"{match['series']}: error {match['errno']}"
        elif match := _MISSING_ENTRY.search(line):
            missing = f"{match['vid'].lower()}:{match['pid'].lower()}"
        elif _USB_ERROR.search(line):
            usb.append(line)
    return KernelLog(
        driver_enabled=enabled,
        driver_enabled_pid=enabled_pid,
        driver_disabled=disabled,
        firmware_old=old,
        firmware_old_line=old_line,
        init_error=init_error,
        missing_entry=missing,
        usb_errors=tuple(usb[-usb_error_lines:]),
    )


@dataclass(frozen=True)
class Evidence:
    """What `diagnose` reads, gathered by `collect` (or built by a test from a fixture)."""

    controls: dict[str, MixerControl] | None = None  # `amixer -c N contents`; None: not read
    controls_error: str = ""
    kernel_log: str | None = None  # None: not read
    kernel_log_state: str = LOG_OK
    kernel_log_error: str = ""


@dataclass(frozen=True)
class Diagnosis:
    state: str  # ABSENT | NO_CARD | NO_CONTROLS | HEALTHY
    label: str  # "Focusrite Scarlett 2i2 (4th Gen)"
    usb_id: str | None = None
    card: int | None = None
    cause: str = ""  # NO_CONTROLS only: CAUSE_*
    firmware: int | None = None
    minimum_firmware: int | None = None
    msd: bool | None = None  # None: the driver does not offer the switch (it is hidden while MSD is off)
    kernel_lines: tuple[str, ...] = ()  # the kernel lines that justify the diagnosis, verbatim
    log_note: str = ""  # why the kernel log could not back the diagnosis, when it could not
    log_state: str = LOG_OK
    controls_error: str = ""
    expected: bool = False  # the rig names a Scarlett although none is on the bus

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "label": self.label,
            "usb_id": self.usb_id,
            "card": self.card,
            "cause": self.cause,
            "firmware": self.firmware,
            "minimum_firmware": self.minimum_firmware,
            "msd": self.msd,
            "kernel_lines": list(self.kernel_lines),
        }


def scarlett_usb_ids(catalogue: Catalogue) -> dict[str, str]:
    """USB id -> label of every Scarlett the scarlett2 driver covers (the catalogue's `driver = "scarlett"`)."""
    return {
        usb: device.label
        for device in catalogue.devices
        if device.audio is not None and device.audio.driver == "scarlett"
        for usb in device.usb
    }


def _is_scarlett_card(card: AlsaCard, ids: dict[str, str]) -> bool:
    if card.usb_id is not None:
        return card.usb_id in ids
    return bool(re.search(r"scarlett|focusrite", card.name, re.I))


def find(snapshot: Snapshot, catalogue: Catalogue) -> tuple[UsbNode | None, AlsaCard | None]:
    """The Scarlett on the bus and its sound card, when each exists."""
    ids = scarlett_usb_ids(catalogue)
    node = next((u for u in snapshot.usb if u.usb_id in ids), None)
    card = next((c for c in snapshot.alsa if _is_scarlett_card(c, ids)), None)
    return node, card


def expected(catalogue: Catalogue, rig_id: str) -> bool:
    """The rig names a Scarlett among the devices it expects."""
    rig = catalogue.rig(rig_id)
    ids = {d.id for d in catalogue.devices if d.audio is not None and d.audio.driver == "scarlett"}
    return rig is not None and any(e.id in ids for e in rig.expects)


def _int(control: MixerControl | None) -> int | None:
    return control.value if control is not None and isinstance(control.value, int) else None


def _cause_of(log: KernelLog, msd: bool | None, firmware: int | None, minimum: int | None) -> str:
    """Order matters: the driver tests the firmware first, then MSD (mixer_scarlett2.c scarlett2_read_configs)."""
    if (firmware is not None and minimum is not None and firmware < minimum) or log.firmware_old is not None:
        return CAUSE_OLD_FIRMWARE
    if msd:
        return CAUSE_MSD
    if log.driver_disabled is not None:
        return CAUSE_DRIVER_OFF
    return CAUSE_UNKNOWN


def diagnose(
    snapshot: Snapshot, catalogue: Catalogue, evidence: Evidence, rig_id: str = "", label: str | None = None
) -> Diagnosis | None:
    """The state of the Scarlett on this machine. None when there is no Scarlett on the bus and the rig does not
    expect one (nothing to say)."""
    node, card = find(snapshot, catalogue)
    names = scarlett_usb_ids(catalogue)
    log = parse_kernel_log(evidence.kernel_log or "")
    wanted = expected(catalogue, rig_id)
    if node is None and card is None:
        if not wanted:
            return None
        return Diagnosis(ABSENT, label or "Focusrite Scarlett", kernel_lines=log.usb_errors, expected=True)
    usb_id = node.usb_id if node else card.usb_id if card else None
    name = label or names.get(usb_id or "", "") or "Focusrite Scarlett"
    log_note = _log_note(evidence)
    if card is None:
        return Diagnosis(
            NO_CARD, name, usb_id, kernel_lines=log.usb_errors, log_note=log_note, log_state=evidence.kernel_log_state
        )
    controls = evidence.controls
    if controls is None:
        return Diagnosis(
            NO_CONTROLS,
            name,
            usb_id,
            card.number,
            cause=CAUSE_UNREADABLE,
            controls_error=evidence.controls_error or "amixer gave no answer",
        )
    firmware = _int(controls.get("Firmware Version"))
    minimum = _int(controls.get("Minimum Firmware Version"))
    msd_ctl = controls.get("MSD Mode Switch")
    msd = bool(msd_ctl.value) if msd_ctl is not None and msd_ctl.value is not None else None
    # Real controls beyond the three the driver creates before it gives up mean it went all the way.
    extra = [n for n in controls if n not in ("Firmware Version", "Minimum Firmware Version", "MSD Mode Switch")]
    went_all_the_way = firmware is not None and bool(extra) and not log.firmware_old and not msd
    if went_all_the_way and (minimum is None or (firmware or 0) >= minimum):
        return Diagnosis(HEALTHY, name, usb_id, card.number, firmware=firmware, minimum_firmware=minimum, msd=bool(msd))
    lines: list[str] = []
    if log.firmware_old_line:
        lines.append(log.firmware_old_line)
    if log.driver_disabled:
        lines.append(log.driver_disabled)
    if log.init_error:
        lines.append(f"Error initialising Mixer Driver: {log.init_error}")
    if log.missing_entry:
        lines.append(f"missing device entry for {log.missing_entry}")
    cause = _cause_of(log, msd, firmware, minimum)
    if cause == CAUSE_MSD and not lines:
        lines.append("MSD Mode Switch is on (no other controls are created while MSD mode is on)")
    if cause == CAUSE_OLD_FIRMWARE and not log.firmware_old_line and firmware is not None and minimum is not None:
        lines.append(f"Firmware Version {firmware} is below Minimum Firmware Version {minimum}")
    return Diagnosis(
        NO_CONTROLS,
        name,
        usb_id,
        card.number,
        cause=cause,
        firmware=firmware,
        minimum_firmware=minimum,
        msd=msd,
        kernel_lines=tuple(lines),
        log_note=log_note,
        log_state=evidence.kernel_log_state,
    )


def _log_note(evidence: Evidence) -> str:
    if evidence.kernel_log is not None:
        return ""
    reason = {
        "absent": "neither journalctl nor dmesg is installed",
        "denied": "this user may not read the kernel log",
        "timeout": "the kernel log did not answer in time",
    }.get(evidence.kernel_log_state, evidence.kernel_log_error or "the kernel log could not be read")
    return f"kernel log not read: {reason}"


# -- doctor rows -----------------------------------------------------------------------------------------------------


def _row(state: str, title: str, detail: str = "", hint: str = "") -> dict[str, str]:
    return {"id": CHECK_ID, "group": GROUP, "state": state, "title": title, "detail": detail, "hint": hint}


def _quote(lines: tuple[str, ...]) -> str:
    return "; ".join(f"kernel: {redact_text(line) or line}" for line in lines)


def rows(diagnosis: Diagnosis | None, evidence: Evidence | None = None) -> list[dict[str, str]]:
    """The doctor rows for a diagnosis. Every row that is not ok names the fix."""
    if diagnosis is None:
        return []
    label = diagnosis.label
    note = f"; {diagnosis.log_note}" if diagnosis.log_note else ""
    if diagnosis.state == ABSENT:
        if not diagnosis.kernel_lines:
            return []  # the audio interface row already says "not enumerating" and names the same fix
        return [
            _row(
                "warn",
                f"{label}: not on USB, and the kernel reports USB errors",
                _quote(diagnosis.kernel_lines),
                ABSENT_FIX,
            )
        ]
    if diagnosis.state == NO_CARD:
        detail = _quote(diagnosis.kernel_lines) if diagnosis.kernel_lines else "the kernel log shows no USB error"
        return [
            _row(
                "fail",
                f"{label}: on USB ({diagnosis.usb_id}) but no sound card",
                detail + note,
                NO_CARD_FIX,
            )
        ]
    card = diagnosis.card if diagnosis.card is not None else 0
    if diagnosis.state == NO_CONTROLS:
        title, fix = {
            CAUSE_MSD: (f"{label}: no controls, MSD (Easy Start) mode is on", MSD_FIX.format(card=card)),
            CAUSE_OLD_FIRMWARE: (f"{label}: no controls, its firmware is too old", FIRMWARE_FIX),
            CAUSE_DRIVER_OFF: (f"{label}: no controls, the scarlett2 driver is switched off", DRIVER_OFF_FIX),
            CAUSE_UNREADABLE: (
                f"{label}: sound card present, its controls could not be read",
                "install alsa-utils and make sure this user may read the sound card (audio group), then run "
                "`streamstack doctor` again",
            ),
        }.get(diagnosis.cause, (f"{label}: sound card present but no scarlett2 controls", UNKNOWN_FIX))
        versions = ""
        if diagnosis.firmware is not None:
            versions = f"firmware {diagnosis.firmware}" + (
                f", the driver needs {diagnosis.minimum_firmware}" if diagnosis.minimum_firmware is not None else ""
            )
        parts = [p for p in (versions, _quote(diagnosis.kernel_lines)) if p]
        if diagnosis.cause == CAUSE_UNREADABLE:
            parts = [diagnosis.controls_error]
        detail = "; ".join(parts) + note
        if diagnosis.cause == CAUSE_UNKNOWN and diagnosis.log_state == "denied":
            fix = f"{fix}; {LOG_DENIED_FIX}"
        return [_row("fail", title, detail, fix)]
    firmware = f"firmware {diagnosis.firmware}" if diagnosis.firmware is not None else "firmware unknown"
    if diagnosis.minimum_firmware is not None:
        firmware += f" (driver minimum {diagnosis.minimum_firmware})"
    msd = {True: "MSD mode on", False: "MSD mode off", None: "MSD mode off"}[diagnosis.msd]
    return [_row("ok", f"{label}: scarlett2 controls present", f"{firmware}; {msd}")]


# -- collecting the evidence -----------------------------------------------------------------------------------------

Runner = Callable[[list[str], float], "CompletedProcess[str] | None"]
KERNEL_LOG_LINES = 6000
_LOG_COMMANDS = (
    ["journalctl", "-k", "-b", "--no-pager", "-q", "-n", str(KERNEL_LOG_LINES), "-o", "short-monotonic"],
    ["dmesg", "--notime"],
)


def read_kernel_log(
    run: Runner = system.run, which: Callable[[str], str | None] = shutil.which, timeout: float = 6.0
) -> tuple[str | None, str, str]:
    """(text, state, error): the kernel log from journalctl, then dmesg. Read-only. `state` is LOG_OK, "absent"
    (neither tool), "denied", "timeout" or "error"."""
    state, error = "absent", "neither journalctl nor dmesg is installed"
    for argv in _LOG_COMMANDS:
        if not which(argv[0]):
            continue
        result = run(argv, timeout)
        if result is None:
            state, error = "timeout", f"{argv[0]} did not finish within {timeout:g} s"
            continue
        text = result.stdout or ""
        message = (result.stderr or "").strip()
        denied = _DENIED.search(message)
        if result.returncode == 0 and not denied:
            return text, LOG_OK, ""  # readable, possibly empty
        state = "denied" if denied else "error"
        error = message.splitlines()[-1][:200] if message else f"{argv[0]} exited {result.returncode}"
    return None, state, error


def collect(
    snapshot: Snapshot,
    catalogue: Catalogue,
    rig_id: str = "",
    run: Runner = system.run,
    which: Callable[[str], str | None] = shutil.which,
) -> Evidence | None:
    """Read what the diagnosis needs: the card's mixer controls, then the kernel log only when the controls do not
    already show a healthy interface (the log is the slow read). None when there is nothing to diagnose."""
    node, card = find(snapshot, catalogue)
    if node is None and card is None and not expected(catalogue, rig_id):
        return None
    controls: dict[str, MixerControl] | None = None
    controls_error = ""
    if card is not None:
        mixer = Mixer(card.number, run=run, binary="amixer")
        if which("amixer") is None:
            controls_error = "amixer is not installed (alsa-utils)"
        else:
            try:
                controls = mixer.controls()
            except MixerError as exc:
                controls_error = str(exc)
                if EMPTY_MIXER in controls_error:
                    controls, controls_error = {}, ""  # the card answered: it simply has no controls
    evidence = Evidence(controls=controls, controls_error=controls_error)
    probe = diagnose(snapshot, catalogue, evidence, rig_id)
    if probe is not None and probe.state == HEALTHY:
        return evidence
    text, state, error = read_kernel_log(run, which)
    return Evidence(controls, controls_error, text, state, error)


def checks(snapshot: Snapshot, catalogue: Catalogue, rig_id: str, evidence: Evidence | None) -> list[dict[str, str]]:
    """The doctor rows for this machine; empty when there is no Scarlett to speak of."""
    if evidence is None:
        return []
    return rows(diagnose(snapshot, catalogue, evidence, rig_id), evidence)
