"""The Console's background blur control: the action contract, the messages and the demo simulation."""

from __future__ import annotations

import pytest

from streamstack import blur
from streamstack.console import blur as console_blur
from streamstack.console.errors import ActionError

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


def test_empty_state_lists_the_ranges_and_the_gate() -> None:
    state = console_blur.empty_state()
    assert state["available"] is None and state["on"] is False
    assert state["limits"] == {
        "strength": [blur.MIN_STRENGTH, blur.MAX_STRENGTH],
        "rate": [blur.MIN_RATE, blur.MAX_RATE],
    }
    assert state["cost"]["limits"]["p95_ms"] == blur.GATE_P95_MS


def test_the_missing_backend_message_names_the_hint() -> None:
    assert blur.INSTALL_HINT in console_blur.missing_message(blur.Plugin(installed=False))
