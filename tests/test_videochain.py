"""The doctor's one-sentence verdict on the camera chain (the 2026-10-27 reboot)."""

from __future__ import annotations

from streamstack import config, videochain

CFG = {**config.DEFAULTS, "STREAMSTACK_CAMERA_EXTERNAL": "true"}


def run(fed: dict[str, bool | None], cfg: dict | None = None, present: bool = True, access: bool = True):
    return videochain.chain_checks(
        cfg or CFG, probe=lambda d: fed.get(d), exists=lambda d: present, can_open=lambda d: access
    )


def test_nothing_feeds_either_node_is_the_incident() -> None:
    (row,) = run({"/dev/video42": False, "/dev/video43": False})
    assert row["state"] == "fail" and row["id"] == "video-chain" and row["group"] == "video"
    assert "StreamStack Camera is not listed" in row["title"] and "/dev/video43" in row["title"]
    assert "Canon EOS 50D (/dev/video42) has no producer" in row["detail"]
    assert "canonctl-ptp-webcam.service" in row["hint"]


def test_the_engine_not_running_with_a_fed_source_points_at_the_console() -> None:
    (row,) = run({"/dev/video42": True, "/dev/video43": False})
    assert row["state"] == "fail" and "engine is not running" in row["detail"] and "console" in row["hint"]


def test_a_fed_program_node_over_a_dead_source_is_a_warning() -> None:
    (row,) = run({"/dev/video42": False, "/dev/video43": True})
    assert row["state"] == "warn" and "camera reconnecting" in row["detail"]


def test_a_healthy_chain_is_ok() -> None:
    (row,) = run({"/dev/video42": True, "/dev/video43": True})
    assert row["state"] == "ok"


def test_the_own_bridge_hint_names_the_streamstack_unit() -> None:
    (row,) = run({"/dev/video42": False, "/dev/video43": False}, {**config.DEFAULTS})
    assert "streamstack-canon.service" in row["hint"]


def test_no_probe_or_no_access_or_no_node_says_nothing() -> None:
    assert run({}) == []
    assert run({"/dev/video43": False}, present=False) == []
    assert run({"/dev/video43": False}, access=False) == []


def idle_row(video: dict | None):
    return videochain.idle_checks(video)


def test_idle_row_is_silent_without_a_console_or_an_idle_block() -> None:
    assert idle_row(None) == []
    assert idle_row({"engine": {"state": "running"}}) == []


def test_idle_row_says_the_engine_is_resting_and_what_wakes_it() -> None:
    (row,) = idle_row({"engine": {"idle": {"mode": "auto", "state": "idle", "reason": "no app read it.", "wakes": 3}}})
    assert row["state"] == "ok" and row["id"] == "engine-idle" and "idle" in row["title"]
    assert "3 times" in row["detail"]


def test_idle_row_for_waking_running_keep_alive_and_off() -> None:
    (waking,) = idle_row({"engine": {"idle": {"mode": "auto", "state": "waking"}}})
    assert "waking" in waking["title"]
    (busy,) = idle_row({"engine": {"idle": {"mode": "auto", "state": "running", "readers": ["Microsoft Edge"]}}})
    assert "running" in busy["title"] and "Microsoft Edge" in busy["detail"]
    (kept,) = idle_row({"engine": {"idle": {"mode": "auto", "state": "running", "keep_alive": True}}})
    assert "Keep awake" in kept["detail"]
    (off,) = idle_row({"engine": {"idle": {"mode": "off", "state": "running"}}})
    assert "never idles" in off["title"]
