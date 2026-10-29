"""The Console's network quality monitor: is the connection good enough for a call, and if not, why.

Every `interval_s` (5 s) it sends one tiny probe to each target (Teams, Google Meet and Zoom by default: a STUN
binding request or a TCP connect) and one ping to the default gateway, reads the Wi-Fi link when there is one, and
keeps five minutes of history (`streamstack.netquality`). The state carries the rating (good, degraded, poor,
offline), the numbers, the likely cause and a sparkline; a degraded link raises one warning per episode, during a call
too, with the likely cause.

What it never does:

* send more than probe packets on its own (a few kB a minute). The bandwidth test is the one exception: it is an
  explicit action (`network.bandwidth`), rate-limited, and refused during a call unless `confirm` is given;
* need the internet: with no route it says "offline" and why, and keeps probing so it notices the way back.

Targets are configurable (`network.set`) and kept in `console-network.json`. Pre-flight reads `facts()`.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from streamstack import config, netquality
from streamstack import store as filestore
from streamstack.console.audio import bool_param, number_param
from streamstack.console.errors import ActionError
from streamstack.netquality import Alerter, Bandwidth, Fetcher, ProbeError, Sample, Target, Transport, Wifi, Window

log = logging.getLogger(__name__)

Notify = Callable[[str, str], None]

SETTINGS_FILE = "console-network.json"
MIN_SAMPLES = 3  # a target is rated once it has this many probes
BANDWIDTH_MIN_GAP_S = 60.0
BANDWIDTH_FRESH_S = 600.0  # Pre-flight uses a bandwidth result this young
TONE = {"good": "ok", "degraded": "warn", "poor": "error", "offline": "error"}


def live_settings_path() -> Path:
    return config.state_dir() / SETTINGS_FILE


def empty_state() -> dict[str, Any]:
    """The `network` object of a rig without the monitor."""
    return {
        "available": False,
        "enabled": False,
        "rating": "unknown",
        "text": "",
        "cause": "",
        "rtt_ms": None,
        "jitter_ms": None,
        "loss_pct": 0.0,
        "targets": [],
        "gateway": None,
        "wifi": None,
        "history": [],
        "interval_s": netquality.INTERVAL_S,
        "bandwidth": {
            "state": "idle", "at": None, "up_mbps": None, "down_mbps": None, "verdict": "unknown", "error": None,
            "needed_up_mbps": netquality.NEEDED_UP_MBPS, "blocked": None,
        },
        "settings": {"targets": [], "defaults": [], "max_targets": netquality.MAX_TARGETS},
        "demo": False,
    }  # fmt: skip


def _target_dict(t: Target) -> dict[str, Any]:
    return {"name": t.name, "host": t.host, "port": t.port, "kind": t.kind}


def read_settings(path: Path | None) -> dict[str, Any]:
    """The saved settings, with anything unusable replaced by the defaults."""
    settings: dict[str, Any] = {"enabled": True, "targets": [_target_dict(t) for t in netquality.DEFAULT_TARGETS]}
    if path is None:
        return settings
    data = filestore.read_json(path, None, validate=filestore.is_object)
    if not isinstance(data, dict):
        return settings
    if isinstance(data.get("enabled"), bool):
        settings["enabled"] = data["enabled"]
    raw = data.get("targets")
    if isinstance(raw, list) and 1 <= len(raw) <= netquality.MAX_TARGETS:
        try:
            settings["targets"] = [_target_dict(netquality.parse_target(t)) for t in raw]
        except ValueError as exc:
            log.warning("ignoring the targets in %s: %s", path, exc)
    return settings


def _iso(wall: float | None) -> str | None:
    return None if wall is None else time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(wall))


class NetworkController:
    def __init__(
        self,
        notify: Notify | None = None,
        *,
        transport: Transport | None = None,
        fetcher: Fetcher | None = None,
        call: Any = None,
        settings_path: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        interval_s: float = netquality.INTERVAL_S,
        parallel: bool = True,
    ) -> None:
        self.notify = notify
        self.call = call  # anything with active(): rig.call
        self._transport = transport or netquality.SocketTransport(clock)
        self._fetcher = fetcher or netquality.UrlFetcher()
        self._clock, self._wall = clock, wall
        self._interval = interval_s
        self._parallel = parallel
        self._path = settings_path
        self._settings = read_settings(settings_path)
        self._lock = threading.RLock()
        self._windows: dict[str, Window] = {}
        self._gateway_host: str | None = None
        self._gateway = Window()
        self._wifi: Wifi | None = None
        self._history: list[float | None] = []
        self._alerter = Alerter(clock=clock)
        self._rating = "unknown"
        self._eval: dict[str, Any] = {}
        self._bandwidth: dict[str, Any] = {"state": "idle", "at": None, "result": None, "error": None}
        self._bw_started: float | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ----------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="network-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=netquality.PROBE_TIMEOUT_S * 4)

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._settings["enabled"]:
                try:
                    self.tick()
                except Exception:  # a failing probe round must not end the monitor
                    log.exception("network monitor: probe round failed")
            self._wake.wait(self._interval)
            self._wake.clear()

    # -- one probe round ----------------------------------------------------------

    def _probe(self, target: Target) -> Sample:
        start = self._clock()
        try:
            rtt = self._transport.probe(target, netquality.PROBE_TIMEOUT_S)
        except ProbeError as exc:
            return Sample(start, None, exc.kind)
        except OSError as exc:
            log.debug("probe of %s failed: %s", target.host, exc)
            return Sample(start, None, "timeout")
        return Sample(start, rtt)

    def tick(self) -> None:
        """Probe every target and the gateway once, then evaluate."""
        with self._lock:
            targets = [Target(**t) for t in self._settings["targets"]]
        results: dict[str, Sample] = {}

        def run(target: Target) -> None:
            results[target.name] = self._probe(target)

        if self._parallel:
            threads = [threading.Thread(target=run, args=(t,), daemon=True) for t in targets]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(netquality.PROBE_TIMEOUT_S + 2)
        else:
            for target in targets:
                run(target)
        gateway = self._transport.gateway()
        gateway_sample: Sample | None = None
        if gateway is not None:
            start = self._clock()
            try:
                rtt = self._transport.ping(gateway, netquality.PROBE_TIMEOUT_S)
                gateway_sample = None if rtt is None else Sample(start, rtt)
            except ProbeError as exc:
                gateway_sample = Sample(start, None, exc.kind)
        wifi = self._transport.wifi()
        with self._lock:
            for target in targets:
                sample = results.get(target.name, Sample(self._clock(), None, "timeout"))
                self._windows.setdefault(target.name, Window()).add(sample)
            self._gateway_host = gateway
            if gateway_sample is not None:
                self._gateway.add(gateway_sample)
            self._wifi = wifi
            self._evaluate(targets)
        self._alert()

    def _evaluate(self, targets: list[Target]) -> None:
        per: list[dict[str, Any]] = []
        ratings: dict[str, str] = {}
        for t in targets:
            window = self._windows.get(t.name)
            stats = window.stats() if window is not None else netquality.summarize([])
            if stats.count < MIN_SAMPLES:
                rating = "unknown"
            elif stats.rtt_ms is None:
                rating = "offline"
            else:
                rating = netquality.rate(stats.rtt_ms, stats.jitter_ms, stats.loss_pct)
            ratings[t.name] = rating
            answered = [s for s in (window.samples() if window else []) if s.rtt_ms is not None]
            per.append({
                **_target_dict(t), "rtt_ms": _round(stats.rtt_ms), "jitter_ms": _round(stats.jitter_ms),
                "loss_pct": round(stats.loss_pct, 1), "rating": rating, "error": stats.last_error,
                "last_ok": _iso(self._wall() - (self._clock() - answered[-1].at)) if answered else None,
            })  # fmt: skip
        known = {n: r for n, r in ratings.items() if r != "unknown"}
        live = [p for p in per if p["rating"] not in ("unknown", "offline")]
        gateway = self._gateway.stats()
        gateway_rating = (
            None if gateway.count < MIN_SAMPLES
            else "offline" if gateway.rtt_ms is None
            else netquality.rate(gateway.rtt_ms, gateway.jitter_ms, gateway.loss_pct)
        )  # fmt: skip
        rtt = netquality.median(p["rtt_ms"] for p in live)
        jitter = netquality.median(p["jitter_ms"] for p in live)
        loss = netquality.median(p["loss_pct"] for p in live) or 0.0
        if not known:
            rating = "unknown"
        elif not live:
            rating = "offline"
        else:
            rating = netquality.rate(rtt, jitter, loss)
        cause = netquality.diagnose(rating, known, gateway_rating, self._wifi) if rating != "unknown" else ""
        found = netquality.problems(rtt, jitter, loss)
        text = {
            "unknown": "Measuring the connection",
            "good": "The connection is good for a call",
            "offline": "No route to the call services",
        }.get(rating) or f"The connection is {rating}: {', '.join(found) or 'out of range'}"
        self._history.append(rtt if rating not in ("offline", "unknown") else None)
        del self._history[: -netquality.HISTORY]
        self._rating = rating
        self._eval = {
            "rating": rating, "text": text, "cause": cause, "rtt_ms": _round(rtt), "jitter_ms": _round(jitter),
            "loss_pct": round(loss, 1), "targets": per, "problems": found,
            "gateway": None if self._gateway_host is None else {
                "host": self._gateway_host, "rtt_ms": _round(gateway.rtt_ms), "loss_pct": round(gateway.loss_pct, 1),
                "rating": gateway_rating,
            },
            "wifi": None if self._wifi is None else {
                "iface": self._wifi.iface, "signal_dbm": self._wifi.signal_dbm, "rate_mbps": self._wifi.rate_mbps,
                "retry_pct": _round(self._wifi.retry_pct), "weak": self._wifi.weak,
            },
        }  # fmt: skip

    def _alert(self) -> None:
        with self._lock:
            verdict = self._alerter.update(self._rating)
            text, cause = self._eval.get("text", ""), self._eval.get("cause", "")
        if verdict is None or self.notify is None:
            return
        kind, rating = verdict
        if kind == "ok":
            self.notify("ok", "The network is back to normal.")
        else:
            during = " during your call" if self._call_active() else ""
            self.notify("error" if rating in ("poor", "offline") else "warn", f"{text}{during}. Likely cause: {cause}.")

    def _call_active(self) -> bool:
        return self.call is not None and bool(self.call.active())

    # -- state --------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        with self._lock:
            ev = self._eval
            bw = self._bandwidth
            enabled = self._settings["enabled"]
            result: Bandwidth | None = bw["result"]
            return {
                "available": True,
                "enabled": enabled,
                "rating": ev.get("rating", "unknown"),
                "text": ev.get("text") or ("Measuring the connection" if enabled else "Monitoring is off"),
                "cause": ev.get("cause", ""),
                "rtt_ms": ev.get("rtt_ms"),
                "jitter_ms": ev.get("jitter_ms"),
                "loss_pct": ev.get("loss_pct", 0.0),
                "targets": ev.get("targets", []),
                "gateway": ev.get("gateway"),
                "wifi": ev.get("wifi"),
                "history": list(self._history),
                "interval_s": self._interval,
                "bandwidth": {
                    "state": bw["state"], "at": _iso(bw["at"]), "error": bw["error"],
                    "up_mbps": None if result is None else result.up_mbps,
                    "down_mbps": None if result is None else result.down_mbps,
                    "verdict": "unknown" if result is None else result.verdict,
                    "needed_up_mbps": netquality.NEEDED_UP_MBPS,
                    "blocked": self._bandwidth_block(),
                },
                "settings": {
                    "targets": [dict(t) for t in self._settings["targets"]],
                    "defaults": [_target_dict(t) for t in netquality.DEFAULT_TARGETS],
                    "max_targets": netquality.MAX_TARGETS,
                },
                "demo": False,
            }  # fmt: skip

    def facts(self) -> dict[str, Any] | None:
        """What Pre-flight needs: the current numbers and a fresh bandwidth result, or None before a measurement."""
        with self._lock:
            if not self._eval or self._rating == "unknown":
                return None
            result: Bandwidth | None = self._bandwidth["result"]
            fresh = result is not None and self._wall() - (self._bandwidth["at"] or 0.0) < BANDWIDTH_FRESH_S
            return {
                "rating": self._rating,
                "rtt_ms": self._eval.get("rtt_ms"),
                "jitter_ms": self._eval.get("jitter_ms"),
                "loss_pct": self._eval.get("loss_pct", 0.0),
                "up_mbps": result.up_mbps if fresh and result is not None else None,
                "down_mbps": result.down_mbps if fresh and result is not None else None,
            }

    # -- actions ------------------------------------------------------------------

    def act(self, action: str, params: dict[str, Any]) -> str | None:
        if action == "network.set":
            return self._act_set(params)
        if action == "network.bandwidth":
            return self._act_bandwidth(params)
        raise ActionError(f"unknown network action {action!r}", 400)

    def _act_set(self, params: dict[str, Any]) -> str | None:
        unknown = sorted(set(params) - {"enabled", "targets", "reset_targets"})
        if unknown:
            raise ActionError(f"unknown network option {unknown[0]!r}", 400)
        if not params:
            raise ActionError("nothing to change: pass enabled, targets or reset_targets", 400)
        settings = dict(self._settings)
        if "enabled" in params:
            settings["enabled"] = bool_param(params, "enabled")
        if params.get("reset_targets") is True:
            settings["targets"] = [_target_dict(t) for t in netquality.DEFAULT_TARGETS]
        elif "targets" in params:
            raw = params["targets"]
            if not isinstance(raw, list) or not 1 <= len(raw) <= netquality.MAX_TARGETS:
                raise ActionError(f"targets is a list of 1 to {netquality.MAX_TARGETS} entries", 400)
            try:
                parsed = [netquality.parse_target(t) for t in raw]
            except ValueError as exc:
                raise ActionError(str(exc), 400) from exc
            names = [t.name for t in parsed]
            if len(set(names)) != len(names):
                raise ActionError("each target needs its own name", 400)
            settings["targets"] = [_target_dict(t) for t in parsed]
        with self._lock:
            changed = settings["targets"] != self._settings["targets"]
            self._settings = settings
            if changed:
                self._clear()
            self._save()
        self._wake.set()
        return None

    def _clear(self) -> None:
        self._windows.clear()
        self._gateway.clear()
        self._history.clear()
        self._eval = {}
        self._rating = "unknown"
        self._alerter.reset()

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            filestore.write_json(self._path, self._settings)
        except OSError as exc:
            log.warning("cannot save %s: %s", self._path, exc)

    def _bandwidth_block(self) -> str | None:
        if self._bandwidth["state"] == "running":
            return "A bandwidth test is already running."
        if self._bw_started is not None:
            wait = BANDWIDTH_MIN_GAP_S - (self._clock() - self._bw_started)
            if wait > 0:
                return f"The bandwidth test was just run. Wait {wait:.0f} s."
        return None

    def _act_bandwidth(self, params: dict[str, Any]) -> str | None:
        unknown = sorted(set(params) - {"confirm"})
        if unknown:
            raise ActionError(f"unknown network option {unknown[0]!r}", 400)
        confirm = params.get("confirm", False)
        if not isinstance(confirm, bool):
            raise ActionError("confirm must be true or false", 400)
        if self._call_active() and not confirm:
            raise ActionError(
                "A call is on. The bandwidth test uses your uplink for several seconds and can hurt the call. "
                "Run it anyway with confirm.",
                409,
            )
        with self._lock:
            if reason := self._bandwidth_block():
                raise ActionError(reason, 409)
            self._bandwidth.update(state="running", error=None)
            self._bw_started = self._clock()
        threading.Thread(target=self._run_bandwidth, name="network-bandwidth", daemon=True).start()
        return "Measuring upload and download: a few seconds"

    def _run_bandwidth(self) -> None:
        try:
            result = self._measure_bandwidth()
        except ProbeError as exc:
            with self._lock:
                self._bandwidth.update(state="error", error=f"The bandwidth test failed: {exc}", at=self._wall())
            if self.notify is not None:
                self.notify("error", f"The bandwidth test failed: {exc}")
            return
        with self._lock:
            self._bandwidth.update(state="done", result=result, error=None, at=self._wall())
        if self.notify is not None:
            up = "unknown" if result.up_mbps is None else f"{result.up_mbps:g}"
            down = "unknown" if result.down_mbps is None else f"{result.down_mbps:g}"
            word = {"enough": "plenty for", "tight": "just enough for", "short": "not enough for"}.get(
                result.verdict, "unknown for"
            )
            self.notify(
                "ok" if result.verdict in ("enough", "tight") else "warn",
                f"Upload {up} Mbit/s, download {down} Mbit/s: {word} a 1080p call "
                f"(needs about {netquality.NEEDED_UP_MBPS:g} up).",
            )

    def _measure_bandwidth(self) -> Bandwidth:
        return netquality.measure_bandwidth(self._fetcher)


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


# ---------------------------------------------------------------------------
# The simulated twin
# ---------------------------------------------------------------------------

DEMO_INTERVAL_S = 0.25


class DemoLink:
    """A scripted connection: base round trip, injected jitter and loss, a gateway, an optional Wi-Fi link."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.rtt_ms = 28.0
            self.jitter_ms = 3.0
            self.loss_pct = 0.0
            self.route = True
            self.gateway_rtt_ms = 2.0
            self.gateway_loss_pct = 0.0
            self.wifi: Wifi | None = None
            self.up_mbps = 40.0
            self.down_mbps = 220.0
            self.bw_fails = False
            self._counts: dict[str, int] = {}
            self._rng = random.Random(7)  # noqa: S311 - a deterministic simulation

    def next(self, key: str, rtt: float, jitter: float, loss: float) -> float:
        """The next round trip for a probe stream, or ProbeError. Jitter alternates and loss is spread evenly, so a
        scripted 150 ms or 5 % is measured as 150 ms or 5 % (not a lucky or unlucky random draw)."""
        with self._lock:
            if not self.route:
                raise ProbeError("no-route", "network unreachable")
            n = self._counts.get(key, 0)
            self._counts[key] = n + 1
            if int((n + 1) * loss / 100.0) > int(n * loss / 100.0):
                raise ProbeError("timeout")
            return rtt + (jitter if n % 2 else 0.0) + self._rng.uniform(0.0, 1.0)


