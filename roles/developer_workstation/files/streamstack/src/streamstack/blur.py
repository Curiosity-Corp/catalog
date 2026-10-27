"""Background blur: the settings contract the Console's blur controls speak, and the cost gate they are judged by.

The blur itself is a segmentation mask composited inside the native video engine (an OpenVINO model on the Arc,
see docs/native-video-engine.md). That mask is not in the engine yet, so the real rig reports the blur as
unavailable (`video.blur.native`) and answers the action with a clear 409. This module holds what does not depend
on how the mask is made: the ranges of the two settings (strength and how often the mask is recomputed), the
validators, the per-frame cost gate decided in docs/hardware-ai-evaluation.md, and the description of the mask
backend (`Plugin`: is a model present, on which device) the Camera page renders.
"""

from __future__ import annotations

from dataclasses import dataclass

MIN_STRENGTH, MAX_STRENGTH, DEFAULT_STRENGTH = 1, 20, 8  # blur passes; 0 would remove the background
MIN_RATE, MAX_RATE, DEFAULT_RATE = 1, 6, 2  # recompute the mask every Nth frame: 2 is 15 per second at 30 fps
RATE_PRESETS = {"light": 3, "balanced": 2, "smooth": 1}

INSTALL_HINT = "background blur arrives with the OpenVINO mask in the native video engine"

DEVICE_LABELS = {"cpu": "CPU", "openvino": "OpenVINO (Arc GPU)"}

# The per-frame cost gate (docs/hardware-ai-evaluation.md): decided before measuring.
GATE_P95_MS = 8.0
GATE_CPU_PERCENT = 1.0
WINDOW_SAMPLES = 60


class BlurError(ValueError):
    """A blur setting is out of range (the message says what is allowed)."""


def _whole(value: object, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BlurError(f"{name} must be a whole number from {low} to {high}.")
    if isinstance(value, float) and (value != value or not value.is_integer()):  # NaN or fractional
        raise BlurError(f"{name} must be a whole number from {low} to {high}.")
    number = int(value)
    if not low <= number <= high:
        raise BlurError(f"{name} must be from {low} to {high}.")
    return number


def validate_strength(value: object) -> int:
    return _whole(value, "Blur strength", MIN_STRENGTH, MAX_STRENGTH)


def validate_rate(value: object) -> int:
    return _whole(value, "Mask rate", MIN_RATE, MAX_RATE)


@dataclass(frozen=True)
class Plugin:
    """What the Camera page shows about the mask backend: whether a model is present and where it runs."""

    installed: bool
    version: str | None = None
    models_ok: bool = False
    loaded: bool | None = None
    devices: tuple[str, ...] = ("cpu",)

    @property
    def usable(self) -> bool:
        return self.installed and self.models_ok

    @property
    def device_labels(self) -> list[str]:
        return [DEVICE_LABELS.get(d, d) for d in self.devices]

    def public(self) -> dict[str, object]:
        return {
            "installed": self.installed,
            "version": self.version,
            "models_ok": self.models_ok,
            "loaded": self.loaded,
            "devices": list(self.devices),
            "device_labels": self.device_labels,
            "device": "cpu",
            "hint": "" if self.usable else INSTALL_HINT,
        }


def health_check(plugin: Plugin | None = None) -> dict[str, str]:
    """The doctor / Console check "Background blur" (same shape as cli.check)."""
    found = plugin or Plugin(installed=False)
    base = {"id": "background-blur", "group": "video"}
    if not found.usable:
        return {
            **base,
            "state": "warn",
            "title": "Background blur is off: the mask is not in the native video engine yet",
            "detail": "no segmentation model is wired into the engine",
            "hint": INSTALL_HINT,
        }
    version = f" {found.version}" if found.version else ""
    return {
        **base,
        "state": "ok",
        "title": f"Background blur is available{version}",
        "detail": ", ".join(found.device_labels),
        "hint": "",
    }
