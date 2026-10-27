"""The Console server: auth, headers, static files, actions, SSE, MJPEG and the single-instance runtime file."""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

import pytest
from http_support import HTTP_TIMEOUT

from streamstack.console import launcher
from streamstack.console.errors import ActionError
from streamstack.console.hub import Client, Hub, sse
from streamstack.console.rig import Rig
from streamstack.console.server import ConsoleServer, safe_join
from streamstack.console.studio import DemoStudio

JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes\xff\xd9"


class FakeAudio:
    def __init__(self, clip: Path | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.gain = 0.0
        self.clip = clip
        self.started = self.stopped = False
        self.runtime_file_seen = False
        self.empty = False

    def state(self) -> dict:
        return {"mic": {"gain_db": self.gain, "muted": False}}

    def meters(self) -> dict:
        if self.empty:
            return {}
        return {"t": time.time(), "mic": {"peak_db": -18.0, "rms_db": -27.0, "clip": False}, "voice": None}

    def act(self, action: str, params: dict) -> str | None:
        self.calls.append((action, params))
        if action == "audio.gain":
            db = params.get("db")
            if not isinstance(db, (int, float)):
                raise ActionError("db must be a number.", 400)
            self.gain = float(db)
            return None
        if action == "audio.absent":
            raise ActionError("The microphone is not connected.", 409)
        if action == "audio.boom":
            raise RuntimeError("kaboom")
        return "done"

    def clip_path(self, clip_id: str) -> Path | None:
        return self.clip if clip_id == "abc" else None

    def start(self) -> None:
        self.started = True
        self.runtime_file_seen = launcher.runtime_file().exists()

    def stop(self) -> None:
        self.stopped = True


class FakeVideo:
    def __init__(self) -> None:
        self.served = 0

    def state(self) -> dict:
        return {"selected_source": "program", "scenes": []}

    def act(self, action: str, params: dict) -> str | None:
        return None

    def frame(self, source: str, width: int) -> bytes | None:
        if source == "none":
            return None
        self.served += 1
        return JPEG

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    root = tmp_path / "console"
    (root / "pages").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>Console</title>")
    (root / "app.js").write_text("export const x = 1;\n")
    (root / "theme.css").write_text("body{}")
    (root / "pages" / "icon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (tmp_path / "secret.txt").write_text("top secret")
    (root / "escape").symlink_to(tmp_path / "secret.txt")
    return root


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    path = tmp_path / "clip.wav"
    path.write_bytes(b"RIFF" + bytes(range(100)))
    return path


@pytest.fixture
def rig(clip: Path) -> Rig:
    # A frozen clock: the demo studio's CPU figure is a function of time, and a snapshot that changes by
    # itself would make "pushed only on change" depend on how fast the test runs.
    return Rig(FakeAudio(clip), FakeVideo(), DemoStudio(delay=0, clock=lambda: 0.0))


@pytest.fixture
def server(rig: Rig, static_dir: Path) -> Iterator[ConsoleServer]:
    srv = ConsoleServer(rig, port=0, token="s3cret-token", static_dir=static_dir, allow_hosts=set())
    srv.start()
    yield srv
    srv.close()


def request(
    server: ConsoleServer,
    method: str,
    path: str,
    body: object = None,
    headers: dict[str, str] | None = None,
    token: bool = True,
    raw_body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=HTTP_TIMEOUT)
    send = dict(headers or {})
    if token:
        send.setdefault("X-StreamStack-Token", server.token)
    payload = raw_body
    if body is not None:
        payload = json.dumps(body).encode()
        send.setdefault("Content-Type", "application/json")
    try:
        conn.request(method, path, body=payload, headers=send)
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


def post(server: ConsoleServer, body: object) -> tuple[int, dict]:
    status, _, data = request(server, "POST", "/api/action", body)
    return status, json.loads(data)


def frame(client: Client, timeout: float) -> bytes:
    return client.get(timeout) or b""


def stream(resp: http.client.HTTPResponse) -> BinaryIO:
    assert resp.fp is not None
    return resp.fp


def readline(resp: http.client.HTTPResponse) -> bytes:
    return stream(resp).readline()


def read_events(resp: http.client.HTTPResponse, wanted: set[str], timeout: float = 5.0) -> dict[str, list[dict]]:
    """Read SSE frames until every wanted event name was seen."""
    seen: dict[str, list[dict]] = {}
    deadline = time.monotonic() + timeout
    name = None
    while time.monotonic() < deadline and not wanted <= seen.keys():
        line = readline(resp).decode().rstrip("\n")
        if line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: ") and name:
            seen.setdefault(name, []).append(json.loads(line[6:]))
    return seen


# -- auth, host and headers ---------------------------------------------------


def test_api_needs_the_token(server: ConsoleServer) -> None:
    assert request(server, "GET", "/api/state", token=False)[0] == 401
    assert request(server, "GET", "/api/state", headers={"X-StreamStack-Token": "nope"}, token=False)[0] == 401
    assert request(server, "GET", "/api/state?token=nope", token=False)[0] == 401
    assert request(server, "GET", "/api/state")[0] == 200
    assert request(server, "GET", f"/api/state?token={server.token}", token=False)[0] == 200
    status, _, _ = request(server, "POST", "/api/action", {"action": "audio.mute"}, token=False)
    assert status == 401


def test_static_files_need_no_token(server: ConsoleServer) -> None:
    assert request(server, "GET", "/", token=False)[0] == 200
    assert request(server, "GET", "/static/app.js", token=False)[0] == 200


def test_host_header_must_be_loopback(server: ConsoleServer) -> None:
    assert request(server, "GET", "/api/state", headers={"Host": "evil.example"})[0] == 403
    assert request(server, "GET", "/api/state", headers={"Host": f"127.0.0.1:{server.port + 1}"})[0] == 403
    assert request(server, "GET", "/", headers={"Host": "evil.example"}, token=False)[0] == 403
    assert request(server, "GET", "/api/state", headers={"Host": f"localhost:{server.port}"})[0] == 200


def test_extra_hosts_can_be_allowed(rig: Rig, static_dir: Path) -> None:
    srv = ConsoleServer(rig, token="t", static_dir=static_dir, allow_hosts={"console.example", "proxy.example:8443"})
    srv.start()
    try:
        assert request(srv, "GET", "/api/state", headers={"Host": "console.example:1234"})[0] == 200
        assert request(srv, "GET", "/api/state", headers={"Host": "proxy.example:8443"})[0] == 200
        assert request(srv, "GET", "/api/state", headers={"Host": "other.example"})[0] == 403
    finally:
        srv.close()
    anywhere = ConsoleServer(rig, token="t", static_dir=static_dir, allow_hosts={"*"})
    try:
        assert anywhere.host_allowed("whatever.example")
    finally:
        anywhere.server_close()


def test_security_headers_and_no_cors(server: ConsoleServer) -> None:
    for path in ("/api/state", "/", "/static/app.js"):
        _, headers, _ = request(server, "GET", path)
        assert headers["cache-control"] == "no-store"
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "no-referrer"
        assert "default-src 'self'" in headers["content-security-policy"]
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert not [h for h in headers if h.startswith("access-control-")]
    _, headers, _ = request(server, "OPTIONS", "/api/action", headers={"Origin": "https://evil.example"})
    assert not [h for h in headers if h.startswith("access-control-")]


# -- static files -------------------------------------------------------------


def test_static_mime_types(server: ConsoleServer) -> None:
    assert request(server, "GET", "/")[1]["content-type"].startswith("text/html")
    assert request(server, "GET", "/static/app.js")[1]["content-type"].startswith("text/javascript")
    assert request(server, "GET", "/static/theme.css")[1]["content-type"].startswith("text/css")
    assert request(server, "GET", "/static/pages/icon.svg")[1]["content-type"] == "image/svg+xml"


@pytest.mark.parametrize(
    "path",
    [
        "/static/../secret.txt",
        "/static/%2e%2e/secret.txt",
        "/static/..%2fsecret.txt",
        "/static/pages/../../secret.txt",
        "/static/..\\secret.txt",
        "/static//etc/passwd",
        "/static/escape",  # a symlink leading out of the directory
        "/static/missing.js",
        "/static/pages",
        "/static/%00",
    ],
)
def test_static_traversal_is_blocked(server: ConsoleServer, path: str) -> None:
    status, _, body = request(server, "GET", path, token=False)
    assert status == 404
    assert b"top secret" not in body and b"root:" not in body


def test_safe_join_rejects_escapes(static_dir: Path) -> None:
    assert safe_join(static_dir, "app.js") == (static_dir / "app.js").resolve()
    assert safe_join(static_dir, "../secret.txt") is None
    assert safe_join(static_dir, "escape") is None
    assert safe_join(static_dir, "") is None


def test_missing_frontend_gets_the_builtin_page(rig: Rig, tmp_path: Path) -> None:
    srv = ConsoleServer(rig, token="t", static_dir=tmp_path / "nothing-here", allow_hosts=set())
    srv.start()
    try:
        status, headers, body = request(srv, "GET", "/", token=False)
        assert status == 200
        assert b"frontend is not installed" in body
        assert "script-src" not in headers["content-security-policy"]
        assert request(srv, "GET", "/static/app.js", token=False)[0] == 404
    finally:
        srv.close()


# -- state and actions ----------------------------------------------------------


def test_state_snapshot_has_every_section(server: ConsoleServer) -> None:
    status, headers, body = request(server, "GET", "/api/state")
    state = json.loads(body)
    assert status == 200 and headers["content-type"] == "application/json"
    assert set(state) == {
        "version", "demo", "studio", "video", "audio", "auto", "framing", "health", "system", "lighting",
        "interface", "virtual", "presets", "hardware", "stream", "storage", "deck", "diagnostics", "call", "slots",
        "hotkeys",
    }  # fmt: skip
    assert state["audio"]["mic"]["gain_db"] == 0.0
    assert state["studio"]["running"] is True


def test_action_routes_by_prefix_and_passes_params(server: ConsoleServer, rig: Rig) -> None:
    assert post(server, {"action": "audio.gain", "db": 3.5}) == (200, {"ok": True})
    assert rig.audio.calls == [("audio.gain", {"db": 3.5})]
    status, answer = post(server, {"action": "audio.mute", "on": True})
    assert (status, answer) == (200, {"ok": True, "message": "done"})
    status, answer = post(server, {"action": "studio.stop"})
    assert status == 200 and answer["message"] == "Stopping the studio..."
    state = json.loads(request(server, "GET", "/api/state")[2])
    assert state["studio"]["running"] is False
    assert state["audio"]["mic"]["gain_db"] == 3.5


def test_action_errors_become_json(server: ConsoleServer) -> None:
    assert post(server, {"action": "audio.gain", "db": "loud"}) == (400, {"ok": False, "error": "db must be a number."})
    assert post(server, {"action": "audio.absent"}) == (409, {"ok": False, "error": "The microphone is not connected."})
    assert post(server, {"action": "nothing.here"})[0] == 400
    assert post(server, {"action": "no-dot"})[0] == 400
    assert post(server, {"action": "../etc"})[0] == 400
    assert post(server, {"action": 7})[0] == 400
    assert post(server, {"action": "open.app", "name": "rm -rf"})[0] == 400


def test_unexpected_errors_are_500_and_never_kill_the_server(
    server: ConsoleServer, capfd: pytest.CaptureFixture
) -> None:
    status, answer = post(server, {"action": "audio.boom"})
    assert status == 500 and answer["ok"] is False and "kaboom" in answer["error"]
    assert "Traceback" in capfd.readouterr().err
    assert request(server, "GET", "/api/state")[0] == 200


def test_post_body_rules(server: ConsoleServer) -> None:
    assert request(server, "POST", "/api/action", raw_body=b'{"action":"audio.mute"}')[0] == 415
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    assert request(server, "POST", "/api/action", raw_body=b"action=audio.mute", headers=form)[0] == 415
    json_type = {"Content-Type": "application/json"}
    assert request(server, "POST", "/api/action", raw_body=b"{not json", headers=json_type)[0] == 400
    assert request(server, "POST", "/api/action", raw_body=b"[1, 2]", headers=json_type)[0] == 400
    assert request(server, "POST", "/api/action", raw_body=b"{}", headers=json_type)[0] == 400
    assert request(server, "POST", "/api/action", raw_body=b" " * (64 * 1024 + 1), headers=json_type)[0] == 413
    assert request(server, "GET", "/api/action")[0] == 405
    assert request(server, "POST", "/api/state", {"x": 1})[0] == 405
    assert request(server, "DELETE", "/api/state")[0] == 405
    assert request(server, "GET", "/api/nope")[0] == 404
    assert request(server, "GET", "/elsewhere", token=False)[0] == 404
    assert request(server, "POST", "/", {"x": 1}, token=False)[0] == 405


def test_keep_alive_connection_survives_errors(server: ConsoleServer) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=HTTP_TIMEOUT)
    headers = {"X-StreamStack-Token": server.token, "Content-Type": "application/json"}
    try:
        for body, expected in ((b'{"action":"audio.absent"}', 409), (b'{"action":"audio.gain","db":1}', 200)):
            conn.request("POST", "/api/action", body=body, headers=headers)
            resp = conn.getresponse()
            resp.read()
            assert resp.status == expected
    finally:
        conn.close()


# -- SSE ------------------------------------------------------------------------


def open_events(server: ConsoleServer, **kwargs: str) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=HTTP_TIMEOUT)
    conn.request("GET", f"/api/events?token={server.token}", headers=kwargs)
    return conn, conn.getresponse()


