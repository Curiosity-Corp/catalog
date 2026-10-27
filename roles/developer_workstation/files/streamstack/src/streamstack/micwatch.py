"""Microphone watchdog: detect a stalled capture node and bring it back.

The incident this exists for (2026-10-22, mid-call): the Wave XLR capture node showed RUNNING in
`pactl list short sources`, but nothing reached the call. `parec` returned 0 bytes. A card profile cycle
did not help; `pactl suspend-source <mic> 1; pactl suspend-source <mic> 0` fixed it at once.

The ground truth: the ALSA PCM itself
-------------------------------------
PipeWire's own views are NOT reliable evidence. `pw-dump` dumps a stalled node exactly like a healthy
one, and `pw-top -b` only reports the driver that happened to run in the sampled cycle: with two
independent drivers (the USB mic and a Bluetooth sink) a perfectly healthy mic flips between
`R 1024 48000` and `R 0 0 ??? ???` from one snapshot to the next. Neither is used here.

The kernel is the arbiter. For an ALSA source the PCM's procfs file
`/proc/asound/card{C}/pcm{D}c/sub{S}/status` says whether the device is capturing (`state: RUNNING`)
and carries two frame counters: `hw_ptr` (where the hardware has written) and `appl_ptr` (where the
reader, PipeWire, has consumed up to). A healthy 48 kHz microphone advances both by about 48000 frames
a second, `appl_ptr` trailing `hw_ptr` by a period or two. The card/device/subdevice come from the
source's own properties (`alsa.card`, `alsa.device`, `alsa.subdevice`, `device.api`) in
`pactl -f json list sources`.

A microphone is stalled when it is present, unmuted and RUNNING in pactl (somebody is capturing from
it) and, over a window of at least five seconds, the PCM

* is not `RUNNING` at all (closed, XRUN, SETUP, ...), or
* `hw_ptr` does not advance (the device delivers nothing), or
* `hw_ptr` advances but `appl_ptr` does not (PipeWire is not reading).

Sources that are not ALSA (Bluetooth, virtual) are never judged: stalled is "unknown", there is no check
and no banner, rather than a guess. The meter `parec` receiving no bytes is only corroboration, mentioned
in the detail when the ALSA signal already says stalled, never a trigger on its own.

Digital silence (2026-10-27, dm313red): the other way a live microphone says nothing
------------------------------------------------------------------------------------
The pointers can advance perfectly while every sample is exactly 0. A live preamp always has a noise floor
(about -43 dBFS at 75 dB gain on the Wave XLR), so a whole window of exact zeros is never a quiet room: the device's
internal UAC feature unit (capture mute/volume) is out of sync with ALSA's cached values, and `arecord -D hw:XLR`
reads zeros too. It is reported distinctly from a stall (`kind == "silence"`, check `mic-digital-silence`) and the fix
is to re-write the card's capture switch and volume so the device receives them (`revive_mic(resync=...)`, built by
`feature_unit_resync`). The signal is the sample content: the Console's meter reader tracks how long the unbroken run
of zeros is, the CLI captures a few seconds with `parec` (`SignalCapture`).

Nothing here ever revives a microphone by itself: that is an explicit action (Console button, CLI).
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import system
from .hardware import alsa

WINDOW_S = 5.0  # the PCM must show the fault over at least this long before it is believed
SAMPLE_S = 1.0  # one cheap procfs read per second
CLEAR_S = 2.0  # the flag drops only after the PCM looked healthy this long (no flapping)
VERIFY_S = 4.0  # a revive step is judged within this long (healthy as soon as both pointers move)
PACTL_TIMEOUT = 5.0
SUSPEND_GAP_S = 0.4  # between suspend and resume
SETTLE_S = 1.5  # let the node come back before looking again
PROFILE_OFF_S = 1.0
SOURCE_BACK_S = 6.0  # how long to wait for the source to reappear after a profile cycle
PROC_ASOUND = Path("/proc/asound")
SILENCE_WINDOW_S = 4.0  # exact zeros for this long on a running capture are digital silence, never a quiet room
VERIFY_SIGNAL_S = 3.0  # a revive step is judged on this much captured audio when the fault was digital silence
SIGNAL_RATE = 48000
SIGNAL_BYTES_PER_S = SIGNAL_RATE * 4  # float32le, mono
VERIFY_MIN_S = 2.4  # at least this much of a VERIFY_SIGNAL_S capture must arrive for the verdict to count
RESYNC_GAP_S = 0.2  # between writing a control away from its value and back
REPROBE_WAIT_S = 12.0  # how long to wait for the input profile to come back after a wireplumber restart

CHECK_ID = "mic-stall"
SILENCE_CHECK_ID = "mic-digital-silence"
SILENCE_TITLE = "Microphone delivers digital silence - nobody can hear you"
SILENCE_HINT = (
    "the device's capture mute/volume is out of sync: press Revive mic in the Console (Shift R) or run "
    "`streamstack mic-revive`, which re-writes the capture switch and volume (needs alsa-utils)"
)
KIND_STALL = "stall"
KIND_SILENCE = "silence"
STALL_TITLE = "Microphone stalled - nobody can hear you"
STALL_HINT = "press Revive mic in the Console, or run `streamstack mic-revive`"
SILENCE_REPLUG_HINT = (
    "Still digital silence after re-writing the capture controls, suspend/resume and a card profile cycle. Check the "
    "device's own mute (the Wave XLR knob or button), then unplug and replug its USB cable. `systemctl --user restart "
    "wireplumber` (`streamstack mic-revive --reprobe`) is the last resort: it briefly drops every audio stream, so it "
    "is never done automatically."
)
REPLUG_HINT = (
    "Still stalled after suspend/resume and a card profile cycle. Unplug and replug the microphone's USB cable, "
    "or run `systemctl --user restart wireplumber` (that briefly drops every audio stream, so it is never done "
    "automatically)."
)

Run = Callable[..., Any]
ReadStatus = Callable[["AlsaPcm"], "PcmSample | None"]


# ---------------------------------------------------------------------------
# Mapping a pactl source to its ALSA PCM
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlsaPcm:
    card: int
    device: int
    subdevice: int = 0

    @property
    def path(self) -> Path:
        return PROC_ASOUND / f"card{self.card}" / f"pcm{self.device}c" / f"sub{self.subdevice}" / "status"


@dataclass(frozen=True)
class SourceInfo:
    """What `pactl -f json list sources` says about one source, as far as the watchdog cares."""

    name: str
    state: str  # RUNNING | IDLE | SUSPENDED
    muted: bool
    pcm: AlsaPcm | None  # None for anything that is not an ALSA capture device


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def parse_source(sources_json: str, name: str) -> SourceInfo | None:
    """The named source from `pactl -f json list sources` (None when absent or unreadable)."""
    try:
        sources = json.loads(sources_json)
    except ValueError:
        return None
    entry = (
        next((s for s in sources if isinstance(s, dict) and s.get("name") == name), None)
        if isinstance(sources, list)
        else None
    )
    if entry is None:
        return None
    props = entry.get("properties")
    props = props if isinstance(props, dict) else {}
    pcm: AlsaPcm | None = None
    card = _int(props.get("alsa.card"))
    if card is None:
        card = _int(props.get("api.alsa.pcm.card"))
    device = _int(props.get("alsa.device"))
    capture = props.get("api.alsa.pcm.stream", "capture") == "capture"
    if props.get("device.api") == "alsa" and card is not None and device is not None and capture:
        sub = _int(props.get("alsa.subdevice"))
        pcm = AlsaPcm(card, device, sub if sub is not None else 0)
    return SourceInfo(name, str(entry.get("state", "")).upper(), bool(entry.get("mute", False)), pcm)


# ---------------------------------------------------------------------------
# /proc/asound/.../status
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PcmSample:
    state: str  # RUNNING | PREPARED | XRUN | SETUP | closed | ...
    hw_ptr: int | None = None
    appl_ptr: int | None = None
    at: float = 0.0


def parse_pcm_status(text: str, at: float = 0.0) -> PcmSample | None:
    """A PCM status file as a sample (None when it is not one). A closed device is just `closed`."""
    fields: dict[str, str] = {}
    first = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("-"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
        elif not first:
            first = line
    state = fields.get("state") or first
    if not state:
        return None
    return PcmSample(state, _int(fields.get("hw_ptr")), _int(fields.get("appl_ptr")), at)


def read_pcm_status(pcm: AlsaPcm, clock: Callable[[], float] = time.monotonic) -> PcmSample | None:
    """Read the live status file; None when it cannot be read (no such card, no permission)."""
    try:
        text = pcm.path.read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    return parse_pcm_status(text, clock())


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StallProbe:
    """One verdict: `stalled` is None when it cannot be judged (non-ALSA, too early, unreadable)."""

    stalled: bool | None
    state: str = "unknown"  # watching | healthy | stalled | digital-silence | muted | idle | absent | unsupported | ...
    detail: str = ""
    kind: str = ""  # when stalled: KIND_STALL (nothing moves) or KIND_SILENCE (it moves, but only zeros)


def judge(samples: list[PcmSample]) -> StallProbe:
    """Is a PCM that something is capturing from actually moving? `samples` span the whole window."""
    running = [s for s in samples if s.state == "RUNNING"]
    if not running:
        states = sorted({s.state for s in samples})
        return StallProbe(True, "stalled", f"the ALSA capture device is {'/'.join(states)}, not RUNNING")
    first, last = running[0], running[-1]
    if first is last or last.at <= first.at:
        return StallProbe(None, "watching", "not enough samples yet")
    hw = _advance(first.hw_ptr, last.hw_ptr)
    if hw is False:
        return StallProbe(True, "stalled", "the device's hw_ptr is not advancing: the microphone delivers no audio")
    appl = _advance(first.appl_ptr, last.appl_ptr)
    if appl is False:
        return StallProbe(
            True, "stalled", "the device captures but PipeWire is not reading it (appl_ptr is not advancing)"
        )
    if hw is None or appl is None:
        return StallProbe(None, "unreadable", "the PCM status has no pointers to compare")
    return StallProbe(False, "healthy", "the microphone is captured and consumed (hw_ptr and appl_ptr advance)")


def _advance(a: int | None, b: int | None) -> bool | None:
    """Did a counter move between two reads? None when unknown. A decrease is a wrap, which is movement."""
    if a is None or b is None:
        return None
    return a != b


# ---------------------------------------------------------------------------
# Digital silence: the sample content
# ---------------------------------------------------------------------------

SILENCE_DETAIL = (
    "the capture runs but every sample is exactly zero; a live preamp always has a noise floor, so the device's "
    "capture mute/volume (UAC feature unit) is out of sync with ALSA"
)


@dataclass(frozen=True)
class SignalSample:
    """What a short capture of the source contained: bytes received and how many of them were not zero."""

    received: int
    nonzero: int

    @property
    def seconds(self) -> float:
        return self.received / SIGNAL_BYTES_PER_S

    def digital_silence(self, min_seconds: float = SILENCE_WINDOW_S) -> bool | None:
        """True: at least `min_seconds` of audio arrived and not one sample was nonzero. False: there is signal.
        None: less than `min_seconds` arrived, so nothing can be said (a capture that gets nothing is a stall)."""
        if self.nonzero:
            return False
        return True if self.seconds >= min_seconds else None


class SignalCapture:
    """A `parec` child (float32, mono, 48 kHz) read in a thread, counting zero and nonzero bytes. Exact zeros are the
    all-zero byte pattern of +0.0, so counting zero bytes needs no decoding. `spawn` is injectable for tests."""

    def __init__(self, source: str, spawn: Callable[..., subprocess.Popen[bytes]] | None = None) -> None:
        self.source = source
        self._spawn = spawn or subprocess.Popen
        self._proc: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._received = 0
        self._nonzero = 0

    def start(self) -> bool:
        try:
            self._proc = self._spawn(
                [
                    "parec",
                    "--raw",
                    "--format=float32le",
                    f"--rate={SIGNAL_RATE}",
                    "--channels=1",
                    f"--device={self.source}",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return False
        self._thread = threading.Thread(target=self._pump, name="signal-capture", daemon=True)
        self._thread.start()
        return True

    def _pump(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            fd = proc.stdout.fileno()
            while chunk := os.read(fd, 65536):
                self.feed(chunk)
        except (OSError, ValueError):
            return  # the pipe closed under us: stop() or the source went away

    def feed(self, data: bytes) -> None:
        self._received += len(data)
        self._nonzero += len(data) - data.count(0)

    def stop(self) -> SignalSample:
        """End the capture and return what it heard."""
        proc = self._proc
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        if self._thread is not None:
            self._thread.join(timeout=2)
        return SignalSample(self._received, self._nonzero)


def capture_signal(
    source: str,
    seconds: float,
    sleep: Callable[[float], object] = time.sleep,
    spawn: Callable[..., subprocess.Popen[bytes]] | None = None,
) -> SignalSample | None:
    """Listen to `source` for `seconds`; None when parec cannot start."""
    capture = SignalCapture(source, spawn)
    if not capture.start():
        return None
    sleep(seconds)
    return capture.stop()


def silence_verdict(verdict: StallProbe, zero_for: float | None, window: float = SILENCE_WINDOW_S) -> StallProbe:
    """A healthy verdict (the pointers move) overruled by the sample content: `zero_for` is how long the unbroken
    run of exact zeros has lasted (None when unknown). Anything but a healthy verdict is returned unchanged, so a
    stall is always reported as a stall."""
    if verdict.stalled is False and verdict.state == "healthy" and zero_for is not None and zero_for >= window:
        return StallProbe(True, "digital-silence", SILENCE_DETAIL, KIND_SILENCE)
    return verdict


class StallMonitor:
    """Samples the microphone's ALSA PCM once per `SAMPLE_S` and judges it over `window` seconds.

    Stateful (it keeps the recent samples); `probe()` is cheap, so the supervisor calls it every second.
    `read`, `clock` are injected for tests.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        read: Callable[[AlsaPcm], PcmSample | None] | None = None,
        window: float = WINDOW_S,
    ) -> None:
        self._clock = clock
        self._read = read
        self.window = window
        self._samples: list[PcmSample] = []
        self._pcm: AlsaPcm | None = None

    def reset(self) -> None:
        self._samples.clear()

    def _sample(self, pcm: AlsaPcm) -> PcmSample | None:
        if self._read is not None:
            got = self._read(pcm)
            return PcmSample(got.state, got.hw_ptr, got.appl_ptr, self._clock()) if got else None
        return read_pcm_status(pcm, self._clock)

    def probe(self, mic: str, sources_json: str, silent: bool = False) -> StallProbe:
        """Judge `mic` from a fresh `pactl -f json list sources` text. `silent`: our own meter heard nothing.

        Only the PCM pointers are judged here; whether the samples are all zeros is `silence_verdict`'s question."""
        info = parse_source(sources_json, mic)
        if info is None:
            self.reset()
            return StallProbe(None, "absent", "the microphone is not listed")
        if info.pcm is None:
            self.reset()
            return StallProbe(None, "unsupported", "not an ALSA device: no stall detection for this source")
        if info.muted:
            self.reset()
            return StallProbe(False, "muted", "the microphone is muted")
        if info.state != "RUNNING":
            self.reset()
            return StallProbe(False, "idle", f"nothing is capturing (source is {info.state.lower() or 'unknown'})")
        if info.pcm != self._pcm:
            self.reset()
            self._pcm = info.pcm
        sample = self._sample(info.pcm)
        if sample is None:
            self.reset()
            return StallProbe(None, "unreadable", f"cannot read {info.pcm.path}")
        now = sample.at
        self._samples = [s for s in self._samples if now - s.at <= self.window + SAMPLE_S] + [sample]
        if now - self._samples[0].at < self.window:
            return StallProbe(None, "watching", "watching the microphone")
        verdict = judge(self._samples)
        if verdict.stalled and silent:
            return StallProbe(
                True, "stalled", verdict.detail + "; our own level meter heard nothing either", KIND_STALL
            )
        if verdict.stalled:
            return StallProbe(True, verdict.state, verdict.detail, KIND_STALL)
        return verdict

    def verify(
        self,
        mic: str,
        run: Run | None = None,
        sleep: Callable[[float], object] = time.sleep,
        timeout: float = VERIFY_S,
    ) -> bool | None:
        """After a revive step: True while still stalled, False once audio flows, None when it cannot tell.

        Uses the same signal, with a short window: healthy the moment RUNNING and both pointers advance.
        """
        run = run or system.run
        listing = run(["pactl", "-f", "json", "list", "sources"], timeout=PACTL_TIMEOUT)
        info = parse_source(listing.stdout, mic) if listing and listing.returncode == 0 else None
        if info is None or info.pcm is None:
            return None
        samples: list[PcmSample] = []
        start = self._clock()
        while True:
            got = self._sample(info.pcm)
            if got is None:
                return None
            samples.append(got)
            if len(samples) > 1 and judge(samples).stalled is False:
                return False
            if self._clock() - start >= timeout:
                break
            sleep(0.5)
        return True if judge(samples).stalled else None


