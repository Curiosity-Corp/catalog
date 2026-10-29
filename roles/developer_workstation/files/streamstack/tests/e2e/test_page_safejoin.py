"""Safe join: a call app takes the camera, the Console keeps you off air until you press Go live."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from support import LOADED, App, Rig, poll


def safe(rig: Rig) -> None:
    rig.act("safejoin.set", mode="safe")
    poll(rig, lambda s: s["safejoin"]["mode"] == "safe")


def join(rig: Rig) -> None:
    rig.act("call.demo", app="Teams")
    poll(rig, lambda s: s["safejoin"]["holding"], timeout=3)


def test_a_call_start_holds_privacy_and_mute_and_go_live_releases_both(app: App, page: Page, rig: Rig) -> None:
    safe(rig)
    app.goto("live")
    join(rig)
    state = rig.state()
    assert state["video"]["privacy"] and state["audio"]["mic"]["muted"]
    banner = page.get_by_role("alert").filter(has_text="You are not on air")
    expect(banner).to_be_visible(timeout=LOADED)
    expect(banner).to_contain_text("Teams has the camera")
    banner.get_by_role("button", name="Go live").click()
    poll(rig, lambda s: not s["safejoin"]["holding"])
    state = rig.state()
    assert not state["video"]["privacy"] and not state["audio"]["mic"]["muted"]
    expect(banner).to_be_hidden(timeout=LOADED)


def test_space_goes_live(app: App, page: Page, rig: Rig) -> None:
    safe(rig)
    app.goto("live")
    join(rig)
    expect(page.get_by_role("alert").filter(has_text="You are not on air")).to_be_visible(timeout=LOADED)
    page.locator("body").press("Space")
    poll(rig, lambda s: not s["safejoin"]["holding"])
    assert not rig.state()["video"]["privacy"]


def test_the_mini_strip_shows_the_banner_too(app: App, page: Page, rig: Rig) -> None:
    safe(rig)
    app.goto("mini")
    join(rig)
    banner = page.get_by_role("alert").filter(has_text="You are not on air")
    expect(banner).to_be_visible(timeout=LOADED)
    banner.get_by_role("button", name="Go live").click()
    poll(rig, lambda s: not s["safejoin"]["holding"])


def test_a_microphone_you_muted_first_is_left_as_you_had_it(app: App, page: Page, rig: Rig) -> None:
    safe(rig)
    rig.act("audio.mute", on=True)
    poll(rig, lambda s: s["audio"]["mic"]["muted"])
    join(rig)
    assert rig.state()["safejoin"]["held"] == {"privacy": True, "mute": False}
    rig.act("safejoin.go_live")
    poll(rig, lambda s: not s["safejoin"]["holding"])
    assert rig.state()["audio"]["mic"]["muted"]  # still your mute


def test_warn_mode_only_tells_you(app: App, page: Page, rig: Rig) -> None:
    app.goto("live")
    rig.act("call.demo", app="Teams")
    expect(page.get_by_text("Teams has the camera. You are on air")).to_be_visible(timeout=LOADED)
    state = rig.state()
    assert not state["safejoin"]["holding"] and not state["video"]["privacy"] and not state["audio"]["mic"]["muted"]


def test_off_and_a_call_without_the_camera_do_nothing(app: App, page: Page, rig: Rig) -> None:
    rig.act("safejoin.set", mode="off")
    rig.act("call.demo", app="Teams")
    poll(rig, lambda s: s["call"]["state"] != "idle")
    assert not rig.state()["safejoin"]["holding"]
    rig.act("call.demo", app=None)
    poll(rig, lambda s: s["call"]["state"] == "idle")
    safe(rig)
    rig.act("call.demo", app="Teams", camera=False)
    poll(rig, lambda s: s["call"]["state"] != "idle")
    assert not rig.state()["safejoin"]["holding"] and not rig.state()["video"]["privacy"]


def test_the_system_page_sets_the_mode_and_lists_the_call_starts(app: App, page: Page, rig: Rig) -> None:
    app.goto("system")
    section = page.get_by_role("region", name="On call start")
    expect(section).to_be_visible(timeout=LOADED)
    section.get_by_label("On call start").select_option("safe")
    poll(rig, lambda s: s["safejoin"]["mode"] == "safe")
    join(rig)
    expect(section.get_by_role("list", name="Recent call starts").get_by_text("Teams")).to_be_visible(timeout=LOADED)
    rig.act("safejoin.go_live")


def test_the_call_ending_gives_privacy_and_mute_back(app: App, page: Page, rig: Rig) -> None:
    safe(rig)
    join(rig)
    rig.act("call.demo", app=None)
    poll(rig, lambda s: not s["safejoin"]["holding"], timeout=5)
    state = rig.state()
    assert not state["video"]["privacy"] and not state["audio"]["mic"]["muted"]
