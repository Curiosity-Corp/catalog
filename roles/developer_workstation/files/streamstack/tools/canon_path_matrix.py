#!/usr/bin/env python3
"""The Canon EOS 50D picture-path matrix (issue #129): measure one path, then print the table row.

A manual tool, not shipped. The three paths:

    A  USB live view through libgphoto2 (the bridge writes /dev/video42)
    B  HDMI through the Cam Link 4K, as the camera sends it
    C  HDMI with the camera's overlays removed (Magic Lantern; owner's decision, see docs/hardware/dm313red.md)

Per path, with the picture path set up and the desk idle (no call):

    python3 tools/canon_path_matrix.py capture --path A --device /dev/video42 --iso 800 --overlay no   # dry run
    python3 tools/canon_path_matrix.py capture --path A --device /dev/video42 --iso 800 --overlay no --execute \
        --cpu-of streamstack --latency-ms 140 --out paths.jsonl
    python3 tools/canon_path_matrix.py table paths.jsonl

`capture` reads the device for --seconds with `v4l2-ctl --stream-mmap` (delivered fps) and `ffmpeg` (active picture
size with cropdetect, and noise as the mean absolute difference between consecutive frames over the whole picture),
and samples `ps` for the CPU of a named process. Point the camera at a static, evenly lit scene at the ISO you pass,
because the noise number is only comparable between paths on the same scene. Overlay present or not, and the
glass-to-glass latency (measured with the #89 bench, a phone clock or a flashing LED), are human observations passed on
the command line.

Safety: read-only on the device (a reader never disturbs the bridge or the engine), refuses to run when a call app
holds a capture stream or has the device open, never changes a camera setting or the default source.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

CALL_APPS = re.compile(r"msedge|microsoft[- ]edge|teams|zoom|chrome|chromium|webex|slack|discord|skype|firefox", re.I)
PATHS = {"A": "USB live view (libgphoto2)", "B": "HDMI via Cam Link", "C": "HDMI, overlays removed"}

Run = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


class MatrixError(Exception):
    """The measurement cannot run or its input is unusable."""


def run(argv: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return subprocess.CompletedProcess(argv, 127, "", f"{argv[0]}: not installed")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"{argv[0]}: timed out")


# -- parsers (pure) -----------------------------------------------------------------------------------------------

FPS_LINE = re.compile(r"^\s*(?:<+|>+)\s*([0-9]+(?:\.[0-9]+)?)\s*fps\b", re.M)
CROP_LINE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")
YAVG_LINE = re.compile(r"lavfi\.signalstats\.YAVG=([0-9]+(?:\.[0-9]+)?)")
SIZE_LINE = re.compile(r"Width/Height\s*:\s*(\d+)/(\d+)")


def parse_fps(stderr: str) -> float | None:
    """The delivered rate: `v4l2-ctl --stream-mmap` prints one `<<<<< 29.97 fps` line per second; use the median."""
    values = sorted(float(v) for v in FPS_LINE.findall(stderr))
    if not values:
        return None
    return values[len(values) // 2]


def parse_active_size(stderr: str) -> tuple[int, int] | None:
    """The most common `crop=W:H:X:Y` cropdetect settled on (black bars are not picture)."""
    crops = [(int(w), int(h)) for w, h, _x, _y in CROP_LINE.findall(stderr)]
    if not crops:
        return None
    return Counter(crops).most_common(1)[0][0]


def parse_noise(stderr: str) -> float | None:
    """Mean of the per-frame mean absolute temporal difference (luma, 0-255); lower is quieter."""
    values = [float(v) for v in YAVG_LINE.findall(stderr)]
    if not values:
        return None
    return sum(values) / len(values)


def parse_frame_size(v4l2_all: str) -> tuple[int, int] | None:
    """The negotiated size from `v4l2-ctl --all`."""
    match = SIZE_LINE.search(v4l2_all)
    return (int(match.group(1)), int(match.group(2))) if match else None


def call_apps_in(text: str) -> list[str]:
    found: list[str] = []
    for line in text.splitlines():
        if re.search(r"application\.(name|process\.binary)|media\.name|^\s*\S+\s*$", line):
            match = CALL_APPS.search(line)
            if match and match.group(0).lower() not in found:
                found.append(match.group(0).lower())
    return found


def parse_ps_cpu(ps_output: str) -> float:
    """Sum of the %CPU column of `ps -C NAME -o %cpu=`."""
    total = 0.0
    for token in ps_output.split():
        try:
            total += float(token)
        except ValueError:
            continue
    return total


# -- the measurement ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    path: str
    device: str
    iso: int | None
    fps: float | None
    width: int | None
    height: int | None
    active_width: int | None
    active_height: int | None
    overlay: str
    noise: float | None
    latency_ms: float | None
    cpu_percent: float | None
    note: str = ""

    def line(self) -> str:
        return json.dumps(asdict(self))


def guard(device: str, runner: Run) -> list[str]:
    """Why the capture must not run now (call app on a capture stream, or holding the camera); empty when idle."""
    reasons: list[str] = []
    outputs = runner(["pactl", "list", "source-outputs"])
    if outputs.returncode == 0:
        apps = call_apps_in(outputs.stdout)
        if apps:
            reasons.append(f"a call app holds a capture stream: {', '.join(apps)}")
    held = runner(["fuser", device])
    for token in held.stdout.split():
        if token.isdigit():
            comm = runner(["ps", "-p", token, "-o", "comm="]).stdout.strip()
            if CALL_APPS.search(comm):
                reasons.append(f"{device} is open by {comm} (pid {token})")
    return reasons


def measure(
    path: str,
    device: str,
    seconds: int,
    iso: int | None,
    overlay: str,
    latency_ms: float | None,
    cpu_of: str | None,
    note: str,
    runner: Run = run,
    sleep: Callable[[float], None] = time.sleep,
) -> Row:
    frames = max(10, seconds * 5)
    stream = runner(
        ["v4l2-ctl", "-d", device, "--stream-mmap", f"--stream-count={max(30, seconds * 30)}", "--stream-to=/dev/null"]
    )
    if stream.returncode not in (0,):
        raise MatrixError(f"v4l2-ctl could not stream {device}: {stream.stderr.strip() or stream.returncode}")
    fps = parse_fps(stream.stderr)
    size = parse_frame_size(runner(["v4l2-ctl", "-d", device, "--all"]).stdout)
    probe = runner(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-f", "v4l2", "-i", device, "-frames:v", str(frames),
            "-vf", "cropdetect=limit=24:round=2,tblend=all_mode=difference,signalstats,"
            "metadata=print:key=lavfi.signalstats.YAVG",
            "-f", "null", "-",
        ]
    )  # fmt: skip
    if probe.returncode != 0:
        raise MatrixError(
            f"ffmpeg could not read {device}: {probe.stderr.strip().splitlines()[-1:] or probe.returncode}"
        )
    active = parse_active_size(probe.stderr)
    cpu = None
    if cpu_of:
        sample = runner(["ps", "-C", cpu_of, "-o", "%cpu="])
        cpu = parse_ps_cpu(sample.stdout) if sample.returncode == 0 else None
    return Row(
        path=path, device=device, iso=iso, fps=fps,
        width=size[0] if size else None, height=size[1] if size else None,
        active_width=active[0] if active else None, active_height=active[1] if active else None,
        overlay=overlay, noise=parse_noise(probe.stderr), latency_ms=latency_ms, cpu_percent=cpu, note=note,
    )  # fmt: skip


def cell(value: object, fmt: str = "{}") -> str:
    return "not measured" if value is None else fmt.format(value)


def table(rows: list[Row]) -> str:
    head = (
        "| Path | ISO | Delivered fps | Frame | Active picture | Overlay | Noise (luma diff) | Latency ms | CPU % |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
    )
    body = []
    for r in sorted(rows, key=lambda r: (r.path, r.iso or 0)):
        frame = f"{r.width}x{r.height}" if r.width and r.height else "not measured"
        active = f"{r.active_width}x{r.active_height}" if r.active_width and r.active_height else "not measured"
        body.append(
            f"| {r.path} {PATHS.get(r.path, '')} | {cell(r.iso)} | {cell(r.fps, '{:.1f}')} | {frame} | {active} | "
            f"{r.overlay} | {cell(r.noise, '{:.2f}')} | {cell(r.latency_ms, '{:.0f}')} | "
            f"{cell(r.cpu_percent, '{:.0f}')} |"
        )
    return head + "\n".join(body) + "\n"


def load_rows(path: Path) -> list[Row]:
    rows: list[Row] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MatrixError(f"cannot read {path}: {exc.strerror or exc}") from exc
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(Row(**json.loads(line)))
        except (ValueError, TypeError) as exc:
            raise MatrixError(f"{path}:{n}: not a result row ({exc})") from exc
    return rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    cap = sub.add_parser("capture", help="measure one picture path")
    cap.add_argument("--path", choices=sorted(PATHS), required=True)
    cap.add_argument("--device", default="/dev/video42")
    cap.add_argument("--seconds", type=int, default=10)
    cap.add_argument("--iso", type=int, help="the camera's ISO for this run (set on the camera, not by this tool)")
    cap.add_argument("--overlay", choices=["yes", "no", "unknown"], default="unknown", help="overlays in the picture?")
    cap.add_argument("--latency-ms", type=float, help="glass-to-glass latency measured separately (#89)")
    cap.add_argument("--cpu-of", metavar="NAME", help="process whose CPU to record (the bridge or the engine)")
    cap.add_argument("--note", default="", help="free text, e.g. the Magic Lantern build for path C")
    cap.add_argument("--out", help="append the result row to this JSON-lines file")
    cap.add_argument("--execute", action="store_true", help="really read the device")
    tab = sub.add_parser("table", help="print the markdown table for docs/hardware/dm313red.md")
    tab.add_argument("results")
    return parser


def main(argv: list[str] | None = None, runner: Run = run) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "table":
            sys.stdout.write(table(load_rows(Path(args.results))))
            return 0
        if args.seconds < 1:
            raise MatrixError("--seconds must be at least 1")
        reasons = guard(args.device, runner)
        if reasons:
            raise MatrixError("the desk is not idle: " + "; ".join(reasons))
        if not args.execute:
            print(f"dry run: would read {args.device} for {args.seconds} s as path {args.path} ({PATHS[args.path]})")
            print("nothing was touched; add --execute with the desk idle")
            return 0
        row = measure(
            args.path,
            args.device,
            args.seconds,
            args.iso,
            args.overlay,
            args.latency_ms,
            args.cpu_of,
            args.note,
            runner,
        )
    except MatrixError as exc:
        print(f"canon_path_matrix: {exc}", file=sys.stderr)
        return 2
    print(row.line())
    if args.out:
        try:
            with open(args.out, "a", encoding="utf-8") as handle:
                handle.write(row.line() + "\n")
        except OSError as exc:
            print(f"canon_path_matrix: cannot write {args.out}: {exc.strerror or exc}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