def test_events_stream_state_meters_and_toasts(server: ConsoleServer, rig: Rig) -> None:
    conn, resp = open_events(server)
    try:
        assert resp.status == 200 and resp.getheader("Content-Type") == "text/event-stream"
        seen = read_events(resp, {"state", "meters"})
        assert seen["state"][0]["audio"]["mic"]["gain_db"] == 0.0
        assert seen["meters"][0]["mic"]["peak_db"] == -18.0
        rig.audio.notify("warn", "Mic is hot")
        assert read_events(resp, {"toast"})["toast"][0] == {"level": "warn", "text": "Mic is hot"}
        post(server, {"action": "audio.gain", "db": 5})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            states = read_events(resp, {"state"})["state"]
            if states[-1]["audio"]["mic"]["gain_db"] == 5.0:
                break
        else:
            pytest.fail("no state event after the action")
    finally:
        conn.close()


def test_events_need_the_token(server: ConsoleServer) -> None:
    assert request(server, "GET", "/api/events", token=False)[0] == 401


def test_state_is_pushed_only_on_change(rig: Rig) -> None:
    hub = Hub(rig)
    client = hub.subscribe()
    assert frame(client, 1).startswith(b"event: state")
    assert hub.refresh() is False
    assert client.get(0.05) == b""
    rig.audio.gain = 2.0
    assert hub.refresh() is True
    assert b'"gain_db":2.0' in frame(client, 1)


