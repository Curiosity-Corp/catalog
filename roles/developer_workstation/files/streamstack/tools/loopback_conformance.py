#!/usr/bin/env python3
"""v4l2loopback conformance on a scratch node: run it after a kernel update or a module reload (#86).

    sudo v4l2loopback-ctl add -n "Conformance scratch" -x 1 /dev/video50 && sudo chmod 666 /dev/video50
    python3 tools/loopback_conformance.py --device /dev/video50
    sudo v4l2loopback-ctl delete /dev/video50

Checks that the node is unlisted with no writer and listed with one (exclusive_caps), offers only the contract formats,
delivers frames, goes unlisted again when the writer exits, and survives a second writer. Exit 0 all passed, 1 a step
failed, 2 the node was refused. It never touches the program or camera nodes and refuses any node a process holds.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from streamstack.hardware import conformance


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", required=True, help="the scratch loopback node, for example /dev/video50")
    args = ap.parse_args(argv)
    why = conformance.refuse(args.device)
    if why:
        print(f"loopback_conformance: {why}", file=sys.stderr)
        return 2
    steps = conformance.run_conformance(args.device)
    for step in steps:
        print(f"{'ok  ' if step.ok else 'FAIL'}  {step.id}" + (f": {step.detail}" if step.detail else ""))
    return 0 if conformance.passed(steps) else 1


if __name__ == "__main__":
    raise SystemExit(main())
