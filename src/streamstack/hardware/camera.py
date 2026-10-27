"""Cameras and virtual cameras, by what the hardware profiles say (ADR 0003, ADR 0004).

* capture cards and cameras are found by USB id; the ids come from the device profiles, so a new revision of a card
  is one line in `capture.toml`, not a code change;
* the virtual cameras are found by their card label, and when the module has not created them the rig's profile says
  where they go (`video_nr`, `card_label`, `exclusive_caps`) instead of a hard-coded /dev/video42 and /dev/video43.
"""

from __future__ import annotations

from collections.abc import Mapping

from streamstack import config
from streamstack.hardware import schema
from streamstack.hardware.plan import Plan

Ids = tuple[tuple[str, str], ...]
DEVICE_KEYS = {"camera": "STREAMSTACK_CAMERA_DEVICE", "program": "STREAMSTACK_PROGRAM_DEVICE"}


def usb_ids_of(profile_id: str, catalogue: schema.Catalogue | None = None) -> Ids:
    """The (vendor, product) pairs a device profile matches, lower-case; empty for an unknown profile."""
    profile = (catalogue or schema.packaged()).device(profile_id)
    if profile is None:
        return ()
    pairs: list[tuple[str, str]] = []
    for ident in profile.usb:
        vendor, _, product = ident.lower().partition(":")
        pairs.append((vendor, product))
    return tuple(pairs)


def capture_card_ids(catalogue: schema.Catalogue | None = None) -> Ids:
    """Every USB id any capture-card profile matches (Cam Link 4K revisions 0066 and 0067 among them)."""
    cat = catalogue or schema.packaged()
    found: list[tuple[str, str]] = []
    for profile in cat.of_class("capture-card"):
        for pair in usb_ids_of(profile.id, cat):
            if pair not in found:
                found.append(pair)
    return tuple(found)


def canon_ids(catalogue: schema.Catalogue | None = None) -> Ids:
    """The USB ids of the cameras the Canon bridge drives (the EOS 50D today)."""
    return usb_ids_of("canon-eos-50d", catalogue)


def device_for(role: str, cfg: Mapping[str, str], plan: Plan | None) -> str:
    """The /dev/video node of the "camera" (the Canon bridge's output) or "program" (the engine's output) loopback.

    A setting that differs from the packaged default is the person's choice and wins. Otherwise the node the Plan
    found by card label, then the number the rig's profile declares, then the packaged default."""
    key = DEVICE_KEYS[role]
    default = config.DEFAULTS[key]
    given = (cfg.get(key) or "").strip()
    if given and given != default:
        return given
    if plan is not None:
        found = plan.loopback.camera_path if role == "camera" else plan.loopback.program_path
        if found:
            return found
        declared = plan.loopback.declared_path(role)
        if declared:
            return declared
    return given or default