def test_state_pushes_are_rate_limited(rig: Rig) -> None:
    hub = Hub(rig, poll_seconds=0.05)
    client = hub.subscribe()
    hub.start()
    try:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            rig.audio.gain += 1  # the state differs every time the hub looks
            hub.kick()
            time.sleep(0.005)
    finally:
        hub.stop()
    states = 0
    while True:
        frame = client.get(0)
        if not frame:
            break
        states += frame.startswith(b"event: state")
    assert 2 <= states <= 7  # 4 a second plus the initial snapshot, never one per kick


def test_a_slow_client_never_stalls_the_others(rig: Rig) -> None:
    hub = Hub(rig)
    slow, fast = hub.subscribe(), hub.subscribe()
    for _ in range(500):
        hub._broadcast(sse("meters", "{}"), droppable=True)
        assert fast.get(0)  # the fast one drains in step
    assert slow.frames.qsize() <= 64 and slow.dropped > 0
    hub.toast("info", "later")  # still goes through, replacing the oldest frame
    assert b"later" in b"".join(iter(lambda: frame(slow, 0), b""))


def test_client_close_wakes_a_full_queue() -> None:
    client = Client()
    for _ in range(100):
        client.offer(b"x")
    client.close()
    frames = list(iter(lambda: client.get(0), b""))
    assert frames[-1] is None


