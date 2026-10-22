"""ALSA mixer controls through `amixer`: parse `amixer -c N contents` / `cget` output and set a control by numid.

Controls are addressed by the numid read from the same listing, so no control name ever has to be quoted into a
command line. Nothing here writes unless `Mixer.put` is called.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from subprocess import CompletedProcess
from typing import Protocol

from .. import system

Runner = Callable[[list[str], float], CompletedProcess[str] | None]

NUMID = re.compile(
    r"^numid=(?P<numid>\d+),iface=(?P<iface>[A-Z_]+)(?:,device=\d+)?(?:,subdevice=\d+)?"
    r",name='(?P<name>.*)'(?:,index=\d+)?\s*$"
)
TYPE = re.compile(r"^\s*; type=(?P<type>[A-Z0-9_]+),access=(?P<access>\S+),values=(?P<count>\d+)(?P<rest>.*)$")
ITEM = re.compile(r"^\s*; Item #(?P<index>\d+) '(?P<text>.*)'\s*$")
VALUES = re.compile(r"^\s*: values=(?P<values>.*)$")
LIMIT = re.compile(r"(min|max|step)=(-?\d+)")
DB_MINMAX = re.compile(r"dBminmax-min=(-?[\d.]+)dB,max=(-?[\d.]+)dB")
DB_SCALE = re.compile(r"dBscale-min=(-?[\d.]+)dB,step=([\d.]+)dB")


class MixerError(Exception):
    """A mixer helper that is missing, refused the request or answered something unreadable."""


@dataclass
class MixerControl:
    numid: int
    iface: str
    name: str
    type: str = ""
    access: str = ""
    count: int = 1
    minimum: int | None = None
    maximum: int | None = None
    step: int | None = None
    items: list[str] = field(default_factory=list)
    values: list[int | str] = field(default_factory=list)
    db_range: tuple[float, float] | None = None

    @property
    def writable(self) -> bool:
        """The kernel flips this at run time (autogain makes the gain read-only), so it is read per listing."""
        return len(self.access) > 1 and self.access[1] == "w"

    @property
    def value(self) -> int | str | None:
        return self.values[0] if self.values else None

    def item_text(self) -> str | None:
        """The selected item's name for an enumerated control."""
        index = self.value
        if isinstance(index, int) and 0 <= index < len(self.items):
            return self.items[index]
        return None


def _value(text: str) -> int | str:
    text = text.strip()
    if text == "on":
        return 1
    if text == "off":
        return 0
    try:
        return int(text)
    except ValueError:
        return text


def parse_contents(text: str) -> dict[str, MixerControl]:
    """Every control in an `amixer contents` listing, by name. Lines it does not understand are skipped."""
    controls: dict[str, MixerControl] = {}
    current: MixerControl | None = None
    for line in text.splitlines():
        head = NUMID.match(line)
        if head:
            current = MixerControl(int(head["numid"]), head["iface"], head["name"])
            controls[current.name] = current
            continue
        if current is None:
            continue
        kind = TYPE.match(line)
        if kind:
            current.type, current.access, current.count = kind["type"], kind["access"], int(kind["count"])
            for key, number in LIMIT.findall(kind["rest"]):
                setattr(current, {"min": "minimum", "max": "maximum", "step": "step"}[key], int(number))
            continue
        item = ITEM.match(line)
        if item:
            current.items.append(item["text"])
            continue
        values = VALUES.match(line)
        if values:
            current.values = [_value(v) for v in values["values"].split(",") if v.strip()]
            continue
        stripped = line.strip()
        if stripped.startswith("|"):
            minmax, scale = DB_MINMAX.search(stripped), DB_SCALE.search(stripped)
            if minmax:
                current.db_range = (float(minmax[1]), float(minmax[2]))
            elif scale and current.minimum is not None and current.maximum is not None:
                low, step = float(scale[1]), float(scale[2])
                current.db_range = (low, low + step * (current.maximum - current.minimum))
    return controls


class MixerLike(Protocol):
    """What a driver needs from a mixer: the real `Mixer` and the demo's `SimMixer` both fit."""

    def available(self) -> bool: ...

    def controls(self) -> dict[str, MixerControl]: ...

    def get(self, numid: int) -> MixerControl: ...

    def put(self, numid: int, values: list[int]) -> None: ...


class Mixer:
    """One sound card's mixer. `run` is injectable so tests (and the demo) never start a process."""

    def __init__(self, card: int, run: Runner | None = None, binary: str = "amixer") -> None:
        self.card = card
        self.binary = binary
        self._injected = run is not None
        self._run = run or (lambda argv, timeout: system.run(argv, timeout=timeout))

    def available(self) -> bool:
        return self._injected or system.have(self.binary)

    def _call(self, args: list[str], timeout: float = 5.0) -> str:
        argv = [self.binary, "-c", str(self.card), *args]
        result = self._run(argv, timeout)
        if result is None:
            raise MixerError(f"{self.binary} did not answer (missing or timed out).")
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            raise MixerError(detail[-1] if detail else f"{self.binary} failed ({result.returncode}).")
        return result.stdout

    def controls(self) -> dict[str, MixerControl]:
        """All controls of the card (read-only: `amixer contents`)."""
        controls = parse_contents(self._call(["contents"]))
        if not controls:
            raise MixerError(f"Card {self.card} exposes no mixer controls.")
        return controls

    def get(self, numid: int) -> MixerControl:
        """One control's current state (read-only: `amixer cget`)."""
        found = parse_contents(self._call(["cget", f"numid={numid}"]))
        if len(found) != 1:
            raise MixerError(f"Control {numid} is not on card {self.card}.")
        return next(iter(found.values()))

    def put(self, numid: int, values: list[int]) -> None:
        """Set a control (the only write: `amixer cset numid=N v[,v...]`). Values are plain integers."""
        if not values or not all(isinstance(v, int) and not isinstance(v, bool) for v in values):
            raise MixerError("A mixer write needs integer values.")
        self._call(["-q", "cset", f"numid={numid}", ",".join(str(v) for v in values)])


HW_RATE = re.compile(r"^rate: (\d+)", re.M)
HW_PERIOD = re.compile(r"^period_size: (\d+)", re.M)
STREAM_FREQ = re.compile(r"Momentary freq = (\d+) Hz")


def capture_status(card: int, proc_root: Path = Path("/proc")) -> dict[str, int] | None:
    """The running capture stream's sample rate and period size, from /proc/asound (read-only).

    `hw_params` only has content while a program captures; `stream0` reports the clock the device runs at
    whenever its capture endpoint is active. None when neither says anything.
    """
    base = proc_root / "asound" / f"card{card}"
    found: dict[str, int] = {}
    try:
        params = (base / "pcm0c" / "sub0" / "hw_params").read_text()
    except OSError:
        params = ""
    rate, period = HW_RATE.search(params), HW_PERIOD.search(params)
    if rate:
        found["rate"] = int(rate[1])
    if period:
        found["period"] = int(period[1])
    if "rate" not in found:
        try:
            freq = STREAM_FREQ.search((base / "stream0").read_text())
        except OSError:
            freq = None
        if freq:
            found["rate"] = int(freq[1])
    return found or None
