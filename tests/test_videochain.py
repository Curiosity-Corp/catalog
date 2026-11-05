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


# -- the doctor sees the Console's delivery (#193) --------------------------------------------------------------------


def console_video(mode: str, **extra) -> dict:
    return {"engine": {"delivery": {"mode": mode, "picked": "Canon EOS 50D", "feeding": "Canon EOS 50D", **extra}}}


def test_a_node_fed_by_the_reconnecting_card_is_not_called_healthy() -> None:
    fed = {"/dev/video42": True, "/dev/video43": True}
    healthy = videochain.chain_checks(CFG, probe=fed.get, exists=lambda d: True, can_open=lambda d: True)
    assert healthy[0]["state"] == "ok"
    rows = videochain.chain_checks(
        CFG, probe=fed.get, exists=lambda d: True, can_open=lambda d: True, video=console_video("reconnecting")
    )
    assert rows == []


def test_delivery_checks_reports_a_busy_camera_with_the_holder() -> None:
    reason = "Canon EOS 50D is in use by obs (pid 7): only one app at a time can read /dev/video42"
    (row,) = videochain.delivery_checks(CFG, console_video("reconnecting", reason=reason, busy=True))
    assert row["state"] == "fail" and row["id"] == "video-delivery"
    assert "another app is reading Canon EOS 50D" in row["title"] and "close obs (pid 7)" in row["hint"]
    (plain,) = videochain.delivery_checks(CFG, console_video("reconnecting", reason="no producer"))
    assert plain["state"] == "fail" and "canonctl-ptp-webcam.service" in plain["hint"]


def test_delivery_checks_is_silent_when_live_idle_or_without_a_console() -> None:
    assert videochain.delivery_checks(CFG, None) == []
    assert videochain.delivery_checks(CFG, console_video("live")) == []
    assert videochain.delivery_checks(CFG, console_video("idle")) == []
    (warn,) = videochain.delivery_checks(CFG, console_video("fallback", feeding="Cam Link 4K"))
    assert warn["state"] == "warn"
