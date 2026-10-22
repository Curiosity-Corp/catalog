"""The Console's background blur control: obs-websocket traffic, state, errors and the demo simulation."""

from __future__ import annotations

from typing import Any

import pytest

from streamstack import blur
from streamstack.console import blur as console_blur
from streamstack.console.errors import ActionError

CAMERAS = ["Canon EOS 50D", "Cam Link 4K"]
INSTALLED = blur.Plugin(
    installed=True, version="1.4.1", library="/usr/local/lib/obs-plugins/obs-backgroundremoval.so", models_ok=True
)


class FakeConn:
    """Just enough obs-websocket: filters per source, a filter-kind list and GetStats."""

    def __init__(self, kinds: list[str] | None = None) -> None:
        self.kinds = [blur.FILTER_KIND, "color_filter_v2"] if kinds is None else kinds
        self.filters: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.stats = {
            "averageFrameRenderTime": 2.0,
            "cpuUsage": 10.0,
            "renderSkippedFrames": 0,
            "outputSkippedFrames": 0,
        }
        self.fail: set[str] = set()

    def call(self, request: str, **data: Any) -> dict:
        self.calls.append((request, data))
        if request in self.fail:
            raise RuntimeError(f"{request} refused")
        if request == "GetSourceFilterKindList":
            # the obs-websocket 5 response field, as a real OBS sends it
            return {"sourceFilterKinds": list(self.kinds)}
        if request == "GetStats":
            return dict(self.stats)
        key = (data.get("sourceName", ""), data.get("filterName", ""))
        if request == "GetSourceFilter":
            if key not in self.filters:
                raise RuntimeError("No filter was found")
            return {
                "filterEnabled": self.filters[key]["enabled"],
                "filterSettings": dict(self.filters[key]["settings"]),
            }
        if request == "CreateSourceFilter":
            self.filters[key] = {"enabled": True, "settings": dict(data["filterSettings"]), "kind": data["filterKind"]}
            return {}
        if request == "SetSourceFilterSettings":
            self.filters[key]["settings"].update(data["filterSettings"])
            return {}
        if request == "SetSourceFilterEnabled":
            self.filters[key]["enabled"] = data["filterEnabled"]
            return {}
        raise AssertionError(f"unexpected request {request}")

    def requests(self, name: str) -> list[dict[str, Any]]:
        return [data for request, data in self.calls if request == name]


def control(
    plugin: blur.Plugin = INSTALLED, clock: Any = None
) -> tuple[console_blur.BlurControl, list[tuple[str, str]]]:
    toasts: list[tuple[str, str]] = []
    now = [0.0]
    ctl = console_blur.BlurControl(
        lambda level, text: toasts.append((level, text)),
        detect=lambda: plugin,
        clock=clock or (lambda: now.__setitem__(0, now[0] + 2.0) or now[0]),
    )
    return ctl, toasts


# ---- parse_action ------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"on": "yes"},
        {"strength": 0},
        {"strength": 21},
        {"rate": 9},
        {"on": True, "source": ""},
        {"on": True, "source": 5},
    ],
)
def test_parse_action_rejects_bad_input_with_400(params: dict) -> None:
    with pytest.raises(ActionError) as caught:
        console_blur.parse_action(params)
    assert caught.value.status == 400


def test_parse_action_returns_what_was_asked() -> None:
    assert console_blur.parse_action({"on": True, "strength": 10, "rate": 1, "source": "Cam"}) == (True, 10, 1, "Cam")
    assert console_blur.parse_action({"strength": 4}) == (None, 4, None, None)


# ---- real OBS ----------------------------------------------------------------


def test_state_before_obs_connects_still_reports_the_plugin() -> None:
    ctl, _ = control(blur.Plugin(installed=False))
    state = ctl.state()
    assert state["available"] is None
    assert state["plugin"]["installed"] is False
    assert state["plugin"]["hint"] == blur.INSTALL_HINT


def test_refresh_reads_the_filter_on_each_camera() -> None:
    ctl, _ = control()
    conn = FakeConn()
    conn.filters[(CAMERAS[0], blur.FILTER_NAME)] = {
        "enabled": True,
        "settings": {"blur_background": 12, "mask_every_x_frames": 3},
    }
    ctl.refresh(conn, CAMERAS, structural=True)
    state = ctl.state()
    assert state["available"] is True
    assert state["on"] is True
    assert state["sources"][CAMERAS[0]] == {"present": True, "enabled": True, "strength": 12, "rate": 3}
    assert state["sources"][CAMERAS[1]]["present"] is False
    assert state["strength"] == 12 and state["rate"] == 3


