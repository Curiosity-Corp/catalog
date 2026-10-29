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
        self._latest = "null"
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

    def kick(self) -> None:
        """Push a fresh snapshot as soon as the rate limit allows (after an action)."""
        self._pending = True
        self._wake.set()

    def refresh(self) -> bool:
        """Snapshot the rig now; broadcast it when it differs from the last one. True when it changed."""
        began = time.monotonic()
        text = self.encode(self.rig.snapshot())
        took = time.monotonic() - began
        if took >= SLOW_SNAPSHOT_S:
            print(f"streamstack console: the state snapshot took {took:.1f} s", file=sys.stderr, flush=True)
        frame = sse("state", text)
        with self._lock:
            if self._have_state and text == self._latest:
                return False
            self._latest, self._have_state = text, True
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
