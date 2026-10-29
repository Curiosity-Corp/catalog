#!/usr/bin/env python3
"""Glass-to-glass latency per path, from a time stamp painted into the picture.

    python3 tools/latency_rig.py run  --device /dev/video50 --label "Latency scratch"   # loopback + browser leg
    python3 tools/latency_rig.py feed --device /dev/video50 --seconds 60                # a stand-in camera only
    python3 tools/latency_rig.py measure --label "StreamStack Camera"                   # decode a camera's stamps
    python3 tools/latency_rig.py glass --label "Cam Link 4K"                            # screen, camera, browser

`run` feeds a scratch loopback node and measures it in one go. To include the engine, run `feed` into the engine's
camera input node, start the engine on it and `measure --label "StreamStack Camera"`. `glass` shows the stamp on this
screen; film it with a real camera and the number includes display, camera, capture and browser. Every command prints
count, min, p50, p95, max and mean in milliseconds (`--json` for the samples). `feed` refuses the nodes the Console
feeds (video42 and video43 by default) and any node that is not a free v4l2loopback node: use a scratch node.
See docs/camera-proof.md for how to read the numbers and what each path covers.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from streamstack import latencyrig
from streamstack.latencyrig import RigError


def _print(result: dict, as_json: bool) -> int:
    summary = result["summary"]
    if as_json:
        print(json.dumps(result, indent=2))
    elif not result.get("matched"):
        seen = ", ".join(d["label"] for d in result.get("devices", [])) or "no cameras"
        print(f"{result['label']!r} is not listed by the browser (it lists: {seen})", file=sys.stderr)
    elif not summary["count"]:
        print(
            f"{result['matched']}: {result['frames']} frames, none carried a readable stamp "
            f"({result['undecoded']} undecoded): is the stamp in view, and is the picture at least 1280 wide?",
            file=sys.stderr,
        )
    else:
        print(
            f"{result['matched']} {result['size'][0]}x{result['size'][1]}: {summary['count']} frames, latency ms "
            f"min {summary['min']} p50 {summary['p50']} p95 {summary['p95']} max {summary['max']} "
            f"mean {summary['mean']} ({result['undecoded']} undecoded)"
        )
    return 0 if summary["count"] else 1


def _engine_leg(out_device: str, size: str, fps: int, seconds: float) -> dict[str, str]:
    """`streamstack engine bench` into a scratch node: its `key: value` lines as a dict (empty when it fails)."""
    argv = [sys.executable, "-m", "streamstack", "engine", "bench", "--size", size, "--fps", str(fps),
            "--seconds", str(seconds), "--output", out_device]  # fmt: skip
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src")}
    done = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=seconds + 120, check=False)
    if done.returncode != 0:
        print(f"engine bench failed: {done.stderr.strip() or done.stdout.strip()}", file=sys.stderr)
        return {}
    return dict(line.split(": ", 1) for line in done.stdout.splitlines() if ": " in line)


def _paths(args: argparse.Namespace) -> int:
    """Every path measurable without a person: loopback + browser, engine, and their sum, as a table."""
    loop = {}
    for size in ("1280x720",):
        width, _, height = size.partition("x")
        stop = threading.Event()
        errors: list[RigError] = []

        def pump(width=int(width), height=int(height), stop=stop, errors=errors) -> None:
            try:
                latencyrig.feed(args.device, args.seconds + 8, size=(width, height), fps=30, stop=stop)
            except RigError as exc:
                errors.append(exc)

        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        stop.wait(2.0)
        try:
            if errors:
                raise errors[0]
            loop[size] = latencyrig.measure(args.label, args.seconds, channel=args.channel)["summary"]
        finally:
            stop.set()
            thread.join(timeout=10)
    rows = [("loopback + browser (Chromium)", "1280x720@30", loop["1280x720"])]
    engine = {}
    for size in ("1280x720", "1920x1080"):
        leg = _engine_leg(args.out_device, size, 30, args.seconds)
        if leg:
            engine[size] = leg
    print("| Path | Size | p50 ms | p95 ms | max ms |\n|---|---|---|---|---|")
    for name, size, s in rows:
        print(f"| {name} | {size} | {s.get('p50', 'n/a')} | {s.get('p95', 'n/a')} | {s.get('max', 'n/a')} |")
    for size, leg in engine.items():
        print(f"| engine (GPU graph + loopback + reader) | {size}@30 | {leg.get('latency_p50_ms')} | "
              f"{leg.get('latency_p95_ms')} | {leg.get('latency_max_ms')} |")  # fmt: skip
    if "1280x720" in engine and loop["1280x720"].get("p50") is not None:
        total = float(engine["1280x720"]["latency_p50_ms"]) + float(loop["1280x720"]["p50"])
        print(f"| engine then browser, estimated sum | 1280x720@30 | {total:.1f} | | |")
    return 0 if loop["1280x720"].get("count") else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["run", "feed", "measure", "glass", "paths"])
    ap.add_argument("--device", help="scratch loopback node to feed (run, feed, paths)")
    ap.add_argument("--out-device", help="second scratch loopback node the engine bench writes (paths)")
    ap.add_argument("--label", help="camera label to measure through (run, measure, glass)")
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--size", default="1280x720", help="picture size WxH of the stamped feed (default 1280x720)")
    ap.add_argument("--fps", type=int, default=30, help="frame rate of the stamped feed (default 30)")
    ap.add_argument("--channel", default=None, help="browser channel for the measuring browser, for example msedge")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        width, _, height = args.size.partition("x")
        size = (int(width), int(height))
    except ValueError:
        ap.error("--size must look like 1280x720")
    try:
        if args.command == "feed":
            if not args.device:
                ap.error("feed needs --device")
            frames = latencyrig.feed(args.device, args.seconds, size=size, fps=args.fps)
            print(f"wrote {frames} stamped frames to {args.device}")
            return 0
        if args.command == "paths":
            if not (args.device and args.out_device and args.label):
                ap.error("paths needs --device, --out-device and --label (two scratch nodes)")
            return _paths(args)
        if not args.label:
            ap.error(f"{args.command} needs --label")
        if args.command == "measure":
            return _print(latencyrig.measure(args.label, args.seconds, channel=args.channel), args.json)
        if args.command == "glass":
            return _print(latencyrig.glass(args.label, args.seconds, channel=args.channel), args.json)
        if not args.device:
            ap.error("run needs --device")
        stop = threading.Event()
        errors: list[RigError] = []

        def pump() -> None:
            try:
                latencyrig.feed(args.device, args.seconds + 8, size=size, fps=args.fps, stop=stop)
            except RigError as exc:
                errors.append(exc)

        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        stop.wait(2.0)  # exclusive_caps: the node is listed only once the feed is writing
        try:
            if errors:
                raise errors[0]
            result = latencyrig.measure(args.label, args.seconds, channel=args.channel)
        finally:
            stop.set()
            thread.join(timeout=10)
        if errors:
            raise errors[0]
        return _print(result, args.json)
    except RigError as exc:
        print(f"latency_rig: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
