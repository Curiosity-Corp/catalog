"""The per-account Twitch capability probe (stream/twitch.py) and how the stream service uses it."""

from __future__ import annotations

import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from test_stream_service import Env, start_with_audio

from streamstack.stream import service
from streamstack.stream.twitch import AccountProbe, TwitchCaps, is_twitch, parse_configuration

KEY = "live_123456_SECRETKEYVALUE"
ENROLLED = {
    "status": {"result": "success"},
    "ingest_endpoints": [{"protocol": "RTMPS", "url_template": "rtmps://x.contribute.live-video.net/app/{stream_key}"}],
    "encoder_configurations": [
        {"type": "obs_x264", "width": 1920, "height": 1080, "settings": {"bitrate": 6000}},
        {"type": "jim_hevc", "width": 1920, "height": 1080, "settings": {"bitrate": 8500}},
        {"type": "jim_av1", "width": 2560, "height": 1440, "settings": {"bitrate": 12000}},
    ],
}
LEGACY = {"encoder_configurations": [{"type": "obs_x264", "settings": {"bitrate": 6000}}]}


def serve(status: int, body: object, seen: list[dict[str, object]]) -> tuple[HTTPServer, str]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/api/v3/GetClientConfiguration"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("rtmp://live.twitch.tv/app", True),
        ("rtmps://sea02.contribute.live-video.net/app", True),
        ("rtmp://live-sea.twitch.tv/app", True),
        ("rtmp://nottwitch.tv/app", False),
        ("rtmp://twitch.tv.evil.test/app", False),
        ("rtmp://a.rtmp.youtube.com/live2", False),
    ],
)
def test_is_twitch(url: str, expected: bool) -> None:
    assert is_twitch(url) is expected


def test_an_enrolled_account_lists_av1_hevc_and_h264() -> None:
    caps = parse_configuration(ENROLLED)
    assert caps.known and caps.codecs == ("av1", "hevc", "h264") and caps.max_bitrate_kbps == 12000


def test_a_legacy_account_is_h264_only() -> None:
    caps = parse_configuration(LEGACY)
    assert caps.known and caps.codecs == ("h264",)


@pytest.mark.parametrize("answer", [{}, {"encoder_configurations": []}, {"encoder_configurations": ["x"]}])
def test_unknown_shapes_are_not_guessed(answer: dict[str, object]) -> None:
    assert parse_configuration(answer).known is False


def test_an_error_status_carries_twitch_s_reason_without_markup() -> None:
    caps = parse_configuration({"status": {"result": "error", "html_en_us": "<b>Invalid</b> stream key"}})
    assert not caps.known and "Invalid stream key" in caps.reason and "<b>" not in caps.reason


def test_the_probe_posts_the_key_in_the_body_and_caches_by_hash() -> None:
    seen: list[dict[str, object]] = []
    server, url = serve(200, ENROLLED, seen)
    try:
        probe = AccountProbe(url)
        first, second = probe.lookup(KEY), probe.lookup(KEY)
    finally:
        server.shutdown()
    assert first.codecs == ("av1", "hevc", "h264") and second is first
    assert len(seen) == 1 and seen[0]["authentication"] == KEY
    assert KEY not in repr(probe.__dict__)  # the cache holds a hash, never the key


def test_http_errors_and_a_dead_server_mean_not_known() -> None:
    seen: list[dict[str, object]] = []
    server, url = serve(400, {"status": {"result": "error", "html_en_us": "Bad key"}}, seen)
    try:
        caps = AccountProbe(url).lookup(KEY)
    finally:
        server.shutdown()
    assert not caps.known and "HTTP 400" in caps.reason and "Bad key" in caps.reason and KEY not in caps.reason
    dead = AccountProbe("http://127.0.0.1:1/x").lookup(KEY)
    assert not dead.known and KEY not in dead.reason


def test_a_failure_is_retried_sooner_than_a_success_expires() -> None:
    now = [0.0]
    calls: list[int] = []

    def post(_url: str, _body: dict[str, object], _timeout: float) -> dict[str, object]:
        calls.append(1)
        raise urllib.error.URLError("offline")

    probe = AccountProbe("https://x.test/c", post=post, clock=lambda: now[0])
    probe.lookup(KEY)
    probe.lookup(KEY)
    assert len(calls) == 1
    now[0] += 61
    probe.lookup(KEY)
    assert len(calls) == 2
    assert not probe.lookup("").known


def test_the_address_must_be_http_or_https() -> None:
    with pytest.raises(ValueError, match="http or https"):
        AccountProbe("file:///etc/passwd")


# -- in the stream service ----------------------------------------------------------------------------------


class FakeAccount(AccountProbe):
    def __init__(self, caps: TwitchCaps) -> None:
        super().__init__("https://x.test/c")
        self.caps, self.asked = caps, []

    def lookup(self, key: str) -> TwitchCaps:
        self.asked.append(key)
        return self.caps


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setattr(service, "AUDIO_HEADER_WAIT_S", 0.05)
    return Env(tmp_path)


def twitch_dest(**extra: object) -> dict[str, object]:
    return {"id": "tw", "kind": "rtmp", "name": "Twitch", "url": "rtmp://live.twitch.tv/app", "key": KEY, **extra}


def test_an_account_without_enhanced_broadcasting_streams_h264_at_once(env: Env) -> None:
    account = FakeAccount(TwitchCaps(True, ("h264",), 6000))
    env.svc._twitch = account
    env.svc.set({"destinations": [twitch_dest()]})
    start_with_audio(env, env.svc.start_stream)
    state = env.svc.state()["destinations"][0]
    assert state["codec"] == "h264" and not state["moved"]
    assert state["account"]["codecs"] == ["h264"] and account.asked == [KEY]
    assert any("may publish H264" in text for _, text in env.events)
    assert not any(KEY in text for _, text in env.events) and KEY not in repr(env.svc.state())
    env.svc.shutdown()


def test_an_enrolled_account_still_gets_the_best_codec(env: Env) -> None:
    env.svc._twitch = FakeAccount(TwitchCaps(True, ("av1", "hevc", "h264"), 12000))
    env.svc.set({"destinations": [twitch_dest()]})
    start_with_audio(env, env.svc.start_stream)
    assert env.svc.state()["destinations"][0]["codec"] == "av1"
    env.svc.shutdown()


def test_an_unknown_answer_changes_nothing_and_says_so(env: Env) -> None:
    env.svc._twitch = FakeAccount(TwitchCaps(False, reason="Twitch could not be asked (offline)."))
    env.svc.set({"destinations": [twitch_dest()]})
    start_with_audio(env, env.svc.start_stream)
    assert env.svc.state()["destinations"][0]["codec"] == "av1"  # the old behaviour, with its own fallback
    assert any("could not be asked" in text for _, text in env.events)
    env.svc.shutdown()


def test_other_hosts_and_pinned_modes_are_never_probed(env: Env) -> None:
    account = FakeAccount(TwitchCaps(True, ("h264",)))
    env.svc._twitch = account
    env.svc.set(
        {
            "destinations": [
                {"id": "yt", "kind": "rtmp", "url": "rtmp://a.rtmp.youtube.com/live2", "key": "yt-key"},
                twitch_dest(id="tw2", options={"mode": "enhanced"}),
            ]
        }
    )
    start_with_audio(env, env.svc.start_stream)
    assert account.asked == []
    env.svc.shutdown()
