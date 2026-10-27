"""Is a call in progress? One small public answer for everything that must behave differently during a call.

This is the shared primitive of the "Meeting-ready autopilot" features: talking-while-muted (`console.call`), Safe join,
the echo check (never plays a chirp during a call), Pre-flight (never opens the camera under a call app) and the
network monitor (never runs an uplink test unasked). They all ask the same question and get the same answer.

    from streamstack.callstate import CallState, CallMonitor, current_call

    snap = current_call(cfg)            # one look right now, no hysteresis: fine for a CLI or a one-shot guard
    snap.state, snap.service, snap.active   # CallState.IN_CALL, "Teams", True

    monitor = CallMonitor(cfg)          # a long-running owner: steady states, subscribe to changes
    monitor.start()
    monitor.snapshot()                  # the latest CallSnapshot (cheap, thread safe)
    monitor.subscribe(lambda before, after: ...)

Inside the Console the controller owns the monitor: `rig.call.snapshot()` and `rig.call.subscribe(cb)`.

What it looks at (the same two consumers the live-desk checks use, `pactl list source-outputs` and `fuser /dev/video4x`)
------------------------------------------------------------------------------------------------------------------
* **Video**: the processes that hold the program camera (or the raw camera) open, from `/proc/<pid>/fd` symlinks. This
  is `camerause.ProcScanner`: it never opens a /dev/video node, so it cannot steal, wake or reconfigure the camera.
* **Audio**: the capture streams (PipeWire's `source-outputs`) of known call apps, from `pactl -f json list
  source-outputs`: only the application name, process, corked and mute flags. **It never opens or records a stream and
  never reads call audio**; the microphone content it hears elsewhere is StreamStack's own meter on the raw mic.
* **Window titles** (optional hint): `wmctrl -lp` on X11 names the service behind a browser tab ("Google Meet"). With
  none (Wayland, no wmctrl) the browser's own name is the service.

Only known call apps count (Teams, Zoom, Slack, Discord, Skype, Webex and the browsers): OBS, parec, ffmpeg,
Easy Effects and StreamStack's own meters never make a call.

States
------
* `idle`: no call app has the camera or a capture stream (for `LEAVE_HOLD_S`: browsers drop and reopen streams).
* `joining`: a call app has just opened the camera or the microphone (the first `settle_s`), or has the camera and no
  microphone stream at all.
* `in_call`: a call app has held a capture stream for `settle_s`.

A pre-join lobby holds the same devices as a call, so the two cannot be told apart without the app's help; both are
"call active" (`CallSnapshot.active`), which is what every consumer needs.

What the microphone state means (`CallSnapshot.mic`)
----------------------------------------------------
`muted` when every capture stream of the call apps is corked or muted, or there is none, or an adapter reported it
(`ExternalMute`); `open` when a stream is live. Browsers keep the stream open while the page is muted, so `open` means
"not known to be muted", and the optional adapter closes that gap.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from streamstack import camerause, system

log = logging.getLogger(__name__)

POLL_S = 1.0  # how often the monitor looks
SETTLE_S = 4.0  # a capture stream must have been held this long for the call to count as "in call"
LEAVE_HOLD_S = 3.0  # evidence must be gone this long before the call is over (browsers reopen streams)
EXTERNAL_FRESH_S = 15.0  # an adapter's mute report older than this is stale and ignored
PACTL_TIMEOUT_S = 3.0
WMCTRL_TIMEOUT_S = 2.0


class CallState(StrEnum):
    IDLE = "idle"
    JOINING = "joining"
    IN_CALL = "in_call"


class MicState(StrEnum):
    NONE = "none"  # no call: nothing to say
    OPEN = "open"  # a live capture stream (not known to be muted)
    MUTED = "muted"  # corked, muted, absent or reported muted by an adapter


# ---------------------------------------------------------------------------
# Which processes are call apps
# ---------------------------------------------------------------------------

# (needle in the lower-cased binary or application name, the name a person recognises, is it a browser)
CALL_APPS: tuple[tuple[str, str, bool], ...] = (
    ("ms-teams", "Teams", False),
    ("teams-for-linux", "Teams", False),
    ("teams", "Teams", False),
    ("zoom", "Zoom", False),
    ("slack", "Slack", False),
    ("discord", "Discord", False),
    ("skype", "Skype", False),
    ("webex", "Webex", False),
    ("ciscocollabhost", "Webex", False),
    ("msedge", "Edge", True),
    ("microsoft-edge", "Edge", True),
    ("chromium", "Chromium", True),
    ("chrome", "Chrome", True),
    ("firefox", "Firefox", True),
    ("brave", "Brave", True),
    ("vivaldi", "Vivaldi", True),
    ("opera", "Opera", True),
)

# Window-title hints that name the service behind a browser tab.
TITLE_SERVICES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bgoogle meet\b|meet\.google\.com|^meet\b|\bmeet\s[-–—]", re.I), "Google Meet"),
    (re.compile(r"\bmicrosoft teams\b|\bteams\.microsoft\b|\bteams\.live\b", re.I), "Teams"),
    (re.compile(r"\bzoom\b", re.I), "Zoom"),
    (re.compile(r"\bwebex\b", re.I), "Webex"),
    (re.compile(r"\bslack\b.*\bhuddle\b|\bhuddle\b.*\bslack\b", re.I), "Slack"),
)


@dataclass(frozen=True)
class AppName:
    label: str
    browser: bool


def app_name(*names: str | None) -> AppName | None:
    """The call app a process or application name belongs to, or None when it is not one (OBS, parec, ffmpeg...)."""
    for raw in names:
        lowered = (raw or "").lower()
        if not lowered or lowered.startswith("streamstack"):
            continue
        for needle, label, browser in CALL_APPS:
            if needle in lowered:
                return AppName(label, browser)
    return None


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaptureStream:
    """One capture stream of a call app, as `pactl list source-outputs` describes it. Metadata only."""

    app: str
    process: str
    pid: int | None
    corked: bool
    muted: bool

    @property
    def live(self) -> bool:
        return not (self.corked or self.muted)


@dataclass(frozen=True)
class AppEvidence:
    """What one call app is holding right now: the camera, capture streams, windows."""

    app: str
    browser: bool
    camera: bool = False
    streams: tuple[CaptureStream, ...] = ()
    titles: tuple[str, ...] = ()

    @property
    def service(self) -> str:
        """The service named by a window title, else the app itself."""
        for title in self.titles:
            for pattern, service in TITLE_SERVICES:
                if pattern.search(title):
                    return service
        return self.app


@dataclass(frozen=True)
class ExternalMute:
    """The true mute state of a call app, reported by an adapter (the optional companion extension, a Teams bridge)."""

    muted: bool
    source: str
    at: float  # monotonic time of the report


def _int(value: object) -> int | None:
    text = str(value if value is not None else "").strip()
    return int(text) if text.isdigit() else None


def parse_source_outputs(text: str) -> list[CaptureStream]:
    """The capture streams of known call apps in `pactl -f json list source-outputs` (empty for anything unreadable).

    Monitor captures (screen-share audio: `stream.capture.sink`) are not microphones and are skipped."""
    try:
        data: Any = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    found: list[CaptureStream] = []
    for obj in data:
        if not isinstance(obj, dict):
            continue
        props = obj.get("properties")
        props = props if isinstance(props, dict) else {}
        if str(props.get("stream.capture.sink", "")).lower() == "true":
            continue
        binary = str(props.get("application.process.binary") or "")
        name = app_name(binary, str(props.get("application.name") or ""))
        if name is None:
            continue
        found.append(
            CaptureStream(
                app=name.label,
                process=binary or str(props.get("application.name") or ""),
                pid=_int(props.get("application.process.id")),
                corked=bool(obj.get("corked")),
                muted=bool(obj.get("mute")),
            )
        )
    return found


def read_capture_streams(runner: Callable[..., Any] = system.run) -> tuple[list[CaptureStream], bool]:
    """(the call apps' capture streams, whether the audio server could be asked)."""
    result = runner(["pactl", "-f", "json", "list", "source-outputs"], timeout=PACTL_TIMEOUT_S)
    if result is None or result.returncode != 0:
        return [], False
    return parse_source_outputs(result.stdout), True


def read_window_titles(
    runner: Callable[..., Any] = system.run, proc: Path = camerause.PROC
) -> dict[str, tuple[str, ...]]:
    """Window titles by app label from `wmctrl -lp` (X11 and XWayland windows). Empty when wmctrl is missing."""
    result = runner(["wmctrl", "-lp"], timeout=WMCTRL_TIMEOUT_S)
    if result is None or result.returncode != 0:
        return {}
    titles: dict[str, list[str]] = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 4)  # id desktop pid host title
        if len(parts) < 5 or not parts[2].isdigit():
            continue
        comm = camerause._read(proc / parts[2] / "comm")
        name = app_name(comm)
        if name is not None:
            titles.setdefault(name.label, []).append(parts[4])
    return {label: tuple(items) for label, items in titles.items()}


