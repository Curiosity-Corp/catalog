#!/usr/bin/env python3
"""Run the camera probe through every browser a call runs in and print one table (the #87 matrix).

    python3 tools/call_matrix.py                    # real cameras: Chromium, Chrome, Edge, Teams in Edge, Firefox
    python3 tools/call_matrix.py --fake             # each browser's own test camera (what CI runs)
    python3 tools/call_matrix.py --only Edge,Firefox --markdown matrix.md

Exit status: 0 every installed browser passed, 1 one failed, 2 nothing ran (or, with --require-all, one was skipped).
Real runs open the camera for reading only. Desktop Teams and Zoom cannot be driven: see docs/camera-proof.md.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from streamstack import callmatrix


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--label", default=None, help="camera label (default StreamStack Camera, or each browser's test camera)"
    )
    ap.add_argument("--fake", action="store_true")
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--only", default="", help="comma separated browser names")
    ap.add_argument("--require-all", action="store_true", help="a browser that is not installed counts as a failure")
    ap.add_argument("--markdown", default=None, help="also write the table to this file")
    args = ap.parse_args(argv)
    only = tuple(n.strip() for n in args.only.split(",") if n.strip())
    results = callmatrix.run_matrix(args.label, fake=args.fake, seconds=args.seconds, only=only)
    text = callmatrix.table(results)
    print(text)
    if args.markdown:
        Path(args.markdown).write_text(text + "\n", encoding="utf-8")
    return callmatrix.exit_code(results, require_all=args.require_all)


if __name__ == "__main__":
    raise SystemExit(main())
