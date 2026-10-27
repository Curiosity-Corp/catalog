"""Studio, auto mode, health and system state for the Console, and a simulated twin for --demo and tests.

Slow probes (pgrep, v4l2-ctl, pactl, systemctl, the doctor checks) run in
background threads and are cached, so `state()` is instant. Actions that take
seconds (a Claude pass) run the `streamstack` command in a worker
thread, return at once and report through `state` and a toast.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path

from streamstack import camlink, cli, config, system, virtualdevices
from streamstack.console import director
from streamstack.console.errors import ActionError

APPS = ("qpwgraph", "pavucontrol", "qv4l2")
JOURNAL_LIMIT = 30
JOURNAL_TAIL_BYTES = 64 * 1024
ANSI = re.compile(r"\x1b\[[0-9;]*m")
Notify = Callable[[str, str], None]
Runner = Callable[[list[str]], tuple[int, str]]
Report = Callable[[int, str], tuple[str, str]]


def _no_notify(level: str, text: str) -> None:
    return None


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def cpu_times(text: str | None = None) -> tuple[int, int] | None:
    """(busy, total) jiffies from the first line of /proc/stat, or None when unreadable."""
    try:
        line = (text if text is not None else Path("/proc/stat").read_text()).splitlines()[0]
        fields = [int(v) for v in line.split()[1:]]
    except (OSError, IndexError, ValueError):
        return None
    if len(fields) < 5:
        return None
    idle = fields[3] + fields[4]  # idle + iowait
    return sum(fields) - idle, sum(fields)


def cpu_percent(before: tuple[int, int], after: tuple[int, int]) -> float:
    total = after[1] - before[1]
    return round(100.0 * (after[0] - before[0]) / total, 1) if total > 0 else 0.0


LIGHT_REASON = "Easing towards Daylight natural's light for the morning."
CAMERA_REASON = "The face was 96 luma, below the 100-140 band; the camera's own exposure compensation moves first."
GAIN_REASON = "Speech was quiet against the room."


def event_kind(event: str, entry: dict | None = None) -> str:
    """The journal's event names folded into the four kinds the Console colours."""
    if "error" in event:
        return "error"
    if event in ("mic-gain", "audio"):
        return "audio"
    if event in ("exposure", "video", "video-paused"):
        return "video"
    if event == "change":
        return "audio" if (entry or {}).get("kind") in ("audio", "interface") else "video"
    return "ai"


CHANGE_FIELDS = ("actuator", "target", "key", "before", "after", "reason", "source")


def journal_entry(entry: dict) -> dict:
    event = str(entry.get("event", ""))
    try:
        text = cli.describe(entry)
    except (KeyError, TypeError, ValueError, AttributeError):
        text = str(entry.get("error") or entry.get("reason") or "")
    out = {"time": entry.get("time"), "event": event, "text": text, "kind": event_kind(event, entry)}
    reason = entry.get("reason")
    if event in ("exposure", "mic-gain", "change") and isinstance(reason, str):
        out["reason"] = reason[:240]
    if event == "change":
        out["change"] = {k: entry.get(k) for k in CHANGE_FIELDS}
    if event == "ai":
        out["persona"], out["slot"] = entry.get("persona"), entry.get("slot")
    return out