def test_closing_the_server_ends_open_streams(rig: Rig, static_dir: Path) -> None:
    srv = ConsoleServer(rig, token="t", static_dir=static_dir, allow_hosts=set())
    srv.start()
    conn, resp = open_events(srv)
    assert resp.status == 200
    read_events(resp, {"state"})
    closer = threading.Thread(target=srv.close)
    closer.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and readline(resp):
        pass
    assert readline(resp) == b""
    conn.close()
    closer.join(timeout=5)
    assert rig.audio.stopped


# -- previews and clips -----------------------------------------------------------


def test_preview_jpg(server: ConsoleServer) -> None:
    status, headers, body = request(server, "GET", "/api/preview.jpg?source=program&width=320")
    assert (status, headers["content-type"], body) == (200, "image/jpeg", JPEG)
    assert request(server, "GET", "/api/preview.jpg?source=none")[0] == 503
    assert request(server, "GET", "/api/preview.jpg?width=wide")[0] == 400
    assert request(server, "GET", "/api/preview.jpg?source=" + "x" * 200)[0] == 400
    assert request(server, "GET", "/api/preview.mjpg?source=none")[0] == 503
    assert request(server, "GET", "/api/preview.jpg", token=False)[0] == 401


def test_director_sheet_is_served_to_the_page_only(
    server: ConsoleServer, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, headers, body = request(server, "GET", "/api/director/sheet.jpg?v=1")
    assert (status, headers["content-type"]) == (200, "image/jpeg") and body[:2] == b"\xff\xd8"
    assert request(server, "GET", "/api/director/sheet.jpg", token=False)[0] == 401
    monkeypatch.setattr(rig.studio, "sheet", lambda: None)
    assert request(server, "GET", "/api/director/sheet.jpg")[0] == 404


def test_preview_stream_is_multipart_and_stops_with_the_client(server: ConsoleServer, rig: Rig) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=HTTP_TIMEOUT)
    conn.request("GET", f"/api/preview.mjpg?fps=15&width=320&token={server.token}")
    resp = conn.getresponse()
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "multipart/x-mixed-replace; boundary=frame"
    for _ in range(2):
        assert readline(resp).strip() == b"--frame"
        assert readline(resp).strip() == b"Content-Type: image/jpeg"
        length = int(readline(resp).split(b":")[1])
        assert readline(resp).strip() == b""
        assert stream(resp).read(length) == JPEG
        assert readline(resp).strip() == b""
    resp.close()
    conn.close()
    time.sleep(0.5)
    served = rig.video.served
    time.sleep(0.5)
    assert rig.video.served == served  # the loop noticed the closed socket


