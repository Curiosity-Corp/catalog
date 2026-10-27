"""The deck service loop with a fake device, Console and clock."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from streamstack.deck import service
from streamstack.deck.service import DeviceInfo, Service, ServiceError


class FakeDevice:
    def __init__(self, rows: int = 2, cols: int = 3) -> None:
        self._info = DeviceInfo("Fake Deck", "SER1", rows, cols, 72, {"size": (72, 72), "format": "JPEG"})
        self.up = True
        self.images: dict[int, bytes] = {}
        self.writes: list[int] = []
        self.brightness: list[int] = []
        self.callback: Callable[[int, bool], None] | None = None
        self.closed = False

    def info(self) -> DeviceInfo:
        return self._info

    def connected(self) -> bool:
        return self.up

    def set_image(self, key: int, data: bytes) -> None:
        self.images[key] = data
        self.writes.append(key)

    def set_brightness(self, percent: int) -> None:
        self.brightness.append(percent)

    def set_callback(self, callback: Callable[[int, bool], None]) -> None:
        self.callback = callback

    def close(self) -> None:
        self.closed = True


class FakeClient:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    def post(self, action: str, params: dict | None = None) -> bool:
        self.posts.append((action, params or {}))
        return True

    def actions(self) -> list[str]:
        return [a for a, _ in self.posts]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def surface(*revs: str, brightness: int = 70, sleep: int = 15) -> dict:
    return {
        "deck": {
            "surface": {"keys": [{"i": i, "rev": r, "label": r} for i, r in enumerate(revs)]},
            "profile": {"settings": {"brightness": brightness, "sleep_minutes": sleep}},
        }
    }


class Rig:
    def __init__(self, opener_result: Any = None) -> None:
        self.device = FakeDevice()
        self.client = FakeClient()
        self.clock = Clock()
        self.result = opener_result if opener_result is not None else self.device
        self.drawn: list[str] = []

        def draw(view: dict, size: int) -> str:
            self.drawn.append(view["rev"])
            return view["rev"]

        def opener() -> Any:
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

        self.svc = Service(opener, self.client, self.clock, draw, lambda img, fmt: str(img).encode())

    def start(self, state: dict | None = None) -> None:
        self.svc.step("state", state or surface("a", "b", "c", "d", "e", "f"))


def test_opening_the_deck_reports_it_and_draws_every_key() -> None:
    rig = Rig()
    rig.start()
    assert rig.client.posts[0] == (
        "deck.attach", {"name": "Fake Deck", "serial": "SER1", "rows": 2, "cols": 3, "size": 72}
    )  # fmt: skip
    assert sorted(rig.device.images) == [0, 1, 2, 3, 4, 5]
    assert rig.device.images[2] == b"c"
    assert rig.device.brightness == [70]
    assert rig.device.callback is not None


def test_only_keys_whose_picture_changed_are_sent_again() -> None:
    rig = Rig()
    rig.start()
    rig.device.writes.clear()
    rig.svc.step("state", surface("a", "B", "c", "d", "e", "f"))
    assert rig.device.writes == [1]
    rig.svc.step("state", surface("a", "B", "c", "d", "e", "f"))
    assert rig.device.writes == [1]


def test_identical_pictures_are_drawn_once() -> None:
    rig = Rig()
    rig.start(surface("x", "x", "x", "x", "x", "x"))
    assert rig.drawn == ["x"]
    assert len(rig.device.images) == 6


def test_missing_keys_are_blanked_and_extra_ones_ignored() -> None:
    rig = Rig()
    rig.start(surface("a", "b"))
    assert rig.device.images[5] == b"0"
    rig2 = Rig()
    rig2.start(surface(*"abcdefgh"))
    assert sorted(rig2.device.images) == [0, 1, 2, 3, 4, 5]


def test_without_a_console_the_deck_shows_the_offline_screen() -> None:
    rig = Rig()
    rig.svc.step("state", None)
    assert rig.device.images[0] == b"offline"
    assert all(rig.device.images[i] == b"0" for i in range(1, 6))


def test_a_press_goes_to_the_console_and_a_release_does_not() -> None:
    rig = Rig()
    rig.start()
    rig.svc.post_press(3, False)
    assert rig.svc.events.empty()
    rig.svc.post_press(3, True)
    kind, value = rig.svc.events.get_nowait()
    rig.svc.step(kind, value)
    assert ("deck.press", {"key": 3}) in rig.client.posts


def test_a_press_before_the_console_has_spoken_is_dropped() -> None:
    rig = Rig()
    rig.svc.step("tick")
    rig.svc.pressed(1)
    assert "deck.press" not in rig.client.actions()


def test_the_deck_sleeps_after_the_timeout_and_the_waking_press_is_swallowed() -> None:
    rig = Rig()
    rig.start(surface("a", "b", "c", "d", "e", "f", sleep=5))
    rig.clock.now += 5 * 60 + 1
    rig.svc.step("tick")
    assert rig.svc.asleep and rig.device.brightness[-1] == 0
    rig.svc.step("press", 2)
    assert not rig.svc.asleep and rig.device.brightness[-1] == 70
    assert "deck.press" not in rig.client.actions()
    rig.svc.step("press", 2)
    assert ("deck.press", {"key": 2}) in rig.client.posts


def test_sleep_off_never_sleeps_and_turning_it_off_wakes_the_deck() -> None:
    rig = Rig()
    rig.start(surface("a", "b", "c", "d", "e", "f", sleep=0))
    rig.clock.now += 10_000
    rig.svc.step("tick")
    assert not rig.svc.asleep
    rig.svc.asleep = True
    rig.svc.step("tick")
    assert not rig.svc.asleep


def test_brightness_follows_the_profile_setting() -> None:
    rig = Rig()
    rig.start()
    rig.svc.step("state", surface("a", "b", "c", "d", "e", "f", brightness=30))
    assert rig.device.brightness == [70, 30]


def test_unplugging_closes_the_device_tells_the_console_and_a_replug_redraws() -> None:
    rig = Rig()
    rig.start()
    rig.device.up = False
    rig.svc.step("tick")
    assert rig.device.closed and rig.svc.device is None
    assert rig.client.actions()[-1] == "deck.detach"
    fresh = FakeDevice()
    rig.result = fresh
    rig.clock.now += service.SCAN_SECONDS + 1
    rig.svc.step("tick")
    assert sorted(fresh.images) == [0, 1, 2, 3, 4, 5]


def test_the_attach_heartbeat_repeats() -> None:
    rig = Rig()
    rig.start()
    first = rig.client.actions().count("deck.attach")
    rig.clock.now += service.ATTACH_SECONDS + 0.1
    rig.svc.step("tick")
    assert rig.client.actions().count("deck.attach") == first + 1


def test_a_deck_that_cannot_be_opened_is_reported_once_per_interval() -> None:
    rig = Rig(ServiceError("Another app has the deck. Stop it."))
    rig.svc.step("tick")
    assert rig.client.posts == [("deck.attach", {"error": "Another app has the deck. Stop it."})]
    rig.svc.step("tick")  # not scanned again yet
    assert len(rig.client.posts) == 1
    assert rig.svc.device is None


def test_no_deck_plugged_in_is_quiet() -> None:
    rig = Rig()
    rig.result = None
    rig.svc.opener = lambda: None
    rig.svc.step("tick")
    assert rig.client.posts == []


def test_a_transport_error_while_drawing_does_not_crash_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = Rig()

    def boom(key: int, data: bytes) -> None:
        raise OSError("gone")

    monkeypatch.setattr(rig.device, "set_image", boom)
    rig.start()
    assert rig.svc.shown == {}


def test_run_stops_on_request_and_closes_the_device() -> None:
    rig = Rig()
    rig.start()
    rig.svc.stop()
    rig.svc.run()
    assert rig.device.closed


def test_the_console_client_cannot_post_when_no_console_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    client = service.ConsoleClient(autostart=False)
    monkeypatch.setattr(client, "_running", lambda: None)
    assert client.post("deck.press", {"key": 1}) is False
