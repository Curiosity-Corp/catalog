#!/usr/bin/env python3
"""Soak the lighting e2e suites: every file, several times, at several simulated hours.

    scripts/e2e_lighting_soak.py [--hours 03:30,12:00,18:30] [--repeat 3] [--date 2026-06-21] [-- PYTEST_ARGS...]

The demo rig's lighting engine runs on a pinned clock (``STREAMSTACK_DEMO_CLOCK``, New York), so the daylight schedule
sits at a known place on its curve. A race between the schedule and a hand on a light, or a test that only passes at
one time of day, shows up as a failure in one of these passes. The hours cover the night, noon and the evening
transition. A developer tool: the standard library is all it needs, and it is not part of the wheel.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ("tests/e2e/test_page_lighting.py", "tests/e2e/test_page_lighting_automation.py")
HOURS = "03:30,12:00,18:30"
NEW_YORK_OFFSET = "-04:00"  # June: the zone's city is what matters, the offset only has to be fixed
HOUR = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def clock_for(date: str, hour: str) -> str:
    if not HOUR.match(hour):
        raise SystemExit(f"hour must be HH:MM, got {hour!r}")
    return f"{date}T{hour}:00{NEW_YORK_OFFSET}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hours", default=HOURS, help=f"comma separated HH:MM, New York time (default {HOURS})")
    parser.add_argument("--repeat", type=int, default=3, help="passes over the lighting files per hour (default 3)")
    parser.add_argument("--date", default="2026-06-21", help="the simulated day (default 2026-06-21)")
    parser.add_argument("pytest_args", nargs="*", help="extra arguments for pytest, after --")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    hours = [h.strip() for h in args.hours.split(",") if h.strip()]
    if not hours:
        parser.error("--hours needs at least one HH:MM")
    failures: list[str] = []
    started = time.monotonic()
    for hour in hours:
        env = {**os.environ, "E2E_DEMO_CLOCK": clock_for(args.date, hour)}
        for number in range(1, args.repeat + 1):
            label = f"{hour} pass {number}/{args.repeat}"
            print(f"== lighting e2e at {label}", flush=True)
            code = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args.pytest_args, *FILES],
                cwd=ROOT,
                env=env,
                check=False,
            ).returncode
            if code != 0:
                failures.append(f"{label} (exit {code})")
    took = time.monotonic() - started
    if failures:
        print(f"\nlighting soak FAILED after {took:.0f}s:\n  " + "\n  ".join(failures), file=sys.stderr)
        return 1
    print(f"\nlighting soak green: {len(hours)} hours x {args.repeat} passes in {took:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
