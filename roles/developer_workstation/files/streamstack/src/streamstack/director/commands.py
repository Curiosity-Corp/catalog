"""`streamstack auto persona` and `streamstack auto property`: the director's settings from the command line."""

from __future__ import annotations

from datetime import datetime

from streamstack import config
from streamstack.director import daylight, personas, settings


def persona(values: list[str]) -> int:
    """List personas (the current one marked), or choose one by id."""
    library = personas.load_library()
    for problem in library.problems:
        print(f"  skipped: {problem}")
    current = library.get(settings.load().get("persona"))["id"]
    if not values:
        for item in library.listing():
            mark = "*" if item["id"] == current else " "
            print(f" {mark} {item['id']:<18} {item['name']} - {item['tagline']}")
        print("Choose one with: streamstack auto persona <id>")
        return 0
    wanted = values[0]
    if wanted not in library.personas:
        print(f"No persona {wanted!r}. Choose one of: {', '.join(library.personas)}")
        return 1
    try:
        settings.set_persona(wanted)
    except settings.SettingsError as exc:
        print(f"Cannot save the choice: {exc}")
        return 1
    print(f"Persona: {library.personas[wanted]['name']}. Auto mode picks it up within a minute.")
    return 0


def prop(values: list[str]) -> int:
    """The property location and the sun there now; `NAME LATITUDE LONGITUDE` sets it, `clear` removes it."""
    cfg = config.load_settings()
    if values == ["clear"]:
        settings.clear_property()
        print("Property location cleared; using the time zone's city.")
    elif values:
        if len(values) != 3:
            print("Usage: streamstack auto property NAME LATITUDE LONGITUDE | clear")
            return 1
        try:
            settings.set_property(values[0], values[1], values[2])
        except settings.SettingsError as exc:
            print(f"Not saved: {exc}")
            return 1
    now = datetime.now().astimezone()
    location = daylight.resolve_location(settings.load(), cfg, now)
    sun = daylight.sun_state(now, location)
    print(f"{location.name} ({location.latitude:.2f}, {location.longitude:.2f}; from {location.source})")
    print(f"Now: {sun.label}")
    times = sun.times
    for name in ("civil_dawn", "sunrise", "solar_noon", "sunset", "civil_dusk"):
        moment = times.get(name)
        print(f"  {name.replace('_', ' '):<11} {moment.strftime('%H:%M') if moment else 'none today'}")
    return 0
