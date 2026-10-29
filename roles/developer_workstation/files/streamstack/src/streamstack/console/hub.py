"""Fan-out of rig state, live meters and toasts to every connected Console window (Server-Sent Events)."""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
import traceback
from collections.abc import Callable
from typing import TYPE_CHECKING

from streamstack import crash

if TYPE_CHECKING:
    from streamstack.console.rig import Rig

METERS_HZ = 30
STATE_POLL_SECONDS = 1.0
SLOW_SNAPSHOT_S = 1.0  # a snapshot that takes longer is logged: every window waits for it
STATE_MIN_GAP = 0.25  # at most four state pushes a second, however fast actions arrive
CLIENT_QUEUE = 64


def sse(event: str, data: str) -> bytes:
    return f"event: {event}\ndata: {data}\n\n".encode()


class Client:
    """One SSE connection: a bounded queue the HTTP handler thread drains.

    A window that stops reading must never slow the others, so `offer` never
    blocks: meter frames are dropped first, and when the queue is still full
    the oldest frame goes (state frames are complete snapshots, so losing an
    old one costs nothing).
    """

    def __init__(self) -> None:
        self.frames: queue.Queue[bytes | None] = queue.Queue(maxsize=CLIENT_QUEUE)
        self.dropped = 0

    def offer(self, frame: bytes, droppable: bool = False) -> None:
        try:
            self.frames.put_nowait(frame)
            return
        except queue.Full:
            self.dropped += 1
        if droppable:
            return
        try:
            self.frames.get_nowait()
            self.frames.put_nowait(frame)
        except (queue.Empty, queue.Full):
            pass  # another thread raced us; the next state push replaces this one

    def close(self) -> None:
        """Wake the handler thread with the end-of-stream marker, even when its queue is full."""
        while True:
            try:
                self.frames.put_nowait(None)
                return
            except queue.Full:
                try:
                    self.frames.get_nowait()
                except queue.Empty:
                    pass

    def get(self, timeout: float) -> bytes | None:
        """The next frame, b"" after `timeout` seconds of quiet, None when the hub is closing."""
        try:
            return self.frames.get(timeout=timeout)
        except queue.Empty:
            return b""


