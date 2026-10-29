#!/usr/bin/env python3
"""Check that a browser lists "StreamStack Camera" and gets frames from it, the way Teams, Zoom and Meet do.

    python3 tools/camera_probe.py                       # headless Chromium, the real cameras of this machine
    python3 tools/camera_probe.py --channel msedge      # the Edge that Teams runs in
    python3 tools/camera_probe.py --label "Cam Link"    # any camera by (part of) its label
    python3 tools/camera_probe.py --fake                # Chromium's own test camera: what CI runs (proves the probe)
    python3 tools/camera_probe.py --json

Exit status: 0 every check passed, 1 a check failed (the rows say which and why), 2 the probe could not run.
It only reads: the browser opens the camera for reading and closes it; nothing is written to any node. Needs
`pip install playwright` and a browser (`playwright install chromium`, or an installed Edge with --channel msedge).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from streamstack import camprobe


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", default=None, help=f"camera label to look for (default: {camprobe.DEFAULT_LABEL!r})")
    ap.add_argument("--seconds", type=float, default=3.0, help="how long to watch frames (default 3)")
    ap.add_argument("--channel", default=None, help="browser channel, for example msedge or chrome")
    ap.add_argument(
        "--fake", action="store_true", help="use Chromium's fake camera (CI); the label defaults to fake_device_0"
    )
    ap.add_argument("--min-frames", type=int, default=camprobe.MIN_FRAMES)
    ap.add_argument("--min-fps", type=float, default=camprobe.MIN_FPS)
    ap.add_argument("--json", action="store_true", help="print the raw report and the verdict rows as JSON")
    args = ap.parse_args(argv)
    label = args.label or (camprobe.FAKE_LABEL if args.fake else camprobe.DEFAULT_LABEL)
    try:
        report = camprobe.run(label, seconds=args.seconds, fake=args.fake, channel=args.channel)
    except RuntimeError as exc:
        print(f"camera_probe: {exc}", file=sys.stderr)
        return 2
    rows = camprobe.evaluate(report, label, min_frames=args.min_frames, min_fps=args.min_fps)
    ok = camprobe.passed(rows)
    if args.json:
        print(json.dumps({"ok": ok, "label": label, "report": report, "rows": [asdict(r) for r in rows]}, indent=2))
    else:
        for row in rows:
            print(f"{'ok  ' if row.ok else 'FAIL'}  {row.title}")
            if row.detail:
                print(f"      {row.detail}")
            if not row.ok and row.hint:
                print(f"      fix: {row.hint}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
