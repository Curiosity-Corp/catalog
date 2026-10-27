"""`streamstack deck status`: the service, the Console it talks to and the deck it holds."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

from streamstack.console import launcher


def console_deck() -> dict | None:
    """The `deck` section of the running Console's state, or None when there is no Console or it does not answer."""
    info = launcher.read_running()
    if info is None:
        return None
    request = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}/api/state", headers={"X-StreamStack-Token": info["token"]}
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            deck = json.load(response).get("deck")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return deck if isinstance(deck, dict) else None


def report(active: bool) -> int:
    """Print the status; 0 when a deck is connected, 1 otherwise (so it works in scripts)."""
    print(f"Deck service {'running' if active else 'not running (start it with `streamstack deck enable`)'}.")
    deck = console_deck()
    if deck is None:
        print(
            "The Console is not running (or predates deck support), so no deck is reported. "
            "`streamstack console` starts it."
        )
        return 1
    device = deck.get("device") or {}
    if device.get("connected"):
        where = "simulated" if device.get("source") == "demo" else "USB"
        print(f"Connected: {device.get('name')} ({where}), {device.get('cols')}x{device.get('rows')} keys.")
        surface = deck.get("surface") or {}
        print(f"Page: {(surface.get('page') or {}).get('name', '?')}")
        return 0
    print(f"No deck connected. {device.get('error', 'Plug one in, with the service running.')}", file=sys.stdout)
    return 1
