"""Telemetry: the exposition format, the collector, and the Console's token-guarded, opt-in /metrics."""

from __future__ import annotations

import http.client
import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from http_support import HTTP_TIMEOUT

from streamstack import config, metrics
from streamstack.console.diagnostics import Diagnostics, collect, light_label
from streamstack.console.rig import Rig
from streamstack.console.server import ConsoleServer

SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{.*\})? (-?[0-9.e+-]+|NaN|\+Inf|-Inf)$")


def parse(text: str) -> dict[str, list[tuple[dict[str, str], float]]]:
    """A strict reader of the 0.0.4 text format: every line is a comment or a sample."""
    out: dict[str, list[tuple[dict[str, str], float]]] = {}
    for line in text.splitlines():
        if line.startswith("#"):
            assert re.match(r"# (HELP|TYPE) [a-zA-Z_:][a-zA-Z0-9_:]* .+", line), line
            continue
        match = SAMPLE.match(line)
        assert match, f"not a sample: {line!r}"
        labels = dict(re.findall(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"', match[2] or ""))
        out.setdefault(match[1], []).append((labels, float(match[3])))
    return out


def test_families_escape_and_render() -> None:
    fam = metrics.Family("x_total", "counter", "line one\nline two")
    fam.add(3, {"path": 'a"b\\c\nd'})
    text = metrics.render([fam, metrics.Family("empty", "gauge", "no samples")])
    assert "# HELP x_total line one\\nline two" in text
    assert 'x_total{path="a\\"b\\\\c\\nd"} 3' in text
    assert "empty" not in text
    assert parse(text)["x_total"][0][1] == 3.0


def test_bad_names_and_duplicates_are_refused() -> None:
    with pytest.raises(ValueError, match="valid metric name"):
        metrics.Family("1bad", "gauge", "h")
    with pytest.raises(ValueError, match="metric type"):
        metrics.Family("ok", "summary", "h")
    with pytest.raises(ValueError, match="label name"):
        metrics.Family("ok", "gauge", "h").add(1, {"bad-label": "v"})
    fam = metrics.gauge("dup", "h", 1)
    with pytest.raises(ValueError, match="twice"):
        metrics.render([fam, fam])


def test_counter_and_histogram_are_cumulative_and_bounded() -> None:
    counter = metrics.Counter("c_total", "h", ("action",), max_series=2)
    for name in ("a", "b", "c", "d", "a"):
        counter.inc(action=name)
    series = {labels["action"]: value for labels, value in parse(metrics.render([counter.collect()]))["c_total"]}
    assert series == {"a": 2.0, "b": 1.0, "other": 2.0}

    hist = metrics.Histogram("h_seconds", "h", ("action",), buckets=(0.1, 1.0))
    for value in (0.05, 0.5, 2.0):
        hist.observe(value, action="x")
    parsed = parse(metrics.render([hist.collect()]))
    buckets = {labels["le"]: value for labels, value in parsed["h_seconds_bucket"]}
    assert buckets == {"0.1": 1.0, "1": 2.0, "+Inf": 3.0}
    assert parsed["h_seconds_count"][0][1] == 3.0
    assert parsed["h_seconds_sum"][0][1] == pytest.approx(2.55)
    with pytest.raises(ValueError, match="increasing"):
        metrics.Histogram("bad", "h", buckets=(1.0, 0.5))


SNAPSHOT = {
    "version": "9.9.9",
    "studio": {"running": True, "busy": False, "voice_chain": True},
    "video": {"canon": {"state": "running"}},
    "audio": {"mic": {"present": True, "gain_db": -3.5, "muted": False}},
    "health": {
        "summary": "warn",
        "checks": [{"id": "mic-stall", "state": "ok"}, {"id": "disk", "state": "warn"}],
    },
    "lighting": {
        "automation": {"available": True, "status": {"running": True}},
        "lights": [{"id": "Elgato Key Light 192.168.1.50", "on": True, "brightness": 40, "temperature": 4500}],
    },
}


def test_collect_describes_the_rig_without_naming_it() -> None:
    text = metrics.render(collect(SNAPSHOT))
    parsed = parse(text)
    services = {labels["service"]: value for labels, value in parsed["streamstack_service_up"]}
    assert services["console"] == 1 and services["studio"] == 1
    assert services["canon_bridge"] == 1 and services["lights_service"] == 1 and services["mic_watch"] == 1
    assert "192.168.1.50" not in text and "Elgato" not in text  # lights are labelled by a hash
    assert light_label("Elgato Key Light 192.168.1.50") in text


def test_collect_survives_an_empty_or_odd_snapshot() -> None:
    for snap in ({}, {"studio": None, "audio": {"mic": []}}, {"lighting": {"lights": [None, 3]}}):
        metrics.render(collect(snap))  # no exception, valid output


@pytest.fixture
def served(tmp_path: Path) -> Iterator[ConsoleServer]:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html>")
    server = ConsoleServer(Rig.demo(), port=0, token="console-token", static_dir=static, allow_hosts=set())
    server.start()
    yield server
    server.close()


def get(server: ConsoleServer, path: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=HTTP_TIMEOUT)
    conn.request("GET", path, headers=headers or {})
    res = conn.getresponse()
    body = res.read().decode()
    conn.close()
    return res.status, body


def act(server: ConsoleServer, action: str, **params: object) -> dict:
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=HTTP_TIMEOUT)
    conn.request(
        "POST",
        "/api/action",
        body=json.dumps({"action": action, **params}),
        headers={"X-StreamStack-Token": server.token, "Content-Type": "application/json"},
    )
    res = conn.getresponse()
    body = json.loads(res.read())
    conn.close()
    return {"status": res.status, **body}


def test_metrics_is_off_by_default_and_asks_for_a_token_first(served: ConsoleServer) -> None:
    assert get(served, "/metrics")[0] == 401  # no hint whether telemetry exists
    status, body = get(served, "/metrics", {"X-StreamStack-Token": "console-token"})
    assert status == 404 and "off" in body
    assert served.rig.snapshot()["diagnostics"]["telemetry"]["enabled"] is False


def test_turning_it_on_serves_metrics_to_either_token(served: ConsoleServer) -> None:
    assert act(served, "telemetry.set", on=True)["ok"] is True
    config_text = act(served, "telemetry.scrape_config")["message"]
    scrape_token = re.search(r"credentials: (\S+)", config_text)[1]
    assert scrape_token != "console-token"

    for headers in (
        {"X-StreamStack-Token": "console-token"},
        {"Authorization": f"Bearer {scrape_token}"},
        {"X-StreamStack-Token": scrape_token},
    ):
        status, body = get(served, "/metrics", headers)
        assert status == 200
        parsed = parse(body)
        assert {"streamstack_build_info", "streamstack_uptime_seconds", "streamstack_service_up"} <= set(parsed)
    assert get(served, f"/metrics?token={scrape_token}")[0] == 200
    assert get(served, "/metrics", {"Authorization": "Bearer wrong"})[0] == 401
    assert get(served, "/api/state", {"X-StreamStack-Token": scrape_token})[0] == 401  # scoped to /metrics only


def test_the_scrape_token_never_reaches_the_broadcast_state(served: ConsoleServer) -> None:
    act(served, "telemetry.set", on=True)
    token = re.search(r"credentials: (\S+)", act(served, "telemetry.scrape_config")["message"])[1]
    assert token not in served.hub.state_json()
    assert token not in json.dumps(served.rig.snapshot())
    assert config.secrets_file().stat().st_mode & 0o777 == 0o600


def test_rotating_the_token_locks_out_the_old_one(served: ConsoleServer) -> None:
    act(served, "telemetry.set", on=True)
    old = re.search(r"credentials: (\S+)", act(served, "telemetry.scrape_config")["message"])[1]
    act(served, "telemetry.rotate_token")
    assert get(served, "/metrics", {"Authorization": f"Bearer {old}"})[0] == 401


def test_the_switch_persists_and_turning_off_removes_the_endpoint(served: ConsoleServer) -> None:
    act(served, "telemetry.set", on=True)
    assert config.read_env_file(config.user_env_file())["STREAMSTACK_TELEMETRY"] == "true"
    act(served, "telemetry.set", on=False)
    assert get(served, "/metrics", {"X-StreamStack-Token": "console-token"})[0] == 404
    assert act(served, "telemetry.scrape_config")["status"] == 409
    assert act(served, "telemetry.set", on="yes")["status"] == 400


def test_actions_and_errors_are_measured(served: ConsoleServer) -> None:
    act(served, "telemetry.set", on=True)
    act(served, "audio.gain", db=-2)
    act(served, "no.such.action")
    _, body = get(served, "/metrics", {"X-StreamStack-Token": "console-token"})
    parsed = parse(body)
    assert any(
        labels.get("action") == "audio.gain" for labels, _ in parsed["streamstack_action_duration_seconds_count"]
    )
    assert "streamstack_action_errors_total" in parsed
    assert "streamstack_event_loop_lag_max_seconds" in parsed
    assert "streamstack_process_resident_memory_bytes" in parsed


def test_it_stays_off_when_the_config_says_so_and_on_when_it_says_on(tmp_path: Path) -> None:
    assert Diagnostics({}).enabled is False
    assert Diagnostics({"STREAMSTACK_TELEMETRY": "true"}).enabled is True
    assert Diagnostics({"STREAMSTACK_TELEMETRY": "maybe"}).enabled is False


def test_the_metric_text_holds_no_paths_or_secrets(served: ConsoleServer, tmp_path: Path) -> None:
    act(served, "telemetry.set", on=True)
    _, body = get(served, "/metrics", {"X-StreamStack-Token": "console-token"})
    assert str(tmp_path) not in body and "console-token" not in body and "/home/" not in body


def test_streaming_state_becomes_stream_metrics_without_naming_destinations() -> None:
    stream = {
        "status": "live",
        "bitrate_kbps": 6000.0,
        "dropped_frames": 3,
        "latency_ms": 80,
        "uptime_s": 12,
        "recording": {"active": True},
        "destinations": [
            {"id": "my-twitch-key-name", "kind": "rtmp", "state": "live", "health": "ok", "restarts": 1},
            {"id": "backup", "kind": "srt", "state": "backoff", "health": "warn", "restarts": 4},
        ],
    }
    text = metrics.render(collect({**SNAPSHOT, "stream": stream}))
    parsed = parse(text)
    assert parsed["streamstack_stream_live"][0][1] == 1 and parsed["streamstack_stream_bitrate_kbps"][0][1] == 6000
    assert {labels["kind"]: v for labels, v in parsed["streamstack_stream_destination_up"]} == {"rtmp": 1, "srt": 0}
    assert "my-twitch-key-name" not in text and "backup" not in text
    services = {labels["service"]: v for labels, v in parsed["streamstack_service_up"]}
    assert services["stream"] == 1
