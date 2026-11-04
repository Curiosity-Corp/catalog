"""The frame hub: the engine's raw program frames, fanned out to the recording and streaming encoders (#184)."""

from __future__ import annotations

import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from streamstack.engine import framehub
from streamstack.engine.framehub import FrameHub

W, H = 64, 48
SIZE = framehub.frame_bytes(W, H)


def frame(n: int) -> bytes:
    return bytes([n % 256]) * SIZE


def wait_for(check, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.01)
    return False


def read_exact(conn: socket.socket, count: int) -> bytes:
    conn.settimeout(5)
    data = b""
    while len(data) < count:
        chunk = conn.recv(count - len(data))
        if not chunk:
            break
        data += chunk
    return data


@pytest.fixture
def hub() -> Iterator[FrameHub]:
    folder = Path(tempfile.mkdtemp(prefix="hub"))  # a short path: a Unix socket path is limited to 108 bytes
    made = FrameHub(folder / "f.sock", W, H, 30.0)
    made.start()
    yield made
    made.stop()
    shutil.rmtree(folder, ignore_errors=True)


def connect(hub: FrameHub) -> socket.socket:
    conn = socket.socket(socket.AF_UNIX)
    conn.connect(str(hub.path))
    assert wait_for(lambda: hub.state()["readers"] >= 1)
    return conn


def test_the_hub_describes_what_it_carries(hub: FrameHub) -> None:
    assert framehub.read_info(hub.path) == {"width": W, "height": H, "fps": 30.0, "pix_fmt": "nv12"}


def test_no_hub_no_description(tmp_path: Path) -> None:
    assert framehub.read_info(tmp_path / "none.sock") is None
    (tmp_path / "x.sock").write_text("")
    assert framehub.read_info(tmp_path / "x.sock") is None  # no readable description
    (tmp_path / "x.sock.json").write_text('{"width": 63, "height": 48, "fps": 30, "pix_fmt": "nv12"}')
    assert framehub.read_info(tmp_path / "x.sock") is None  # an odd side cannot be NV12


def test_every_reader_gets_whole_frames_in_order(hub: FrameHub) -> None:
    first, second = connect(hub), connect(hub)
    assert wait_for(lambda: hub.state()["readers"] == 2)
    stream = frame(1) + frame(2) + frame(3)
    for i in range(0, len(stream), 1000):  # arbitrary pipe read sizes
        hub.feed(stream[i : i + 1000])
        time.sleep(0.005)  # the two-frame queue keeps up with an engine, which is paced
    for conn in (first, second):
        assert read_exact(conn, 3 * SIZE) == stream


def test_a_reader_joins_on_a_frame_boundary(hub: FrameHub) -> None:
    hub.feed(frame(7)[:100])  # half a frame is on the way when the encoder connects
    conn = connect(hub)
    hub.feed(frame(7)[100:] + frame(8))
    assert read_exact(conn, 2 * SIZE) == frame(7) + frame(8)


def test_a_slow_reader_loses_old_frames_and_never_blocks_the_engine(hub: FrameHub) -> None:
    conn = connect(hub)
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
    start = time.monotonic()
    for n in range(200):  # the reader reads nothing
        hub.feed(frame(n))
    assert time.monotonic() - start < 2.0
    assert hub.state()["frames"] == 200
    assert wait_for(lambda: hub.state()["dropped"] > 0)
    conn.close()


def test_a_gone_reader_is_forgotten(hub: FrameHub) -> None:
    conn = connect(hub)
    conn.close()
    assert wait_for(lambda: (hub.feed(frame(1)), hub.state()["readers"] == 0)[1])


def test_a_new_size_closes_the_readers_and_is_described(hub: FrameHub) -> None:
    conn = connect(hub)
    hub.set_geometry(128, 96, 25.0)
    conn.settimeout(5)
    assert conn.recv(10) == b""  # closed: it would misparse the new frames
    assert framehub.read_info(hub.path) == {"width": 128, "height": 96, "fps": 25.0, "pix_fmt": "nv12"}


def test_the_last_frame_is_repeated_while_the_engine_is_silent(hub: FrameHub) -> None:
    conn = connect(hub)
    hub.feed(frame(9))
    got = read_exact(conn, 3 * SIZE)  # the original, then repeats of it
    assert got == frame(9) * 3


def test_a_late_reader_gets_a_picture_at_once(hub: FrameHub) -> None:
    hub.feed(frame(5))
    conn = connect(hub)
    assert read_exact(conn, SIZE) == frame(5)


def test_the_pump_feeds_from_a_pipe_and_closes_it(hub: FrameHub) -> None:
    import os

    read_fd, write_fd = os.pipe()
    conn = connect(hub)
    thread = threading.Thread(target=hub.pump, args=(read_fd,))
    thread.start()
    os.write(write_fd, frame(4) + frame(6))
    os.close(write_fd)
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert read_exact(conn, 2 * SIZE) == frame(4) + frame(6)
    with pytest.raises(OSError):
        os.fstat(read_fd)  # the hub closed its end


def test_stop_removes_the_socket(hub: FrameHub) -> None:
    hub.stop()
    assert not hub.path.exists() and framehub.read_info(hub.path) is None


def test_a_stale_socket_of_a_crashed_console_is_replaced() -> None:
    folder = Path(tempfile.mkdtemp(prefix="hub"))
    try:
        path = folder / "f.sock"
        path.write_text("stale")
        made = FrameHub(path, W, H, 30.0)
        made.start()
        try:
            assert connect(made) is not None
        finally:
            made.stop()
    finally:
        shutil.rmtree(folder, ignore_errors=True)