class DemoTransport(Transport):
    def __init__(self, link: DemoLink) -> None:
        self.link = link

    def stun(self, host: str, port: int, timeout: float) -> float:
        return self.link.next(host, self.link.rtt_ms, self.link.jitter_ms, self.link.loss_pct)

    tcp = stun

    def ping(self, host: str, timeout: float) -> float | None:
        link = self.link
        return link.next("gateway", link.gateway_rtt_ms, 0.5, link.gateway_loss_pct)

    def gateway(self) -> str | None:
        return "192.168.1.1"

    def wifi(self) -> Wifi | None:
        return self.link.wifi


class DemoFetcher(Fetcher):
    def __init__(self, link: DemoLink) -> None:
        self.link = link

    def download(self, url: str, seconds: float) -> tuple[int, float]:
        if self.link.bw_fails or not self.link.route:
            raise ProbeError("no-route", "no route to the test server")
        return int(self.link.down_mbps * 1e6 / 8), 1.0

    def upload(self, url: str, payload: bytes, timeout: float) -> tuple[int, float]:
        if self.link.bw_fails or not self.link.route:
            raise ProbeError("no-route", "no route to the test server")
        return int(self.link.up_mbps * 1e6 / 8), 1.0


class DemoNetwork(NetworkController):
    """The demo rig's twin: a scripted link (`network.demo`) probed by the real monitor, at a fast tick."""

    def __init__(self, notify: Notify | None = None, **kwargs: Any) -> None:
        self.link = DemoLink()
        kwargs.setdefault("interval_s", DEMO_INTERVAL_S)
        kwargs.setdefault("parallel", False)
        super().__init__(notify, transport=DemoTransport(self.link), fetcher=DemoFetcher(self.link), **kwargs)

    def state(self) -> dict[str, Any]:
        return {**super().state(), "demo": True}

    def act(self, action: str, params: dict[str, Any]) -> str | None:
        if action == "network.demo":
            self._act_demo(params)
            return None
        if action == "network.demo_reset":
            self.link.reset()
            with self._lock:
                self._settings = read_settings(None)
                self._clear()
                self._bandwidth = {"state": "idle", "at": None, "result": None, "error": None}
                self._bw_started = None
            self._wake.set()
            return None
        return super().act(action, params)

    def _act_demo(self, params: dict[str, Any]) -> None:
        link = self.link
        numbers = {
            "rtt_ms": (0.0, 2000.0), "jitter_ms": (0.0, 1000.0), "loss_pct": (0.0, 100.0),
            "gateway_rtt_ms": (0.0, 2000.0), "gateway_loss_pct": (0.0, 100.0),
            "up_mbps": (0.1, 10000.0), "down_mbps": (0.1, 10000.0),
        }  # fmt: skip
        for key, (low, high) in numbers.items():
            if key in params:
                setattr(link, key, number_param(params, key, low=low, high=high))
        if "route" in params:
            link.route = bool_param(params, "route")
        if "bw_fails" in params:
            link.bw_fails = bool_param(params, "bw_fails")
        if "wifi" in params:
            raw = params["wifi"]
            if raw is None:
                link.wifi = None
            elif isinstance(raw, dict):
                link.wifi = Wifi(
                    "wlan0",
                    number_param(raw, "signal_dbm", low=-100.0, high=0.0),
                    866.0,
                    number_param(raw, "retry_pct", low=0.0, high=100.0) if "retry_pct" in raw else 1.0,
                )
            else:
                raise ActionError("wifi is an object with signal_dbm and retry_pct, or null", 400)
        with self._lock:
            self._clear()  # the new link is judged on its own probes, not on the old window
        self._wake.set()
