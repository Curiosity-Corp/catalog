"""CPU time and memory of the StreamStack processes of this login, read from /proc (Linux, standard library only).

A process counts when its command line is StreamStack's own (`streamstack ...`, `streamstack-auto`, `python -m
streamstack ...`). Only the role is reported (console, lights, canon-bridge, auto, tray, or cli for the rest), never a
command line or a path, so nothing a person typed can leave the machine.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROLES = ("console", "lights", "canon-bridge", "auto", "tray", "cli")
ENTRY_ROLES = {"streamstack-auto": "auto", "streamstack-tray": "tray", "streamstack-console": "console"}
SUBCOMMAND_ROLES = {"console": "console", "lights": "lights", "canon-bridge": "canon-bridge", "auto": "auto"}
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


@dataclass(frozen=True)
class Usage:
    count: int
    cpu_seconds: float
    rss_bytes: int


def role_of(argv: list[str]) -> str | None:
    """The StreamStack role of a command line, or None when it is not StreamStack."""
    if not argv:
        return None
    names = [os.path.basename(a) for a in argv[:3]]
    if names[0].startswith("python"):
        if len(argv) >= 3 and argv[1] == "-m" and argv[2].split(".")[0] == "streamstack":
            rest = argv[3:]
        elif len(argv) >= 2 and os.path.basename(argv[1]) in ("streamstack", *ENTRY_ROLES):
            names, rest = names[1:], argv[2:]
            if names[0] in ENTRY_ROLES:
                return ENTRY_ROLES[names[0]]
        else:
            return None
    elif names[0] == "streamstack" or names[0] in ENTRY_ROLES:
        if names[0] in ENTRY_ROLES:
            return ENTRY_ROLES[names[0]]
        rest = argv[1:]
    else:
        return None
    for arg in rest:
        if not arg.startswith("-"):
            return SUBCOMMAND_ROLES.get(arg, "cli")
    return "cli"


def read_process(pid: int, root: Path = Path("/proc"), known_role: str | None = None) -> tuple[str, float, int] | None:
    """(role, cpu seconds, resident bytes) of one StreamStack process, or None. `known_role` skips the command line."""
    base = root / str(pid)
    try:
        if base.stat().st_uid != os.getuid():
            return None
        argv = (base / "cmdline").read_bytes().decode("utf-8", "replace").split("\0")
        role = known_role or role_of([a for a in argv if a])
        if role is None:
            return None
        stat = (base / "stat").read_text().rsplit(")", 1)[1].split()
        cpu = (int(stat[11]) + int(stat[12])) / CLK_TCK  # utime and stime: fields 14 and 15 of the whole line
        rss = int((base / "statm").read_text().split()[1]) * PAGE
    except (OSError, ValueError, IndexError):
        return None  # the process ended while it was being read, or is not ours to read
    return role, cpu, rss


def scan(root: Path = Path("/proc"), known: dict[int, str] | None = None) -> dict[str, Usage]:
    """Usage per role over every StreamStack process (roles with no process are absent).

    `known` names processes by pid whose role is certain (the Console counts itself however it was started).
    """
    totals: dict[str, list[float]] = {}
    try:
        entries = [int(p.name) for p in root.iterdir() if p.name.isdigit()]
    except OSError:
        return {}
    for pid in entries:
        found = read_process(pid, root, (known or {}).get(pid))
        if found is None:
            continue
        role, cpu, rss = found
        row = totals.setdefault(role, [0, 0.0, 0])
        row[0] += 1
        row[1] += cpu
        row[2] += rss
    return {role: Usage(int(c), cpu, int(rss)) for role, (c, cpu, rss) in totals.items()}
