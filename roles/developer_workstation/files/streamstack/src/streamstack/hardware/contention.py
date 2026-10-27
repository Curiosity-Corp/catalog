"""Who holds the camera nodes, and does that break "one producer per node"? (#86, and the "OBS blocked the cam" report.)

A v4l2loopback node takes **one producer**. A second program that opens it for output (a leftover OBS virtual camera,
a test `ffmpeg ... -f v4l2`) races the engine for it: frames from two writers, a format the engine did not choose, or
the engine failing to open the node at all. A UVC or HDMI capture card (the Cam Link) is worse: it has **one reader**,
so while OBS (or a browser) holds it, StreamStack's engine cannot read it and the other way round. Both look to the
person like "OBS blocked the camera in Teams". This module finds the holders from `/proc/<pid>/fd` and nothing else:
**it never opens a /dev/video node**, so it cannot steal one or change a format.

Roles per node:

* `program`  (StreamStack Camera): the only allowed writer is StreamStack itself (the engine child, or the slate).
  Every other writer, and OBS in any role, is a competing producer. Call apps and browsers read it, which is the point.
* `camera`   (the Canon loopback): one feeder (the Canon bridge: `streamstack-canon` or the external PTP bridge's
  ffmpeg); a second writer is a competing producer. Readers are fine.
* `capture`  (the Cam Link): any holder that is not StreamStack is a rival reader.

Whether a process *writes* is judged from what it is: OBS always; `ffmpeg` when the node is its last argument (the
output); GStreamer when the node is the `v4l2sink` device. A process that merely has the node open and is not a known
producer is reported as a reader. That is a heuristic and the rows say "may" where it is one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from streamstack import camerause

GROUP = "video"
PRODUCER_COMMS = ("obs", "obs-studio", "obs-ffmpeg-mux", "v4l2loopback-ctl")
FIX_OBS = "close OBS, or in OBS choose Tools > Stop Virtual Camera and remove it from the startup actions"


@dataclass(frozen=True)
class Holder:
    pid: int
    comm: str
    device: str
    app: str
    own: bool  # a StreamStack process, or an ffmpeg/gst helper started by one
    writes: bool | None  # True: a producer; False: a reader; None: cannot tell

    def public(self) -> dict[str, object]:
        return {"pid": self.pid, "process": self.comm, "app": self.app, "device": self.device, "own": self.own}


def _writes(comm: str, argv: list[str], device: str) -> bool | None:
    name = comm.lower()
    if any(name == p or name.startswith(p + "-") for p in PRODUCER_COMMS):
        return True
    if name == "ffmpeg":
        if argv and argv[-1] == device:
            return True
        return False if "-i" in argv and argv[argv.index("-i") + 1 : argv.index("-i") + 2] == [device] else None
    if name.startswith("gst-launch"):
        text = " ".join(argv)
        if "v4l2sink" in text and device in text:
            return True
        return False if "v4l2src" in text else None
    return False if camerause.friendly(comm) != comm else None


def holders(devices: set[str], *, proc: Path = camerause.PROC, own_pid: int | None = None) -> list[Holder]:
    """Every process of this user with one of `devices` open. Read-only."""
    scanner = camerause.ProcScanner(proc, own_pid=own_pid)
    wanted = {os.path.realpath(d): d for d in devices} | {d: d for d in devices}
    found: list[Holder] = []
    for pid in scanner._pids():
        try:
            fds = os.listdir(proc / str(pid) / "fd")
        except OSError:
            continue
        held = None
        for fd in fds:
            try:
                target = os.readlink(proc / str(pid) / "fd" / fd)
            except OSError:
                continue
            if target in wanted:
                held = wanted[target]
                break
        if held is None:
            continue
        comm = camerause._read(proc / str(pid) / "comm")
        argv = camerause._argv(proc, pid)
        found.append(
            Holder(
                pid, comm, held, camerause.friendly(comm), scanner._is_plumbing(pid, comm), _writes(comm, argv, held)
            )
        )
    return found


def _row(state: str, cid: str, title: str, detail: str = "", hint: str = "") -> dict[str, str]:
    return {"id": cid, "group": GROUP, "state": state, "title": title, "detail": detail, "hint": hint}


def _who(items: list[Holder]) -> str:
    return ", ".join(f"{h.app} (pid {h.pid})" for h in items)


def _fix_for(items: list[Holder]) -> str:
    return (
        FIX_OBS
        if any(h.app.lower().startswith("obs") or h.comm.lower().startswith("obs") for h in items)
        else ("stop that program (kill its pid), or point it at another device")
    )


def checks(
    found: list[Holder], *, program: str, camera: str, capture: str | None, program_label: str
) -> list[dict[str, str]]:
    """Rows `video-holders-program`, `-camera` and `-capture` for the holders `found`."""
    rows: list[dict[str, str]] = []
    on = lambda device: [h for h in found if h.device == device]  # noqa: E731

    rival = [h for h in on(program) if not h.own and (h.writes or h.comm.lower().startswith("obs"))]
    if rival:
        rows.append(
            _row(
                "fail",
                "video-holders-program",
                f"{_who(rival)} also feeds {program}, so {program_label} has two producers",
                "a loopback node takes one producer: the picture flickers between them or the engine cannot open it",
                _fix_for(rival),
            )
        )
    else:
        rows.append(_row("ok", "video-holders-program", f"only StreamStack feeds {program}"))

    cam_writers = [h for h in on(camera) if h.writes and not h.own] + [h for h in on(camera) if h.own and h.writes]
    if len(cam_writers) > 1:
        rows.append(
            _row(
                "fail",
                "video-holders-camera",
                f"{len(cam_writers)} programs feed {camera}: {_who(cam_writers)}",
                "the camera node takes one producer",
                _fix_for(cam_writers),
            )
        )

    if capture:
        rivals = [h for h in on(capture) if not h.own]
        if rivals:
            rows.append(
                _row(
                    "warn",
                    "video-holders-capture",
                    f"{_who(rivals)} holds the Cam Link ({capture}): StreamStack cannot read it, and it cannot be "
                    "shared with a call app",
                    "a capture card has one reader",
                    _fix_for(rivals).replace(
                        "stop that program", "close it, or let it read StreamStack Camera instead"
                    ),
                )
            )
    return rows


def guard_checks(cfg: dict[str, str], capture: str | None, *, proc: Path = camerause.PROC) -> list[dict[str, str]]:
    program, camera = cfg["STREAMSTACK_PROGRAM_DEVICE"], cfg["STREAMSTACK_CAMERA_DEVICE"]
    devices = {d for d in (program, camera, capture) if d}
    found = holders(devices, proc=proc)
    return checks(
        found, program=program, camera=camera, capture=capture, program_label=cfg["STREAMSTACK_PROGRAM_LABEL"]
    )