class Hub:
    def __init__(self, rig: Rig, meters_hz: float = METERS_HZ, poll_seconds: float = STATE_POLL_SECONDS) -> None:
        self.rig = rig
        self.meter_period = 1.0 / meters_hz
        self.poll_seconds = poll_seconds
        self._clients: set[Client] = set()
        self._lock = threading.Lock()
        self._latest = "null"  # the newest broadcast snapshot, with its `seq`
        self._latest_body = "null"  # the same without `seq`: what "changed" is judged on
        self._seq = 0  # snapshots begun so far (see `mark`)
        self._ack_wanted = False
        self._have_state = False
        self._wake = threading.Event()
        self._pending = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_client_seen = time.monotonic()
        # Called with every meter frame, even with no window open (telemetry counts clips that way).
        self.meter_listeners: list[Callable[[dict], None]] = []
        rig.on_toast = self.toast

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="console-hub", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=3)
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            client.close()

    # -- clients --------------------------------------------------------------

    def subscribe(self) -> Client:
        client = Client()
        self.state_json()  # the first window builds the snapshot (outside the lock: it asks the whole rig)
        # Hand over the newest snapshot and join the broadcast in one step with `refresh`: a push that lands between
        # the two would otherwise reach neither, and a window that opened just as an action landed would show the old
        # state until the next change (a switch that was clicked and never turned over).
        with self._lock:
            client.offer(sse("state", self._latest))
            self._clients.add(client)
        return client

    def unsubscribe(self, client: Client) -> None:
        with self._lock:
            self._clients.discard(client)
            self._last_client_seen = time.monotonic()

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def idle_seconds(self) -> float:
        """How long no window has been listening (0 while one is)."""
        with self._lock:
            return 0.0 if self._clients else time.monotonic() - self._last_client_seen

    # -- state ----------------------------------------------------------------

    @staticmethod
    def encode(snapshot: dict) -> str:
        return json.dumps(snapshot, separators=(",", ":"), sort_keys=True)

    def state_json(self) -> str:
        """The latest broadcast snapshot as JSON; the first caller before the loop ran builds it."""
        with self._lock:
            if self._have_state:
                return self._latest
        self.refresh()
        with self._lock:
            return self._latest

    def _begin(self) -> tuple[int, bool]:
        """Number a snapshot that is about to be built, and take the pending acknowledgement request."""
        with self._lock:
            self._seq += 1
            wanted, self._ack_wanted = self._ack_wanted, False
            return self._seq, wanted

    def mark(self) -> int:
        """The number of the first snapshot that will be built after this call.

        An action's answer carries it as `seq`: the Console keeps what it just did on screen until a snapshot with this
        number or a higher one arrives, because an older snapshot (built before the action, delivered after its
        answer) does not contain the change. A snapshot is judged by when its build began, so one that was already
        under way when the action finished always has a lower number. A kick follows, and when that snapshot turns out
        identical to the last one, an `ack` event carries its number instead."""
        with self._lock:
            self._ack_wanted = True
            return self._seq + 1

    @staticmethod
    def with_seq(text: str, seq: int) -> str:
        """The encoded snapshot with its number as the first key (a snapshot is always a non-empty object)."""
        return f'{{"seq":{seq},{text[1:]}' if text.startswith("{") and text != "{}" else text

    def fresh_json(self) -> str:
        """A snapshot built now for one caller (GET /api/state), numbered but not broadcast."""
        seq, _ = self._begin()
        return self.with_seq(self.encode(self.rig.snapshot()), seq)

    def kick(self) -> None:
        """Push a fresh snapshot as soon as the rate limit allows (after an action)."""
        self._pending = True
        self._wake.set()

    def refresh(self) -> bool:
        """Snapshot the rig now; broadcast it when it differs from the last one. True when it changed."""
        seq, wanted = self._begin()
        began = time.monotonic()
        body = self.encode(self.rig.snapshot())
        took = time.monotonic() - began
        if took >= SLOW_SNAPSHOT_S:
            print(f"streamstack console: the state snapshot took {took:.1f} s", file=sys.stderr, flush=True)
        text = self.with_seq(body, seq)
        with self._lock:
            if self._have_state and body == self._latest_body:
                if wanted:  # an action finished and nothing it touched shows: say the picture is current
                    ack = sse("ack", json.dumps({"seq": seq}))
                    for client in self._clients:
                        client.offer(ack)
                return False
            self._latest, self._latest_body, self._have_state = text, body, True
            frame = sse("state", text)
            for client in self._clients:  # under the lock, so `subscribe` sees the new state or gets the frame
                client.offer(frame)  # never blocks
        return True

    def toast(self, level: str, text: str) -> None:
        self._broadcast(sse("toast", json.dumps({"level": level, "text": text})))

    def _broadcast(self, frame: bytes, droppable: bool = False) -> None:
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            client.offer(frame, droppable)

    # -- the loop -------------------------------------------------------------

    def _loop(self) -> None:
        last_state = last_meters = 0.0
        while not self._stop.is_set():
            self._wake.clear()
            now = time.monotonic()
            if now - last_state >= (STATE_MIN_GAP if self._pending else self.poll_seconds):
                self._pending = False  # cleared before the snapshot, so a racing kick cannot be lost
                last_state = now
                self._guarded(self.refresh)
            if now - last_meters >= self.meter_period:
                last_meters = now
                self._guarded(self._push_meters)
            next_state = last_state + (STATE_MIN_GAP if self._pending else self.poll_seconds)
            self._wake.wait(max(0.001, min(last_meters + self.meter_period, next_state) - time.monotonic()))

    def _push_meters(self) -> None:
        listeners = list(self.meter_listeners)
        watching = self.client_count() > 0
        if not (watching or listeners):
            return
        frame = self.rig.meters()
        for listener in listeners:
            try:
                listener(frame)
            except Exception:  # a listener bug must never stop the meters
                traceback.print_exc()
        if watching:
            self._broadcast(sse("meters", json.dumps(frame, separators=(",", ":"))), droppable=True)

    def _guarded(self, step) -> None:
        """A controller bug must never kill the loop; log it and carry on."""
        try:
            step()
        except Exception as exc:
            print("streamstack console: hub step failed", file=sys.stderr)
            traceback.print_exc()
            crash.record_exception(exc, "console hub step", "console")
            time.sleep(0.5)
