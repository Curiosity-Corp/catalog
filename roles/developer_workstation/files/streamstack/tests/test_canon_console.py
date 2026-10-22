"""The Console side of the Canon controls: socket, CanonLink, the simulated Canon, and the CLI."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_canon_bridge import Rig as BridgeRig
from test_canon_bridge import wait_for

from streamstack import cli
from streamstack.canon import bridge as bridge_mod
from streamstack.canon import ipc
from streamstack.console.canon import CanonLink, DemoCanon, empty_canon
from streamstack.console.errors import ActionError
from streamstack.console.rig import Rig


@pytest.fixture
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_mod, "REFRESH_EVERY", 0.01)
    monkeypatch.setattr(bridge_mod, "STATS_EVERY", 0.05)


@pytest.fixture
def sock(tmp_path: Path) -> Path:
    # AF_UNIX paths are short; tmp_path can be long, so keep the socket directly under it
    return tmp_path / "c.sock"


def test_ipc_round_trip_and_refusals(sock: Path) -> None:
    server, path = ipc.serve(lambda req: {"ok": True, "echo": req}, sock)
    try:
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"
        assert ipc.request({"op": "status"}, 5, path) == {"ok": True, "echo": {"op": "status"}}
        with pytest.raises(ipc.BridgeUnavailable):
            ipc.request({"op": "x"}, 1, sock.with_name("absent.sock"))
    finally:
        server.shutdown()
        server.server_close()


def test_ipc_rejects_garbage_and_oversize(sock: Path) -> None:
    import socket

    server, path = ipc.serve(lambda req: {"ok": True}, sock)
    try:
        for payload, expect in (
            (b"not json\n", "bad request"),
            (b"[1]\n", "bad request"),
            (b"x" * 20000 + b"\n", "too large"),
        ):
            with socket.socket(socket.AF_UNIX) as s:
                s.settimeout(5)
                s.connect(str(path))
                s.sendall(payload)
                answer = json.loads(s.recv(4096))
            assert answer["ok"] is False and expect in answer["error"]
    finally:
        server.shutdown()
        server.server_close()


def test_console_link_reads_status_and_forwards_actions_over_the_socket(fast: None, sock: Path) -> None:
    camera = BridgeRig().start()
    server, path = ipc.serve(camera.bridge.handle, sock)
    try:
        wait_for(lambda: camera.bridge.readings and camera.encoders and camera.encoders[-1].fed > 2)
        link = CanonLink(
            {"STREAMSTACK_CANON_BRIDGE": "gphoto2"},
            request=lambda payload, timeout: ipc.request(payload, timeout, path),
        )
        link.refresh()
        state = link.state()
        assert state["available"] and state["state"] == "running" and state["controls"]["iso"]["value"] == "400"
        assert state["decode"]["path"] == "qsv" and set(empty_canon()) <= set(state)
        assert link.act("video.canon_set", {"id": "iso", "value": "800"}) is None
        assert camera.session.settings["iso"].value == "800"
        with pytest.raises(ActionError, match="mode dial"):
            link.act("video.canon_set", {"id": "aperture", "value": "4"})
        link.act("video.canon_action", {"name": "focus", "step": 1})
        assert camera.session.writes[-1] == [("manualfocusdrive", "Far 1")]
    finally:
        server.shutdown()
        server.server_close()
        camera.stop()


def test_console_link_says_so_when_the_bridge_is_not_running(sock: Path) -> None:
    def down(payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        raise ipc.BridgeUnavailable("no socket")

    link = CanonLink({"STREAMSTACK_CANON_BRIDGE": "gphoto2"}, request=down)
    link.refresh()
    assert link.state()["state"] == "offline" and link.state()["available"] is False
    with pytest.raises(ActionError, match="not running"):
        link.act("video.canon_set", {"id": "iso", "value": "100"})


def test_console_link_is_disabled_for_an_external_camera_or_no_bridge() -> None:
    for cfg in ({"STREAMSTACK_CAMERA_EXTERNAL": "true"}, {"STREAMSTACK_CANON_BRIDGE": "none"}):
        link = CanonLink(cfg)
        assert link.state()["state"] == "disabled"
        with pytest.raises(ActionError):
            link.act("video.canon_refresh", {})


def test_actions_need_their_parameters() -> None:
    link = CanonLink({}, request=lambda p, timeout: {"ok": True})
    with pytest.raises(ActionError):
        link.act("video.canon_set", {})
    with pytest.raises(ActionError):
        link.act("video.canon_nope", {})


# ---------- the simulated Canon, through the real Rig ----------
@pytest.fixture
def rig() -> Iterator[Rig]:
    r = Rig.demo()
    r.start()
    yield r
    r.stop()


def canon(rig: Rig) -> dict[str, Any]:
    return rig.snapshot()["video"]["canon"]


def test_demo_rig_carries_a_canon_section(rig: Rig) -> None:
    state = canon(rig)
    assert state["available"] and state["state"] == "running" and set(empty_canon()) <= set(state)
    assert state["controls"]["iso"]["choices"] and state["controls"]["model"]["readonly"]


def test_demo_set_validates_like_the_camera(rig: Rig) -> None:
    rig.act("video.canon_set", {"id": "iso", "value": "800"})
    assert canon(rig)["controls"]["iso"]["value"] == "800"
    with pytest.raises(ActionError, match="offers"):
        rig.act("video.canon_set", {"id": "iso", "value": "7"})
    with pytest.raises(ActionError):
        rig.act("video.canon_set", {"id": "model", "value": "x"})


def test_demo_kelvin_switches_white_balance_and_snaps(rig: Rig) -> None:
    rig.act("video.canon_set", {"id": "whitebalance", "value": "Daylight"})
    rig.act("video.canon_set", {"id": "kelvin", "value": 4333})
    controls = canon(rig)["controls"]
    assert controls["kelvin"]["value"] == "4300" and controls["whitebalance"]["value"] == "Color Temperature"


def test_demo_readonly_aperture_in_shutter_priority(rig: Rig) -> None:
    demo: DemoCanon = rig.video.canon
    demo.values["autoexposuremode"] = "Shutter priority"
    ap = canon(rig)["controls"]["aperture"]
    assert ap["readonly"] and "mode dial" in ap["reason"]
    with pytest.raises(ActionError, match="mode dial"):
        rig.act("video.canon_set", {"id": "aperture", "value": "4"})


def test_demo_actions(rig: Rig) -> None:
    rig.act("video.canon_action", {"name": "autofocus"})
    rig.act("video.canon_action", {"name": "focus", "step": 2})
    for bad in ({"name": "focus", "step": 0}, {"name": "focus", "step": True}, {"name": "dance"}):
        with pytest.raises(ActionError):
            rig.act("video.canon_action", bad)


# ---------- CLI ----------
def test_canon_status_without_a_bridge_says_how_to_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("STREAMSTACK_CANON_SOCKET", str(tmp_path / "nothing.sock"))
    assert cli.main(["canon", "status"]) == 1
    err = capsys.readouterr().err
    assert "not reachable" in err and "systemctl --user status streamstack-canon.service" in err


def test_canon_commands_against_a_running_bridge(
    fast: None,
    monkeypatch: pytest.MonkeyPatch,
    sock: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    camera = BridgeRig().start()
    server, path = ipc.serve(camera.bridge.handle, sock)
    monkeypatch.setenv("STREAMSTACK_CANON_SOCKET", str(path))
    try:
        wait_for(lambda: camera.bridge.readings and camera.encoders and camera.encoders[-1].fed > 2)
        assert cli.main(["canon", "status"]) == 0
        out = capsys.readouterr().out
        assert "GPU (Intel Quick Sync)" in out and "iso" in out
        assert cli.main(["canon", "get", "iso"]) == 0
        assert "400" in capsys.readouterr().out
        assert cli.main(["canon", "set", "iso", "800"]) == 0
        assert cli.main(["canon", "set", "iso", "7"]) == 1
        assert cli.main(["canon", "focus", "-1"]) == 0
        assert cli.main(["canon", "af"]) == 0
        assert cli.main(["canon", "status", "--json"]) == 0
        capsys.readouterr()
    finally:
        server.shutdown()
        server.server_close()
        camera.stop()
