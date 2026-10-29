"""Make StreamStack Camera a PipeWire camera after boot (#176).

Why it is missing. The program node is a v4l2loopback device with `exclusive_caps=1` (ADR 0004): until a process writes
it, it offers only the OUTPUT capability, and a node that cannot capture is not a camera. WirePlumber's v4l2 monitor
probes a device when udev announces it, which for the loopback is module load at boot, long before the Console feeds
it, and v4l2loopback sends no uevent when a writer attaches later. So WirePlumber keeps the stale OUTPUT-only
probe (`api.v4l2.cap.capabilities` 0x...2 in `pw-dump`) and never publishes a `Video/Source`, while Teams, Zoom and
Chrome (which open `/dev/video43` directly) are fine. A WirePlumber that starts after the node is fed publishes it,
which is why a host whose WirePlumber was restarted by hand later shows the camera and a freshly booted one does not.

The fix is to let WirePlumber probe the node again once it is fed: `systemctl --user restart wireplumber`. That drops
every audio stream of this user for a moment, so it runs only when nothing is using the camera or the microphone
(a call), unless the person forces it. `streamstack camera-publish` is that step; the workstation role runs it after
`streamstack-console.service` has started. Everything here is injectable, and nothing opens a /dev/video node.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass

from streamstack import camerause, pipewirecam, system

DONE, FAILED, BUSY = 0, 1, 3  # exit codes: published (or already), could not, refused because a call may be running
FED_POLL_S = 1.0
PUBLISH_WAIT_S = 20.0
PLUMBING_BINARIES = ("pacat", "parec", "pw-cat", "pw-record")


@dataclass(frozen=True)
class Result:
    code: int
    message: str

    @property
    def ok(self) -> bool:
        return self.code == DONE


def foreign_capture_streams(listing: str) -> list[str] | None:
    """Names of the capture streams (pactl source-outputs) that are not StreamStack's own plumbing; None when the
    text is not pactl's JSON. A call captures the microphone, so any of these may be one."""
    try:
        streams = json.loads(listing)
    except json.JSONDecodeError:
        return None
    if not isinstance(streams, list):
        return None
    found = []
    for stream in streams:
        props = stream.get("properties") if isinstance(stream, dict) else None
        if not isinstance(props, dict):
            continue
        binary = str(props.get("application.process.binary") or "")
        names = [str(props.get(k) or "") for k in ("application.name", "media.name", "node.name")]
        if binary in PLUMBING_BINARIES or any("streamstack" in n.lower() for n in [binary, *names]):
            continue
        found.append(next((n for n in [names[0], binary, names[1], names[2]] if n), "an audio stream"))
    return found


def capture_streams() -> list[str] | None:
    """The other programs capturing audio now, or None when pactl cannot say."""
    done = system.run(["pactl", "-f", "json", "list", "source-outputs"], timeout=10)
    if done is None or done.returncode != 0:
        return None
    return foreign_capture_streams(done.stdout)


def camera_readers(program: str) -> list[str]:
    """The apps holding the program node open (StreamStack's own writers and their helpers are not counted)."""
    return [c.app for c in camerause.ProcScanner().scan({program}).consumers]


def busy_reason(readers: list[str], streams: list[str] | None) -> str:
    """A sentence naming what may be a call, empty when nothing is."""
    parts = []
    if readers:
        parts.append(f"{', '.join(dict.fromkeys(readers))} has the camera open")
    if streams:
        parts.append(f"{', '.join(dict.fromkeys(streams))} is capturing audio")
    if streams is None:
        parts.append("pactl cannot say whether a call is capturing audio")
    return " and ".join(parts)


def publish(
    cfg: dict[str, str],
    *,
    wait_fed: float = 0.0,
    force: bool = False,
    probe: Callable[[str], bool | None] = system.loopback_has_producer,
    sources: Callable[[], list[pipewirecam.CameraNode] | None] = pipewirecam.read_sources,
    readers: Callable[[str], list[str]] = camera_readers,
    streams: Callable[[], list[str] | None] = capture_streams,
    run: Callable[..., object] = system.run,
    sleep: Callable[[float], object] = time.sleep,
    say: Callable[[str], object] = lambda text: None,
) -> Result:
    """Restart WirePlumber once so it probes the fed program node, unless it already lists it or a call may be on."""
    program, label = cfg["STREAMSTACK_PROGRAM_DEVICE"], cfg["STREAMSTACK_PROGRAM_LABEL"]
    waited = 0.0
    while probe(program) is not True:
        if waited >= wait_fed:
            return Result(FAILED, f"{label} ({program}) has no producer yet, so there is nothing to publish.")
        sleep(FED_POLL_S)
        waited += FED_POLL_S

    def published() -> bool | None:
        nodes = sources()
        return None if nodes is None else any(n.path == f"v4l2:{program}" for n in nodes)

    now = published()
    if now is None:
        return Result(FAILED, "PipeWire does not answer (is pipewire running for this user?).")
    if now:
        return Result(DONE, f"{label} is already a PipeWire camera.")
    if not force:
        reason = busy_reason(readers(program), streams())
        if reason:
            return Result(
                BUSY,
                f"Not restarting WirePlumber: {reason}, and the restart drops every audio stream. "
                "Run it again when the call is over (--force restarts anyway).",
            )
    say("Restarting WirePlumber so it probes the fed program camera (audio streams drop for a moment)...")
    done = run(["systemctl", "--user", "restart", "wireplumber"], timeout=30)
    if done is None or getattr(done, "returncode", 1) != 0:
        detail = str(getattr(done, "stderr", "") or getattr(done, "stdout", "")).strip() if done else "did not run"
        return Result(FAILED, f"WirePlumber did not restart: {detail}")
    waited = 0.0
    while waited < PUBLISH_WAIT_S:
        sleep(1.0)
        waited += 1.0
        if published():
            return Result(DONE, f"{label} is now a PipeWire camera.")
    return Result(
        FAILED,
        f"WirePlumber restarted but PipeWire still does not list {label}. "
        f"Check that `v4l2-ctl -d {program} --info` offers Video Capture and that no WirePlumber config "
        "disables the v4l2 monitor.",
    )