def test_clips_are_served_with_ranges(server: ConsoleServer, clip: Path) -> None:
    data = clip.read_bytes()
    status, headers, body = request(server, "GET", "/api/clips/abc.wav")
    assert (status, headers["content-type"], body) == (200, "audio/wav", data)
    status, headers, body = request(server, "GET", "/api/clips/abc.wav", headers={"Range": "bytes=4-9"})
    assert (status, body) == (206, data[4:10])
    assert headers["content-range"] == f"bytes 4-9/{len(data)}"
    assert request(server, "GET", "/api/clips/abc.wav", headers={"Range": "bytes=999-"})[0] == 416
    assert request(server, "GET", "/api/clips/other.wav")[0] == 404
    assert request(server, "GET", "/api/clips/..%2Fetc.wav")[0] == 404


# -- the rig ----------------------------------------------------------------------


def test_rig_wires_notify_and_lifecycle(rig: Rig) -> None:
    toasts: list[tuple[str, str]] = []
    rig.on_toast = lambda level, text: toasts.append((level, text))
    rig.studio.notify("ok", "from studio")
    rig.video.notify("bogus", "odd level")
    assert toasts == [("ok", "from studio"), ("info", "odd level")]
    rig.start()
    rig.start()
    assert rig.audio.started
    rig.stop()
    assert rig.audio.stopped


