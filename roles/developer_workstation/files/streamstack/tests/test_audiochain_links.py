"""The chain's capture must read the Plan's microphone and nothing else (issue #139), judged from pw-dump."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from streamstack import cli, micwatch
from streamstack.audiochain import chain, graph, links, runtime

FIXTURES = Path(__file__).parent / "fixtures" / "pw_dump"
XLR = "alsa_input.usb-Elgato_Systems_Elgato_Wave_XLR_DS23M2A00049-00.mono-fallback"
CAM = "alsa_input.usb-Elgato_Cam_Link_4K_00066EBB11000-03.analog-stereo"


def dump(name: str) -> list[object]:
    data: list[object] = json.loads((FIXTURES / f"{name}.json").read_text())
    return data


def test_the_config_pins_the_capture_and_never_falls_back() -> None:
    text = graph.build_config({}, engine="off", microphone=XLR, plugin_paths={})
    capture = text.split("capture.props")[1].split("playback.props")[0]
    assert f'target.object = "{XLR}"' in capture
    assert "node.dont-fallback = true" in capture
    assert "node.linger = true" in capture  # without it WirePlumber destroys a stream whose target is missing
    assert "node.dont-reconnect" not in capture  # it must relink when the microphone returns


def test_no_microphone_leaves_the_connection_to_the_session_manager() -> None:
    text = graph.build_config({}, engine="off", microphone=None, plugin_paths={})
    assert "dont-fallback" not in text and "target.object" not in text


@pytest.mark.parametrize(
    ("fixture", "sources", "present"),
    [
        ("healthy", (XLR,), True),
        ("linked_to_cam_link", (CAM,), True),
        ("both_linked", (XLR, CAM), True),
        ("waiting_for_xlr", (), True),
    ],
)
def test_the_linked_sources_come_from_the_link_objects(fixture: str, sources: tuple[str, ...], present: bool) -> None:
    found = links.parse(dump(fixture))
    assert found.present is present
    assert found.sources == sources


def test_only_the_wanted_microphone_may_be_linked() -> None:
    assert links.parse(dump("healthy")).wrong(XLR) == ()
    assert links.parse(dump("linked_to_cam_link")).wrong(XLR) == (CAM,)
    assert links.parse(dump("both_linked")).wrong(XLR) == (CAM,)
    assert XLR not in links.parse(dump("waiting_for_xlr")).available
    assert {XLR, CAM} <= links.parse(dump("healthy")).available


def test_a_dump_without_the_chain_or_not_a_list_has_no_capture() -> None:
    assert not links.parse([]).present
    assert not links.parse({"x": 1}).present
    assert links.parse_text("not json") is None


def test_doctor_row_is_green_only_when_linked_to_the_plans_microphone() -> None:
    ok = links.check_row(XLR, links.parse(dump("healthy")), True)
    assert ok is not None and ok["state"] == "ok"


def test_doctor_row_fails_when_linked_to_the_cam_link() -> None:
    row = links.check_row(XLR, links.parse(dump("linked_to_cam_link")), True)
    assert row is not None and row["state"] == "fail" and row["id"] == "voice-chain-source"
    assert "Cam_Link" in row["detail"] and "streamstack chain" in row["hint"]
    both = links.check_row(XLR, links.parse(dump("both_linked")), True)
    assert both is not None and both["state"] == "fail"


def test_doctor_row_fails_when_nothing_feeds_the_chain() -> None:
    waiting = links.check_row(XLR, links.parse(dump("waiting_for_xlr")), True)
    assert waiting is not None and waiting["state"] == "fail" and "not present" in waiting["detail"]
    present = links.check_row(XLR, links.parse(dump("unlinked_xlr_present")), True)
    assert present is not None and present["state"] == "fail" and "is present" in present["detail"]


def test_doctor_row_says_nothing_when_there_is_nothing_to_judge() -> None:
    healthy = links.parse(dump("healthy"))
    assert links.check_row(None, healthy, True) is None
    assert links.check_row(XLR, None, True) is None
    assert links.check_row(XLR, healthy, False) is None
    assert links.check_row(XLR, links.parse([]), True) is None


def test_find_nodes_carries_the_links() -> None:
    nodes = runtime.find_nodes(
        lambda argv, t: subprocess.CompletedProcess([], 0, json.dumps(dump("linked_to_cam_link")), "")
    )
    assert nodes is not None and nodes.links.sources == (CAM,)


# -- the supervisor ------------------------------------------------------------------------------------------------


class Rig:
    """A ChainRuntime over a fake pw-dump; the host is never really stopped."""

    def __init__(self, tmp_path: Path, fixture: str, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = 100.0
        self.fixture = fixture
        self.stops = 0
        self.runtime = chain.ChainRuntime(
            chain.ChainModel(store=tmp_path / "s.json", engines=lambda: []),
            tmp_path,
            runner=self.runner,
            manage_default=False,
            clock=lambda: self.now,
        )
        self.runtime.set_microphone(XLR)
        monkeypatch.setattr(graph, "missing_required", lambda: [])
        monkeypatch.setattr(runtime, "host_pids", lambda conf, proc=Path("/proc"): [1234])
        monkeypatch.setattr(runtime, "stop_host", self.stop_host)
        monkeypatch.setattr(self.runtime, "_running_signature", lambda: self.runtime.desired_signature())

    def stop_host(self, conf: Path, wait: float = 3.0) -> bool:
        self.stops += 1
        return True

    def runner(self, argv: list[str], timeout: float = 10) -> subprocess.CompletedProcess[str] | None:
        if argv[0] == "pw-dump":
            return subprocess.CompletedProcess(argv, 0, json.dumps(dump(self.fixture)), "")
        return None


def test_a_capture_linked_to_another_source_makes_the_host_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, "linked_to_cam_link", monkeypatch)
    rig.runtime.supervise()
    assert rig.stops == 1 and not rig.runtime.running
    assert rig.runtime.error is not None and "relinking" in rig.runtime.error


def test_rebuilds_are_rate_limited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, "linked_to_cam_link", monkeypatch)
    rig.runtime.supervise()
    rig.now += 5
    rig.runtime.supervise()
    assert rig.stops == 1
    rig.now += chain.RELINK_MIN_S
    rig.runtime.supervise()
    assert rig.stops == 2


def test_a_correct_link_is_left_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig(tmp_path, "healthy", monkeypatch)
    rig.runtime.supervise()
    assert rig.stops == 0 and rig.runtime.running


def test_a_capture_waiting_for_an_absent_microphone_is_left_to_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, "waiting_for_xlr", monkeypatch)
    for _ in range(5):
        rig.now += 60
        rig.runtime.supervise()
    assert rig.stops == 0


def test_an_unlinked_capture_gets_a_grace_period_then_a_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, "unlinked_xlr_present", monkeypatch)
    rig.runtime.supervise()
    rig.now += chain.UNLINKED_GRACE_S - 1
    rig.runtime.supervise()
    assert rig.stops == 0
    rig.now += 2
    rig.runtime.supervise()
    assert rig.stops == 1


# -- doctor ---------------------------------------------------------------------------------------------------------


def test_the_silence_row_needs_exact_zeros_for_the_whole_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "have", lambda tool: True)
    seconds = micwatch.VERIFY_SIGNAL_S
    nbytes = int(seconds * micwatch.SIGNAL_BYTES_PER_S)
    monkeypatch.setattr(micwatch, "capture_signal", lambda source, s: micwatch.SignalSample(nbytes, 0))
    row = cli._voice_chain_silence_row()
    assert row is not None and row["state"] == "fail" and row["id"] == "voice-chain-silence"
    monkeypatch.setattr(micwatch, "capture_signal", lambda source, s: micwatch.SignalSample(nbytes, 7))
    assert cli._voice_chain_silence_row() is None
    monkeypatch.setattr(micwatch, "capture_signal", lambda source, s: micwatch.SignalSample(10, 0))
    assert cli._voice_chain_silence_row() is None  # too little audio to say anything
    monkeypatch.setattr(micwatch, "capture_signal", lambda source, s: None)
    assert cli._voice_chain_silence_row() is None