def test_refresh_without_the_plugin_kind_marks_it_unavailable_and_skips_filter_reads() -> None:
    ctl, _ = control()
    conn = FakeConn(kinds=["color_filter_v2"])
    ctl.refresh(conn, CAMERAS, structural=True)
    assert ctl.state()["available"] is False
    assert conn.requests("GetSourceFilter") == []
    assert conn.requests("GetStats") == []


def test_enabling_creates_the_filter_with_the_low_latency_settings() -> None:
    ctl, _ = control()
    conn = FakeConn()
    message = ctl.act(conn, {"on": True}, CAMERAS)
    assert message == "Background blur on"
    for name in CAMERAS:
        created = conn.filters[(name, blur.FILTER_NAME)]
        assert created["kind"] == blur.FILTER_KIND
        assert created["enabled"] is True
        assert created["settings"]["useGPU"] == "cpu"
        assert created["settings"]["blur_background"] >= 1
    assert ctl.state()["on"] is True


def test_acting_on_one_source_leaves_the_other_alone() -> None:
    ctl, _ = control()
    conn = FakeConn()
    ctl.act(conn, {"on": True, "source": CAMERAS[1]}, CAMERAS)
    assert (CAMERAS[1], blur.FILTER_NAME) in conn.filters
    assert (CAMERAS[0], blur.FILTER_NAME) not in conn.filters


def test_strength_and_rate_become_plugin_settings() -> None:
    ctl, _ = control()
    conn = FakeConn()
    ctl.act(conn, {"on": True}, CAMERAS)
    ctl.act(conn, {"strength": 15, "rate": 1}, CAMERAS)
    settings = conn.filters[(CAMERAS[0], blur.FILTER_NAME)]["settings"]
    assert settings["blur_background"] == 15 and settings["mask_every_x_frames"] == 1
    patch = conn.requests("SetSourceFilterSettings")[0]
    assert patch["overlay"] is True  # only the changed keys, the rest of the filter stays as it was
    assert ctl.state()["strength"] == 15


def test_enabling_a_filter_whose_strength_was_zero_never_removes_the_background() -> None:
    ctl, _ = control()
    conn = FakeConn()
    conn.filters[(CAMERAS[0], blur.FILTER_NAME)] = {"enabled": False, "settings": {"blur_background": 0}}
    ctl.act(conn, {"on": True, "source": CAMERAS[0]}, CAMERAS)
    assert conn.filters[(CAMERAS[0], blur.FILTER_NAME)]["settings"]["blur_background"] == blur.DEFAULT_STRENGTH
    assert conn.filters[(CAMERAS[0], blur.FILTER_NAME)]["enabled"] is True


def test_switching_off_when_there_is_no_filter_does_nothing_and_needs_no_plugin() -> None:
    ctl, _ = control(blur.Plugin(installed=False))
    conn = FakeConn(kinds=[])
    assert ctl.act(conn, {"on": False}, CAMERAS) == "Background blur off"
    assert conn.requests("CreateSourceFilter") == []


def test_switching_off_disables_an_existing_filter() -> None:
    ctl, _ = control()
    conn = FakeConn()
    ctl.act(conn, {"on": True}, CAMERAS)
    ctl.act(conn, {"on": False}, CAMERAS)
    assert not conn.filters[(CAMERAS[0], blur.FILTER_NAME)]["enabled"]
    assert ctl.state()["on"] is False


def test_enabling_without_the_plugin_kind_says_what_to_install() -> None:
    ctl, _ = control(blur.Plugin(installed=False))
    with pytest.raises(ActionError) as caught:
        ctl.act(FakeConn(kinds=[]), {"on": True}, CAMERAS)
    assert caught.value.status == 409
    assert blur.PLUGIN_NAME in str(caught.value) and "#100" in str(caught.value)


def test_enabling_when_installed_but_not_loaded_says_to_restart_obs() -> None:
    ctl, _ = control()
    with pytest.raises(ActionError) as caught:
        ctl.act(FakeConn(kinds=[]), {"on": True}, CAMERAS)
    assert "restart OBS" in str(caught.value)


def test_unknown_source_and_no_camera_are_refused() -> None:
    ctl, _ = control()
    with pytest.raises(ActionError) as caught:
        ctl.act(FakeConn(), {"on": True, "source": "Webcam"}, CAMERAS)
    assert caught.value.status == 400
    with pytest.raises(ActionError) as caught:
        ctl.act(FakeConn(), {"on": True}, [])
    assert caught.value.status == 409


