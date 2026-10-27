"""Recordings that outlived their recorder: find them after a crash or a power cut and make them whole.

While a recording runs, a small marker file `<recording>.recording` sits next to it (who is writing it, how many
tracks it should hold). A clean stop removes the marker. A marker whose writer is gone is an orphan: the process
was killed (`kill -9`, the OOM killer, a power cut) before it could finish the file.

A Matroska recording made by `FileSink` is already playable after a crash (no cues, an unknown-size segment,
fsynced about once a second), but it has no index, so players cannot seek and the duration is unknown. Recovery
remuxes it (`ffmpeg -c copy`, nothing is re-encoded) into a finished file with an index, checks the result, and
only then swaps it in. A fragmented MP4 needs no repair; it is checked and left as it is.

Nothing here ever deletes a recording that holds frames: when a repair cannot be verified the original stays and
the repaired copy is kept beside it.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

MARKER_SUFFIX = ".recording"
REPAIR_TIMEOUT_S = 600.0  # a remux copies packets; even a multi-hour file takes a fraction of this
PROBE_TIMEOUT_S = 30.0
MIN_KEPT_RATIO = 0.97  # the repaired file must hold at least this share of the original's bytes
SEGMENT = re.compile(r"^(?P<stem>.+)-(?P<n>\d{3})(?P<ext>\.[^.]+)$")

Emit = Callable[[str, str], None]  # (level, text)


@dataclass(frozen=True)
class Marker:
    path: Path  # the marker file
    recording: Path  # the first file; later segments are `<stem>-002.mkv` and so on
    pid: int
    start_ticks: int
    fmt: str
    tracks: int


@dataclass
class Outcome:
    recording: Path
    status: str  # recovered | intact | kept-both | empty | missing | failed
    detail: str


def marker_for(recording: Path) -> Path:
    return recording.with_name(recording.name + MARKER_SUFFIX)


def process_start_ticks(pid: int) -> int:
    """The process's start time in clock ticks since boot (field 22 of /proc/<pid>/stat); 0 when it is gone.

    Together with the pid it names one process for good: a pid the kernel reuses has another start time."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return 0
    try:
        # the command name is in parentheses and may hold spaces; everything after the last ")" is plain fields
        return int(text.rsplit(")", 1)[1].split()[19])
    except (IndexError, ValueError):
        return 0


def write_marker(recording: Path, fmt: str, tracks: int, pid: int | None = None) -> Path:
    """Mark `recording` as being written by this process. Best effort by the caller: a missing marker only means
    a crash is not repaired automatically."""
    pid = os.getpid() if pid is None else pid
    marker = marker_for(recording)
    body = {"pid": pid, "start_ticks": process_start_ticks(pid), "format": fmt, "tracks": tracks}
    marker.parent.mkdir(parents=True, exist_ok=True)
    tmp = marker.with_name(marker.name + ".tmp")
    tmp.write_text(json.dumps(body))
    os.replace(tmp, marker)
    return marker


