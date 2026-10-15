"""The curiosity-tv Samsung TV remote-control CLI (mocked; no network)."""

import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import urllib.parse
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
SCRIPT = ROLE / "files/curiosity-tv"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
SECRET = "98765432"


def _load() -> ModuleType:
    loader = importlib.machinery.SourceFileLoader("curiosity_tv", str(SCRIPT))
    spec = importlib.util.spec_from_loader("curiosity_tv", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


tv = _load()


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("CURIOSITY_TV_TOKEN", raising=False)
    monkeypatch.setattr(tv.time, "sleep", lambda _seconds: None)
    tv._secrets.clear()


class FakeSession:
    """Stands in for tv.Session; records what would be sent to the TV."""

    instances: list["FakeSession"] = []

    def __init__(self, host: str, token: str | None, timeout: float) -> None:
        self.host, self.token, self.timeout = host, token, timeout
        self.sent: list[dict[str, Any]] = []
        self.new_token: str | None = None
        FakeSession.instances.append(self)

    def connect(self) -> None:
        return None

    def close(self) -> None:
        return None

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def send(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)

    def key(self, name: str) -> None:
        self.sent.append({"key": name})

    def emit(self, event: str, data: dict[str, Any] | None = None) -> None:
        self.sent.append({"emit": event, "data": data})

    def installed_apps(self) -> list[dict[str, Any]]:
        return [
            {"appId": "111299001912", "name": "YouTube"},
            {"appId": "3201907018807", "name": "Netflix"},
            {"appId": "3201910019365", "name": "Prime Video"},
            {"appId": "3201910019999", "name": "Prime Gaming"},
            {"appId": "org.tizen.browser", "name": "Internet"},
        ]


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> type[FakeSession]:
    FakeSession.instances = []
    monkeypatch.setattr(tv, "Session", FakeSession)
    monkeypatch.setenv("CURIOSITY_TV_TOKEN", SECRET)
    return FakeSession


# ------------------------------------------------------------------- parsing


def test_parser_accepts_every_subcommand() -> None:
    parser = tv.build_parser()
    cases = [
        ["status"], ["on"], ["on", "--wait", "5"], ["off"], ["pair"],
        ["key", "KEY_VOLUP", "sleep:1", "KEY_VOLDOWN", "--repeat", "3"],
        ["input", "hdmi2"], ["input", "source"], ["volume", "up", "3"], ["volume", "get"],
        ["volume", "set", "12"], ["channel", "up"], ["home"], ["back"], ["apps"],
        ["launch", "youtube"], ["browse", "https://example.org"], ["text", "hello"],
        ["cursor", "move", "5", "-5"], ["cursor", "click"],
    ]
    for argv in cases:
        args = parser.parse_args(argv)
        assert args.command == argv[0]
        assert callable(args.func)


@pytest.mark.parametrize(
    "argv",
    [[], ["bogus"], ["key"], ["input", "hdmi9"], ["volume", "loud"], ["--timeout", "-1", "status"], ["--timeout", "x", "status"]],
)
def test_parser_rejects_bad_usage(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        tv.build_parser().parse_args(argv)
    assert exit_info.value.code == 2


def test_defaults_target_the_deskmeet_tv() -> None:
    args = tv.build_parser().parse_args(["status"])
    assert args.host == "192.168.1.250"
    assert args.mac == "70:09:71:93:5A:20"


def test_host_and_mac_can_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURIOSITY_TV_HOST", "10.1.2.3")
    args = tv.build_parser().parse_args(["status"])
    assert args.host == "10.1.2.3"


def test_key_names_are_normalised_and_validated() -> None:
    assert tv.normalize_key("volup") == "KEY_VOLUP"
    assert tv.normalize_key("KEY_mute") == "KEY_MUTE"
    for bad in ["KEY_", "KEY_A B", "KEY_VOL;UP", "../x", ""]:
        with pytest.raises(tv.TvError):
            tv.normalize_key(bad)


def test_key_steps_support_sleep_and_reject_bad_pauses() -> None:
    assert tv.parse_key_steps(["KEY_UP", "sleep:1.5", "down"]) == [("key", "KEY_UP"), ("sleep", 1.5), ("key", "KEY_DOWN")]
    for bad in (["sleep:abc"], ["sleep:-1"], ["sleep:61"], []):
        with pytest.raises(tv.TvError):
            tv.parse_key_steps(bad)


# ------------------------------------------------------------- Wake-on-LAN


def test_magic_packet_bytes() -> None:
    packet = tv.magic_packet("70:09:71:93:5A:20")
    mac = bytes.fromhex("700971935A20")
    assert len(packet) == 102
    assert packet[:6] == b"\xff" * 6
    assert packet[6:] == mac * 16
    assert tv.magic_packet("70-09-71-93-5a-20") == packet


@pytest.mark.parametrize("bad", ["", "70:09:71:93:5A", "zz:09:71:93:5A:20", "700971935A20"])
def test_invalid_mac_is_rejected(bad: str) -> None:
    with pytest.raises(tv.TvError):
        tv.magic_packet(bad)


def test_wol_goes_to_both_broadcasts_on_port_9(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[bytes, tuple[str, int]]] = []

    class FakeSocket:
        def __enter__(self) -> "FakeSocket":
            return self

        def __exit__(self, *exc_info: object) -> None:
            return None

        def setsockopt(self, *args: object) -> None:
            return None

        def sendto(self, data: bytes, address: tuple[str, int]) -> None:
            sent.append((data, address))

    monkeypatch.setattr(tv.socket, "socket", lambda *a, **k: FakeSocket())
    assert tv.send_wol("70:09:71:93:5A:20") == ["192.168.1.255", "255.255.255.255"]
    assert [address for _data, address in sent] == [("192.168.1.255", 9), ("255.255.255.255", 9)]
    assert all(data == tv.magic_packet("70:09:71:93:5A:20") for data, _address in sent)


def test_on_polls_until_the_tv_reports_on(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    states = iter(["standby", "unreachable", "standby", "on"])
    monkeypatch.setattr(tv, "power_state", lambda host, timeout: next(states))
    packets: list[str] = []
    monkeypatch.setattr(tv, "send_wol", lambda mac: packets.append(mac) or ["192.168.1.255"])
    assert tv.main(["on"]) == 0
    assert packets == ["70:09:71:93:5A:20"]
    assert "TV is on" in capsys.readouterr().out


def test_on_times_out_with_a_clear_error(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    clock = iter(range(0, 1000))
    monkeypatch.setattr(tv.time, "monotonic", lambda: float(next(clock)))
    monkeypatch.setattr(tv, "power_state", lambda host, timeout: "standby")
    monkeypatch.setattr(tv, "send_wol", lambda mac: ["x"])
    assert tv.main(["on", "--wait", "3"]) == 1
    assert "did not report PowerState on" in capsys.readouterr().err


def test_on_does_nothing_when_already_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tv, "power_state", lambda host, timeout: "on")
    monkeypatch.setattr(tv, "send_wol", lambda mac: pytest.fail("no packet expected"))
    assert tv.main(["on"]) == 0


# -------------------------------------------------------------- power / off


@pytest.mark.parametrize(("state", "presses"), [("on", 1), ("standby", 0), ("unreachable", 0)])
def test_off_presses_power_only_when_on(monkeypatch: pytest.MonkeyPatch, session: type[FakeSession], state: str, presses: int) -> None:
    monkeypatch.setattr(tv, "power_state", lambda host, timeout: state)
    assert tv.main(["off"]) == 0
    sent = [item for instance in session.instances for item in instance.sent]
    assert sent == [{"key": "KEY_POWER"}] * presses


def test_power_state_reads_the_rest_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tv, "device_info", lambda host, timeout: {"device": {"PowerState": "Standby"}})
    assert tv.power_state("h", 1) == "standby"
    monkeypatch.setattr(tv, "device_info", lambda host, timeout: {"device": {}})
    assert tv.power_state("h", 1) == "on"

    def unreachable(host: str, timeout: float) -> dict[str, Any]:
        raise tv.TvError("down")

    monkeypatch.setattr(tv, "device_info", unreachable)
    assert tv.power_state("h", 1) == "unreachable"


def test_status_prints_device_fields_without_a_token(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    payload = {"version": "2.0.25", "device": {
        "PowerState": "on", "name": "[TV] Samsung", "modelName": "QN55QN700B",
        "resolution": "7680x4320", "OS": "Tizen", "TokenAuthSupport": "true"}}
    monkeypatch.setattr(tv, "http_json", lambda url, **kw: payload)
    monkeypatch.setattr(tv, "load_token", lambda: pytest.fail("status must not need a token"))
    assert tv.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "power      on" in out and "QN55QN700B" in out and "7680x4320" in out
    assert tv.main(["--json", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["device"]["modelName"] == "QN55QN700B"


# ------------------------------------------------------------ key and aliases


def test_key_repeat_and_sleep(session: type[FakeSession], capsys: pytest.CaptureFixture[str]) -> None:
    assert tv.main(["key", "KEY_VOLUP", "sleep:2", "voldown", "--repeat", "2"]) == 0
    keys = [item["key"] for item in session.instances[0].sent]
    assert keys == ["KEY_VOLUP", "KEY_VOLDOWN"] * 2
    assert capsys.readouterr().out.count("sent KEY_") == 4


def test_key_repeat_out_of_range(session: type[FakeSession], capsys: pytest.CaptureFixture[str]) -> None:
    assert tv.main(["key", "KEY_UP", "--repeat", "0"]) == 1
    assert "--repeat" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "keys"),
    [
        (["volume", "up"], ["KEY_VOLUP"]),
        (["volume", "down", "3"], ["KEY_VOLDOWN"] * 3),
        (["volume", "mute"], ["KEY_MUTE"]),
        (["home"], ["KEY_HOME"]),
        (["back"], ["KEY_RETURN"]),
        (["channel", "up"], ["KEY_CHUP"]),
        (["channel", "down"], ["KEY_CHDOWN"]),
        (["channel", "42"], ["KEY_4", "KEY_2", "KEY_ENTER"]),
        (["input", "source"], ["KEY_HDMI"]),
    ],
)
def test_convenience_commands_send_the_expected_keys(session: type[FakeSession], argv: list[str], keys: list[str]) -> None:
    assert tv.main(argv) == 0
    assert [item["key"] for item in session.instances[0].sent] == keys


@pytest.mark.parametrize("argv", [["channel", "abc"], ["channel", "12345"], ["volume", "mute", "2"], ["volume", "up", "0"], ["volume", "set", "101"], ["volume", "set"]])
def test_convenience_commands_validate_input(session: type[FakeSession], argv: list[str]) -> None:
    assert tv.main(argv) == 1
    assert not session.instances or not session.instances[0].sent


def test_input_hdmi_launches_the_source_app(session: type[FakeSession]) -> None:
    assert tv.main(["input", "hdmi3"]) == 0
    assert session.instances[0].sent == [{"emit": "ed.apps.launch", "data": {"appId": "org.tizen.hdmi3", "action_type": "NATIVE_LAUNCH"}}]


def test_alias_resolution() -> None:
    def nothing() -> list[dict[str, Any]]:
        pytest.fail("aliases and ids must not query the TV")

    assert tv.resolve_app("YouTube", nothing) == "111299001912"
    assert tv.resolve_app("netflix", nothing) == "3201907018807"
    assert tv.resolve_app("browser", nothing) == "org.tizen.browser"
    assert tv.resolve_app("Plex", nothing) == "3201512006963"
    assert tv.resolve_app("Prime-Video", nothing) == "3201910019365"
    assert tv.resolve_app("3201999999999", nothing) == "3201999999999"
    assert tv.resolve_app("com.example.app", nothing) == "com.example.app"


def test_app_names_resolve_through_the_installed_list() -> None:
    apps = FakeSession.installed_apps(None)  # type: ignore[arg-type]
    assert tv.resolve_app("Internet", lambda: apps) == "org.tizen.browser"
    assert tv.resolve_app("prime gaming", lambda: apps) == "3201910019999"
    with pytest.raises(tv.TvError, match="ambiguous"):
        tv.resolve_app("prim", lambda: apps)
    with pytest.raises(tv.TvError, match="no installed app"):
        tv.resolve_app("zzz", lambda: apps)


def test_launch_uses_rest_first(monkeypatch: pytest.MonkeyPatch, session: type[FakeSession], capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[str] = []
    monkeypatch.setattr(tv, "rest_launch", lambda host, app_id, timeout: calls.append(app_id) or {"name": "YouTube"})
    assert tv.main(["launch", "youtube"]) == 0
    assert calls == ["111299001912"]
    assert not session.instances
    assert "launched YouTube" in capsys.readouterr().out


def test_launch_falls_back_to_the_websocket(monkeypatch: pytest.MonkeyPatch, session: type[FakeSession]) -> None:
    def broken(host: str, app_id: str, timeout: float) -> dict[str, Any]:
        raise tv.TvError("TV answered 500 Server Error")

    monkeypatch.setattr(tv, "rest_launch", broken)
    assert tv.main(["launch", "netflix"]) == 0
    assert session.instances[0].sent == [{"emit": "ed.apps.launch", "data": {"appId": "3201907018807", "action_type": "DEEP_LINK"}}]


def test_launch_reports_a_missing_app(monkeypatch: pytest.MonkeyPatch, session: type[FakeSession], capsys: pytest.CaptureFixture[str]) -> None:
    def missing(host: str, app_id: str, timeout: float) -> dict[str, Any]:
        raise tv.TvError("TV answered 404 Not Found for POST /api/v2/applications/1")

    monkeypatch.setattr(tv, "rest_launch", missing)
    assert tv.main(["launch", "plex"]) == 1
    assert "has no app 3201512006963" in capsys.readouterr().err
    assert not session.instances


def test_browse_sends_native_launch_with_meta_tag(session: type[FakeSession]) -> None:
    assert tv.main(["browse", "https://example.org/a?b=1"]) == 0
    assert session.instances[0].sent == [{"emit": "ed.apps.launch", "data": {
        "appId": "org.tizen.browser", "action_type": "NATIVE_LAUNCH", "metaTag": "https://example.org/a?b=1"}}]


@pytest.mark.parametrize("url", ["example.org", "file:///etc/passwd", "javascript:alert(1)", "http://"])
def test_browse_rejects_non_http_urls(session: type[FakeSession], url: str) -> None:
    assert tv.main(["browse", url]) == 1
    assert not session.instances


def test_text_is_sent_as_base64_input(session: type[FakeSession]) -> None:
    assert tv.main(["text", "héllo"]) == 0
    params = session.instances[0].sent[0]["params"]
    assert params["TypeOfRemote"] == "SendInputString" and params["DataOfCmd"] == "base64"
    import base64

    assert base64.b64decode(params["Cmd"]).decode() == "héllo"


def test_cursor_commands(session: type[FakeSession]) -> None:
    assert tv.main(["cursor", "move", "10", "-4"]) == 0
    position = session.instances[0].sent[0]["params"]["Position"]
    assert (position["x"], position["y"]) == (10, -4)
    assert tv.main(["cursor", "click"]) == 0
    assert session.instances[1].sent[0]["params"]["Cmd"] == "LeftClick"
    assert tv.main(["cursor", "move", "1"]) == 1


def test_apps_lists_sorted_apps(session: type[FakeSession], capsys: pytest.CaptureFixture[str]) -> None:
    assert tv.main(["apps"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[0] == "org.tizen.browser" or "Internet" in lines[0]
    assert any("111299001912" in line and "YouTube" in line for line in lines)


# ------------------------------------------------------------------ websocket


class FakeWebSocket:
    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = [json.dumps(reply) for reply in replies]
        self.sent: list[str] = []

    def settimeout(self, _seconds: float) -> None:
        return None

    def recv(self) -> str:
        if not self.replies:
            raise tv._websocket().WebSocketTimeoutException("timeout")
        return self.replies.pop(0)

    def send(self, data: str) -> None:
        self.sent.append(data)

    def close(self) -> None:
        return None


def _connect_with(monkeypatch: pytest.MonkeyPatch, replies: list[dict[str, Any]], token: str | None) -> tuple[Any, list[str]]:
    pytest.importorskip("websocket")
    urls: list[str] = []
    fake = FakeWebSocket(replies)

    def create_connection(url: str, **kwargs: Any) -> FakeWebSocket:
        urls.append(url)
        return fake

    monkeypatch.setattr(tv._websocket(), "create_connection", create_connection)
    session = tv.Session("192.168.1.250", token, 2.0)
    return session, urls


def test_connect_uses_the_trusted_client_name_and_token_in_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    session, urls = _connect_with(monkeypatch, [{"event": "ms.channel.connect", "data": {}}], SECRET)
    session.connect()
    parsed = urllib.parse.urlsplit(urls[0])
    assert parsed.scheme == "wss" and parsed.port == 8002
    assert parsed.path == "/api/v2/channels/samsung.remote.control"
    query = urllib.parse.parse_qs(parsed.query)
    import base64

    assert base64.b64decode(query["name"][0]) == b"Curiosity DeskMeet"
    assert query["token"] == [SECRET]
    assert session.new_token is None


def test_connect_rejected_without_a_token_hints_at_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    session, _urls = _connect_with(monkeypatch, [{"event": "ms.channel.unauthorized"}], None)
    with pytest.raises(tv.AuthError, match="pair"):
        session.connect()


def test_refreshed_token_goes_to_the_cache_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    new = "55555555"
    session, _urls = _connect_with(monkeypatch, [{"event": "ms.channel.connect", "data": {"token": new}}], SECRET)
    monkeypatch.setattr(tv, "Session", lambda host, token, timeout: session)
    monkeypatch.setenv("CURIOSITY_TV_TOKEN", SECRET)
    assert tv.main(["key", "KEY_UP"]) == 0
    captured = capsys.readouterr()
    cache = tmp_path / "state/curiosity-tv/token"
    assert cache.read_text().strip() == new
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    assert stat.S_IMODE(cache.parent.stat().st_mode) == 0o700
    assert "refreshed" in captured.err and "Key Vault" in captured.err
    for stream in (captured.out, captured.err):
        assert new not in stream and SECRET not in stream


def test_installed_apps_event(monkeypatch: pytest.MonkeyPatch) -> None:
    replies = [
        {"event": "ms.channel.connect", "data": {}},
        {"event": "ms.channel.clientConnect"},
        {"event": "ed.installedApp.get", "data": {"data": [{"appId": "1", "name": "A"}, {"name": "no id"}]}},
    ]
    session, _urls = _connect_with(monkeypatch, replies, SECRET)
    session.connect()
    assert session.installed_apps() == [{"appId": "1", "name": "A"}]
    assert json.loads(session.ws.sent[0]) == {"method": "ms.channel.emit", "params": {"event": "ed.installedApp.get", "to": "host"}}


def test_remote_key_payload_matches_the_prototype(monkeypatch: pytest.MonkeyPatch) -> None:
    session, _urls = _connect_with(monkeypatch, [{"event": "ms.channel.connect", "data": {}}], SECRET)
    session.connect()
    session.key("KEY_VOLUP")
    assert json.loads(session.ws.sent[0]) == {"method": "ms.remote.control", "params": {
        "Cmd": "Click", "DataOfCmd": "KEY_VOLUP", "Option": "false", "TypeOfRemote": "SendRemoteKey"}}


# ---------------------------------------------------------------------- token


def test_token_order_env_then_cache_then_key_vault(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert kwargs["capture_output"] is True
        return subprocess.CompletedProcess(command, 0, stdout="24681357\n", stderr="")

    monkeypatch.setattr(tv.subprocess, "run", fake_run)
    monkeypatch.setenv("CURIOSITY_TV_TOKEN", "11112222")
    assert tv.load_token() == "11112222" and not calls
    monkeypatch.delenv("CURIOSITY_TV_TOKEN")
    assert tv.load_token() == "24681357"
    assert calls == [[
        "az", "keyvault", "secret", "show", "--vault-name", "omlab-secrets",
        "--name", "deskmeet-samsung-tv-ws-token", "--query", "value", "-o", "tsv"]]
    cache = tmp_path / "state/curiosity-tv/token"
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    assert tv.load_token() == "24681357" and len(calls) == 1


def test_cached_token_wins_over_key_vault(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cache = tmp_path / "state/curiosity-tv/token"
    cache.parent.mkdir(parents=True)
    cache.write_text("33334444\n")
    monkeypatch.setattr(tv.subprocess, "run", lambda *a, **k: pytest.fail("Key Vault must not be queried"))
    assert tv.load_token() == "33334444"


def test_missing_az_is_a_clear_auth_error(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def no_az(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError("az")

    monkeypatch.setattr(tv.subprocess, "run", no_az)
    assert tv.main(["key", "KEY_UP"]) == 3
    assert "CURIOSITY_TV_TOKEN" in capsys.readouterr().err


def test_unusable_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURIOSITY_TV_TOKEN", "bad token&x=1")
    with pytest.raises(tv.TvError, match="unexpected characters"):
        tv.load_token()


def test_token_never_reaches_stdout_or_stderr(
    monkeypatch: pytest.MonkeyPatch, session: type[FakeSession], capsys: pytest.CaptureFixture[str]
) -> None:
    assert tv.main(["key", "KEY_VOLUP", "KEY_VOLDOWN"]) == 0
    assert tv.main(["volume", "up", "2"]) == 0
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert session.instances[0].token == SECRET


def test_errors_are_redacted(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("CURIOSITY_TV_TOKEN", SECRET)

    def boom(*args: Any, **kwargs: Any) -> int:
        raise tv.TvError(f"handshake failed for wss://tv:8002/x?name=abc&token={SECRET} and {SECRET}")

    monkeypatch.setattr(tv, "run_keys", boom)
    tv.load_token()
    assert tv.main(["key", "KEY_UP"]) == 1
    err = capsys.readouterr().err
    assert SECRET not in err and "token=***" in err


def test_redact_handles_url_quoting() -> None:
    tv._secrets.add("a/b+c")
    assert "a/b+c" not in tv.redact("x a/b+c y") and "a%2Fb%2Bc" not in tv.redact("x a%2Fb%2Bc y")


# ----------------------------------------------------------------------- UPnP

DESCRIPTION = b"""<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><device><serviceList>
<service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType><controlURL>/upnp/control/AVTransport1</controlURL></service>
<service><serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType><controlURL>/upnp/control/RenderingControl1</controlURL></service>
</serviceList></device></root>"""
VOLUME_REPLY = b"""<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>
<u:GetVolumeResponse xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1"><CurrentVolume>17</CurrentVolume></u:GetVolumeResponse></s:Body></s:Envelope>"""


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_upnp_volume_get_and_set(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    requests: list[Any] = []

    def urlopen(request: Any, timeout: float = 0) -> FakeResponse:
        requests.append(request)
        url = request if isinstance(request, str) else request.full_url
        return FakeResponse(DESCRIPTION if url.endswith("/dmr") else VOLUME_REPLY)

    monkeypatch.setattr(tv.urllib.request, "urlopen", urlopen)
    assert tv.main(["volume", "get"]) == 0
    assert capsys.readouterr().out.strip() == "17"
    soap = requests[-1]
    assert soap.full_url == "http://192.168.1.250:9197/upnp/control/RenderingControl1"
    assert soap.get_header("Soapaction") == '"urn:schemas-upnp-org:service:RenderingControl:1#GetVolume"'
    assert tv.main(["volume", "set", "25"]) == 0
    body = requests[-1].data.decode()
    assert "<DesiredVolume>25</DesiredVolume>" in body and "SetVolume" in body


def test_upnp_absent_is_a_clear_error(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def refused(*args: Any, **kwargs: Any) -> None:
        raise tv.urllib.error.URLError("refused")

    monkeypatch.setattr(tv.urllib.request, "urlopen", refused)
    assert tv.main(["volume", "get"]) == 1
    assert "UPnP is not reachable" in capsys.readouterr().err


# --------------------------------------------------------------- role wiring


def test_script_is_executable_python() -> None:
    assert os.access(SCRIPT, os.X_OK)
    assert SCRIPT.read_text().startswith("#!/usr/bin/python3\n")
    subprocess.run(["python3", "-I", "-m", "py_compile", str(SCRIPT)], check=True)
    help_run = subprocess.run(["python3", "-I", str(SCRIPT), "--help"], capture_output=True, text=True, check=False)
    assert help_run.returncode == 0 and "curiosity-tv" in help_run.stdout


def test_script_never_puts_the_token_on_a_command_line_or_logs_it() -> None:
    source = SCRIPT.read_text()
    assert "print(token" not in source and "info(token" not in source
    assert '"--query", "value", "-o", "tsv"' in source and "capture_output=True" in source


def test_only_deskmeet_opts_in_to_the_tv_cli() -> None:
    for name, profile in DEFAULTS["hardware_profiles"].items():
        assert bool(profile.get("samsung_tv_cli", False)) == (name == "deskmeet-b760"), name


def test_role_installs_the_cli_and_its_dependency() -> None:
    tasks = yaml.safe_load((ROLE / "tasks/samsung-tv-cli.yml").read_text())
    by_name = {t["name"]: t for t in tasks}
    apt = by_name["Install the Samsung TV CLI dependencies"]["ansible.builtin.apt"]
    assert apt["name"] == ["python3-websocket"]
    copy = by_name["Install the Samsung TV remote-control CLI"]["ansible.builtin.copy"]
    assert copy == {"src": "curiosity-tv", "dest": "/usr/local/bin/curiosity-tv", "owner": "root", "group": "root", "mode": "0755"}
    main = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
    include = next(t for t in main if t.get("ansible.builtin.include_tasks") == "samsung-tv-cli.yml")
    assert "samsung_tv_cli_enabled | bool" in include["when"]
    assert "samsung_tv_cli_enabled" in DEFAULTS