def test_rig_meters_always_have_every_key() -> None:
    audio = FakeAudio()
    audio.empty = True
    frame = Rig(audio, FakeVideo(), DemoStudio(delay=0)).meters()
    assert {"t", "mic", "voice"} <= frame.keys()


def test_rig_rejects_unknown_actions(rig: Rig) -> None:
    for action in ("", "x", "audio", "evil.thing", "Audio.mute", "audio.mute; rm", None, 3):
        with pytest.raises(ActionError) as caught:
            rig.act(action)
        assert caught.value.status == 400


# -- runtime file and single instance -----------------------------------------------


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "run"
    path.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(path))
    return path


def test_runtime_file_is_private_and_round_trips(runtime: Path, server: ConsoleServer) -> None:
    launcher.write_runtime(os.getpid(), server.port, server.token)
    assert (runtime / "streamstack-console.json").stat().st_mode & 0o777 == 0o600
    assert launcher.read_running() == {"pid": os.getpid(), "port": server.port, "token": server.token}


def test_stale_runtime_files_are_ignored(runtime: Path, server: ConsoleServer) -> None:
    gone = subprocess.Popen(["true"])
    gone.wait()
    launcher.write_runtime(gone.pid, server.port, "t")
    assert launcher.read_running() is None  # dead pid
    launcher.write_runtime(os.getpid(), 1, "t")
    assert launcher.read_running() is None  # nothing listens on the port
    for text in ("", "not json", "[]", '{"pid": "x", "port": 1, "token": "t"}', '{"pid": 1, "port": 2}'):
        (runtime / "streamstack-console.json").write_text(text)
        assert launcher.read_running() is None


def test_remove_runtime_only_removes_our_own(runtime: Path) -> None:
    launcher.write_runtime(os.getpid() + 1, 5, "t")
    launcher.remove_runtime(os.getpid())
    assert launcher.runtime_file().exists()
    launcher.remove_runtime(os.getpid() + 1)
    assert not launcher.runtime_file().exists()


