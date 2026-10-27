"""Talk to PipeWire directly: find the chain's nodes, set filter-graph controls, run the chain's host process.

Everything goes through the PipeWire command line tools (`pw-dump`, `pw-cli`) and one `pipewire -c <file>` client
process, never through the audio server's configuration files, so a running PipeWire is never restarted or modified.
`Runner` is injectable so the logic is testable without a server.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import system
from . import graph

log = logging.getLogger(__name__)

Runner = Callable[[list[str], float], "subprocess.CompletedProcess[str] | None"]

SOURCE_CLASS = "Audio/Source"


def default_runner(argv: list[str], timeout: float = 10) -> subprocess.CompletedProcess[str] | None:
    return system.run(argv, timeout=timeout)


def spa_number(value: float) -> str:
    return f"{value:.6g}"


def props_json(controls: dict[str, float]) -> str:
    """The `Props` parameter body for `pw-cli set-param`: control names are quoted (they contain spaces)."""
    parts = []
    for name, value in controls.items():
        if '"' in name or "\\" in name:
            raise ValueError(f"bad control name {name!r}")
        parts.append(f'"{name}" {spa_number(value)}')
    return "{ params = [ " + " ".join(parts) + " ] }"


class Nodes:
    """What `pw-dump` says about the chain: the ids of its two nodes and the facts apps depend on."""

    def __init__(self, source: dict[str, Any] | None, capture: dict[str, Any] | None) -> None:
        self.source = source
        self.capture = capture

    @property
    def present(self) -> bool:
        return self.source is not None and self.capture is not None

    @property
    def control_id(self) -> int | None:
        """The node that carries the filter-graph controls (the capture side)."""
        return int(self.capture["id"]) if self.capture else None

    @property
    def source_id(self) -> int | None:
        return int(self.source["id"]) if self.source else None

    def contract(self) -> dict[str, Any]:
        props = (self.source or {}).get("info", {}).get("props", {})
        return {
            "name": props.get("node.name"),
            "description": props.get("node.description") or props.get("media.name"),
            "media_class": props.get("media.class"),
            "rate": props.get("audio.rate"),
            "channels": props.get("audio.channels"),
            "state": (self.source or {}).get("info", {}).get("state"),
        }


def find_nodes(runner: Runner = default_runner) -> Nodes | None:
    """The chain's nodes, or None when `pw-dump` cannot be run or read."""
    result = runner(["pw-dump"], 10)
    if result is None or result.returncode != 0:
        return None
    try:
        objects = json.loads(result.stdout)
    except ValueError:
        return None
    source = capture = None
    for obj in objects if isinstance(objects, list) else []:
        if not isinstance(obj, dict) or obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = obj.get("info", {}).get("props", {})
        name = props.get("node.name")
        if name == graph.NODE_NAME and props.get("media.class") == SOURCE_CLASS:
            source = obj
        elif name == graph.CAPTURE_NAME:
            capture = obj
    return Nodes(source, capture)


def set_controls(node_id: int, controls: dict[str, float], runner: Runner = default_runner) -> bool:
    """Write filter-graph controls to the live graph (no restart, no dropout). True when PipeWire accepted them."""
    if not controls:
        return True
    result = runner(["pw-cli", "set-param", str(node_id), "Props", props_json(controls)], 5)
    if result is None or result.returncode != 0:
        return False
    return "rror" not in (result.stdout + result.stderr)


# ---------------------------------------------------------------------------
# The host process
# ---------------------------------------------------------------------------


def conf_path(state_dir: Path) -> Path:
    return state_dir / "audio-chain" / "filter-chain.conf"


def write_conf(path: Path, text: str) -> bool:
    """Write the configuration atomically; True when the content changed."""
    try:
        if path.read_text() == text:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".conf.tmp")
    tmp.write_text(text)
    tmp.replace(path)
    return True


def host_pids(conf: Path, proc: Path = Path("/proc")) -> list[int]:
    """Our own host processes: `pipewire -c <conf>` run by this user (matched on the exact command line)."""
    wanted = [b"pipewire", b"-c", str(conf).encode()]
    found = []
    try:
        entries = list(proc.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            argv = (entry / "cmdline").read_bytes().split(b"\0")[:3]
        except OSError:
            continue
        if argv == wanted:
            found.append(int(entry.name))
    return found


def start_host(conf: Path, log_path: Path | None = None) -> subprocess.Popen[bytes] | None:
    """Start `pipewire -c <conf>` detached (it outlives the Console, so a call keeps its microphone)."""
    exe = shutil.which("pipewire")
    if exe is None:
        return None
    sink: Any = subprocess.DEVNULL
    if log_path is not None:
        with contextlib.suppress(OSError):
            log_path.parent.mkdir(parents=True, exist_ok=True)
            sink = log_path.open("ab")
    try:
        return subprocess.Popen(
            ["pipewire", "-c", str(conf)],
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=sink,
            start_new_session=True,
        )
    except OSError as exc:
        log.warning("cannot start the voice chain host: %s", exc)
        return None
    finally:
        if hasattr(sink, "close"):
            sink.close()


def stop_host(conf: Path, wait: float = 3.0) -> bool:
    """SIGTERM our host processes (SIGKILL after `wait`); True when none is left."""
    import signal

    pids = host_pids(conf)
    for pid in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
    end = time.monotonic() + wait
    while time.monotonic() < end and host_pids(conf):
        time.sleep(0.05)
    for pid in host_pids(conf):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
    time.sleep(0.05)
    return not host_pids(conf)
