"""The write journal: who last wrote to a Litra light, shared by every StreamStack process that does.

The lights service tells a person's change (the Console, the light's own buttons) from its own by comparing what a
light shows with what the engine last set. Anything else that writes to a light, the AI Director, an agent driving a
test, a script, the test suite, looks the same from there: the light moved and the engine did not do it. Each
`Litra` write therefore appends one line here, tagged with who wrote. The engine reads the lines of the last few
seconds and does not hold a light for a change that a non-person wrote.

A writer is a short tag. `person` and `console` (the Console page, a person's hand) are the ones that *do* count as a
change by hand; every other tag, `engine`, `director`, `agent`, `test`, `cli`, never does. A process that is not the
Console sets its tag with `STREAMSTACK_LIGHT_WRITER` (an agent, a script and the test suite do); with no tag set,
the write is untagged and counts as a person's, because that is what a write nobody declared most likely is.

Writes by software that does not use `Litra` (a third party tool talking to the hidraw node itself) cannot be
tagged. They look exactly like the light's own buttons, and are treated as a person's.

The file is JSON lines under the state directory. A failed append never blocks a write to a light.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from streamstack import config

log = logging.getLogger(__name__)

ENV = "STREAMSTACK_LIGHT_WRITER"
PERSON_WRITERS = frozenset({"", "person", "console"})
KEEP_LINES = 200  # a trim keeps this many of the newest lines
TRIM_BYTES = 64 * 1024


def path() -> Path:
    return config.state_dir() / "lights-writes.jsonl"


def default_writer() -> str:
    """The tag of this process, from the environment; empty when it declared none."""
    return os.environ.get(ENV, "").strip().lower()[:32]


def is_person(writer: str) -> bool:
    return writer in PERSON_WRITERS


def record(
    light_id: str,
    *,
    writer: str | None = None,
    power: bool | None = None,
    lumen: int | None = None,
    kelvin: int | None = None,
    wall: float | None = None,
) -> None:
    """Append one write. `wall` is epoch seconds (the engine compares it with its own wall clock)."""
    who = default_writer() if writer is None else writer
    entry: dict[str, Any] = {
        "ts": round(time.time() if wall is None else wall, 3),
        "light": light_id,
        "writer": who,
        "pid": os.getpid(),
    }
    for key, value in (("power", power), ("lumen", lumen), ("kelvin", kelvin)):
        if value is not None:
            entry[key] = value
    target = path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
        if target.stat().st_size > TRIM_BYTES:
            _trim(target)
    except OSError as exc:
        log.warning("cannot record a light write in %s: %s", target, exc.strerror or exc)


def _trim(target: Path) -> None:
    try:
        lines = target.read_text(encoding="utf-8").splitlines()[-KEEP_LINES:]
        tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        log.warning("cannot trim %s: %s", target, exc.strerror or exc)


def recent(light_id: str, since: float) -> list[dict[str, Any]]:
    """The writes to `light_id` at or after `since` (epoch seconds), oldest first. Damaged lines are skipped."""
    target = path()
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError as exc:
        log.warning("cannot read %s: %s", target, exc.strerror or exc)
        return []
    found: list[dict[str, Any]] = []
    for line in lines[-KEEP_LINES:]:
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # a half-written or damaged line: the journal only ever helps, never blocks
        if (
            isinstance(entry, dict)
            and entry.get("light") == light_id
            and isinstance(entry.get("ts"), (int, float))
            and entry["ts"] >= since
        ):
            found.append(entry)
    return found