def merge_evidence(
    camera: Iterable[camerause.Consumer],
    streams: Iterable[CaptureStream],
    titles: Mapping[str, Sequence[str]] | None = None,
) -> list[AppEvidence]:
    """One entry per call app that holds the camera or a capture stream (a browser's many processes are one app)."""
    by_app: dict[str, dict[str, Any]] = {}
    for consumer in camera:
        name = app_name(consumer.comm)
        if name is None:
            continue
        entry = by_app.setdefault(name.label, {"browser": name.browser, "camera": False, "streams": []})
        entry["camera"] = True
    for stream in streams:
        name = app_name(stream.process, stream.app)
        entry = by_app.setdefault(stream.app, {"browser": bool(name and name.browser), "camera": False, "streams": []})
        entry["streams"].append(stream)
    return [
        AppEvidence(
            app=label,
            browser=entry["browser"],
            camera=entry["camera"],
            streams=tuple(entry["streams"]),
            titles=tuple((titles or {}).get(label, ())),
        )
        for label, entry in sorted(by_app.items())
    ]


@dataclass(frozen=True)
class Gathered:
    apps: list[AppEvidence]
    audio_ok: bool  # the audio server answered
    video_ok: bool  # /proc could be scanned


def gather(
    cfg: Mapping[str, str],
    *,
    scanner: camerause.ProcScanner | None = None,
    runner: Callable[..., Any] = system.run,
    titles: Callable[[], Mapping[str, Sequence[str]]] | None = None,
) -> Gathered:
    """Look once: who holds the camera, who captures audio, what their windows say. Read only; never raises."""
    devices = camerause.watched_devices(dict(cfg))
    video_ok = True
    consumers: list[camerause.Consumer] = []
    try:
        consumers = (scanner or camerause.ProcScanner()).scan(devices).consumers if devices else []
    except OSError as exc:
        log.warning("call state: cannot scan /proc: %s", exc)
        video_ok = False
    streams, audio_ok = read_capture_streams(runner)
    hints: Mapping[str, Sequence[str]] = {}
    if consumers or streams:  # titles are only worth a process spawn when something might be a call
        hints = titles() if titles is not None else read_window_titles(runner)
    return Gathered(merge_evidence(consumers, streams, hints), audio_ok, video_ok)


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CallSnapshot:
    """The answer. `since` is the monotonic time the current state began (None for a monitor that has not looked)."""

    state: CallState = CallState.IDLE
    service: str | None = None
    app: str | None = None
    apps: tuple[str, ...] = ()
    camera: bool = False
    mic: MicState = MicState.NONE
    since: float | None = None
    reason: str = ""
    external: ExternalMute | None = None

    @property
    def active(self) -> bool:
        """A call is on or starting: the camera or microphone is held by a call app."""
        return self.state is not CallState.IDLE

    @property
    def in_call(self) -> bool:
        return self.state is CallState.IN_CALL

    @property
    def muted(self) -> bool:
        return self.mic is MicState.MUTED

    def public(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "active": self.active,
            "service": self.service,
            "app": self.app,
            "apps": list(self.apps),
            "camera": self.camera,
            "mic": self.mic.value,
            "reason": self.reason,
        }