def test_an_obs_error_becomes_a_409_with_the_source_named() -> None:
    ctl, _ = control()
    conn = FakeConn()
    conn.fail.add("CreateSourceFilter")
    with pytest.raises(ActionError) as caught:
        ctl.act(conn, {"on": True}, CAMERAS)
    assert caught.value.status == 409 and CAMERAS[0] in str(caught.value)


def test_reset_forgets_obs_state_but_keeps_what_the_files_say() -> None:
    ctl, _ = control()
    ctl.act(FakeConn(), {"on": True}, CAMERAS)
    ctl.reset()
    state = ctl.state()
    assert state["on"] is False and state["sources"] == {} and state["available"] is None
    assert state["plugin"]["installed"] is True


def test_cost_over_budget_toasts_once_and_raises_a_health_warning() -> None:
    now = [0.0]

    def clock() -> float:
        now[0] += 2.0
        return now[0]

    ctl, toasts = control(clock=clock)
    conn = FakeConn()
    ctl.act(conn, {"on": True}, CAMERAS)
    for _ in range(10):
        ctl.refresh(conn, CAMERAS)
    conn.stats["averageFrameRenderTime"] = 14.0
    for _ in range(14):
        ctl.refresh(conn, CAMERAS)
    assert ctl.state()["cost"]["state"] == "over"
    assert [level for level, _ in toasts].count("warn") == 1
    check = ctl.health_checks()[0]
    assert check["state"] == "warn" and check["id"] == "background-blur" and check["hint"]


def test_health_checks_are_empty_until_obs_is_connected_and_warn_when_not_loaded() -> None:
    ctl, _ = control()
    assert ctl.health_checks() == []
    ctl.refresh(FakeConn(kinds=[]), CAMERAS, structural=True)
    check = ctl.health_checks()[0]
    assert check["state"] == "warn" and "restart OBS" in check["hint"]


def test_plugin_detection_failure_keeps_the_last_answer() -> None:
    calls = {"n": 0}

    def flaky() -> blur.Plugin:
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("disk hiccup")
        return INSTALLED

    ticks = iter(range(0, 1000, 100))
    ctl = console_blur.BlurControl(detect=flaky, clock=lambda: float(next(ticks)))
    assert ctl.plugin().installed
    assert ctl.plugin().installed  # re-read after the TTL failed: the last answer stands


# ---- demo --------------------------------------------------------------------


def demo() -> console_blur.DemoBlur:
    return console_blur.DemoBlur(("Canon EOS 50D", "Cam Link 4K"))


def test_demo_blur_starts_off_and_installed() -> None:
    state = demo().state()
    assert state["available"] is True and state["on"] is False
    assert state["plugin"]["installed"] is True
    assert state["cost"]["state"] == "off"


def test_demo_blur_cost_is_ok_by_default_and_over_at_the_smoothest_rate() -> None:
    d = demo()
    d.act({"on": True})
    assert d.state()["cost"]["state"] == "ok"
    d.act({"rate": 1})
    cost = d.state()["cost"]
    assert cost["state"] == "over" and cost["reasons"]
    d.act({"rate": 3})
    assert d.state()["cost"]["state"] == "ok"


def test_demo_blur_notifies_once_when_it_goes_over() -> None:
    seen: list[tuple[str, str]] = []
    d = console_blur.DemoBlur(("Cam",), lambda level, text: seen.append((level, text)))
    d.act({"on": True})
    d.act({"rate": 1})
    d.act({"strength": 9})
    assert len(seen) == 1 and seen[0][0] == "warn"


def test_demo_blur_missing_plugin_refuses_enabling_but_not_switching_off() -> None:
    d = demo()
    d.act({"on": True})
    d.set_installed(False)
    assert d.state()["on"] is False
    with pytest.raises(ActionError) as caught:
        d.act({"on": True})
    assert caught.value.status == 409
    assert d.act({"on": False}) == "Background blur off"
    assert d.health_checks()[0]["state"] == "warn"
    d.set_installed(True)
    d.act({"on": True})
    assert d.health_checks()[0]["state"] == "ok"


def test_demo_blur_unknown_source_is_a_400() -> None:
    with pytest.raises(ActionError) as caught:
        demo().act({"on": True, "source": "nope"})
    assert caught.value.status == 400
