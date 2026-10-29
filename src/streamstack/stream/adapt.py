"""Automatic bandwidth downshift and recovery for live destinations (issue #98, docs/stream.md "Bandwidth adaptation").

A live stream that outruns the uplink does not degrade gracefully: the send queue grows, frames drop, the platform
buffers, viewers see stalls. The cure is to send less before that happens. This module holds the decision, and nothing
else (no threads, no sockets, no clock of its own, no ffmpeg), so every rule can be tested by feeding it samples:

- `Step` and `build_steps`: the ladder. Step 0 is what the person chose (a platform and a bandwidth tier, resolved by
  `streamstack.presets.plan`); each later step is a plan the Presets page could have produced, with less picture or
  fewer bits, going down through the lower tiers to the floor.
- `LinkSample` and `sample_from_status`: what the service can measure for one destination: send-queue backlog, dropped
  frames, reconnects, and the path loss and round trip when the transport measures them (WHIP today, SRT from the path
  measurement). Anything missing is simply not judged.
- `judge`: one destination's samples to a `Verdict` (ok, warn, congested, severe) with a human reason.
- `AdaptController`: verdicts to `Shift`s with hysteresis. It steps down after a few congested evaluations in a row (at
  once when severe, two steps when very bad), waits out a cooldown after every change, steps up one rung only after a
  long calm spell, and doubles that spell after a recovery that failed within the flap window, so a link that cannot
  carry a step is not hammered with it.

`StreamService` owns an `AdaptController`, feeds it from the supervisor tick and applies its shifts as live settings
changes (docs/stream.md). The Console's network quality monitor (#41) is an optional extra opinion about the uplink,
passed in as a rating, never a dependency.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from streamstack.presets.plan import ladder_tier, resolve
from streamstack.presets.schema import PresetError
from streamstack.stream import planmap
from streamstack.stream.model import TIERS, EncoderSettings

log = logging.getLogger(__name__)

OK, WARN, CONGESTED, SEVERE = "ok", "warn", "congested", "severe"
VERDICT_RANK = {OK: 0, WARN: 1, CONGESTED: 2, SEVERE: 3}
UPLINK_RATINGS = ("good", "degraded", "poor", "offline")  # the network monitor's words (#41)
HISTORY = 12
ADAPT_KEYS = ("tier", "fps", "bitrate_kbps", "quality")  # what a downshift may change; never codec, encoder or audio


@dataclass(frozen=True)
class AdaptConfig:
    """Thresholds and timings. The defaults are deliberately patient: a downshift costs a visible change of picture."""

    eval_interval_s: float = 2.0  # how often samples are judged
    down_after: int = 2  # congested evaluations in a row before a step down (severe needs one)
    cooldown_s: float = 15.0  # after any change, nothing moves for this long (severe may after min_gap_s)
    min_gap_s: float = 5.0  # a severe verdict can cut the cooldown short, never below this
    up_hold_s: float = 30.0  # calm for this long before one step up
    flap_window_s: float = 90.0  # a step down this soon after a step up doubles up_hold_s
    max_hold_factor: float = 8.0
    reset_calm_s: float = 300.0  # calm this long at the top step forgets the doubling
    loss_warn: float = 1.0  # percent packet loss
    loss_congested: float = 3.0
    loss_severe: float = 8.0
    queue_warn_ms: float = 400.0  # video waiting to be sent
    queue_congested_ms: float = 1200.0
    queue_severe_ms: float = 4000.0
    rtt_factor: float = 3.0  # congested when the round trip is this many times the best seen ...
    rtt_excess_ms: float = 150.0  # ... and at least this much above it
    dropped_congested: int = 5  # frames dropped since the last evaluation
    dropped_severe: int = 60
    speed_congested: float = 0.9  # a send process that cannot keep real time
    speed_severe: float = 0.6
    settle_s: float = 10.0  # a connection younger than this is not judged on speed


@dataclass(frozen=True)
class Step:
    """One rung of the ladder: the picture and bitrate to send, and the stream settings that make it so."""

    label: str
    patch: Mapping[str, Any]
    width: int
    height: int
    fps: int
    bitrate_kbps: int | None

    def as_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "bitrate_kbps": self.bitrate_kbps,
        }


def step_label(height: int, fps: int, kbps: int | None) -> str:
    rate = f" at {kbps / 1000:.1f} Mbit/s" if kbps else ""
    return f"{height}p{fps}{rate}"


def build_steps(platform: dict[str, Any], tiers: Sequence[dict[str, Any]], tier_id: str) -> list[Step]:
    """The ladder for a platform and a bandwidth tier, step 0 being the chosen plan. Raises PresetError."""
    ladder = ladder_tier(platform, tiers, tier_id)
    steps: list[Step] = []
    for index in range(len(ladder["steps"])):
        plan = resolve(platform, ladder, index=index if platform["tier_policy"] != "ignore" else None)
        video = plan["video"]
        try:
            patch = planmap.plan_to_settings(plan)
        except planmap.PlanError as exc:
            raise PresetError(str(exc)) from exc
        kbps = next((c.get("bitrate_kbps") for c in video["codecs"] if c.get("bitrate_kbps")), None)
        width, height = TIERS[patch["tier"]] if patch.get("tier") in TIERS else (video["width"], video["height"])
        label = step_label(height, video["fps"], kbps)  # the size the encoder really makes (a 360p plan sends 480p)
        step = Step(label, patch, width, height, video["fps"], kbps)
        if steps and (step.width, step.height, step.fps, step.bitrate_kbps) == (
            steps[-1].width,
            steps[-1].height,
            steps[-1].fps,
            steps[-1].bitrate_kbps,
        ):
            continue  # a plan size below the lowest resolution tier sends the same picture: not a step
        steps.append(step)
    return steps


def overlay(base: EncoderSettings, step: Step) -> EncoderSettings:
    """`base` (what the person chose) stepped down to `step`: only the picture size, frame rate and bitrate move."""
    return replace(base, **{k: v for k, v in step.patch.items() if k in ADAPT_KEYS})


# -- what the link says -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LinkSample:
    """One reading of one destination. Counters (`dropped`, `restarts`) are cumulative; `judge` takes the difference."""

    dest: str
    state: str = "live"
    loss_percent: float | None = None
    rtt_ms: float | None = None
    queued_ms: float = 0.0
    dropped: int = 0
    restarts: int = 0
    speed: float | None = None
    connected_s: float = 0.0
    name: str = ""


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def sample_from_status(dest_id: str, name: str, kind: str, status: Mapping[str, Any], kbps: float) -> LinkSample:
    """A sample from a sink's `status()` dict. `kbps` is the rate being sent (to turn queued bytes into time).

    WHIP reports loss and round trip under `whip`, SRT's path measurement under `srt` (docs/stream.md); a transport
    with neither (RTMP) is judged on the queue, drops, reconnects and send speed alone.
    """
    queued_bytes = _number(status.get("queued_bytes")) or 0.0
    measured: Mapping[str, Any] = {}
    if kind == "whip" and isinstance(status.get("whip"), Mapping):
        measured = status["whip"]
    elif kind == "srt" and isinstance(status.get("srt"), Mapping):
        measured = status["srt"]
    speed = _number(status.get("speed"))
    return LinkSample(
        dest=dest_id,
        name=name,
        state=str(status.get("state", "idle")),
        loss_percent=_number(measured.get("loss_percent", measured.get("probe_loss_percent"))),
        rtt_ms=_number(measured.get("rtt_ms")),
        queued_ms=queued_bytes * 8 / max(kbps, 1.0),
        dropped=int(_number(status.get("dropped_frames")) or 0),
        restarts=int(_number(status.get("restarts")) or 0),
        speed=speed if speed else None,  # 0.0 means "not reported yet" in the sinks that have no speed
        connected_s=_number(status.get("connected_s")) or 0.0,
    )


@dataclass
class Verdict:
    level: str = OK
    reason: str = ""
    dest: str = ""

    def worse_than(self, other: Verdict) -> bool:
        return VERDICT_RANK[self.level] > VERDICT_RANK[other.level]


class Judge:
    """Keeps the last sample and the best round trip per destination, and turns each new sample into a verdict."""

    def __init__(self, config: AdaptConfig) -> None:
        self.config = config
        self._last: dict[str, LinkSample] = {}
        self._best_rtt: dict[str, float] = {}

    def forget(self) -> None:
        self._last.clear()
        self._best_rtt.clear()

    def judge(self, sample: LinkSample) -> Verdict:
        cfg = self.config
        previous = self._last.get(sample.dest)
        self._last[sample.dest] = sample
        if sample.state != "live":
            self._best_rtt.pop(sample.dest, None)
            return Verdict(OK, "", sample.dest)  # not connected: nothing about the link can be read
        who = sample.name or sample.dest
        found: list[Verdict] = []

        def flag(level: str, why: str) -> None:
            found.append(Verdict(level, f"{who}: {why}", sample.dest))

        if sample.loss_percent is not None:
            if sample.loss_percent >= cfg.loss_severe:
                flag(SEVERE, f"{sample.loss_percent:.0f}% packet loss")
            elif sample.loss_percent >= cfg.loss_congested:
                flag(CONGESTED, f"{sample.loss_percent:.0f}% packet loss")
            elif sample.loss_percent >= cfg.loss_warn:
                flag(WARN, f"{sample.loss_percent:.1f}% packet loss")
        if sample.queued_ms >= cfg.queue_severe_ms:
            flag(SEVERE, f"{sample.queued_ms / 1000:.1f} s of video waiting to be sent")
        elif sample.queued_ms >= cfg.queue_congested_ms:
            flag(CONGESTED, f"{sample.queued_ms / 1000:.1f} s of video waiting to be sent")
        elif sample.queued_ms >= cfg.queue_warn_ms:
            flag(WARN, f"{sample.queued_ms:.0f} ms of video waiting to be sent")
        if sample.rtt_ms is not None:
            best = min(self._best_rtt.get(sample.dest, sample.rtt_ms), sample.rtt_ms)
            self._best_rtt[sample.dest] = best
            if sample.rtt_ms > best * cfg.rtt_factor and sample.rtt_ms - best >= cfg.rtt_excess_ms:
                flag(CONGESTED, f"round trip {sample.rtt_ms:.0f} ms, normally {best:.0f} ms")
        if previous is not None and previous.state == "live":
            dropped = sample.dropped - previous.dropped
            if dropped >= cfg.dropped_severe:
                flag(SEVERE, f"{dropped} frames dropped")
            elif dropped >= cfg.dropped_congested:
                flag(CONGESTED, f"{dropped} frames dropped")
            reconnects = sample.restarts - previous.restarts
            if reconnects >= 2:
                flag(SEVERE, "the connection keeps dropping")
            elif reconnects == 1:
                flag(CONGESTED, "the connection dropped")
        if sample.speed is not None and sample.connected_s >= cfg.settle_s:
            if sample.speed < cfg.speed_severe:
                flag(SEVERE, f"sending at {sample.speed:.2f}x real time")
            elif sample.speed < cfg.speed_congested:
                flag(CONGESTED, f"sending at {sample.speed:.2f}x real time")
        return max(found, key=lambda v: VERDICT_RANK[v.level], default=Verdict(OK, "", sample.dest))


# -- the decision -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Shift:
    direction: str  # "down" | "up"
    to: int  # the step index to move to
    reason: str


@dataclass
class _Change:
    time: float
    direction: str
    frm: int
    to: int
    reason: str


@dataclass
class AdaptController:
    """Verdicts in, shifts out. `evaluate` proposes, `commit` records that the proposal was applied."""

    steps: Sequence[Step]
    config: AdaptConfig = field(default_factory=AdaptConfig)
    index: int = 0
    _bad: int = 0
    _calm_since: float | None = None
    _next_move: float = 0.0
    _last_change: float | None = None
    _last_up: float | None = None
    _hold_factor: float = 1.0
    _history: deque[_Change] = field(default_factory=lambda: deque(maxlen=HISTORY))

    @property
    def last(self) -> int:
        return len(self.steps) - 1

    @property
    def hold_s(self) -> float:
        return self.config.up_hold_s * self._hold_factor

    def reset(self) -> None:
        """Back to the top step with no memory: a stream (re)started, or the person chose new settings."""
        self.index = 0
        self._bad = 0
        self._calm_since = None
        self._next_move = 0.0
        self._last_change = None
        self._last_up = None
        self._hold_factor = 1.0

    def replace_steps(self, steps: Sequence[Step]) -> None:
        self.steps = steps
        self.reset()

    def evaluate(self, now: float, verdict: Verdict, uplink: str | None = None) -> Shift | None:
        """Judge one round. `uplink` is the network monitor's rating, if there is one: "poor" or "offline" stops a
        recovery (a link the monitor distrusts is not the moment to send more) but never causes a step down alone."""
        cfg = self.config
        level = verdict.level
        if level == OK and uplink in ("poor", "offline"):
            level = WARN
        if level in (CONGESTED, SEVERE):
            self._bad += 1
            self._calm_since = None
        else:
            self._bad = 0
            if level == OK:
                if self._calm_since is None:
                    self._calm_since = now
            else:
                self._calm_since = None
        if self.index == 0 and self._calm_since is not None and now - self._calm_since >= cfg.reset_calm_s:
            self._hold_factor = 1.0
        severe = level == SEVERE
        gap = cfg.min_gap_s if severe else cfg.cooldown_s
        since = None if self._last_change is None else now - self._last_change
        wants_down = level in (CONGESTED, SEVERE) and self.index < self.last
        if wants_down and self._bad >= (1 if severe else cfg.down_after) and (since is None or since >= gap):
            heavy = severe and self._bad >= 2
            return Shift("down", min(self.last, self.index + (2 if heavy else 1)), verdict.reason or "congestion")
        if (
            level == OK
            and self.index > 0
            and self._calm_since is not None
            and now - self._calm_since >= self.hold_s
            and (since is None or since >= cfg.cooldown_s)
        ):
            return Shift("up", self.index - 1, f"steady for {now - self._calm_since:.0f} s")
        return None

    def commit(self, now: float, shift: Shift) -> None:
        cfg = self.config
        if shift.direction == "down" and self._last_up is not None and now - self._last_up < cfg.flap_window_s:
            self._hold_factor = min(cfg.max_hold_factor, self._hold_factor * 2)
        if shift.direction == "up":
            self._last_up = now
        self._history.append(_Change(now, shift.direction, self.index, shift.to, shift.reason))
        self.index = shift.to
        self._bad = 0
        self._calm_since = now if shift.direction == "up" else None
        self._last_change = now

    def snapshot(self, now: float) -> dict[str, Any]:
        cfg = self.config
        hold_left = 0.0
        if self.index > 0 and self._calm_since is not None:
            hold_left = max(0.0, self.hold_s - (now - self._calm_since))
        elif self.index > 0:
            hold_left = self.hold_s
        return {
            "step": self.index,
            "steps": [s.as_json() for s in self.steps],
            "current": self.steps[self.index].as_json() if self.steps else None,
            "degraded": self.index > 0,
            "recover_in_s": round(hold_left, 1),
            "cooldown_s": 0.0
            if self._last_change is None
            else round(max(0.0, cfg.cooldown_s - (now - self._last_change)), 1),
            "hold_factor": self._hold_factor,
            "history": [
                {
                    "at": c.time,
                    "direction": c.direction,
                    "from": self.steps[c.frm].label if c.frm < len(self.steps) else "",
                    "to": self.steps[c.to].label if c.to < len(self.steps) else "",
                    "reason": c.reason,
                }
                for c in self._history
            ],
        }


def worst(verdicts: Sequence[Verdict]) -> Verdict:
    """The worst of several destinations' verdicts (the first one wins a tie, so the reason is stable)."""
    return max(verdicts, key=lambda v: VERDICT_RANK[v.level], default=Verdict())
