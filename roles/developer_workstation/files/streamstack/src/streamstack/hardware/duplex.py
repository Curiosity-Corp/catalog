"""The duplex guard: an audio interface whose sound card came up with output profiles only (2026-10-27).

After a reboot and after a physical replug the Wave XLR's PipeWire card sat at `output:analog-stereo` (or
`output:stereo-fallback`) with no input part, so PipeWire had no microphone source for it. The suspected cause is a
second local user's WirePlumber probing the card at the same moment; `systemctl --user restart wireplumber` probes it
again and restores `output:analog-stereo+input:mono-fallback`.

`lost_input(plan, snapshot)` finds that state from the Plan and the snapshot (pure). A card at profile `off` is not
this fault (the Plan and the voice chain already switch it on); a card with an input profile is fine; a card whose
profile is unknown is judged by whether it exposes any source node.
"""

from __future__ import annotations

import json

from .model import PipewireCard, Snapshot
from .plan import Plan

PRO_AUDIO = "pro-audio"  # one profile with every channel in both directions


def profile_has_input(profile: str) -> bool:
    """Whether a card profile name includes capture ("output:a+input:b", "input:mono-fallback", "pro-audio")."""
    return profile == PRO_AUDIO or any(part.startswith("input:") for part in profile.split("+"))


def lost_input(plan: Plan, snapshot: Snapshot) -> PipewireCard | None:
    """The interface's PipeWire card when it is on but has no input, else None."""
    interface = plan.audio_interface()
    if interface is None or interface.card_off or not interface.pipewire_card:
        return None
    card = next((c for c in snapshot.pipewire.cards if c.name == interface.pipewire_card), None)
    if card is None:
        return None
    if card.profile is not None:
        return card if card.profile != "off" and not profile_has_input(card.profile) else None
    return None if snapshot.pipewire.nodes_of(card.name, "source") else card


def cards_have_input(cards_json: str, card: str) -> bool:
    """Whether `pactl -f json list cards` shows `card` with an input profile active (False when unreadable)."""
    try:
        cards = json.loads(cards_json)
    except ValueError:
        return False
    if not isinstance(cards, list):
        return False
    for entry in cards:
        if isinstance(entry, dict) and entry.get("name") == card:
            active = entry.get("active_profile")
            return isinstance(active, str) and profile_has_input(active)
    return False
