#!/usr/bin/env python3
"""Print the planned rename of every developer_workstation public variable.

This is the machine-readable half of docs/variable-naming-plan.md. It only
reads roles/developer_workstation/defaults/main.yml (and vars/ if present) and
changes nothing: no variable is renamed by this script or by the role yet.

The rule, in one line: a name already starting with `developer_workstation_`
stays; a leading `workstation_` is folded into the prefix; everything else gets
the prefix. A collision (two current names that would map to one new name) is
an error, so the plan can be checked before any rename is scheduled.

    python3 scripts/variable_mapping.py            # old -> new, tab separated
    python3 scripts/variable_mapping.py --check    # exit 1 on a collision
    python3 scripts/variable_mapping.py --families # counts per current prefix
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles" / "developer_workstation"
PREFIX = "developer_workstation_"


def new_name(old: str) -> str:
    """Return the collection-scoped name for one current variable name."""
    if old.startswith(PREFIX):
        return old
    if old.startswith("workstation_"):
        return PREFIX + old[len("workstation_"):]
    return PREFIX + old


def public_names(role: Path = ROLE) -> list[str]:
    names: list[str] = []
    for rel in ("defaults/main.yml", "vars/main.yml"):
        path = role / rel
        if path.is_file():
            data = yaml.safe_load(path.read_text()) or {}
            names.extend(key for key in data if not key.startswith("_"))
    return sorted(set(names))


def mapping(names: list[str]) -> dict[str, str]:
    return {name: new_name(name) for name in names}


def collisions(table: dict[str, str]) -> dict[str, list[str]]:
    reverse: dict[str, list[str]] = collections.defaultdict(list)
    for old, new in table.items():
        reverse[new].append(old)
    return {new: olds for new, olds in reverse.items() if len(olds) > 1}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="exit 1 if two names would collide")
    parser.add_argument("--families", action="store_true", help="count variables per current first word")
    parser.add_argument("--role", default=str(ROLE), help="role directory (for tests)")
    args = parser.parse_args(argv)
    table = mapping(public_names(Path(args.role)))
    clash = collisions(table)
    if args.families:
        counts = collections.Counter(name.split("_")[0] for name in table)
        for word, count in counts.most_common():
            print(f"{count}\t{word}")
    elif not args.check:
        for old, new in table.items():
            print(f"{old}\t{new}")
    for new, olds in sorted(clash.items()):
        print(f"collision: {', '.join(olds)} -> {new}", file=sys.stderr)
    return 1 if clash else 0


if __name__ == "__main__":
    sys.exit(main())
