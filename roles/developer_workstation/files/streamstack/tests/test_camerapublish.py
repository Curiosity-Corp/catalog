"""camera-publish (#176): WirePlumber probed the loopback before it was fed; restart it once, never during a call."""

from __future__ import annotations

import json
from types import SimpleNamespace

from streamstack import camerapublish
from streamstack.pipewirecam import CameraNode

CFG = {"STREAMSTACK_PROGRAM_DEVICE": "/dev/video43", "STREAMSTACK_PROGRAM_LABEL": "StreamStack Camera"}
CAMLINK = CameraNode(49, "v4l2_input.pci-0000_00_14.0-usb-0_2_1.0", "Cam Link 4K (V4L2)", "v4l2:/dev/video0")
PROGRAM = CameraNode(
    60, "v4l2_input._sys_devices_virtual_video4linux_video43", "StreamStack Camera", "v4l2:/dev/video43"
)


class World:
    """PipeWire after a boot: the program node is fed but not published; a WirePlumber restart publishes it."""

    def __init__(self, *, fed: bool = True, published: bool = False, restart_publishes: bool = True) -> None:
        self.fed, self.published, self.restart_publishes = fed, published, restart_publishes
        self.commands: list[list[str]] = []
        self.readers: list[str] = []
        self.streams: list[str] | None = []
        self.restart_rc = 0
        self.slept = 0.0

    def probe(self, device: str) -> bool | None:
        return self.fed

    def sources(self) -> list[CameraNode] | None:
        return [CAMLINK, PROGRAM] if self.published else [CAMLINK]

    def run(self, argv: list[str], timeout: float = 10) -> SimpleNamespace:
        self.commands.append(argv)
        if self.restart_rc == 0 and self.restart_publishes:
            self.published = True
        return SimpleNamespace(returncode=self.restart_rc, stdout="", stderr="Failed to restart wireplumber.service")

    def sleep(self, seconds: float) -> None:
        self.slept += seconds
        if self.slept >= 3 and self.restart_rc == 0 and self.commands and self.restart_publishes:
            self.published = True

    def go(self, **kw) -> camerapublish.Result:
        return camerapublish.publish(
            CFG,
            probe=self.probe,
            sources=self.sources,
            readers=lambda device: self.readers,
            streams=lambda: self.streams,
            run=self.run,
            sleep=self.sleep,
            **kw,
        )


def test_a_fed_node_wireplumber_never_published_gets_one_restart() -> None:
    world = World()
    result = world.go()
    assert result.ok and "now a PipeWire camera" in result.message
    assert world.commands == [["systemctl", "--user", "restart", "wireplumber"]]


def test_an_already_published_camera_is_left_alone() -> None:
    world = World(published=True)
    result = world.go()
    assert result.ok and "already" in result.message and world.commands == []


def test_a_node_nothing_feeds_cannot_be_published() -> None:
    world = World(fed=False)
    result = world.go()
    assert result.code == camerapublish.FAILED and "no producer" in result.message and world.commands == []


def test_it_waits_for_the_node_to_be_fed() -> None:
    world = World(fed=False)
    original = world.sleep

    def sleep(seconds: float) -> None:
        original(seconds)
        world.fed = world.slept >= 2

    world.sleep = sleep  # type: ignore[method-assign]
    assert world.go(wait_fed=10).ok and len(world.commands) == 1


def test_an_app_holding_the_camera_blocks_the_restart() -> None:
    world = World()
    world.readers = ["Zoom"]
    result = world.go()
    assert result.code == camerapublish.BUSY and "Zoom has the camera open" in result.message and world.commands == []


def test_a_call_capturing_audio_blocks_the_restart() -> None:
    world = World()
    world.streams = ["Microsoft Edge"]
    result = world.go()
    assert result.code == camerapublish.BUSY and "Microsoft Edge is capturing audio" in result.message
    assert world.commands == []


def test_not_knowing_whether_audio_is_captured_blocks_the_restart() -> None:
    world = World()
    world.streams = None
    assert world.go().code == camerapublish.BUSY and world.commands == []


def test_force_restarts_even_with_a_call() -> None:
    world = World()
    world.readers, world.streams = ["Zoom"], ["Zoom"]
    assert world.go(force=True).ok and len(world.commands) == 1


def test_a_failed_restart_is_reported_with_systemctls_words() -> None:
    world = World()
    world.restart_rc = 1
    result = world.go()
    assert result.code == camerapublish.FAILED and "Failed to restart wireplumber.service" in result.message


def test_a_restart_that_does_not_publish_says_what_to_check() -> None:
    world = World(restart_publishes=False)
    result = world.go()
    assert result.code == camerapublish.FAILED and "still does not list StreamStack Camera" in result.message


def test_no_pipewire_is_a_failure_not_a_restart() -> None:
    world = World()
    world.sources = lambda: None  # type: ignore[method-assign]
    result = world.go()
    assert result.code == camerapublish.FAILED and "PipeWire does not answer" in result.message and not world.commands


def _stream(binary: str = "", app: str = "", media: str = "", node: str = "") -> dict:
    return {
        "properties": {
            "application.process.binary": binary,
            "application.name": app,
            "media.name": media,
            "node.name": node,
        }
    }


def test_streamstacks_own_capture_streams_do_not_count_as_a_call() -> None:
    listing = json.dumps(
        [
            _stream("pacat", "parec", "parec", "parec"),
            _stream("", "", "StreamStack Mic", "streamstack_mic.capture"),
            _stream("msedge", "Microsoft Edge", "Capture", "Chromium input"),
        ]
    )
    assert camerapublish.foreign_capture_streams(listing) == ["Microsoft Edge"]
    assert camerapublish.foreign_capture_streams("[]") == []


def test_unreadable_pactl_output_is_unknown() -> None:
    assert camerapublish.foreign_capture_streams("not json") is None
    assert camerapublish.foreign_capture_streams("{}") is None


def test_busy_reason_names_both_signals() -> None:
    text = camerapublish.busy_reason(["Zoom", "Zoom"], ["Zoom"])
    assert text == "Zoom has the camera open and Zoom is capturing audio"
    assert camerapublish.busy_reason([], []) == ""
