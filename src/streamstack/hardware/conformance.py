"""The v4l2loopback conformance run for a scratch node (#86): the camera contract, proven on a live module.

Acceptance of #86 is "conformance tests pass after a kernel change and a module reload". This is that test, run by hand
or in a script after a kernel update or a `modprobe -r v4l2loopback; modprobe v4l2loopback ...`. It needs a **scratch**
node (`sudo v4l2loopback-ctl add -n "Conformance scratch" -x 1 /dev/video50`) and never touches the program or camera
nodes. Steps, each a row:

1. `unfed`: with no writer, an `exclusive_caps=1` node must not advertise Video Capture (so call apps do not list it);
2. `fed`: with a writer (ffmpeg's test picture, YUYV 1280x720 at 30 fps) it must advertise Video Capture;
3. `contract`: the formats it then offers satisfy `contract.violations`;
4. `frames`: a reader gets frames from it;
5. `unfed again`: when the writer exits it is unlisted again (so a dead producer is visible, the dm313red case);
6. `refed`: a second writer works (the node survives a producer restart).
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from streamstack import config, system
from streamstack.hardware import contention, contract

SIZE, FPS = "1280x720", 30


@dataclass(frozen=True)
class Step:
    id: str
    ok: bool
    detail: str = ""


def capture_listed(device: str, run: Callable[..., object] = system.run) -> bool | None:
    """True when the node advertises Video Capture (what makes a call app list it); None when it cannot be read."""
    done = run(["v4l2-ctl", "-d", device, "--info"])
    if done is None or getattr(done, "returncode", 1) != 0:
        return None
    caps = str(getattr(done, "stdout", ""))
    block = caps.split("Device Caps", 1)[-1]
    return "Video Capture" in block


def refuse(device: str, cfg: dict[str, str] | None = None) -> str | None:
    """A sentence saying why `device` must not be used, or None when it is a free scratch loopback node."""
    settings = cfg if cfg is not None else config.load_settings()
    real = os.path.realpath(device)
    protected = {
        os.path.realpath(p)
        for p in ("/dev/video42", "/dev/video43", settings.get("STREAMSTACK_PROGRAM_DEVICE", ""),
                  settings.get("STREAMSTACK_CAMERA_DEVICE", ""))
        if p
    }  # fmt: skip
    if real in protected:
        return f"{device} is a node the Console feeds: use a scratch node (see tools/loopback_conformance.py)"
    if not Path(real).exists():
        return f"{device} does not exist"
    info = system.run(["v4l2-ctl", "-d", real, "--info"])
    if info is None or info.returncode != 0:
        return f"cannot read {device} with v4l2-ctl (is v4l-utils installed and the node readable?)"
    if "loopback" not in info.stdout.lower():
        return f"{device} is not a v4l2loopback node"
    held = contention.holders({device, real})
    if held:
        return f"{device} is already held by " + ", ".join(f"{h.comm} ({h.pid})" for h in held)
    return None


def _writer(device: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-re", "-f", "lavfi", "-i",
         f"testsrc2=size={SIZE}:rate={FPS}", "-pix_fmt", "yuyv422", "-f", "v4l2", device],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )  # fmt: skip


def _wait(predicate: Callable[[], bool | None], seconds: float, sleep: Callable[[float], None] = time.sleep) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        sleep(0.2)
    return bool(predicate())


def read_frames(device: str, count: int = 10) -> bool:
    done = system.run(
        ["v4l2-ctl", "-d", device, "--stream-mmap", f"--stream-count={count}", "--stream-to=/dev/null"], timeout=20
    )
    return done is not None and done.returncode == 0


def run_conformance(
    device: str,
    *,
    writer: Callable[[str], subprocess.Popen[bytes]] = _writer,
    listed: Callable[[str], bool | None] = capture_listed,
    modes: Callable[[str], list[system.Mode]] = system.camera_modes,
    frames: Callable[[str], bool] = read_frames,
    wait: Callable[..., bool] = _wait,
) -> list[Step]:
    """Run the six steps against a node already checked with `refuse`. Always stops its writers."""
    steps = [Step("unfed", listed(device) is False, "no writer: Video Capture must not be advertised")]
    proc = writer(device)
    try:
        fed = wait(lambda: listed(device), 8)
        steps.append(Step("fed", fed, "a writer is attached: Video Capture must be advertised"))
        if fed:
            found = modes(device)
            bad = contract.violations(found)
            steps.append(
                Step("contract", bool(found) and not bad, "; ".join(bad) or ("no formats offered" if not found else ""))
            )
            steps.append(Step("frames", frames(device), "a reader receives frames"))
    finally:
        _stop(proc)
    gone = wait(lambda: listed(device) is False, 8)
    steps.append(Step("unfed again", gone, "the writer exited: the node must be unlisted again"))
    proc = writer(device)
    try:
        steps.append(Step("refed", wait(lambda: listed(device), 8), "a second writer must work (producer restart)"))
    finally:
        _stop(proc)
    return steps


def _stop(proc: subprocess.Popen[bytes]) -> None:
    system.reap(proc)


def passed(steps: list[Step]) -> bool:
    return bool(steps) and all(s.ok for s in steps)
