from __future__ import annotations

import json
import subprocess

from streamstack import pipewirecam

CFG = {"STREAMSTACK_PROGRAM_DEVICE": "/dev/video43", "STREAMSTACK_PROGRAM_LABEL": "StreamStack Camera"}


def node(oid: int, nick: str, path: str, klass: str = "Video/Source") -> dict:
    return {
        "id": oid,
        "info": {
            "props": {"media.class": klass, "node.name": f"v4l2_input.{oid}", "node.nick": nick, "object.path": path}
        },
    }


DUMP = json.dumps(
    [
        node(191, "Cam Link 4K", "v4l2:/dev/video0"),
        node(122, "StreamStack Camera", "v4l2:/dev/video43"),
        node(5, "Speakers", "alsa:x", "Audio/Sink"),
        {"id": 1, "type": "PipeWire:Interface:Core"},
    ]
)


def test_it_lists_only_video_sources() -> None:
    found = pipewirecam.video_sources(DUMP)
    assert [(n.id, n.nick, n.path) for n in found] == [
        (191, "Cam Link 4K", "v4l2:/dev/video0"),
        (122, "StreamStack Camera", "v4l2:/dev/video43"),
    ]


def test_text_that_is_not_a_pipewire_dump_is_none() -> None:
    assert pipewirecam.video_sources("not json") is None
    assert pipewirecam.video_sources('{"a": 1}') is None


def check(nodes, fed=True, exists=True):
    return pipewirecam.checks(CFG, sources=lambda: nodes, probe=lambda d: fed, exists=lambda p: exists)


def test_a_published_node_is_ok_and_names_the_portal_path() -> None:
    (row,) = check(pipewirecam.video_sources(DUMP))
    assert row["state"] == "ok" and row["id"] == "video-pipewire"
    assert "portal" in row["detail"]


def test_a_fed_node_pipewire_does_not_publish_warns_and_says_who_is_unaffected() -> None:
    (row,) = check(pipewirecam.video_sources(DUMP)[:1])
    assert row["state"] == "warn"
    assert "Cam Link 4K" in row["detail"] and "Teams in Edge" in row["detail"] and "camera-publish" in row["hint"]


def test_no_row_when_the_node_is_unfed_absent_or_pipewire_is_silent() -> None:
    assert check(pipewirecam.video_sources(DUMP), fed=False) == []
    assert check(pipewirecam.video_sources(DUMP), exists=False) == []
    assert check(None) == []


def test_read_sources_handles_a_missing_or_failing_pw_dump() -> None:
    assert pipewirecam.read_sources(lambda argv, timeout=10: None) is None
    assert pipewirecam.read_sources(lambda argv, timeout=10: subprocess.CompletedProcess(argv, 1, "", "no")) is None
    ok = pipewirecam.read_sources(lambda argv, timeout=10: subprocess.CompletedProcess(argv, 0, DUMP, ""))
    assert ok and len(ok) == 2
