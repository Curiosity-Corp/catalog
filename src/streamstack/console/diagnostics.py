"""Opt-in telemetry and crash reports for the Console: the `/metrics` page, the System page section behind it.

Telemetry is local first. Nothing is sent anywhere: when it is on (`STREAMSTACK_TELEMETRY=true`, or the switch on the
System page) the Console answers `GET /metrics` in the Prometheus text format to a caller that presents a token, and
that is all. When it is off the endpoint does not exist (404) and nothing is measured. The metrics are numbers about
the rig (service health, audio levels, light state, CPU and memory of StreamStack's own processes, how long
actions take), never video, audio, names of lights, meeting titles or file paths. docs/telemetry.md lists every metric.

The scrape token is separate from the Console's own (which changes on every start): it is created when telemetry is
first enabled, kept in the private secrets file (`auto.env`, mode 600) and never part of the state the windows receive.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
import time
from typing import TYPE_CHECKING, Any

from streamstack import __version__, config, crash, metrics, procstats, store
from streamstack.console.errors import ActionError

if TYPE_CHECKING:
    from streamstack.console.server import ConsoleServer

log = logging.getLogger("streamstack.telemetry")

ENABLE_KEY = "STREAMSTACK_TELEMETRY"
TOKEN_KEY = "STREAMSTACK_TELEMETRY_TOKEN"  # noqa: S105 - the name of a setting
METRICS_PATH = "/metrics"
SAMPLE_SECONDS = 0.5
LAG_BUCKETS = (0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0)
ACTION_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 15.0, 30.0)
PROCESS_CACHE_SECONDS = 5.0
STATE_OK = {"ok": 0, "warn": 1, "fail": 2}
TELEMETRY_ACTIONS = ("telemetry.set", "telemetry.rotate_token", "telemetry.scrape_config")


def light_label(light_id: str) -> str:
    """A short stable label for a light: the id itself names a USB port and serial, which a scrape does not need."""
    return hashlib.sha256(light_id.encode()).hexdigest()[:8]


class Diagnostics:
    """Telemetry switch, sampler, metrics renderer and crash-report listing (`telemetry.*`, `crash.*` actions)."""

    def __init__(self, cfg: dict[str, str] | None = None) -> None:
        cfg = cfg if cfg is not None else config.load_settings()
        self._lock = threading.RLock()  # the switch and the token
        self._data_lock = threading.Lock()  # what the sampler and the meter tap measure; never held across a join
        self._enabled = config.truthy(cfg.get(ENABLE_KEY, "false"))
        self._token: str | None = None
        self._server: ConsoleServer | None = None
        self._started = time.monotonic()
        self._running = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.scrapes = 0
        self.last_scrape: float | None = None
        # instruments
        self.action_seconds = metrics.Histogram(
            "streamstack_action_duration_seconds",
            "Time the Console took to run an action.",
            ("action",),
            ACTION_BUCKETS,
        )
        self.actions = metrics.Counter("streamstack_actions_total", "Actions run, by outcome.", ("action", "outcome"))
        self.action_errors = metrics.Counter(
            "streamstack_action_errors_total", "Actions that failed, by kind.", ("action", "kind")
        )
        self.http_errors = metrics.Counter(
            "streamstack_http_errors_total", "Requests the Console answered with a server error.", ("status",)
        )
        self.clips = metrics.Counter("streamstack_audio_clip_events_total", "Audio clips seen.", ("source",))
        self.loop_lag = metrics.Histogram(
            "streamstack_event_loop_lag_seconds",
            "How late the Console's sampler thread wakes up, a measure of how starved its threads are.",
            (),
            LAG_BUCKETS,
        )
        self._lag_max = 0.0
        self._audio: dict[str, dict[str, Any]] = {}
        self._clip_was: dict[str, bool] = {}
        self._processes: tuple[float, dict[str, procstats.Usage]] = (0.0, {})
        self._crash_cache: tuple[tuple[int, int], list[dict[str, Any]]] | None = None

    # -- wiring -------------------------------------------------------------------------------------------------

    def attach(self, server: ConsoleServer) -> None:
        """Called by the server: gives the sampler the hub's meters and the scrape URL its port."""
        self._server = server
        crash.set_state_provider(self.crash_summary)

    def start(self) -> None:
        with self._lock:
            if self._enabled:
                self._start_sampler()

    def stop(self) -> None:
        with self._lock:
            self._stop_sampler()

    def _start_sampler(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop.clear()
        if self._server is not None:
            self._server.hub.meter_listeners.append(self._on_meters)
        self._thread = threading.Thread(target=self._sample_loop, name="console-telemetry", daemon=True)
        self._thread.start()

    def _stop_sampler(self) -> None:
        if not self._running:
            return
        self._running = False
        self._stop.set()
        if self._server is not None and self._on_meters in self._server.hub.meter_listeners:
            self._server.hub.meter_listeners.remove(self._on_meters)
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    @property
    def enabled(self) -> bool:
        return self._enabled

    # -- sampling -----------------------------------------------------------------------------------------------

    def _sample_loop(self) -> None:
        while not self._stop.is_set():
            began = time.monotonic()
            if self._stop.wait(SAMPLE_SECONDS):
                return
            lag = max(0.0, time.monotonic() - began - SAMPLE_SECONDS)
            self.loop_lag.observe(lag)
            with self._data_lock:
                self._lag_max = max(self._lag_max, lag)

    def _on_meters(self, frame: dict[str, Any]) -> None:
        for source in ("mic", "voice"):
            reading = frame.get(source)
            if not isinstance(reading, dict):
                continue
            clipping = bool(reading.get("clip"))
            if clipping and not self._clip_was.get(source, False):
                self.clips.inc(source=source)
            self._clip_was[source] = clipping
            with self._data_lock:
                row = self._audio.setdefault(source, {"peak": -90.0})
                row["peak"] = max(row["peak"], float(reading.get("peak_db", -90.0)))
                row["rms"] = float(reading.get("rms_db", -90.0))
                row["lufs"] = float(reading.get("lufs_s", -90.0))
                row["latched"] = bool(reading.get("latched"))

    # -- actions the server reports -----------------------------------------------------------------------------

    def observe_action(self, action: str, seconds: float, status: int | None) -> None:
        """One action finished: `status` is None on success, else the HTTP status it ended in."""
        if not self._enabled:
            return
        name = action if isinstance(action, str) and len(action) <= 40 else "invalid"
        self.action_seconds.observe(seconds, action=name)
        outcome = "ok" if status is None else "client_error" if status < 500 else "error"
        self.actions.inc(action=name, outcome=outcome)
        if status is not None:
            self.action_errors.inc(action=name, kind=outcome)

    def observe_http_error(self, status: int) -> None:
        if self._enabled and status >= 500:
            self.http_errors.inc(status=str(status))

    # -- the token ----------------------------------------------------------------------------------------------

    def _read_token(self) -> str | None:
        if self._token is None:
            value = config.read_env_file(config.secrets_file()).get(TOKEN_KEY, "")
            self._token = value if len(value) >= 16 else None
        return self._token

    def _ensure_token(self, rotate: bool = False) -> str:
        with self._lock:
            token = None if rotate else self._read_token()
            if token is None:
                token = secrets.token_urlsafe(24)
                try:
                    store.update_env_file(config.secrets_file(), {TOKEN_KEY: token}, mode=0o600)
                except (OSError, ValueError) as exc:
                    raise ActionError(f"Could not save the scrape token: {exc}", 409) from exc
                self._token = token
            return token

    def token_ok(self, supplied: str | None) -> bool:
        """Whether `supplied` is the scrape token (the Console's own token is checked by the server)."""
        with self._lock:
            token = self._read_token()
        return bool(supplied) and token is not None and secrets.compare_digest(supplied.encode(), token.encode())

    # -- state and actions --------------------------------------------------------------------------------------

    def url(self) -> str | None:
        return f"http://127.0.0.1:{self._server.port}{METRICS_PATH}" if self._server is not None else None

    def _crashes(self) -> list[dict[str, Any]]:
        directory = crash.crash_dir()
        try:
            stamp = (directory.stat().st_mtime_ns, sum(1 for _ in directory.glob("*.json")))
        except OSError:
            return []
        if self._crash_cache is None or self._crash_cache[0] != stamp:
            self._crash_cache = (stamp, crash.list_reports())
        return self._crash_cache[1]

    def state(self) -> dict[str, Any]:
        return {
            "telemetry": {
                "enabled": self._enabled,
                "path": METRICS_PATH,
                "url": self.url(),
                "scrapes": self.scrapes,
                "last_scrape": self.last_scrape,
                "token_set": self._read_token() is not None,
            },
            "crashes": {"dir": store.short_path(crash.crash_dir()), "reports": self._crashes()},
        }

    def act(self, action: str, params: dict[str, Any]) -> str | None:
        if action == "telemetry.set":
            on = params.get("on")
            if not isinstance(on, bool):
                raise ActionError("on must be true or false.", 400)
            return self._set_enabled(on)
        if action == "telemetry.rotate_token":
            self._ensure_token(rotate=True)
            return "New scrape token created. Update the scraper's credentials."
        if action == "telemetry.scrape_config":
            return self.scrape_config()
        if action == "crash.clear":
            return f"Removed {crash.clear_reports()} crash report(s)."
        raise ActionError(f"Unknown action {action!r}.", 400)

    def _set_enabled(self, on: bool) -> str:
        with self._lock:
            if on == self._enabled:
                return "Telemetry is already " + ("on." if on else "off.")
            if on:
                self._ensure_token()
            try:
                config.set_user_setting(ENABLE_KEY, "true" if on else "false")
            except (OSError, ValueError) as exc:
                raise ActionError(f"Could not save the setting: {exc}", 409) from exc
            self._enabled = on
            if on:
                self._start_sampler()
            else:
                self._stop_sampler()
                self._reset_series()
        return "Telemetry is on. Nothing leaves this computer; /metrics answers a token." if on else "Telemetry is off."

    def _reset_series(self) -> None:
        with self._data_lock:
            self._audio.clear()
            self._clip_was.clear()
            self._lag_max = 0.0

    def scrape_config(self) -> str:
        """A Prometheus scrape_config for this Console, with the scrape token (the only place it is ever shown)."""
        if not self._enabled:
            raise ActionError("Turn telemetry on first.", 409)
        url = self.url() or f"http://127.0.0.1:PORT{METRICS_PATH}"
        host = url.removeprefix("http://").removesuffix(METRICS_PATH)
        return (
            "scrape_configs:\n"
            "  - job_name: streamstack\n"
            f"    metrics_path: {METRICS_PATH}\n"
            "    authorization:\n"
            "      type: Bearer\n"
            f"      credentials: {self._ensure_token()}\n"
            "    static_configs:\n"
            f"      - targets: ['{host}']\n"
        )

    # -- the crash summary ----------------------------------------------------------------------------------------

    def crash_summary(self) -> dict[str, Any]:
        """The allow-listed part of the Console state that goes into a crash report."""
        server = self._server
        if server is None:
            return {}
        try:
            snap = json.loads(server.hub.state_json())
        except (ValueError, TypeError):
            return {}
        if not isinstance(snap, dict):
            return {}
        video, audio, health = snap.get("video") or {}, snap.get("audio") or {}, snap.get("health") or {}
        return {
            "version": snap.get("version"),
            "demo": snap.get("demo"),
            "studio": {k: (snap.get("studio") or {}).get(k) for k in ("running", "busy", "bridge", "voice_chain")},
            "health": {
                "summary": health.get("summary"),
                "checks": [{"id": c.get("id"), "state": c.get("state")} for c in health.get("checks", [])],
            },
            "video": {
                "privacy": video.get("privacy"),
                "virtualcam": video.get("virtualcam"),
                "recording": (video.get("recording") or {}).get("active"),
                "streaming": (video.get("streaming") or {}).get("active"),
                "canon": (video.get("canon") or {}).get("state"),
            },
            "audio": {
                "mic_present": (audio.get("mic") or {}).get("present"),
                "stalled": (audio.get("mic") or {}).get("stalled"),
                "chain_running": (audio.get("chain") or {}).get("running"),
            },
            "storage_warnings": len((snap.get("storage") or {}).get("warnings", [])),
        }

    # -- the metrics --------------------------------------------------------------------------------------------

    def _process_usage(self) -> dict[str, procstats.Usage]:
        at, usage = self._processes
        now = time.monotonic()
        if now - at > PROCESS_CACHE_SECONDS:
            usage = procstats.scan(known={os.getpid(): "console"})
            self._processes = (now, usage)
        return usage

    def render(self, snapshot: dict[str, Any]) -> str:
        """The `/metrics` text for `snapshot` (a Console state dict) and everything measured since the last scrape."""
        with self._data_lock:
            self.scrapes += 1
            self.last_scrape = time.time()
            lag_max, self._lag_max = self._lag_max, 0.0
            audio = {k: dict(v) for k, v in self._audio.items()}
            for row in self._audio.values():
                row["peak"] = -90.0
        families = collect(snapshot)
        families += self._own_families(snapshot, lag_max, audio)
        families += [
            self.action_seconds.collect(),
            self.actions.collect(),
            self.action_errors.collect(),
            self.http_errors.collect(),
            self.clips.collect(),
            self.loop_lag.collect(),
        ]
        return metrics.render(families)

    def _own_families(
        self, snapshot: dict[str, Any], lag_max: float, audio: dict[str, dict[str, Any]]
    ) -> list[metrics.Family]:
        fams: list[metrics.Family] = []
        info = metrics.Family("streamstack_build_info", "gauge", "The StreamStack version running.")
        info.add(1, {"version": __version__})
        fams.append(info)
        fams.append(
            metrics.gauge(
                "streamstack_uptime_seconds", "Seconds since the Console started.", time.monotonic() - self._started
            )
        )
        fams.append(
            metrics.gauge(
                "streamstack_event_loop_lag_max_seconds", "Worst sampler wake-up delay since the last scrape.", lag_max
            )
        )
        fams.append(
            metrics.Family("streamstack_telemetry_scrapes_total", "counter", "Scrapes of this endpoint.").add(
                self.scrapes
            )
        )
        if self._server is not None:
            fams.append(
                metrics.gauge(
                    "streamstack_console_windows",
                    "Console windows listening for updates.",
                    self._server.hub.client_count(),
                )
            )
        peak = metrics.Family("streamstack_audio_peak_dbfs", "gauge", "Loudest true peak since the last scrape.")
        rms = metrics.Family("streamstack_audio_rms_dbfs", "gauge", "Latest RMS level.")
        lufs = metrics.Family("streamstack_audio_loudness_lufs", "gauge", "Latest short-term loudness (3 s).")
        latched = metrics.Family("streamstack_audio_clip_latched", "gauge", "1 while the clip lamp is lit.")
        for source, row in sorted(audio.items()):
            peak.add(row["peak"], {"source": source})
            rms.add(row.get("rms", -90.0), {"source": source})
            lufs.add(row.get("lufs", -90.0), {"source": source})
            latched.add(row.get("latched", False), {"source": source})
        fams += [peak, rms, lufs, latched]
        cpu = metrics.Family(
            "streamstack_process_cpu_seconds_total", "counter", "CPU time used by StreamStack processes, by role."
        )
        mem = metrics.Family(
            "streamstack_process_resident_memory_bytes", "gauge", "Memory held by StreamStack processes, by role."
        )
        count = metrics.Family("streamstack_process_count", "gauge", "Running StreamStack processes, by role.")
        for role in procstats.ROLES:
            usage = self._process_usage().get(role)
            count.add(usage.count if usage else 0, {"process": role})
            if usage:
                cpu.add(usage.cpu_seconds, {"process": role})
                mem.add(usage.rss_bytes, {"process": role})
        fams += [cpu, mem, count]
        reports = self._crashes()
        fams.append(metrics.gauge("streamstack_crash_reports", "Crash reports on disk.", len(reports)))
        last = max((r["time"] for r in reports if isinstance(r.get("time"), (int, float))), default=None)
        if last is not None:
            fams.append(
                metrics.gauge("streamstack_last_crash_timestamp_seconds", "Unix time of the newest crash report.", last)
            )
        fams.append(
            metrics.gauge(
                "streamstack_store_warnings",
                "Files StreamStack repaired and the Console still reports.",
                len((snapshot.get("storage") or {}).get("warnings", [])),
            )
        )
        return fams


# ---------------------------------------------------------------------------
# Metrics derived from a state snapshot (pure: easy to test, nothing measured)
# ---------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _optional_up(snapshot: dict[str, Any], key: str) -> bool | None:
    """Health of a service the Console may report under `key` (stream, engine); None when it is not in the state."""
    section = snapshot.get(key)
    if not isinstance(section, dict):
        return None
    for field in ("running", "active", "live", "ok"):
        if isinstance(section.get(field), bool):
            return section[field]
    state = section.get("state")
    return str(state) in ("running", "live", "active", "ok", "streaming") if state is not None else None


def _stream_families(stream: dict[str, Any]) -> list[metrics.Family]:
    """Recording and streaming: overall and per destination (a destination is labelled by its kind and a hash)."""
    live = metrics.gauge(
        "streamstack_stream_live", "1 while at least one destination is live.", stream.get("status") == "live"
    )
    degraded = metrics.gauge(
        "streamstack_stream_degraded",
        "1 while a destination is failing or reconnecting.",
        stream.get("status") == "degraded",
    )
    fams = [live, degraded]
    for name, key, help_text in (
        ("streamstack_stream_bitrate_kbps", "bitrate_kbps", "Outgoing video bitrate."),
        ("streamstack_stream_dropped_frames", "dropped_frames", "Frames dropped since the stream started."),
        ("streamstack_stream_latency_milliseconds", "latency_ms", "Delay between capture and the network."),
        ("streamstack_stream_uptime_seconds", "uptime_s", "Seconds since the stream went live."),
    ):
        value = _num(stream.get(key))
        if value is not None:
            fams.append(metrics.gauge(name, help_text, value))
    recording = stream.get("recording") if isinstance(stream.get("recording"), dict) else {}
    fams.append(
        metrics.gauge(
            "streamstack_recording_active", "1 while a recording is being written.", bool(recording.get("active"))
        )
    )
    health = {"ok": 0, "idle": 0, "warn": 1, "fail": 2}
    dest_up = metrics.Family("streamstack_stream_destination_up", "gauge", "1 when a destination is live.")
    dest_state = metrics.Family(
        "streamstack_stream_destination_health", "gauge", "0 ok or idle, 1 reconnecting, 2 failed."
    )
    dest_restarts = metrics.Family("streamstack_stream_destination_restarts", "gauge", "Reconnects of a destination.")
    for item in stream.get("destinations") or []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        labels = {"destination": light_label(item["id"]), "kind": str(item.get("kind", ""))}
        dest_up.add(item.get("state") == "live", labels)
        dest_state.add(health.get(str(item.get("health")), 0), labels)
        restarts = _num(item.get("restarts"))
        if restarts is not None:
            dest_restarts.add(restarts, labels)
    return [*fams, dest_up, dest_state, dest_restarts]


def collect(snapshot: dict[str, Any]) -> list[metrics.Family]:
    """Families describing the rig in `snapshot`: services, health checks, studio, audio, lights, host."""
    studio = snapshot.get("studio") or {}
    video = snapshot.get("video") or {}
    audio = snapshot.get("audio") or {}
    lighting = snapshot.get("lighting") or {}
    health = snapshot.get("health") or {}
    canon = video.get("canon") or {}
    mic = audio.get("mic") or {}
    checks = [c for c in health.get("checks", []) if isinstance(c, dict)]
    fams: list[metrics.Family] = []

    up = metrics.Family("streamstack_service_up", "gauge", "1 when the named part of the rig is running and healthy.")
    up.add(1, {"service": "console"})
    up.add(bool(studio.get("running")), {"service": "studio"})
    up.add(bool(studio.get("voice_chain")), {"service": "voice_chain"})
    if canon.get("state") not in (None, "disabled"):
        up.add(canon.get("state") == "running", {"service": "canon_bridge"})
    automation = lighting.get("automation") or {}
    if automation.get("available"):
        up.add(bool((automation.get("status") or {}).get("running")), {"service": "lights_service"})
    stall = next((c for c in checks if c.get("id") == "mic-stall"), None)
    if stall is not None:
        up.add(stall.get("state") != "fail", {"service": "mic_watch"})
    up.add(bool((snapshot.get("auto") or {}).get("running")), {"service": "auto_director"})
    stream = snapshot.get("stream") if isinstance(snapshot.get("stream"), dict) else None
    if stream is not None:
        up.add(stream.get("status") != "degraded", {"service": "stream"})
    state = _optional_up(snapshot, "engine")
    if state is not None:
        up.add(state, {"service": "engine"})
    fams.append(up)
    if stream is not None:
        fams.extend(_stream_families(stream))

    states = metrics.Family("streamstack_health_check_state", "gauge", "Pre-flight check result: 0 ok, 1 warn, 2 fail.")
    totals = {"ok": 0, "warn": 0, "fail": 0}
    for check in checks:
        state = str(check.get("state"))
        if state in STATE_OK and check.get("id"):
            states.add(STATE_OK[state], {"check": str(check["id"]), "group": str(check.get("group", ""))})
            totals[state] += 1
    summary = metrics.Family("streamstack_health_checks", "gauge", "Pre-flight checks by result.")
    for state, n in totals.items():
        summary.add(n, {"state": state})
    fams += [states, summary]

    fams += [
        metrics.gauge(
            "streamstack_studio_busy", "1 while the studio is starting or stopping.", bool(studio.get("busy"))
        ),
        metrics.gauge("streamstack_virtual_camera", "1 while the program camera runs.", bool(video.get("virtualcam"))),
        metrics.gauge("streamstack_privacy_scene", "1 while the privacy scene is on air.", bool(video.get("privacy"))),
    ]

    if canon.get("state") not in (None, "disabled"):
        fams += [
            metrics.gauge(
                "streamstack_canon_fps_in", "Canon bridge frames per second from the camera.", _num(canon.get("fps_in"))
            ),
            metrics.gauge(
                "streamstack_canon_fps_out",
                "Canon bridge frames per second to the loopback camera.",
                _num(canon.get("fps_out")),
            ),
            metrics.gauge(
                "streamstack_canon_grab_milliseconds",
                "Time between frames from the camera.",
                _num(canon.get("grab_ms")),
            ),
            metrics.gauge(
                "streamstack_canon_cpu_percent",
                "CPU of the Canon bridge and ffmpeg (percent of one core).",
                _num((canon.get("cpu") or {}).get("total")),
            ),
            metrics.gauge(
                "streamstack_canon_decode_on_gpu",
                "1 when the Canon picture is decoded on the GPU.",
                canon.get("decode", {}).get("path") in ("qsv", "vaapi"),
            ),
        ]
        restarts = metrics.Family(
            "streamstack_canon_restarts_total", "counter", "Canon bridge restarts since it started."
        )
        for kind, value in (canon.get("restarts") or {}).items():
            if _num(value) is not None:
                restarts.add(value, {"kind": str(kind)})
        fams.append(restarts)
        fams.append(
            metrics.Family("streamstack_canon_stalls_total", "counter", "Canon bridge stalls since it started.").add(
                _num(canon.get("stalls")) or 0
            )
        )

    stalled_for = None
    if mic.get("stalled") and isinstance(mic.get("stalled_since"), (int, float)):
        stalled_for = max(0.0, time.time() - float(mic["stalled_since"]))
    fams += [
        metrics.gauge("streamstack_mic_present", "1 when the call microphone is found.", bool(mic.get("present"))),
        metrics.gauge(
            "streamstack_mic_stalled",
            "1 while the microphone captures nothing (the stall watchdog).",
            bool(mic.get("stalled")),
        ),
        metrics.gauge(
            "streamstack_mic_stalled_seconds", "How long the microphone has been stalled.", stalled_for or 0.0
        ),
        metrics.gauge("streamstack_mic_muted", "1 while the microphone is muted.", bool(mic.get("muted"))),
        metrics.gauge("streamstack_mic_gain_db", "Microphone input gain.", _num(mic.get("gain_db"))),
    ]

    lights = [item for item in lighting.get("lights", []) if isinstance(item, dict) and item.get("id")]
    held = {
        k: bool(v.get("held"))
        for k, v in ((automation.get("status") or {}).get("lights") or {}).items()
        if isinstance(v, dict)
    }
    power = metrics.Family(
        "streamstack_light_power", "gauge", "1 when a light is on (light is a short hash of its id)."
    )
    level = metrics.Family("streamstack_light_brightness_percent", "gauge", "Light brightness in percent.")
    kelvin = metrics.Family("streamstack_light_temperature_kelvin", "gauge", "Light colour temperature.")
    connected = metrics.Family("streamstack_light_connected", "gauge", "1 when a light answers.")
    hold = metrics.Family(
        "streamstack_light_held", "gauge", "1 when a light was changed by hand and the schedule leaves it alone."
    )
    for item in lights:
        label = {"light": light_label(str(item["id"]))}
        connected.add(bool(item.get("connected")), label)
        if item.get("connected"):
            power.add(bool(item.get("power")), label)
            if _num(item.get("brightness")) is not None:
                level.add(item["brightness"], label)
            if _num(item.get("kelvin")) is not None:
                kelvin.add(item["kelvin"], label)
        if str(item["id"]) in held:
            hold.add(held[str(item["id"])], label)
    fams += [power, level, kelvin, connected, hold]

    system = snapshot.get("system") or {}
    fams.append(
        metrics.gauge("streamstack_host_cpu_percent", "CPU use of the whole computer.", _num(system.get("cpu_percent")))
    )
    load = system.get("load")
    if isinstance(load, list) and load and _num(load[0]) is not None:
        fams.append(metrics.gauge("streamstack_host_load1", "One-minute load average.", load[0]))
    fams.append(metrics.gauge("streamstack_host_cores", "CPU cores.", _num(system.get("cores"))))
    return fams
