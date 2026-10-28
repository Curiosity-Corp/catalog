#!/usr/bin/env python3
"""Director calibration on real hardware (issue #32): record voice clips, analyse clips and stills, fit the scales.

A manual tool, not shipped. The voice bars (pitch 70-300 Hz, depth, clarity, sibilance, clean) and the picture
heuristics are linear maps checked only on synthetic signals. This tool collects evidence on the real rig so the
bands can be set from numbers instead of guesses. It prints suggestions; it never edits the shipped bands.

    # 1. record 20 s of a person reading the same passage aloud, once per room and mic setup (guarded, dry run first)
    python3 tools/calibrate_director.py record --label xlr-office-day --seconds 20
    python3 tools/calibrate_director.py record --label xlr-office-day --seconds 20 --execute

    # 2. analyse the clips (and, separately, stills taken with the lights on and the person in frame)
    python3 tools/calibrate_director.py analyze --kind voice --label xlr-office-day calibration/*.wav --out voice.jsonl
    python3 tools/calibrate_director.py analyze --kind picture --label canon-lit stills/*.jpg --out picture.jsonl

    # 3. what the numbers say, against the shipped bands
    python3 tools/calibrate_director.py fit voice.jsonl

`record` reads one capture stream (default: the StreamStack Mic, the voice as the call hears it) with ffmpeg; it never
changes the default source, a mixer control or the voice chain, and refuses while a call app holds a capture stream.
Only numbers go into the results files; the WAVs and stills stay where you put them (do not commit them).
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from streamstack.director import audiofeatures, imagefeatures

CALL_APPS = re.compile(r"msedge|microsoft[- ]edge|teams|zoom|chrome|chromium|webex|slack|discord|skype|firefox", re.I)
DEFAULT_SOURCE = "streamstack_mic"
MIN_CLIPS = 8
MIN_LABELS = 2

# The shipped 0..1 maps (audiofeatures.scores): feature -> (low, high, what the bar means).
SHIPPED_BANDS = {
    "f0_hz": (70.0, 300.0, "pitch"),
    "body_db": (-10.0, 10.0, "depth"),
    "clarity_db": (-20.0, 0.0, "clarity"),
    "sibilance_peak_db": (-35.0, -10.0, "sibilance"),
    "snr_db": (15.0, 55.0, "clean"),
}

Run = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


class CalibrationError(Exception):
    """Cannot record or analyse: a call is live, a file is missing, too little evidence."""


def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=600, check=False)
    except FileNotFoundError:
        return subprocess.CompletedProcess(argv, 127, "", f"{argv[0]}: not installed")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"{argv[0]}: timed out")


def call_apps_in(text: str) -> list[str]:
    found: list[str] = []
    for line in text.splitlines():
        if re.search(r"application\.(name|process\.binary)|media\.name", line):
            match = CALL_APPS.search(line)
            if match and match.group(0).lower() not in found:
                found.append(match.group(0).lower())
    return found


def flatten(data: dict, prefix: str = "") -> dict[str, float]:
    """The numeric leaves of a nested feature dict as {"a.b": 1.5}; booleans, strings and None are dropped."""
    out: dict[str, float] = {}
    for key, value in data.items():
        name = f"{prefix}{key}"
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            out[name] = float(value)
        elif isinstance(value, dict):
            out.update(flatten(value, name + "."))
    return out


def record(args: argparse.Namespace, runner: Run = run, now: Callable[[], float] = time.time) -> Path | None:
    outputs = runner(["pactl", "list", "source-outputs"])
    if outputs.returncode != 0:
        raise CalibrationError(
            f"cannot read the capture streams (pactl: {outputs.stderr.strip() or outputs.returncode})"
        )
    apps = call_apps_in(outputs.stdout)
    if apps:
        raise CalibrationError(f"a call app holds a capture stream ({', '.join(apps)}); not recording")
    target = Path(args.dir) / f"{args.label}-{int(now())}.wav"
    if not args.execute:
        print(f"dry run: would record {args.seconds} s from {args.source} to {target}")
        print("read a fixed passage aloud at your normal speaking distance; add --execute when ready")
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    result = runner(
        ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-f", "pulse", "-i", args.source, "-t", str(args.seconds),
         "-ac", "1", "-ar", str(audiofeatures.RATE), str(target)]
    )  # fmt: skip
    if result.returncode != 0:
        raise CalibrationError(f"ffmpeg could not record {args.source}: {result.stderr.strip() or result.returncode}")
    print(target)
    return target


Analyzer = Callable[[Path], dict]


def analyze(
    kind: str,
    label: str,
    files: list[Path],
    voice: Analyzer = audiofeatures.analyze_wav,
    picture: Callable[[list[Path]], dict] = imagefeatures.analyze_stills,
) -> list[dict]:
    """One result row per voice clip, or one row for the whole set of stills (a burst)."""
    missing = [str(f) for f in files if not f.is_file()]
    if missing:
        raise CalibrationError("not a file: " + ", ".join(missing))
    if not files:
        raise CalibrationError("no files given")
    try:
        if kind == "voice":
            return [{"kind": kind, "label": label, "file": f.name, **flatten(voice(f))} for f in files]
        return [{"kind": kind, "label": label, "file": f"{len(files)} stills", **flatten(picture(files))}]
    except (audiofeatures.AudioAnalysisError, imagefeatures.ImageAnalysisError) as exc:
        raise CalibrationError(str(exc)) from exc


def quantile(sorted_values: list[float], fraction: float) -> float:
    position = fraction * (len(sorted_values) - 1)
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)


def load(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CalibrationError(f"cannot read {path}: {exc.strerror or exc}") from exc
    rows: list[dict] = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise CalibrationError(f"{path}:{n}: not JSON ({exc})") from exc
        if not isinstance(row, dict):
            raise CalibrationError(f"{path}:{n}: not a result row")
        rows.append(row)
    return rows


def fit(rows: list[dict]) -> str:
    """The spread of every measured number, and for voice the shipped band against the 5th..95th percentile.

    Suggestions need at least MIN_CLIPS clips from MIN_LABELS different rooms or setups, because a band fitted to one
    voice in one room would just move the bias. Nothing is edited."""
    if not rows:
        raise CalibrationError("no results to fit")
    columns: dict[str, list[float]] = {}
    for row in rows:
        for key, value in row.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                columns.setdefault(key, []).append(float(value))
    labels = {str(r.get("label", "")) for r in rows}
    out = [f"{len(rows)} rows from {len(labels)} setups ({', '.join(sorted(labels))})", ""]
    out.append("| number | n | min | p10 | median | p90 | max |")
    out.append("| --- | --- | --- | --- | --- | --- | --- |")
    for key in sorted(columns):
        v = sorted(columns[key])
        out.append(
            f"| {key} | {len(v)} | {v[0]:.2f} | {quantile(v, 0.1):.2f} | {statistics.median(v):.2f} | "
            f"{quantile(v, 0.9):.2f} | {v[-1]:.2f} |"
        )
    voice = [r for r in rows if r.get("kind") == "voice"]
    if voice:
        out += ["", "Voice bars: shipped band against what these clips show (5th to 95th percentile).", ""]
        enough = len(voice) >= MIN_CLIPS and len({r.get("label") for r in voice}) >= MIN_LABELS
        out.append("| bar | feature | shipped low..high | measured p5..p95 | verdict |")
        out.append("| --- | --- | --- | --- | --- |")
        for feature, (low, high, bar) in SHIPPED_BANDS.items():
            v = sorted(float(r[feature]) for r in voice if isinstance(r.get(feature), (int, float)))
            if not v:
                out.append(f"| {bar} | {feature} | {low:g}..{high:g} | no data | |")
                continue
            p5, p95 = quantile(v, 0.05), quantile(v, 0.95)
            if not enough:
                verdict = f"need {MIN_CLIPS}+ clips from {MIN_LABELS}+ setups before suggesting"
            elif p5 < low or p95 > high:
                verdict = f"clips fall outside the band; consider {min(low, p5):.0f}..{max(high, p95):.0f}"
            elif (p95 - p5) < 0.4 * (high - low):
                verdict = f"band is wide for this data; consider {p5:.0f}..{p95:.0f} (persona bands unchanged)"
            else:
                verdict = "keep"
            out.append(f"| {bar} | {feature} | {low:g}..{high:g} | {p5:.1f}..{p95:.1f} | {verdict} |")
    return "\n".join(out) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record", help="record a voice clip from a capture source")
    rec.add_argument("--label", required=True, help="room and setup, e.g. xlr-office-day")
    rec.add_argument("--seconds", type=int, default=20)
    rec.add_argument("--source", default=DEFAULT_SOURCE)
    rec.add_argument("--dir", default="calibration")
    rec.add_argument("--execute", action="store_true")
    ana = sub.add_parser("analyze", help="analyse voice clips or stills into result rows")
    ana.add_argument("--kind", choices=["voice", "picture"], required=True)
    ana.add_argument("--label", required=True)
    ana.add_argument("files", nargs="+", type=Path)
    ana.add_argument("--out", help="append the rows to this JSON-lines file")
    fi = sub.add_parser("fit", help="spread of the numbers and the shipped bands against them")
    fi.add_argument("results", type=Path)
    return parser


def main(argv: list[str] | None = None, runner: Run = run) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "record":
            if args.seconds < 5:
                raise CalibrationError("--seconds must be at least 5")
            record(args, runner)
        elif args.command == "analyze":
            rows = analyze(args.kind, args.label, args.files)
            for row in rows:
                print(json.dumps(row))
            if args.out:
                try:
                    with open(args.out, "a", encoding="utf-8") as handle:
                        handle.writelines(json.dumps(r) + "\n" for r in rows)
                except OSError as exc:
                    raise CalibrationError(f"cannot write {args.out}: {exc.strerror or exc}") from exc
        else:
            sys.stdout.write(fit(load(args.results)))
    except CalibrationError as exc:
        print(f"calibrate_director: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
