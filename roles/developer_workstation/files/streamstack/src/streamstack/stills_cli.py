"""`streamstack stills`: what this host can write, AVIF snapshots, and the verified JPEG XL archive."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from streamstack import stills


def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("stills", help="AVIF snapshots and the lossless JPEG XL archive of camera JPEGs")
    actions = parser.add_subparsers(dest="stills_action", required=True)
    status = actions.add_parser("status", help="what this host can write (AVIF encoder, libjxl)")
    status.add_argument("--json", action="store_true")
    archive = actions.add_parser(
        "archive", help="recompress JPEGs to JPEG XL, prove each rebuilds byte for byte, optionally remove the JPEG"
    )
    archive.add_argument("files", nargs="+", type=Path)
    archive.add_argument(
        "--remove-original", action="store_true", help="delete each JPEG after its archive is verified"
    )
    restore = actions.add_parser("restore", help="rebuild the original JPEG from an archive")
    restore.add_argument("files", nargs="+", type=Path)
    snap = actions.add_parser("snapshot", help="write an image as AVIF")
    snap.add_argument("file", type=Path)
    snap.add_argument("--output", type=Path, help="default: next to the input, with .avif")
    snap.add_argument("--quality", type=int, default=28, help="AV1 CRF 0-63, lower is better (default 28)")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    try:
        return {"status": _status, "archive": _archive, "restore": _restore, "snapshot": _snapshot}[args.stills_action](
            args
        )
    except stills.StillsError as exc:
        print(f"stills: {exc}", file=sys.stderr)
        return 1


def _status(args: argparse.Namespace) -> int:
    caps = stills.capabilities()
    if args.json:
        print(json.dumps(caps.to_dict(), indent=2))
        return 0
    print(
        f"AVIF snapshots:           {'yes (' + caps.avif_encoder + ')' if caps.avif else 'no: ' + caps.reasons['avif']}"
    )
    jxl = "yes" if caps.jxl_lossless_jpeg else "no: " + caps.reasons["jxl_lossless_jpeg"]
    print(f"Lossless JPEG recompress: {jxl}")
    return 0 if caps.avif or caps.jxl_lossless_jpeg else 1


def _archive(args: argparse.Namespace) -> int:
    failed = 0
    for path in args.files:
        try:
            done = stills.archive(path, remove_original=args.remove_original)
        except stills.StillsError as exc:
            print(f"stills: {exc}", file=sys.stderr)
            failed += 1
            continue
        print(
            f"{path.name} -> {done.target.name}: {done.original_bytes} -> {done.archive_bytes} bytes "
            f"({done.saved_percent}% smaller), rebuilt byte for byte (sha256 {done.sha256[:16]})"
            + ("; original removed" if done.removed_original else "")
        )
    return 1 if failed else 0


def _restore(args: argparse.Namespace) -> int:
    for path in args.files:
        print(f"{path.name} -> {stills.restore(path).name}")
    return 0


def _snapshot(args: argparse.Namespace) -> int:
    target = args.output or args.file.with_suffix(".avif")
    done = stills.snapshot(args.file, target, args.quality)
    print(f"{args.file.name} -> {done.name} ({done.stat().st_size} bytes)")
    return 0