# ---------------------------------------------------------------------------
# Debounce
# ---------------------------------------------------------------------------


class StallDetector:
    """Turns a yes/no condition into a stable flag: set after `confirm` seconds (the monitor already waited out
    its own window, so 0 by default), cleared after `clear`.

    Pure logic with an injected clock, so it is tested without waiting.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        confirm: float = 0.0,
        clear: float = CLEAR_S,
    ) -> None:
        self._clock, self._wall = clock, wall
        self.confirm, self.clear = confirm, clear
        self.stalled = False
        self.since: str | None = None  # ISO local time the condition began
        self._bad_at: float | None = None
        self._bad_wall = 0.0
        self._good_at: float | None = None

    def reset(self) -> None:
        self.stalled, self.since = False, None
        self._bad_at = self._good_at = None

    def update(self, condition: bool) -> str | None:
        """Feed the current condition; returns "started" or "cleared" on the tick the flag flips."""
        now = self._clock()
        if condition:
            self._good_at = None
            if self._bad_at is None:
                self._bad_at, self._bad_wall = now, self._wall()
            if not self.stalled and now - self._bad_at >= self.confirm:
                self.stalled = True
                self.since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self._bad_wall))
                return "started"
            return None
        if not self.stalled:
            self._bad_at = None
            return None
        if self._good_at is None:
            self._good_at = now
        if now - self._good_at >= self.clear:
            self.reset()
            return "cleared"
        return None


# ---------------------------------------------------------------------------
# The health check
# ---------------------------------------------------------------------------


def check_item(stalled: bool | None, detail: str = "", kind: str = KIND_STALL) -> dict[str, str] | None:
    """The doctor / Console health check (same shape as cli.check); None when there is nothing to say.

    A digital-silence fault (`kind == KIND_SILENCE`) is its own check with its own fix, never a "stall"."""
    if stalled is None:
        return None
    if stalled and kind == KIND_SILENCE:
        return {
            "id": SILENCE_CHECK_ID,
            "group": "audio",
            "state": "fail",
            "title": SILENCE_TITLE,
            "detail": detail or SILENCE_DETAIL,
            "hint": SILENCE_HINT,
        }
    if stalled:
        return {
            "id": CHECK_ID,
            "group": "audio",
            "state": "fail",
            "title": STALL_TITLE,
            "detail": detail or "the microphone is capturing nothing that reaches PipeWire",
            "hint": STALL_HINT,
        }
    return {
        "id": CHECK_ID,
        "group": "audio",
        "state": "ok",
        "title": "microphone is delivering audio",
        "detail": detail,
        "hint": "",
    }


# ---------------------------------------------------------------------------
# Revive
# ---------------------------------------------------------------------------


@dataclass
class ReviveResult:
    ok: bool
    message: str
    steps: list[str] = field(default_factory=list)


class ResyncError(Exception):
    """The capture controls could not be re-written (amixer missing, a control refused), with what to do."""


def feature_unit_resync(
    card: int,
    *,
    mixer: alsa.MixerLike | None = None,
    sleep: Callable[[float], object] = time.sleep,
) -> Callable[[], str]:
    """The resync action for ALSA card `card`: re-write its capture switch(es) and volume(s) so the device's own
    feature unit receives them again (the 2026-10-27 fix: `cset` the Mic Capture Switch off and on, then the Mic
    Capture Volume one step away and back). Every control ends at the value it had, so a muted microphone stays muted.

    Returns a callable that does it and says what it touched; it raises `ResyncError` when it cannot. `mixer` and
    `sleep` are injected for tests; the real mixer shells out to `amixer` (package alsa-utils).
    """
    box = mixer or alsa.Mixer(card)

    def resync() -> str:
        if not box.available():
            raise ResyncError("amixer is missing (install alsa-utils)")
        try:
            touched = alsa.resync_capture(box, sleep=sleep, gap=RESYNC_GAP_S)
        except alsa.MixerError as exc:
            raise ResyncError(f"card {card}: {exc}") from exc
        if not touched:
            raise ResyncError(f"card {card} has no capture switch or capture volume control to re-write")
        return f"re-wrote {', '.join(touched)} on card {card}"

    return resync


def find_card(sources_json: str, cards_json: str, source: str) -> tuple[str, str] | None:
    """(card name, active profile) of the card the source belongs to, from `pactl -f json list sources|cards`."""
    try:
        sources = json.loads(sources_json)
        cards = json.loads(cards_json)
    except ValueError:
        return None
    entry = next((s for s in sources if isinstance(s, dict) and s.get("name") == source), None)
    props = entry.get("properties") if isinstance(entry, dict) else None
    if not isinstance(props, dict) or not isinstance(cards, list):
        return None
    wanted = props.get("device.name")
    alsa_card = props.get("alsa.card")
    for card in cards:
        if not isinstance(card, dict) or not card.get("name") or not card.get("active_profile"):
            continue
        card_props = card.get("properties") if isinstance(card.get("properties"), dict) else {}
        if card["name"] == wanted or (alsa_card is not None and str(card_props.get("alsa.card")) == str(alsa_card)):
            return str(card["name"]), str(card["active_profile"])
    return None


def revive_mic(
    mic: str,
    *,
    run: Run | None = None,
    sleep: Callable[[float], object] = time.sleep,
    stalled: Callable[[], bool | None] = lambda: None,
    say: Callable[[str], object] = lambda text: None,
    kind: str = KIND_STALL,
    resync: Callable[[], str] | None = None,
    silent: Callable[[], bool | None] = lambda: None,
) -> ReviveResult:
    """Bring a stalled or digitally silent microphone back, escalating only as far as needed.

    0. only for digital silence (`kind == KIND_SILENCE`): re-write the card's capture switch and volume through
       `resync` (this fixed the 2026-10-27 incident without a replug), then measure the sample content again;
    1. `pactl suspend-source <mic> 1`, 0.4 s, `... 0`, then look again (this fixed the 2026-10-22 incident at once);
    2. cycle the card profile off and back to the one it had, then restore the default source;
    3. otherwise say what the human has to do (replug, restart wireplumber); nothing drastic is automatic.

    `stalled()` re-checks and returns True/False, or None when it cannot tell (then a finished step is
    reported as done but unverified). For digital silence a healthy `stalled()` is not enough: `silent()` must also
    say audio (not zeros) arrives. `run`, `sleep`, `stalled`, `silent`, `resync` and `say` are injected for tests.
    """
    run = run or system.run
    steps: list[str] = []
    digital_silence = kind == KIND_SILENCE

    def pactl(*args: str) -> bool:
        result = run(["pactl", *args], timeout=PACTL_TIMEOUT)
        return bool(result and result.returncode == 0)

    def verdict(step: str) -> ReviveResult | None:
        state = stalled()
        if state is False and digital_silence:
            state = silent()
        if state is False:
            return ReviveResult(True, f"Microphone is back ({step}).", steps)
        if state is None:
            return ReviveResult(True, f"{step.capitalize()} done; the microphone could not be verified.", steps)
        return None

    if digital_silence:
        say("Digital silence. Re-writing the device's capture switch and volume...")
        steps.append("feature-unit resync")
        if resync is None:
            steps.append("resync unavailable: no sound card known for the microphone")
        else:
            try:
                steps.append(resync())
            except ResyncError as exc:
                steps.append(f"resync failed: {exc}")
                say(f"The capture controls could not be re-written ({exc}). Trying the next step...")
            else:
                sleep(SETTLE_S)
                if (done := verdict("capture controls re-written")) is not None:
                    return done

    say("Suspending and resuming the microphone...")
    steps.append("suspend/resume")
    if not pactl("suspend-source", mic, "1"):
        return ReviveResult(False, "The audio server did not accept the suspend request (is pactl working?).", steps)
    sleep(SUSPEND_GAP_S)
    resumed = pactl("suspend-source", mic, "0")
    sleep(SETTLE_S)
    if not resumed:
        return ReviveResult(False, "The audio server did not accept the resume request.", steps)
    if (done := verdict("suspend and resume")) is not None:
        return done

    say("Still not fixed. Cycling the sound card profile...")
    steps.append("card profile cycle")
    ran, failure = _cycle_profile(mic, run, sleep, pactl, steps)
    if failure is not None:
        return failure
    if ran and (done := verdict("card profile cycle")) is not None:
        return done

    steps.append("manual help needed")
    return ReviveResult(False, SILENCE_REPLUG_HINT if digital_silence else REPLUG_HINT, steps)


def _cycle_profile(
    mic: str, run: Run, sleep: Callable[[float], object], pactl: Callable[..., bool], steps: list[str]
) -> tuple[bool, ReviveResult | None]:
    """Card profile off and back on: (whether the cycle ran, a failure result when it went wrong)."""
    sources = run(["pactl", "-f", "json", "list", "sources"], timeout=PACTL_TIMEOUT)
    cards = run(["pactl", "-f", "json", "list", "cards"], timeout=PACTL_TIMEOUT)
    default = run(["pactl", "get-default-source"], timeout=PACTL_TIMEOUT)
    if not (sources and cards and sources.returncode == 0 and cards.returncode == 0):
        steps.append("card lookup failed")
        return False, None
    found = find_card(sources.stdout, cards.stdout, mic)
    if found is None:
        steps.append("card not found")
        return False, None
    card, profile = found
    previous_default = default.stdout.strip() if default and default.returncode == 0 else ""
    if not pactl("set-card-profile", card, "off"):
        return False, ReviveResult(False, "The audio server did not accept the card profile change.", steps)
    sleep(PROFILE_OFF_S)
    restored = pactl("set-card-profile", card, profile)
    deadline = SOURCE_BACK_S
    while restored and deadline > 0 and not _source_listed(run, mic):
        sleep(0.5)
        deadline -= 0.5
    if not restored:
        pactl("set-card-profile", card, profile)  # one more try: leaving the card off would be worse than anything else
        failed = f"Could not restore the card profile {profile!r}; set it in the sound settings."
        return False, ReviveResult(False, failed, steps)
    if previous_default:
        pactl("set-default-source", previous_default)
    sleep(SETTLE_S)
    return True, None


def _source_listed(run: Run, mic: str) -> bool:
    result = run(["pactl", "list", "short", "sources"], timeout=PACTL_TIMEOUT)
    return bool(
        result and result.returncode == 0 and any(mic in line.split("\t") for line in result.stdout.splitlines())
    )


def reprobe_wireplumber(
    *,
    run: Run | None = None,
    sleep: Callable[[float], object] = time.sleep,
    ready: Callable[[], bool],
    say: Callable[[str], object] = lambda text: None,
    wait: float = REPROBE_WAIT_S,
) -> ReviveResult:
    """Restart the user's WirePlumber so it probes the sound cards again, then wait until `ready()` says the card
    has its input profile back (the 2026-10-27 fix for a card that came up with output profiles only).

    This briefly drops every audio stream of this user (a running call goes silent for a moment), so it is only ever
    run when the human asks for it (`streamstack mic-revive --reprobe`), never by a watchdog.
    """
    run = run or system.run
    steps = ["wireplumber restart"]
    say("Restarting WirePlumber so it probes the sound cards again (audio streams drop for a moment)...")
    result = run(["systemctl", "--user", "restart", "wireplumber"], timeout=30)
    if result is None or result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() if result is not None else "systemctl did not run"
        return ReviveResult(False, f"WirePlumber did not restart: {detail}", steps)
    waited = 0.0
    while waited < wait:
        sleep(1.0)
        waited += 1.0
        if ready():
            return ReviveResult(True, "WirePlumber re-probed the card and the microphone input is back.", steps)
    steps.append("input profile still missing")
    return ReviveResult(
        False,
        "WirePlumber restarted but the card still has no input profile. Unplug and replug the interface's USB cable "
        "(another user's WirePlumber probing it at the same moment can leave it output-only).",
        steps,
    )
