"""Serial numbers out of device names, for anything that is shown or pasted (Console state, doctor, issue notes).

PipeWire node names carry the USB serial of the device. The probe and the Plan keep the real names (the audio chain
needs them to connect); everything a person reads shows them with the serial replaced by `<serial>`.
"""

from __future__ import annotations

import re

# "..._Elgato_Wave_XLR_BS1234567-00.analog-stereo": the token before "-00" is the serial number.
_SERIAL = re.compile(r"_(?!<serial>)([A-Za-z0-9]{4,})-(\d{2})(?=[.\s]|$)")


def redact(name: str | None) -> str | None:
    """A PipeWire name with the device's serial number replaced. Names without one are returned unchanged."""
    if name is None:
        return None
    return _SERIAL.sub(r"_<serial>-\2", name) if name.startswith(("alsa_", "bluez_")) else name


def redact_text(text: str | None) -> str | None:
    if text is None:
        return None
    return " ".join(redact(word) or word for word in text.split(" "))