def _join(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _mic_state(apps: Sequence[AppEvidence], external: ExternalMute | None) -> MicState:
    if external is not None:
        return MicState.MUTED if external.muted else MicState.OPEN
    streams = [s for a in apps for s in a.streams]
    return MicState.OPEN if any(s.live for s in streams) else MicState.MUTED


def _reason(state: CallState, apps: Sequence[AppEvidence], service: str | None) -> str:
    if state is CallState.IDLE:
        return ""
    holds = []
    if any(a.camera for a in apps):
        holds.append("the camera")
    if any(a.streams for a in apps):
        holds.append("a microphone stream")
    names = [a.app for a in apps]
    who = _join(names)
    if service and service not in names:
        who = f"{service} in {who}"
    return f"{who} has {' and '.join(holds)} open"


class CallDetector:
    """Evidence in, steady `CallSnapshot` out. Pure: the clock and the evidence are arguments, nothing is read here."""

    def __init__(
        self, *, settle_s: float = SETTLE_S, leave_s: float = LEAVE_HOLD_S, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.settle_s, self.leave_s, self._clock = settle_s, leave_s, clock
        self._snap = CallSnapshot()
        self._capture_since: float | None = None
        self._gone_since: float | None = None

    @property
    def snapshot(self) -> CallSnapshot:
        return self._snap

    def update(
        self, apps: Sequence[AppEvidence], external: ExternalMute | None = None, now: float | None = None
    ) -> CallSnapshot:
        now = self._clock() if now is None else now
        if external is not None and now - external.at > EXTERNAL_FRESH_S:
            external = None  # a stale report must not outlive the adapter that sent it
        previous = self._snap
        if not apps:
            return self._quiet(previous, now)
        self._gone_since = None
        if any(a.streams for a in apps):
            self._capture_since = self._capture_since if self._capture_since is not None else now
        else:
            self._capture_since = None
        held = self._capture_since is not None and now - self._capture_since >= self.settle_s
        state = CallState.IN_CALL if (held or previous.state is CallState.IN_CALL) else CallState.JOINING
        primary = next((a for a in apps if a.streams), apps[0])
        service = primary.service
        since = previous.since if previous.state is state and previous.since is not None else now
        self._snap = CallSnapshot(
            state=state,
            service=service,
            app=primary.app,
            apps=tuple(a.app for a in apps),
            camera=any(a.camera for a in apps),
            mic=_mic_state(apps, external),
            since=since,
            reason=_reason(state, apps, service),
            external=external,
        )
        return self._snap

    def _quiet(self, previous: CallSnapshot, now: float) -> CallSnapshot:
        if previous.state is CallState.IDLE:
            return previous
        if self._gone_since is None:
            self._gone_since = now
        if now - self._gone_since < self.leave_s:
            return previous  # a browser closing one stream and opening the next is not the end of the call
        self._capture_since = self._gone_since = None
        self._snap = CallSnapshot(since=now)
        return self._snap


def current_call(cfg: Mapping[str, str], **sources: Any) -> CallSnapshot:
    """One look right now, without the steadying a monitor adds: any evidence is a call (state `joining` unless a
    capture stream and the camera are both open). Good for a guard in a CLI command or a one-shot check."""
    found = gather(cfg, **sources)
    detector = CallDetector(settle_s=0.0, leave_s=0.0)
    return detector.update(found.apps, None, now=0.0)


# ---------------------------------------------------------------------------
# The monitor
# ---------------------------------------------------------------------------

Listener = Callable[[CallSnapshot, CallSnapshot], None]


@dataclass
class SourceHealth:
    audio: bool = True
    video: bool = True


class CallMonitor:
    """Polls the evidence once a second on its own thread and keeps the latest `CallSnapshot`.

    `source` is injectable (the demo rig scripts it, tests fake it). Listeners hear (before, after) on every change of
    state, service or microphone state, on the monitor's thread; a listener that raises is logged, never fatal.
    """

    def __init__(
        self,
        cfg: Mapping[str, str],
        *,
        source: Callable[[], Gathered] | None = None,
        detector: CallDetector | None = None,
        clock: Callable[[], float] = time.monotonic,
        poll_s: float = POLL_S,
    ) -> None:
        self._cfg = cfg
        self._source = source or (lambda: gather(self._cfg))
        self._detector = detector or CallDetector(clock=clock)
        self._clock = clock
        self._poll_s = poll_s
        self._lock = threading.Lock()
        self._listeners: list[Listener] = []
        self._external: ExternalMute | None = None
        self._health = SourceHealth()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- reading ----------------------------------------------------------------

    def snapshot(self) -> CallSnapshot:
        with self._lock:
            return self._detector.snapshot

    def health(self) -> SourceHealth:
        with self._lock:
            return SourceHealth(self._health.audio, self._health.video)

    def external(self) -> ExternalMute | None:
        with self._lock:
            return self._external

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    # -- writing ----------------------------------------------------------------

    def report_mute(self, muted: bool | None, source: str) -> None:
        """An adapter's report of the call app's true mute state; None withdraws it."""
        with self._lock:
            self._external = None if muted is None else ExternalMute(muted, source, self._clock())

    def poll_once(self) -> CallSnapshot:
        """Look now and update the snapshot (the thread's body; tests call it directly)."""
        try:
            found = self._source()
        except Exception:  # a probe bug must never stop the monitor
            log.exception("call state probe failed")
            return self.snapshot()
        with self._lock:
            before = self._detector.snapshot
            self._health = SourceHealth(found.audio_ok, found.video_ok)
            after = self._detector.update(found.apps, self._external)
            listeners = list(self._listeners) if _changed(before, after) else []
        for listener in listeners:
            try:
                listener(before, after)
            except Exception:
                log.exception("call state listener failed")
        return after

    # -- lifecycle --------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="call-state", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=PACTL_TIMEOUT_S + 2)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self._poll_s)


def _changed(before: CallSnapshot, after: CallSnapshot) -> bool:
    return (before.state, before.service, before.mic, before.camera) != (
        after.state,
        after.service,
        after.mic,
        after.camera,
    )


__all__ = [
    "AppEvidence",
    "CallDetector",
    "CallMonitor",
    "CallSnapshot",
    "CallState",
    "CaptureStream",
    "ExternalMute",
    "Gathered",
    "MicState",
    "app_name",
    "current_call",
    "gather",
    "merge_evidence",
    "parse_source_outputs",
]
