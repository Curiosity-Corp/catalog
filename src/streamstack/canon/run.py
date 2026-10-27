"""`streamstack canon-bridge` (the service) and `streamstack canon ...` (look at and drive the running bridge)."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from collections.abc import Callable
from typing import Any

from streamstack import system
from streamstack.canon import ipc
from streamstack.canon.bridge import Bridge, Settings
from streamstack.canon.gphoto import CliSession, LibSession, Session, load_gphoto2


def release_gvfs() -> None:
    """GVFS mounts PTP cameras on plug-in and then holds the USB interface."""
    system.run(["gio", "mount", "-s", "gphoto2"], timeout=10)
    system.run(["pkill", "-u", str(os.getuid()), "-f", "gvfsd-gphoto2"], timeout=5)


def session_factory(backend: str) -> tuple[Callable[[], Session], str]:
    """(factory, backend name). auto prefers python-gphoto2 (one shared session) and falls back to the CLI."""
    gp = load_gphoto2() if backend in ("auto", "libgphoto2") else None
    if gp is not None:
        return (lambda: LibSession(gp)), "libgphoto2"
    if backend == "libgphoto2":
        raise RuntimeError(
            "STREAMSTACK_CANON_BACKEND=libgphoto2 needs the python gphoto2 package (pip install gphoto2)"
        )
    return CliSession, "cli"


def run_bridge(cfg: dict[str, str]) -> int:
    try:
        factory, backend = session_factory(cfg.get("STREAMSTACK_CANON_BACKEND", "auto").strip().lower())
    except RuntimeError as exc:
        print(f"streamstack: {exc}", file=sys.stderr)
        return 1
    release_gvfs()
    from streamstack.console.hardware import overrides_from
    from streamstack.hardware.roles import LivePlan

    live = LivePlan(
        lambda: overrides_from(cfg, os.environ)
    )  # the hardware Plan: which GPU decodes, which node is the camera
    bridge = Bridge(Settings.from_config(cfg, live.get()), factory, on_busy=release_gvfs, plan=live.get)
    server, path = ipc.serve(bridge.handle)
    print(f"streamstack canon-bridge: camera backend {backend}, control socket {path}", file=sys.stderr, flush=True)

    def stop(signum: int, frame: object) -> None:
        bridge.stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        return bridge.run()
    finally:
        server.shutdown()
        server.server_close()
        path.unlink(missing_ok=True)


def _call(payload: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return ipc.request(payload)
    except ipc.BridgeUnavailable as exc:
        print(
            f"streamstack: {exc}\n  is the bridge running?  systemctl --user status streamstack-canon.service",
            file=sys.stderr,
        )
        return None


def _show_controls(controls: dict[str, dict[str, Any]], only: str | None = None) -> None:
    for cid, c in controls.items():
        if only and only != cid:
            continue
        flags = "read-only" if c["readonly"] else "writable"
        choices = f"  choices: {', '.join(c['choices'])}" if c["choices"] else ""
        print(f"{cid:14} {c['key']:22} = {c['value']!s:24} [{flags}]{choices}")


def cmd_canon(args: argparse.Namespace) -> int:
    op = args.canon_op
    if op in ("status", "get"):
        answer = _call({"op": "status"})
        if answer is None:
            return 1
        status = answer["status"]
        if args.json:
            print(json.dumps(status, indent=2))
            return 0
        if op == "get":
            _show_controls(status["controls"], getattr(args, "control", None))
            return 0
        decode, out, inp = status["decode"], status["output"], status["input"]
        print(f"state      {status['state']}" + (f" ({status['message']})" if status["message"] else ""))
        print(f"session    {status['session']}   usb: {', '.join(status['usb']) or 'none seen'}")
        print(
            f"decode     {decode['label'] or 'not running'}   ffmpeg {decode['ffmpeg']}"
            f"   render node {decode['render_node']}"
        )
        for t in decode["tried"]:
            print(f"           {t['name']}: {'ok' if t['ok'] else t['reason']}")
        if inp and out:
            print(
                f"picture    {inp['width']}x{inp['height']} {inp['subsampling']} in, "
                f"{out['width']}x{out['height']} out ({out['mode']})"
            )
        print(
            f"rate       {status['fps_in']} fps in, {status['fps_out']} fps out; "
            f"grab {status['grab_ms']} ms, pipe {status['pipe_ms']} ms"
        )
        print(f"cpu        {status['cpu']}")
        print(f"restarts   {status['restarts']}  stalls {status['stalls']}  last error: {status['last_error']}")
        _show_controls(status["controls"])
        return 0
    if op == "set":
        answer = _call({"op": "set", "id": args.control, "value": args.value})
    elif op == "af":
        answer = _call({"op": "action", "name": "af_cancel" if args.cancel else "autofocus"})
    elif op == "focus":
        answer = _call({"op": "action", "name": "focus", "step": args.step})
    else:
        answer = _call({"op": "refresh"})
    if answer is None:
        return 1
    print(json.dumps(answer))
    return 0 if answer.get("ok") else 1


def add_parser(sub: Any) -> None:
    canon = sub.add_parser("canon", help="status of, and settings on, the running Canon bridge")
    ops = canon.add_subparsers(dest="canon_op", required=True)
    for name, text in (("status", "decode path, rates, CPU and every setting"), ("get", "current setting values")):
        p = ops.add_parser(name, help=text)
        p.add_argument("--json", action="store_true")
        if name == "get":
            p.add_argument("control", nargs="?", help="iso, shutter, aperture, whitebalance, kelvin, picturestyle, ...")
        p.set_defaults(func=cmd_canon)
    p = ops.add_parser("set", help="set a camera setting, e.g. canon set iso 400")
    p.add_argument("control")
    p.add_argument("value")
    p.set_defaults(func=cmd_canon, json=False)
    p = ops.add_parser("af", help="autofocus once (or --cancel)")
    p.add_argument("--cancel", action="store_true")
    p.set_defaults(func=cmd_canon, json=False)
    p = ops.add_parser("focus", help="nudge focus: -3..-1 nearer, 1..3 farther")
    p.add_argument("step", type=int)
    p.set_defaults(func=cmd_canon, json=False)
    ops.add_parser("refresh", help="re-read every setting from the camera").set_defaults(func=cmd_canon, json=False)
