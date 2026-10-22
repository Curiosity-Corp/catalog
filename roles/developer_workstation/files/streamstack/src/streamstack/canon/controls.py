"""The Canon controls the Console offers, each tied to a real libgphoto2 (camlibs/ptp2) configuration key.

Keys below come from libgphoto2's ptp2 `config.c` (`iso`, `shutterspeed`, `aperture`, `whitebalance`,
`colortemperature`, `picturestyle`, `exposurecompensation`, `focusmode`, `liveviewsize`, `autofocusdrive`,
`cancelautofocus`, `manualfocusdrive`, `cameramodel`, `lensname`, `batterylevel`, `autoexposuremode`).
What a given body accepts is never assumed: the choices, the read-only flag and the range always come from the
camera itself (`Reading`), and a control the body does not expose is simply left out of the Console.
"""

from __future__ import annotations

from dataclasses import dataclass

KELVIN_RANGE = (2500, 10000, 100)  # Canon EOS colour temperature as the bodies and Canon software express it
WB_KELVIN_CHOICE = "Color Temperature"  # libgphoto2's EOS whitebalance table entry 9
FOCUS_STEPS = (-3, -2, -1, 1, 2, 3)


@dataclass(frozen=True)
class Control:
    id: str
    key: str  # libgphoto2 configuration name
    label: str
    group: str  # exposure | color | focus | info
    writable: bool = True
    numeric: bool = False  # a free integer (Kelvin) instead of a choice list
    prereq: tuple[str, str] | None = None  # (key, value) that must be set first
    readonly_reason: str = "The camera body does not allow changing this right now"


CONTROLS: tuple[Control, ...] = (
    Control("iso", "iso", "ISO", "exposure", readonly_reason="Set the mode dial to M, Av or Tv to change ISO"),
    Control(
        "shutter",
        "shutterspeed",
        "Shutter",
        "exposure",
        readonly_reason="Turn the mode dial to M or Tv to set the shutter speed",
    ),
    Control(
        "aperture",
        "aperture",
        "Aperture",
        "exposure",
        readonly_reason="Turn the mode dial to M or Av (and use a lens with an aperture ring or electronic control)",
    ),
    Control(
        "exposure_comp",
        "exposurecompensation",
        "Exposure comp",
        "exposure",
        readonly_reason="Exposure compensation applies in P, Tv and Av",
    ),
    Control("whitebalance", "whitebalance", "White balance", "color"),
    Control(
        "kelvin",
        "colortemperature",
        "Kelvin",
        "color",
        numeric=True,
        prereq=("whitebalance", WB_KELVIN_CHOICE),
        readonly_reason="Choose the Color Temperature white balance to set Kelvin",
    ),
    Control("picturestyle", "picturestyle", "Picture Style", "color"),
    Control("focusmode", "focusmode", "Focus mode", "focus"),
    Control("liveviewsize", "liveviewsize", "Live view size", "focus"),
    Control("model", "cameramodel", "Camera", "info", writable=False),
    Control("lens", "lensname", "Lens", "info", writable=False),
    Control("battery", "batterylevel", "Battery", "info", writable=False),
    Control("mode", "autoexposuremode", "Mode dial", "info", writable=False),
)
BY_ID = {c.id: c for c in CONTROLS}
BY_KEY = {c.key: c for c in CONTROLS}

# live view size, best first (libgphoto2 Canon `liveviewsize`: Large / Medium / Small)
LIVEVIEW_BEST_FIRST = ("Large", "Medium", "Small")

# focus drive: libgphoto2 `manualfocusdrive` radio choices "Near 1".."Near 3", "None", "Far 1".."Far 3"
FOCUS_DRIVE_KEY = "manualfocusdrive"
AUTOFOCUS_KEY = "autofocusdrive"
AUTOFOCUS_CANCEL_KEY = "cancelautofocus"

ACTIONS = ("autofocus", "af_cancel", "focus")

HINTS = {
    "liveviewsize": "Live view over USB is at most about 1056x704; for a sharper picture use the HDMI output through a "
    "capture card (Cam Link).",
}


def focus_choice(step: int) -> str:
    """The libgphoto2 `manualfocusdrive` choice for a nudge: negative is toward the camera (Near), positive Far."""
    if step not in FOCUS_STEPS:
        raise ValueError(f"focus step must be one of {list(FOCUS_STEPS)}")
    return f"{'Near' if step < 0 else 'Far'} {abs(step)}"


def snap_kelvin(value: object) -> int:
    """A Kelvin request clamped to the supported range and rounded to the step."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("kelvin must be a number")
    try:
        kelvin = float(value)
    except ValueError as exc:
        raise ValueError("kelvin must be a number") from exc
    if kelvin != kelvin or kelvin in (float("inf"), float("-inf")):
        raise ValueError("kelvin must be a finite number")
    lo, hi, step = KELVIN_RANGE
    return int(min(hi, max(lo, round(kelvin / step) * step)))


def best_liveview(choices: list[str]) -> str | None:
    for wanted in LIVEVIEW_BEST_FIRST:
        for choice in choices:
            if choice.lower() == wanted.lower():
                return choice
    return None
