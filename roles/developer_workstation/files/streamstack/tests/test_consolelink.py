"""The Console link: how the auto director reaches the native engine, against a real local HTTP server."""

from __future__ import annotations

import json
import stat
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from streamstack import consolelink
from streamstack.console import launcher
from streamstack.consolelink import ConsoleLink, ConsoleRefused, ConsoleUnavailable

TOKEN = "t0ken"
JPEG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"


class FakeConsole:
    """The Console's answers, steered by the test, and a record of what it was asked."""

    def __init__(self) -> None:
        self.video: object = {
            "selected_source": "program",
            "engine": {"state": "running"},
            "privacy": False,
            "look": {"brightness": 0.1, "contrast": 1.0, "warmth": 0.2, "lut_enabled": True, "extra": 5},
        }
        self.refuse: str | None = None
        self.requests: list[tuple[str, str, dict | None, str | None]] = []


@pytest.fixture
def console() -> Iterator[tuple[FakeConsole, ConsoleLink]]:
    fake = FakeConsole()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, kind: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            fake.requests.append((self.command, self.path, body, self.headers.get(consolelink.TOKEN_HEADER)))
            if fake.refuse:
                self._send(409, json.dumps({"error": fake.refuse}).encode())
            elif self.path == "/api/state":
                self._send(200, json.dumps({"video": fake.video}).encode())
            elif self.path.startswith("/api/preview.jpg"):
                self._send(200, JPEG, "image/jpeg")
            elif self.path == "/api/action":
                self._send(200, b"{}")
            else:
                self._send(404, b"{}")

        do_GET = do_POST = _handle

        def log_message(self, format: str, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fake, ConsoleLink(server.server_address[1], TOKEN)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_every_call_carries_the_token(console: tuple[FakeConsole, ConsoleLink]) -> None:
    fake, link = console
    link.state()
    link.set_look("program", {"brightness": 0.3})
    assert [request[3] for request in fake.requests] == [TOKEN, TOKEN]


def test_the_camera_on_air_is_the_one_to_grade(console: tuple[FakeConsole, ConsoleLink]) -> None:
    fake, link = console
    assert link.camera_sources() == ["program"]
    assert isinstance(fake.video, dict)
    fake.video["privacy"] = True
    assert link.camera_sources() == []  # blacked out: nothing to grade
    fake.video.update(privacy=False, engine={"state": "starting"})
    assert link.camera_sources() == []  # the engine is not on air yet
    fake.video.update(engine={"state": "running"}, selected_source="")
    assert link.camera_sources() == []  # no camera


def test_the_look_is_the_eight_values_the_director_moves(console: tuple[FakeConsole, ConsoleLink]) -> None:
    _, link = console
    assert link.look("program") == {
        "brightness": 0.1,
        "contrast": 1.0,
        "warmth": 0.2,
        "lut_enabled": True,
    }
    with pytest.raises(ConsoleRefused, match="not the camera"):
        link.look("elsewhere")


def test_a_look_change_is_one_video_look_action(console: tuple[FakeConsole, ConsoleLink]) -> None:
    fake, link = console
    link.set_look("program", {"warmth": 0.4, "sharpen_enabled": True})
    method, path, body, _ = fake.requests[-1]
    assert (method, path) == ("POST", "/api/action")
    assert body == {"action": "video.look", "source": "program", "values": {"warmth": 0.4, "sharpen_enabled": True}}


def test_the_graded_picture_is_written_privately(console: tuple[FakeConsole, ConsoleLink], tmp_path: Path) -> None:
    fake, link = console
    target = tmp_path / "shot.jpg"
    assert link.screenshot("program", target, width=640) == target
    assert target.read_bytes() == JPEG
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert fake.requests[-1][1] == "/api/preview.jpg?source=program&width=640"


def test_a_refusal_carries_the_consoles_own_words(console: tuple[FakeConsole, ConsoleLink]) -> None:
    fake, link = console
    fake.refuse = "Blackout is on."
    with pytest.raises(ConsoleRefused, match=r"Blackout is on\."):
        link.set_look("program", {"brightness": 0.0})


def test_an_unreadable_state_is_reported_as_unavailable(console: tuple[FakeConsole, ConsoleLink]) -> None:
    fake, link = console
    fake.video = "nonsense"
    with pytest.raises(ConsoleUnavailable, match="no video state"):
        link.state()


def test_a_console_that_is_not_listening_is_unavailable() -> None:
    server = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = server.server_address[1]
    server.server_close()  # nothing answers on this port now
    with pytest.raises(ConsoleUnavailable, match="not answering"):
        ConsoleLink(port, TOKEN, timeout=1).state()


def test_the_link_is_found_through_the_runtime_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher, "read_running", lambda: None)
    assert ConsoleLink.find() is None
    monkeypatch.setattr(launcher, "read_running", lambda: {"port": 8123, "token": "abc"})
    link = ConsoleLink.find()
    assert link is not None and link.base == "http://127.0.0.1:8123" and link.token == "abc"
    link.close()  # nothing is held open, and closing twice is harmless
    link.close()


def test_the_token_header_is_the_one_the_console_checks() -> None:
    from streamstack.console import server

    assert consolelink.TOKEN_HEADER == server.TOKEN_HEADER