def read_journal(path: Path, limit: int = JOURNAL_LIMIT) -> list[dict]:
    """The newest auto-mode journal entries, newest first, without reading a long file whole."""
    try:
        with open(path, "rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - JOURNAL_TAIL_BYTES))
            raw = handle.read()
            partial = size > JOURNAL_TAIL_BYTES
    except OSError:
        return []
    lines = raw.decode("utf-8", "replace").splitlines()
    if partial and lines:
        lines = lines[1:]  # the first line of a tail is cut off
    entries = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(journal_entry(entry) | {"proposal": entry.get("proposal")})
        if len(entries) >= limit:
            break
    return entries


def last_note(entries: list[dict]) -> tuple[str | None, str | None]:
    """(note, time) of the newest Claude review in the journal entries."""
    for entry in entries:
        if entry["event"] == "ai":
            proposal = entry.get("proposal")
            notes = proposal.get("notes") if isinstance(proposal, dict) else None
            return (str(notes) if notes else entry["text"] or None), entry["time"]
    return None, None


def public_entries(entries: list[dict]) -> list[dict]:
    return [
        {k: e[k] for k in ("time", "event", "text", "kind", "reason", "change", "persona", "slot") if k in e}
        for e in entries
    ]


def probe_studio(cfg: dict[str, str]) -> dict:
    """The studio facts that need subprocesses; run off the HTTP threads."""
    running = cli.studio_running(cfg)
    camera = cfg["STREAMSTACK_CAMERA_DEVICE"]
    bridge = system.user_unit_active(cli.CANON_UNIT) or (
        config.camera_external(cfg) and bool(system.loopback_has_producer(camera))
    )
    sources = system.audio_sources()
    return {
        "running": running,
        "camera_for_calls": cli.call_camera(cfg, running),
        "bridge": bool(bridge),
        "voice_chain": system.VOICE_SOURCE in sources,
        # Re-read every cycle: the card's modes follow the HDMI source, so a camera switched on or to 60 fps shows up.
        "camlink": camlink.probe(cfg, sources).public(),
    }


def run_cli(args: list[str], timeout: float = 240) -> tuple[int, str]:
    """Run `streamstack <args>` as a child; returns (exit code, output without colour codes)."""
    # The child must find this very package, installed or not (`make install` trees are not on sys.path).
    root = str(Path(__file__).resolve().parents[2])
    env = dict(os.environ, NO_COLOR="1", PYTHONPATH=os.pathsep.join(filter(None, [root, os.environ.get("PYTHONPATH")])))
    try:
        result = subprocess.run(
            [sys.executable, "-m", "streamstack", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, f"streamstack {' '.join(args)} took longer than {timeout:g} s"
    except OSError as exc:
        return 127, f"could not run streamstack: {exc}"
    return result.returncode, ANSI.sub("", result.stdout + result.stderr).strip()


def console_checks(cfg: dict[str, str]) -> list[dict[str, str]]:
    """The doctor checks for the Console. The microphone stall check is left out: the audio controller
    watches it continuously and the Console merges its live answer into the health list. The hardware rows come
    from the hardware controller's own probe, also merged by the Console."""
    return cli.run_checks(cfg, stall_probe=False, hardware=False)


def tail(output: str, lines: int = 2) -> str:
    return " ".join(output.splitlines()[-lines:]) if output else ""


def _check_app(params: dict) -> str:
    name = params.get("name")
    if not isinstance(name, str) or name not in APPS:
        raise ActionError(f"name must be one of: {', '.join(APPS)}.", 400)
    return name


# ---------------------------------------------------------------------------
# The real rig
# ---------------------------------------------------------------------------


class StudioController:
    def __init__(
        self,
        cfg: dict[str, str] | None = None,
        *,
        probe_seconds: float = 3.0,
        health_seconds: float = 30.0,
        runner: Runner = run_cli,
        probe: Callable[[dict[str, str]], dict] = probe_studio,
        checks: Callable[[dict[str, str]], list[dict[str, str]]] = console_checks,
    ) -> None:
        self.cfg = cfg or config.load_settings()
        self.notify: Notify = _no_notify
        self.probe_seconds, self.health_seconds = probe_seconds, health_seconds
        self._runner, self._probe, self._checks = runner, probe, checks
        self._lock = threading.Lock()
        self._busy = {"studio": False, "auto": False}
        self._studio: dict = {
            "running": None,
            "camera_for_calls": None,
            "bridge": None,
            "voice_chain": None,
            "camlink": None,
        }
        self._auto: dict = {"running": None, "can_undo": False, "last_note": None, "last_time": None, "journal": []}
        self._health: dict = {"updated": None, "summary": None, "checks": []}
        self._virtual: dict = {"seen": []}
        self._director = director.DirectorPanel(self.cfg)
        self._cpu: float | None = None
        self._cpu_last = cpu_times()
        self._journal_key: tuple[float, int] | None = None
        self._probe_due = True
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._health_wake = threading.Event()
        self._threads: list[threading.Thread] = []

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        if self._threads:
            return
        for target, name in ((self._loop, "console-studio"), (self._health_loop, "console-health")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._health_wake.set()
        for thread in self._threads:
            thread.join(timeout=2)
        self._threads.clear()

    def _guarded(self, step: Callable[[], None]) -> None:
        try:
            step()
        except Exception:
            print("streamstack console: studio probe failed", file=sys.stderr)
            traceback.print_exc()

    def _loop(self) -> None:
        last_probe = 0.0
        while not self._stop.is_set():
            self._wake.clear()
            now = time.monotonic()
            self._guarded(self._sample_cpu)
            self._guarded(self._read_journal)
            if now - last_probe >= self.probe_seconds or self._probe_due:
                self._probe_due = False
                last_probe = now
                self._guarded(self._refresh_studio)
            self._wake.wait(1.0)

    def _health_loop(self) -> None:
        while not self._stop.is_set():
            self._health_wake.clear()
            self._guarded(self._refresh_health)
            self._health_wake.wait(self.health_seconds)

    def _sample_cpu(self) -> None:
        now = cpu_times()
        with self._lock:
            if now and self._cpu_last:
                self._cpu = cpu_percent(self._cpu_last, now)
            self._cpu_last = now

    def _read_journal(self) -> None:
        path = config.state_dir() / "auto.jsonl"
        try:
            stat = path.stat()
            key: tuple[float, int] | None = (stat.st_mtime, stat.st_size)
        except OSError:
            key = None
        if key is not None and key == self._journal_key:
            return
        entries = read_journal(path)
        note, when = last_note(entries)
        with self._lock:
            self._journal_key = key
            self._auto.update(journal=public_entries(entries), last_note=note, last_time=when)

    def _refresh_studio(self) -> None:
        facts = self._probe(self.cfg)
        try:
            from streamstack import auto

            can_undo = bool(auto.load_baseline())
        except ImportError:
            can_undo = False
        running = system.user_unit_active(cli.AUTO_UNIT)
        virtual = virtualdevices.snapshot(self.cfg)
        with self._lock:
            self._virtual = virtual
            self._studio.update(facts)
            self._auto.update(running=running, can_undo=can_undo)

    def _refresh_health(self) -> None:
        try:
            checks = self._checks(self.cfg)
        except Exception as exc:  # a probe crashing is itself a finding, not a reason to blank the page
            checks = [
                {
                    "id": "doctor",
                    "group": "apps",
                    "state": "fail",
                    "title": "The health checks failed to run",
                    "detail": str(exc),
                    "hint": "run `streamstack doctor` in a terminal",
                }
            ]
        with self._lock:
            self._health = {"updated": now_iso(), "summary": cli.summarize(checks), "checks": checks}

    # -- state ----------------------------------------------------------------

    def state(self) -> dict:
        try:
            load: list[float] | None = [round(v, 2) for v in os.getloadavg()]
        except OSError:
            load = None
        director_state = director.safe_state(self._director)
        with self._lock:
            studio = dict(
                self._studio,
                busy=self._busy["studio"],
                program_label=self.cfg["STREAMSTACK_PROGRAM_LABEL"],
                camera_label=self.cfg["STREAMSTACK_CAMERA_LABEL"],
            )
            auto = dict(
                self._auto,
                busy=self._busy["auto"],
                backend=self.cfg["STREAMSTACK_AUTO_BACKEND"],
                director=director_state,
            )
            health = dict(self._health)
            system_state = {"cpu_percent": self._cpu, "load": load, "cores": os.cpu_count() or 1}
            virtual = dict(self._virtual)
        return {"studio": studio, "auto": auto, "health": health, "system": system_state, "virtual": virtual}

    # -- actions --------------------------------------------------------------

    def sheet(self) -> bytes | None:
        """The last contact sheet Claude was shown, for the Auto page."""
        return self._director.sheet()

    def act(self, action: str, params: dict) -> str | None:
        if self._director.handles(action):
            return self._director.act(action, params)
        if action == "studio.start":
            return self._job(
                "studio",
                [["studio"]],
                "Starting the studio...",
                lambda code, out: ("ok", "Studio started") if code == 0 else ("error", tail(out) or "Studio failed"),
            )
        if action == "studio.stop":
            return self._job(
                "studio",
                [["stop"]],
                "Stopping the studio...",
                lambda code, out: ("ok", "Studio stopped") if code == 0 else ("error", tail(out) or "Stop failed"),
            )
        if action == "auto.on":
            return self._job(
                "auto",
                [["auto", "on"]],
                "Turning auto mode on...",
                lambda code, out: ("ok", "Auto mode on") if code == 0 else ("error", tail(out) or "Auto mode failed"),
            )
        if action == "auto.off":
            return self._job(
                "auto",
                [["auto", "off"]],
                "Turning auto mode off...",
                lambda code, out: ("ok", "Auto mode off") if code == 0 else ("error", tail(out)),
            )
        if action == "auto.once":
            return self._job("auto", [["auto", "once"]], "Asking Claude...", self._once_report)
        if action == "auto.reset":
            # Reset refuses to run while auto mode would change things again.
            steps = [["auto", "off"]] if self._auto["running"] else []
            return self._job(
                "auto",
                [*steps, ["auto", "reset"]],
                "Undoing auto changes...",
                lambda code, out: ("ok", "Auto changes undone") if code == 0 else ("warn", tail(out)),
            )
        if action == "health.refresh":
            self._health_wake.set()
            self._wake.set()
            return "Checking..."
        if action == "open.app":
            return self._open(_check_app(params))
        raise ActionError(f"Unknown action {action!r}.", 400)

    def _once_report(self, code: int, output: str) -> tuple[str, str]:
        if code != 0:
            return "error", tail(output) or "Claude gave no proposal"
        self._read_journal()
        note = self._auto["last_note"]
        return "ok", f"Claude: {note}" if note else "Claude reviewed the picture"

    def _open(self, name: str) -> str:
        if not system.have(name):
            raise ActionError(f"{name} is not installed.", 409)
        system.detach([name])
        return f"Opening {name}..."

    def _job(self, kind: str, steps: list[list[str]], message: str, report: Report) -> str:
        with self._lock:
            if self._busy[kind]:
                raise ActionError("Still working on the last request.", 409)
            self._busy[kind] = True
        threading.Thread(target=self._work, args=(kind, steps, report), name=f"console-{kind}", daemon=True).start()
        return message

    def _work(self, kind: str, steps: list[list[str]], report: Report) -> None:
        code, output = 0, ""
        try:
            for step in steps:
                code, output = self._runner(step)
                if code != 0:
                    break
            level, text = report(code, output)
        except Exception as exc:  # the toast must still say something, and busy must clear
            traceback.print_exc()
            level, text = "error", f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                self._busy[kind] = False
            self._probe_due = True
            self._wake.set()
            self._health_wake.set()
        self.notify(level, text)


# ---------------------------------------------------------------------------
# The simulated rig
# ---------------------------------------------------------------------------


class DemoStudio:
    """A believable rig that never touches the machine: actions flip state after `delay` seconds."""

    def __init__(self, delay: float = 1.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.notify: Notify = _no_notify
        self.delay = delay
        self._clock = clock
        self._lock = threading.Lock()
        self._busy = {"studio": False, "auto": False}
        self._running = True
        self._auto_on = False
        self._can_undo = True
        self._checked = now_iso()
        self._journal = self._sample_journal()
        self._journal_total = len(self._journal)
        self._director = director.DemoPanel()
        self._t0 = clock()

    @staticmethod
    def _sample_journal() -> list[dict]:
        base = time.time()

        def at(ago: float) -> str:
            return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(base - ago))

        def change(
            ago: float,
            actuator: str,
            kind: str,
            key: str,
            before: object,
            after: object,
            reason: str,
            target: str = "Canon EOS 50D",
            source: str = "claude",
        ) -> dict:
            return {
                "time": at(ago),
                "event": "change",
                "text": f"{key}: {before} -> {after} ({reason})",
                "kind": kind,
                "reason": reason,
                "change": {
                    "actuator": actuator,
                    "target": target,
                    "key": key,
                    "before": before,
                    "after": after,
                    "reason": reason,
                    "source": source,
                },
            }

        warm = "Skin read cooler than the persona's warmth, so warmth moved up one small step."
        return [
            {
                "time": at(40),
                "event": "mic-gain",
                "text": "+0.0 dB -> +1.5 dB",
                "kind": "audio",
                "reason": "Speech was quiet against the room.",
            },
            change(120, "litra", "video", "kelvin", 5600, 5300, LIGHT_REASON, "Litra Beam 1", "persona"),
            change(150, "canon-exposure", "video", "exposure_comp", 0.0, 0.3, CAMERA_REASON, source="local"),
            change(170, "interface-gain", "audio", "gain_db", 30.0, 32.0, GAIN_REASON, "gain.1", "local"),
            change(190, "program-look", "video", "warmth", 0.0, 0.12, warm),
            {
                "time": at(191),
                "event": "ai",
                "text": "(0.8) Face slightly under-exposed; lifted gamma.",
                "kind": "ai",
                "persona": "daylight-natural",
                "slot": "morning",
            },
            {
                "time": at(260),
                "event": "exposure",
                "text": "Canon EOS 50D: frame luma 96",
                "kind": "video",
                "reason": "The frame was 96 luma, below the 100-140 band for Daylight natural",
            },
            {"time": at(600), "event": "ai-skipped", "text": "no camera with the StreamStack filters", "kind": "ai"},
            {"time": at(900), "event": "mic-gain", "text": "-1.0 dB -> +0.0 dB", "kind": "audio"},
        ]

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    # -- state ----------------------------------------------------------------

    def _checks(self) -> list[dict]:
        def item(group: str, cid: str, state: str, title: str, detail: str = "", hint: str = "") -> dict:
            return cli.check(group, cid, state, title, detail, hint)

        live = self._running
        return [
            item("video", "camera-node", "ok", "Canon EOS 50D (/dev/video42) is live", "1920x1080 YUYV @ 30 fps"),
            item(
                "video",
                "program-node",
                "ok" if live else "warn",
                "StreamStack Camera (/dev/video43) is live"
                if live
                else "StreamStack Camera (/dev/video43) has no picture yet",
                "1920x1080 YUYV @ 30 fps" if live else "",
                "" if live else "start it: streamstack studio",
            ),
            item(
                "video",
                "camlink",
                "ok",
                "Elgato Cam Link on /dev/video0: 1080p59.94 YUYV",
                "offers 1080p59.94 YUYV, 1080p29.97 YUYV",
            ),
            item(
                "video",
                "encoder",
                "ok",
                "The program camera encodes on the GPU (VA-API)",
                "av1_ffmpeg_vaapi_tex: /dev/dri/renderD128, Intel iHD driver, low-power mode",
            ),
            item(
                "video",
                "background-blur",
                "ok",
                "Background blur is available",
                "OpenVINO (Arc GPU)",
            ),
            item(
                "audio",
                "camlink-audio",
                "ok",
                "Cam Link HDMI audio is the camera's sound, not a call microphone",
                "Cam Link 4K Digital Stereo",
            ),
            item("audio", "microphone", "ok", "call microphone", "Wave:3"),
            item("audio", "voice-chain", "ok", "StreamStack Mic (processed voice) is available"),
            item("apps", "app-qpwgraph", "ok", "PipeWire patchbay", "installed"),
            item("apps", "zoom", "warn", "Zoom desktop client", "not installed (zoom.us web client works too)"),
            item(
                "apps", "auto-mode", "ok", "auto mode", f"{'on' if self._auto_on else 'off'} (streamstack auto on|off)"
            ),
        ]

    def state(self) -> dict:
        elapsed = self._clock() - self._t0
        with self._lock:
            checks = self._checks()
            journal = [dict(e) for e in self._journal[:JOURNAL_LIMIT]]
            note, when = next(((e["text"], e["time"]) for e in journal if e["event"] == "ai"), (None, None))
            return {
                "studio": {
                    "running": self._running,
                    "busy": self._busy["studio"],
                    "camera_for_calls": "graded" if self._running else "raw",
                    "program_label": "StreamStack Camera",
                    "camera_label": "Canon EOS 50D",
                    "bridge": True,
                    "voice_chain": True,
                    "camlink": {
                        "present": True,
                        "device": "/dev/video0",
                        "signal": True,
                        "state": "ok",
                        "mode": "1080p59.94 YUYV",
                        "offered": ["1080p59.94 YUYV", "1080p29.97 YUYV"],
                        "active": "1920x1080 YUYV @ 59.94 fps",
                        "audio": "Cam Link 4K Digital Stereo",
                        "note": "",
                    },
                },
                "auto": {
                    "running": self._auto_on,
                    "backend": "claude-cli",
                    "can_undo": self._can_undo,
                    "busy": self._busy["auto"],
                    "last_note": note,
                    "last_time": when,
                    "journal": journal,
                    "journal_total": self._journal_total,  # demo only: the journal list is capped, this keeps counting
                    "director": director.safe_state(self._director),
                },
                "health": {"updated": self._checked, "summary": cli.summarize(checks), "checks": checks},
                "virtual": {
                    "camera": {
                        "label": "StreamStack Camera",
                        "device": "/dev/video43",
                        "present": True,
                        "listed": True,
                        "access": True,
                        "producer": self._running,
                    },
                    "mic": {"label": "StreamStack Mic", "present": True, "default": False},
                    "seen": [
                        {"kind": "camera", "app": "Teams", "pid": 4242, "via": "v4l2"},
                        {"kind": "mic", "app": "Teams", "pid": 4242, "via": "pipewire"},
                    ]
                    if self._running
                    else [],
                },
                "system": {
                    "cpu_percent": round(max(2.0, 24 + 9 * math.sin(elapsed / 4.0) + 4 * math.sin(elapsed * 1.7)), 1),
                    "load": [0.8, 0.7, 0.6],
                    "cores": 8,
                },
            }

    # -- actions --------------------------------------------------------------

    def sheet(self) -> bytes | None:
        return self._director.sheet()

    def act(self, action: str, params: dict) -> str | None:
        if self._director.handles(action):
            return self._director.act(action, params)
        if action == "studio.start":
            return self._later("studio", "Starting the studio...", "Studio started", self._set_running, True)
        if action == "studio.stop":
            return self._later("studio", "Stopping the studio...", "Studio stopped", self._set_running, False)
        if action == "auto.on":
            return self._later("auto", "Turning auto mode on...", "Auto mode on", self._set_auto, True)
        if action == "auto.off":
            return self._later("auto", "Turning auto mode off...", "Auto mode off", self._set_auto, False)
        if action == "auto.once":
            return self._later("auto", "Asking Claude...", "Claude reviewed the picture", self._add_review)
        if action == "auto.reset":
            return self._later("auto", "Undoing auto changes...", "Auto changes undone", self._reset)
        if action == "health.refresh":
            with self._lock:
                self._checked = now_iso()
            return "Checking..."
        if action == "open.app":
            return f"Opening {_check_app(params)} (demo: nothing is launched)."
        raise ActionError(f"Unknown action {action!r}.", 400)

    def _later(self, kind: str, message: str, done: str, change: Callable[..., None], *args: object) -> str:
        with self._lock:
            if self._busy[kind]:
                raise ActionError("Still working on the last request.", 409)
            self._busy[kind] = True

        def finish() -> None:
            with self._lock:
                change(*args)
                self._busy[kind] = False
            self.notify("ok", done)

        if self.delay <= 0:
            finish()
        else:
            timer = threading.Timer(self.delay, finish)
            timer.daemon = True
            timer.start()
        return message

    def _set_running(self, running: bool) -> None:
        self._running = running

    def _set_auto(self, on: bool) -> None:
        self._auto_on = on
        self._push("start" if on else "stop", "auto mode started" if on else "auto mode stopped", "ai")

    def _add_review(self) -> None:
        self._can_undo = True
        reason = "Skin read cooler than the persona's warmth, so warmth moved up one small step."
        self._push(
            "change",
            f"warmth: 0.0 -> 0.12 ({reason})",
            "video",
            reason=reason,
            change={
                "actuator": "program-look",
                "target": "Canon EOS 50D",
                "key": "warmth",
                "before": 0.0,
                "after": 0.12,
                "reason": reason,
                "source": "claude",
            },
        )
        self._push(
            "ai",
            "(0.9) Looks well exposed; nudged warmth up a touch.",
            "ai",
            persona=self._director.chosen(),
            slot=self._director.slot(),
        )

    def _reset(self) -> None:
        self._can_undo = False
        self._push("reset", "restored the look, mic gain and voice preset", "ai")

    def _push(self, event: str, text: str, kind: str, **extra: object) -> None:
        self._journal.insert(0, {"time": now_iso(), "event": event, "text": text, "kind": kind, **extra})
        self._journal_total += 1