def test_second_launch_only_opens_a_window(
    runtime: Path, server: ConsoleServer, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    launcher.write_runtime(os.getpid(), server.port, server.token)
    opened: list[tuple[str, bool]] = []
    monkeypatch.setattr(launcher, "open_window", lambda url, mini=False: opened.append((url, mini)) or True)
    monkeypatch.setattr(Rig, "live", classmethod(lambda cls, cfg=None: pytest.fail("started a second server")))
    assert launcher.run_console(window=True, mini=True) == 0
    assert opened == [(f"http://127.0.0.1:{server.port}/?token={server.token}", True)]
    assert f"?token={server.token}#/mini" in capsys.readouterr().out


def test_run_console_serves_then_cleans_up(
    runtime: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    fake = Rig(FakeAudio(), FakeVideo(), DemoStudio(delay=0))
    monkeypatch.setattr(Rig, "demo", classmethod(lambda cls: fake))
    monkeypatch.setattr(launcher, "open_window", lambda url, mini=False: pytest.fail("--no-window opened one"))
    assert launcher.run_console(demo=True, window=False, idle_exit=0.1, poll=0.05) == 0
    assert fake.audio.runtime_file_seen  # written before the rig started
    assert not launcher.runtime_file().exists()
    assert fake.audio.stopped
    assert "StreamStack Console: http://127.0.0.1:" in capsys.readouterr().out


# -- the window ---------------------------------------------------------------------


def test_app_window_command_line(monkeypatch: pytest.MonkeyPatch) -> None:
    argv = launcher.app_argv(["google-chrome"], "http://127.0.0.1:9/?token=t")
    assert argv[0] == "google-chrome"
    assert "--app=http://127.0.0.1:9/?token=t" in argv
    assert "--class=StreamStack" in argv
    assert any(a.startswith("--user-data-dir=") and a.endswith("streamstack/console-browser") for a in argv)
    assert "--window-size=1360,860" in argv
    mini = launcher.app_argv(["chromium"], "http://127.0.0.1:9/?token=t", mini=True)
    assert "--app=http://127.0.0.1:9/?token=t#/mini" in mini
    assert "--window-size=420,760" in mini


def test_browser_choice_and_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    installed = {"microsoft-edge", "xdg-open"}
    monkeypatch.setattr(launcher.shutil, "which", lambda name: f"/usr/bin/{name}" if name in installed else None)
    monkeypatch.delenv(launcher.BROWSER_ENV, raising=False)
    assert launcher.find_browser() == ["microsoft-edge"]
    monkeypatch.setenv(launcher.BROWSER_ENV, "xdg-open --flag")
    assert launcher.find_browser() == ["xdg-open", "--flag"]
    monkeypatch.setenv(launcher.BROWSER_ENV, "missing-browser")
    assert launcher.find_browser() is None

    launched: list[list[str]] = []
    monkeypatch.setattr(launcher.system, "detach", launched.append)
    monkeypatch.delenv(launcher.BROWSER_ENV)
    assert launcher.open_window("http://x/", mini=True)
    assert launched[-1][0] == "microsoft-edge" and "--app=http://x/#/mini" in launched[-1]
    installed.discard("microsoft-edge")
    assert launcher.open_window("http://x/", mini=True)
    assert launched[-1] == ["xdg-open", "http://x/#/mini"]
    installed.clear()
    assert launcher.open_window("http://x/") is False


def test_console_subcommand_parses() -> None:
    from streamstack import cli

    args = cli.build_parser().parse_args(["console", "--demo", "--no-window", "--mini", "--port", "0"])
    assert (args.demo, args.no_window, args.mini, args.port, args.idle_exit) == (True, True, True, 0, None)
    assert args.func is cli.cmd_console


def test_frontend_ships_the_revive_controls() -> None:
    """Live and Mini mount the stalled banner, app.js registers Shift+R for every page, the op calls audio.revive."""
    from importlib import resources

    root = resources.files("streamstack") / "data" / "console"
    ops = (root / "core" / "ops.js").read_text(encoding="utf-8")
    assert "audio.revive" in ops
    for page in ("live", "mini"):
        js = (root / "pages" / f"{page}.js").read_text(encoding="utf-8")
        assert "mountReviveBanner" in js
    assert "registerReviveControls(ctx)" in (root / "app.js").read_text(encoding="utf-8")
    assert "shift+r" in (root / "core" / "revive.js").read_text(encoding="utf-8")


def test_the_slot_profiles_export_is_served_when_the_rig_has_them(server: ConsoleServer, rig: Rig) -> None:
    from streamstack.console.slots import DemoSlots

    status, _, _ = request(server, "GET", "/api/slots/profiles.json")
    assert status == 404  # this rig has none
    rig.slots = DemoSlots(lambda action, params: None)
    status, headers, body = request(server, "GET", "/api/slots/profiles.json")
    assert status == 200 and headers["content-type"].startswith("application/json")
    assert json.loads(body)["document"] == "streamstack.slot-profiles/1"
