"""`streamstack framing`: what auto-framing needs, fetching the model, and a timing probe."""

from __future__ import annotations

import argparse
import sys

from streamstack import config
from streamstack.framing import detector, health, model

PROBE_RUNS = 30


def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("framing", help="auto-framing: check the face model and backends, --fetch the model")
    parser.add_argument("--fetch", action="store_true", help="download the face model and verify its checksum")
    parser.add_argument("--force", action="store_true", help="with --fetch: download again even if it verifies")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="open the detector and time it on a synthetic picture (uses the Intel GPU when allowed; no camera)",
    )
    parser.set_defaults(func=run)


def synthetic_jpeg(width: int = 1280, height: int = 720) -> bytes:
    """A soft gradient with a face-sized disc: enough to time a detection, no camera involved."""
    import cv2
    import numpy as np

    xs = np.linspace(40, 200, width, dtype=np.uint8)
    image = np.tile(xs, (height, 1))
    image = np.stack([image, image // 2 + 40, 255 - image], axis=2)
    cv2.circle(image, (width // 2, height // 3), height // 8, (170, 190, 230), -1)
    ok, jpeg = cv2.imencode(".jpg", image)
    if not ok:
        raise RuntimeError("could not encode the probe picture")
    return jpeg.tobytes()


def run(args: argparse.Namespace) -> int:
    cfg = config.load_settings()
    spec = model.YUNET
    print(f"model: {spec.name}, {spec.license}")
    print(f"       {spec.source}")
    if args.fetch:
        try:
            path = model.fetch(force=args.force)
        except model.ModelError as exc:
            print(f"streamstack: {exc}", file=sys.stderr)
            return 1
        print(f"model installed and verified: {path}")
    state, detail = model.status()
    print(f"model file: {state}: {detail}")
    modules = detector.available_modules()
    for name, extra in (("numpy", ""), ("cv2", " (opencv)"), ("openvino", " (optional: Intel GPU)")):
        print(f"python package {name}{extra}: {'installed' if modules[name] else 'missing'}")
    ready, why, hint = health.readiness()
    print("ready: yes" if ready else f"ready: no, {why}. Fix: {hint}")
    if not args.probe:
        return 0 if ready else (0 if not args.fetch else 1)
    if not ready:
        return 1
    try:
        det = detector.open_detector(cfg.get("STREAMSTACK_FRAMING_DEVICE", "auto"))
    except detector.DetectorUnavailable as exc:
        print(f"streamstack: no detector can run: {exc}", file=sys.stderr)
        return 1
    try:
        jpeg = synthetic_jpeg()
        for _ in range(PROBE_RUNS):
            det.detect(jpeg)
        timing = det.timing.summary() if hasattr(det, "timing") else {}
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"streamstack: the detector failed on the probe picture: {exc}", file=sys.stderr)
        return 1
    finally:
        det.close()
    print(
        f"detector: {det.backend} on {det.device}: {timing.get('median_ms')} ms median, {timing.get('p95_ms')} ms p95"
    )
    return 0
