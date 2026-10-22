"""Focusrite Scarlett interfaces through the in-kernel `scarlett2` ALSA controls.

Every control name below is the exact name the kernel registers (sound/usb/mixer_scarlett2.c, Linux master;
line numbers are from the file as checked on 2026-10-22). The names are built there by `scarlett2_add_line_in_ctls`
(L6821-L7010) as "Line In %d %s Capture %s" and its siblings, so they are the same on every Scarlett that has the
feature. The driver does not guess a generation from the USB id: it offers exactly the controls the card lists, so
the 2i2 Gen 3 (no gain control, analogue gain knobs) and the 2i2 Gen 4 (software gain, Autogain, Safe, Air with
Presence and Drive) both work, and so would any other scarlett2 model that shows the same names.

    Line In N Level Capture Enum          Line | Inst                      L6832-L6837 (items: L5036)
    Line In N Air Capture Switch          Gen 3 Air, on/off                 L6851-L6858
    Line In N Air Capture Enum            Gen 4: Off | Presence | Presence + Drive (L5279)
    Line In N Pad Capture Switch          input pad (not on the 2i2)        L6841-L6847
    Line In N-M Phantom Power Capture Switch  48V; one switch for both 2i2 inputs   L6887-L6909
    Phantom Power Persistence Capture Switch  keep 48V across power cycles  L6911-L6916
    Line In N Gain Capture Volume         Gen 4 preamp gain, 0..70, TLV 0..69 dB   L6944-L6955 (max: L190, TLV: L641)
    Line In N Autogain Capture Switch     1 starts Autogain                 L6956-L6961 (put: L3911)
    Line In N Autogain Status Capture Enum  Running | Success | ...         L6962-L6968 (texts: L396)
    Autogain Hot|Mean|Peak Target         -30..0 dB targets                 L6974-L6986 (range: L353, L3997)
    Line In N Safe Capture Switch         Safe mode (clip protection)       L6988-L6997
    Line In N Link Capture Switch         link the two inputs' gain         L6930-L6942
    Input Select Capture Enum             Input 1 | Input 2                 L6922-L6928, L4175-L4200
    Direct Monitor Playback Enum          Off | Mono | Stereo (2i2)         L7313-L7316 (items: L7221)
    Direct Monitor Playback Switch        mono-only models                  L7313-L7316
    Firmware Version (CARD)               integer                           L3524
    Minimum Firmware Version (CARD)       integer                           L3564
    Sync Status                           Unlocked | Locked                 L3585-L3629
    Power Status Card Enum                External | Bus | Fail            L7758-L7796
    MSD Mode Switch                       "Easy Start" mass-storage mode    L7640
    Level Meter                           volatile raw meters (not decoded; the Console meters come from PipeWire)

There is no sample-rate or clock-source control in the scarlett2 driver (no such name in the file): the rate is
whatever the capture stream negotiates, so it is read from /proc/asound, not set here.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from ..console.errors import ActionError
from . import alsa
from .alsa import MixerControl, MixerError, MixerLike
from .profiles import Profile
from .usb import UsbDevice
from .widgets import Widget, action, choice, readout, slider, switch

PHANTOM = re.compile(r"^Line In (\d+)(?:-(\d+))? Phantom Power Capture Switch$")
TARGET = re.compile(r"^Autogain (Hot|Mean|Peak) Target$")
INPUTS = range(1, 9)

AUTOGAIN_TEXT = {
    "Running": "Measuring: speak at your normal level",
    "Success": "Done",
    "SuccessDRover": "Done (dynamic range over the target)",
    "WarnMinGainLimit": "Stopped at minimum gain: the signal is very loud",
    "FailDRunder": "Failed: dynamic range under the target",
    "FailMaxGainLimit": "Stopped at maximum gain: the signal is too quiet",
    "FailClipped": "Failed: the input clipped, lower the source and retry",
    "Cancelled": "Cancelled",
}


class Binding:
    """What one widget maps to: the ALSA control and how to turn a requested value into mixer integers."""

    def __init__(self, name: str, kind: str, *, low: int = 0, high: int = 1, options: int = 0, label: str = "") -> None:
        self.name, self.kind, self.low, self.high, self.options, self.label = name, kind, low, high, options, label
        self.danger_on = False


def _bool(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value in (0, 1):
        return value
    raise ActionError("Expected on or off.", 400)


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ActionError("Expected a number.", 400)
    return float(value)


class Scarlett:
    def __init__(self, profile: Profile, device: UsbDevice, mixer: MixerLike, proc_root: Path = Path("/proc")) -> None:
        self.profile, self.device, self.mixer, self.proc_root = profile, device, mixer, proc_root
        self._bindings: dict[str, Binding] = {}

    # -- reading --------------------------------------------------------------------------------

    def read(self) -> dict[str, Any]:
        if not self.mixer.available():
            raise MixerError("amixer is missing (install alsa-utils).")
        controls = self.mixer.controls()
        widgets, bindings = self._build(controls)
        self._bindings = bindings
        info = self._info(controls)
        return {
            "controls": widgets,
            "info": info,
            "notice": None,
            "capabilities": self._capabilities(widgets),
        }

    @staticmethod
    def _capabilities(widgets: list[Widget]) -> dict[str, bool]:
        ids = {w["id"].split(".")[0] for w in widgets}
        return {
            "input_gain": "gain" in ids,
            "autogain": "autogain" in ids,
            "air": "air" in ids,
            "level": "level" in ids,
            "phantom": "phantom" in ids,
            "safe": "safe" in ids,
            "direct_monitor": "direct_monitor" in ids,
            "pad": "pad" in ids,
            "link": "link" in ids,
            "meters": False,  # the driver exposes "Level Meter"; the Console reads levels from PipeWire instead
            "sample_rate": True,  # read-only, from /proc/asound
            "firmware": True,
        }

    def _build(self, ctls: dict[str, MixerControl]) -> tuple[list[Widget], dict[str, Binding]]:
        widgets: list[Widget] = []
        bindings: dict[str, Binding] = {}
        running = {n: self._autogain_running(ctls, n) for n in INPUTS}
        any_running = any(running.values())

        def bind(wid: str, ctl: MixerControl, kind: str, **kw: Any) -> Binding:
            b = Binding(ctl.name, kind, **kw)
            bindings[wid] = b
            return b

        def locked(ctl: MixerControl, because_autogain: bool = False) -> str:
            if ctl.writable:
                return ""
            return "Autogain is running" if (because_autogain or any_running) else "The device has locked this control"

        for n in INPUTS:
            group = f"Input {n}"
            ctl = ctls.get(f"Line In {n} Level Capture Enum")
            if ctl:
                bind(f"level.{n}", ctl, "choice", options=len(ctl.items))
                widgets.append(
                    choice(
                        f"level.{n}",
                        "Input type",
                        group,
                        ctl.items,
                        int(ctl.value or 0),
                        hint="Line for synths and mixers, Inst for a guitar or bass",
                        readonly=locked(ctl),
                    )
                )
            ctl = ctls.get(f"Line In {n} Gain Capture Volume")
            if ctl and ctl.minimum is not None and ctl.maximum is not None:
                # one control step is one dB (TLV step of 100 in L641), so the raw value is the gain in dB
                high = min(ctl.maximum, int(ctl.db_range[1])) if ctl.db_range else ctl.maximum
                bind(f"gain.{n}", ctl, "slider", low=ctl.minimum, high=high)
                widgets.append(
                    slider(
                        f"gain.{n}",
                        "Preamp gain",
                        group,
                        float(ctl.value or 0),
                        float(ctl.minimum),
                        float(high),
                        1.0,
                        "dB",
                        hint="Hardware gain of the preamp",
                        readonly=locked(ctl, running[n]),
                    )
                )
            ctl = ctls.get(f"Line In {n} Autogain Capture Switch")
            if ctl:
                bind(f"autogain.{n}", ctl, "action")
                status = ctls.get(f"Line In {n} Autogain Status Capture Enum")
                key = status.item_text() if status else None
                if running[n]:
                    text = AUTOGAIN_TEXT["Running"]
                elif key and key not in ("Running", "Invalid", "Root"):
                    text = "Last result: " + AUTOGAIN_TEXT.get(key, key)
                else:
                    text = ""
                w = action(
                    f"autogain.{n}",
                    "Autogain",
                    group,
                    running[n],
                    text,
                    hint="The interface listens for a few seconds and sets the gain for you; speak normally",
                    readonly="" if ctl.writable or running[n] else "The device has locked this control",
                )
                if not running[n] and key in ("Success", "SuccessDRover"):
                    w["state"] = "ok"
                elif not running[n] and key in ("WarnMinGainLimit", "FailDRunder", "FailMaxGainLimit", "FailClipped"):
                    w["state"] = "warn"
                widgets.append(w)
            ctl = ctls.get(f"Line In {n} Air Capture Enum")
            if ctl:
                bind(f"air.{n}", ctl, "choice", options=len(ctl.items))
                widgets.append(
                    choice(
                        f"air.{n}",
                        "Air",
                        group,
                        ctl.items,
                        int(ctl.value or 0),
                        hint="Presence brightens the voice; Presence + Drive adds the Air circuit's drive",
                        readonly=locked(ctl),
                    )
                )
            ctl = ctls.get(f"Line In {n} Air Capture Switch")
            if ctl:
                bind(f"air.{n}", ctl, "switch")
                widgets.append(
                    switch(
                        f"air.{n}",
                        "Air",
                        group,
                        bool(ctl.value),
                        hint="Brighter, open top end modelled on Focusrite's ISA preamp",
                        readonly=locked(ctl),
                    )
                )
            ctl = ctls.get(f"Line In {n} Safe Capture Switch")
            if ctl:
                bind(f"safe.{n}", ctl, "switch")
                widgets.append(
                    switch(
                        f"safe.{n}",
                        "Safe mode",
                        group,
                        bool(ctl.value),
                        hint="Catches sudden peaks before they clip; good for a live call",
                        readonly=locked(ctl),
                    )
                )
            ctl = ctls.get(f"Line In {n} Pad Capture Switch")
            if ctl:
                bind(f"pad.{n}", ctl, "switch")
                widgets.append(
                    switch(
                        f"pad.{n}", "Pad", group, bool(ctl.value), hint="Attenuates a hot input", readonly=locked(ctl)
                    )
                )
            ctl = ctls.get(f"Line In {n} Link Capture Switch")
            if ctl:
                bind(f"link.{n}", ctl, "switch")
                widgets.append(
                    switch(
                        f"link.{n}",
                        "Link inputs",
                        group,
                        bool(ctl.value),
                        hint="Move both inputs' gain together",
                        readonly=locked(ctl),
                    )
                )

        for name, ctl in ctls.items():
            match = PHANTOM.match(name)
            if match:
                low, high = match[1], match[2]
                span = f"inputs {low}-{high}" if high else f"input {low}"
                b = bind(f"phantom.{low}", ctl, "switch")
                b.danger_on, b.label = True, f"48V phantom power ({span})"
                w = switch(
                    f"phantom.{low}",
                    "48V phantom power",
                    "Power",
                    bool(ctl.value),
                    hint=f"Powers condenser microphones on {span}. Never with a ribbon microphone connected",
                    readonly=locked(ctl),
                    danger=True,
                )
                widgets.append(w)
        ctl = ctls.get("Phantom Power Persistence Capture Switch")
        if ctl:
            b = bind("phantom_persist", ctl, "switch")
            b.danger_on, b.label = True, "Keep 48V on after power-up"
            widgets.append(
                switch(
                    "phantom_persist",
                    "Keep 48V on after power-up",
                    "Power",
                    bool(ctl.value),
                    hint="The interface restores 48V by itself the next time it starts",
                    readonly=locked(ctl),
                    danger=True,
                )
            )
        ctl = ctls.get("Input Select Capture Enum")
        if ctl:
            bind("input_select", ctl, "choice", options=len(ctl.items))
            widgets.append(
                choice("input_select", "Input select", "Inputs", ctl.items, int(ctl.value or 0), readonly=locked(ctl))
            )

        dm = ctls.get("Direct Monitor Playback Enum")
        if dm:
            bind("direct_monitor", dm, "choice", options=len(dm.items))
            widgets.append(
                choice(
                    "direct_monitor",
                    "Direct monitor",
                    "Monitoring",
                    dm.items,
                    int(dm.value or 0),
                    hint="Hear the inputs with no computer latency, straight from the hardware",
                    readonly=locked(dm),
                )
            )
        dms = ctls.get("Direct Monitor Playback Switch")
        if dms:
            bind("direct_monitor", dms, "switch")
            widgets.append(
                switch(
                    "direct_monitor",
                    "Direct monitor",
                    "Monitoring",
                    bool(dms.value),
                    hint="Hear the input with no computer latency, straight from the hardware",
                    readonly=locked(dms),
                )
            )

        for name, ctl in ctls.items():
            match = TARGET.match(name)
            if match and ctl.minimum is not None and ctl.maximum is not None:
                key = match[1].lower()
                bind(f"autogain_target.{key}", ctl, "slider", low=ctl.minimum, high=ctl.maximum)
                widgets.append(
                    slider(
                        f"autogain_target.{key}",
                        f"{match[1]} target",
                        "Autogain targets",
                        float(ctl.value or 0),
                        float(ctl.minimum),
                        float(ctl.maximum),
                        1.0,
                        "dB",
                        hint="Level Autogain aims for, in dB below full scale",
                        readonly=locked(ctl),
                    )
                )
        return widgets, bindings

    @staticmethod
    def _autogain_running(ctls: dict[str, MixerControl], n: int) -> bool:
        ctl = ctls.get(f"Line In {n} Autogain Capture Switch")
        return bool(ctl and ctl.value)

    def _info(self, ctls: dict[str, MixerControl]) -> list[dict[str, str]]:
        info: list[dict[str, str]] = []
        fw, minimum = ctls.get("Firmware Version"), ctls.get("Minimum Firmware Version")
        if fw and isinstance(fw.value, int):
            stale = bool(minimum and isinstance(minimum.value, int) and fw.value < minimum.value)
            info.append(
                readout(
                    "firmware",
                    "Firmware",
                    "Device",
                    str(fw.value),
                    "warn" if stale else "",
                    hint="Update it with Focusrite Control 2" if stale else "",
                )
            )
        sync = ctls.get("Sync Status")
        if sync and sync.item_text():
            info.append(
                readout(
                    "sync", "Clock", "Device", sync.item_text() or "", "ok" if sync.item_text() == "Locked" else "warn"
                )
            )
        power = ctls.get("Power Status Card Enum")
        if power and power.item_text():
            text = power.item_text() or ""
            info.append(readout("power", "Power", "Device", text, "warn" if text == "Fail" else ""))
        msd = ctls.get("MSD Mode Switch")
        if msd and msd.value:
            info.append(
                readout(
                    "msd",
                    "Easy Start",
                    "Device",
                    "On",
                    "warn",
                    hint="Limited mass-storage mode: turn it off in Focusrite Control 2",
                )
            )
        if self.device.card is not None:
            status = alsa.capture_status(self.device.card, self.proc_root)
            if status and "rate" in status:
                rate = status["rate"]
                text = f"{rate / 1000:g} kHz"
                if "period" in status and rate:
                    text += f", {status['period']} frames per period ({1000 * status['period'] / rate:.1f} ms)"
                info.append(readout("rate", "Capture", "Device", text))
        return [dict(w) for w in info]

    # -- writing --------------------------------------------------------------------------------

    def apply(self, control_id: str, value: Any, confirm: bool) -> str | None:
        binding = self._bindings.get(control_id)
        if binding is None:
            self.read()
            binding = self._bindings.get(control_id)
        if binding is None:
            raise ActionError(f"This interface has no control {control_id!r}.", 400)
        mixer_value, expected, message = self._translate(binding, value)
        try:
            ctl = self.mixer.controls()[binding.name]
        except (MixerError, KeyError) as exc:
            raise ActionError(f"Could not read {binding.name!r}: {exc}", 409) from exc
        if binding.danger_on and mixer_value and not ctl.value and not confirm:
            raise ActionError(f"{binding.label} needs a deliberate confirmation.", 400)
        if not ctl.writable:
            raise ActionError("The device has locked this control right now (Autogain running?).", 409)
        try:
            self.mixer.put(ctl.numid, [mixer_value])
            after = self.mixer.get(ctl.numid)
        except MixerError as exc:
            raise ActionError(f"The interface refused the change: {exc}", 409) from exc
        if binding.kind != "action" and after.value != expected:
            raise ActionError(f"The interface did not accept that value (it reports {after.value}).", 409)
        return message

    @staticmethod
    def _translate(binding: Binding, value: Any) -> tuple[int, int, str | None]:
        """(integer to write, integer expected back, toast message)."""
        if binding.kind == "switch":
            v = _bool(value)
            return v, v, None
        if binding.kind == "action":
            v = _bool(value)
            return v, v, "Autogain started: speak at your normal level" if v else "Autogain cancelled"
        if binding.kind == "choice":
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < binding.options:
                raise ActionError("Pick one of the listed options.", 400)
            return value, value, None
        number = round(_number(value))
        number = max(binding.low, min(binding.high, number))
        return number, number, None
