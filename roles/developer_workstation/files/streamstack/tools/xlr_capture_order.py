#!/usr/bin/env python3
"""Reproduce the Wave XLR capture-after-playback zeros (issue #126). A manual tool, not shipped.

For each cycle: start a playback stream on the XLR sink first (silence), then a capture stream on the XLR source
for 6 seconds (the first buffer arrives about 2 s after open, so a shorter window reads as short), and count captures
that are bit-exact zero. Run it with the voice chain, calls and anything else using the XLR stopped, because it opens
both ends of the card:

    python3 tools/xlr_capture_order.py --cycles 20            # playback first (the suspected failure order)
    python3 tools/xlr_capture_order.py --cycles 20 --capture-first   # the control: capture first

A healthy microphone with 75 dB of gain always shows a noise floor, so any all-zero capture counts. Exit status 1 when
any capture was all zeros. Not for use during a call: it opens the card's playback endpoint.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time

RATE = 48000
BYTES_PER_SECOND = RATE * 4  # float32le, mono
MIN_SECONDS = 6.0  # the XLR delivers its first buffer about 2 s after open, so a 1 s window reads as "short" (#126)


def classify(data: bytes, seconds: float) -> str:
    """Return short (under half the expected audio arrived), zeros (all bit-exact zero) or signal."""
    if len(data) < BYTES_PER_SECOND * seconds * 0.5:
        return "short"
    return "zeros" if data.count(0) == len(data) else "signal"


def summarize(results: list[str]) -> str:
    return (
        f"{len(results)} cycles: {results.count('zeros')} all-zero, {results.count('signal')} with signal, "
        f"{results.count('short')} short"
    )


def find(kind: str) -> str:
    out = subprocess.run(["pactl", "list", "short", kind], capture_output=True, text=True, timeout=10, check=True)
    for line in out.stdout.splitlines():
        name = line.split("\t")[1] if "\t" in line else ""
        if "Wave_XLR" in name and not name.endswith(".monitor"):
            return name
    raise SystemExit(f"no Wave XLR in `pactl list short {kind}`")


def cycle(sink: str, source: str, seconds: float, capture_first: bool) -> str:
    playback = ["pacat", "--raw", "--format=s16le", "--rate=48000", "--channels=2", f"--device={sink}"]
    capture = ["parec", "--raw", "--format=float32le", f"--rate={RATE}", "--channels=1", f"--device={source}"]
    procs: list[subprocess.Popen[bytes]] = []
    try:
        first, second = (capture, playback) if capture_first else (playback, capture)
        a = subprocess.Popen(first, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        procs.append(a)
        time.sleep(0.5)
        b = subprocess.Popen(second, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        procs.append(b)
        for proc, command in ((a, first), (b, second)):
            if command is playback and proc.stdin is not None:
                proc.stdin.write(bytes(BYTES_PER_SECOND))  # a second of silence keeps the stream open
                proc.stdin.flush()
        cap = a if capture_first else b
        time.sleep(seconds)
        cap.terminate()
        data = cap.stdout.read() if cap.stdout is not None else b""
        return classify(data, seconds)
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cycles", type=int, default=20)
    parser.add_argument(
        "--seconds", type=float, default=MIN_SECONDS, help="window per cycle (below 6 the XLR can read as short)"
    )
    parser.add_argument("--capture-first", action="store_true")
    args = parser.parse_args()
    sink, source = find("sinks"), find("sources")
    print(f"sink {sink}\nsource {source}\norder: {'capture' if args.capture_first else 'playback'} first")
    results = []
    for number in range(1, args.cycles + 1):
        results.append(cycle(sink, source, args.seconds, args.capture_first))
        print(f"  {number:2d}: {results[-1]}", flush=True)
        time.sleep(1.0)
    print(summarize(results))
    return 1 if "zeros" in results else 0


if __name__ == "__main__":
    sys.exit(main())