def clear_marker(recording: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        marker_for(recording).unlink()


def _read_marker(path: Path) -> Marker | None:
    try:
        body = json.loads(path.read_text())
        return Marker(
            path=path,
            recording=path.with_name(path.name[: -len(MARKER_SUFFIX)]),
            pid=int(body["pid"]),
            start_ticks=int(body["start_ticks"]),
            fmt=str(body.get("format", "mkv")),
            tracks=int(body.get("tracks", 0)),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def writer_alive(marker: Marker) -> bool:
    """True while the process that wrote the marker still runs (the same process, not a reused pid)."""
    ticks = process_start_ticks(marker.pid)
    return ticks != 0 and (marker.start_ticks == 0 or ticks == marker.start_ticks)


def orphans(record_dir: Path) -> list[Marker]:
    """Markers in `record_dir` whose writer is gone."""
    try:
        names = sorted(record_dir.glob(f"*{MARKER_SUFFIX}"))
    except OSError:
        return []
    found = []
    for path in names:
        marker = _read_marker(path)
        if marker is None:
            continue
        if not writer_alive(marker):
            found.append(marker)
    return found


def _segments(marker: Marker) -> list[Path]:
    """The first file and the segments `FileSink` rolled to when the picture format changed."""
    first = marker.recording
    files = [first] if first.exists() else []
    stem, ext = first.stem, first.suffix
    files += sorted(p for p in first.parent.glob(f"{stem}-[0-9][0-9][0-9]{ext}") if SEGMENT.match(p.name))
    return files


def find_ffprobe(ffmpeg: str) -> str:
    sibling = Path(ffmpeg).with_name("ffprobe")
    if sibling.is_file():
        return str(sibling)
    return shutil.which("ffprobe") or "ffprobe"


def inspect(path: Path, ffprobe: str) -> tuple[int, float] | None:
    """(stream count, duration in seconds) when ffprobe can read `path` and it holds media; None otherwise."""
    try:
        done = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration:stream=index", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    try:
        body = json.loads(done.stdout or "{}")
        streams = len(body.get("streams", []))
        duration = float(body.get("format", {}).get("duration") or 0.0)
    except (ValueError, TypeError):
        return None
    return (streams, duration) if streams else None


def _repair_mkv(path: Path, ffmpeg: str, ffprobe: str) -> Outcome:
    before = inspect(path, ffprobe)
    if before is None:
        return Outcome(path, "failed", f"{path.name} cannot be read (no media in it)")
    original = path.stat().st_size
    repaired = path.with_name(f".{path.stem}.repair{path.suffix}")
    kept = path.with_name(f"{path.stem}.repaired{path.suffix}")
    try:
        done = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(path), "-map", "0",
             "-c", "copy", str(repaired)],
            capture_output=True,
            text=True,
            timeout=REPAIR_TIMEOUT_S,
            check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as exc:
        repaired.unlink(missing_ok=True)
        return Outcome(
            path, "failed", f"{path.name} could not be repaired ({type(exc).__name__}); the file is kept as is"
        )
    after = inspect(repaired, ffprobe) if done.returncode == 0 and repaired.exists() else None
    if after is None:
        repaired.unlink(missing_ok=True)
        why = (done.stderr or "").strip().splitlines()[-1:] or ["ffmpeg gave no reason"]
        return Outcome(path, "failed", f"{path.name} could not be repaired ({why[0]}); the file is kept as is")
    if after[0] < before[0] or repaired.stat().st_size < MIN_KEPT_RATIO * original:
        # a smaller or thinner result: never trade the original for it
        os.replace(repaired, kept)
        return Outcome(
            path,
            "kept-both",
            f"{path.name} was remuxed but the copy looks smaller ({repaired_size(kept)} of {original} bytes); "
            f"both are kept, the copy as {kept.name}",
        )
    # the original is replaced only by a copy that holds every stream and (nearly) every byte
    os.replace(repaired, path)
    return Outcome(path, "recovered", f"{path.name} was repaired after an unclean stop ({after[1]:.0f} s)")


def repaired_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def recover(marker: Marker, ffmpeg: str, ffprobe: str | None = None) -> list[Outcome]:
    """Repair what an orphaned marker points at, then remove the marker (unless a repair failed)."""
    ffprobe = ffprobe or find_ffprobe(ffmpeg)
    outcomes: list[Outcome] = []
    files = _segments(marker)
    if not files:
        marker.path.unlink(missing_ok=True)
        return [Outcome(marker.recording, "missing", f"{marker.recording.name} never got a frame; nothing to recover")]
    for path in files:
        try:
            size = path.stat().st_size
        except OSError as exc:
            outcomes.append(Outcome(path, "failed", f"{path.name} cannot be read: {exc.strerror or exc}"))
            continue
        if size == 0:
            path.unlink(missing_ok=True)
            outcomes.append(Outcome(path, "empty", f"{path.name} was empty and has been removed"))
        elif marker.fmt == "mp4":
            info = inspect(path, ffprobe)
            if info is None:
                outcomes.append(Outcome(path, "failed", f"{path.name} cannot be played; it is kept as is"))
            else:
                outcomes.append(Outcome(path, "intact", f"{path.name} is a fragmented MP4 and plays ({info[1]:.0f} s)"))
        else:
            outcomes.append(_repair_mkv(path, ffmpeg, ffprobe))
    if all(o.status != "failed" for o in outcomes):
        marker.path.unlink(missing_ok=True)
    return outcomes


def recover_orphans(record_dir: Path, ffmpeg: str, emit: Emit, skip: Callable[[Path], bool] | None = None) -> int:
    """Recover every orphaned recording in `record_dir`; says what it did through `emit`. Returns how many it saw.

    `skip` names recordings that are being written right now by this process."""
    found = [m for m in orphans(record_dir) if not (skip and skip(m.recording))]
    for marker in found:
        emit("warn", f"{marker.recording.name} was not closed (the recorder stopped unexpectedly); repairing it.")
        try:
            outcomes = recover(marker, ffmpeg)
        except OSError as exc:
            emit("error", f"{marker.recording.name} could not be recovered: {exc.strerror or exc}")
            continue
        for outcome in outcomes:
            level = {"recovered": "ok", "intact": "ok", "empty": "info", "missing": "info"}.get(outcome.status, "warn")
            emit(level, outcome.detail + ".")
    return len(found)
