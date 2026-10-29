"""The Presets page: profiles apply a whole setup, platform and tier resolve a stream plan, looks grade the picture."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import Locator, Page, expect

from support import EXPECT_TIMEOUT, LOADED, App, Rig, hold, poll, quiet, tap


@pytest.fixture
def pr(app: App) -> App:
    return app.goto("presets")


def profile(page: Page, name: str) -> Locator:
    return page.get_by_role("button", name=f"Apply profile {name}")


def test_gallery_lists_the_builtin_profiles(pr: App) -> None:
    for name in ("Meeting", "Podcast", "Livestream Gaming", "Webinar", "Interview", "Late Night"):
        expect(profile(pr.page, name)).to_be_visible()
    expect(pr.page).to_have_title(re.compile("^Presets"))


def test_applying_a_profile_sets_every_part(pr: App, rig: Rig) -> None:
    tap(pr.page, profile(pr.page, "Podcast"))
    state = poll(rig, lambda s: s["presets"]["profile"] == "Podcast" and s)
    assert state["presets"]["platform"] and state["presets"]["tier"]
    assert state["audio"]["chain"]["preset"].startswith("StreamStack Voice")
    # the lighting preset is read back from the lights, which lag the write: wait for it rather than sample once
    assert poll(rig, lambda st: st["lighting"]["preset"]), "the lights never showed the profile's lighting preset"
    steps = {step["part"]: step["status"] for step in state["presets"]["last_apply"]["steps"]}
    assert steps["look"] == "ok" and steps["voice"] == "ok" and steps["lighting"] == "ok"
    # the stream backend answers stream.set, so the platform and tier step is applied and the page says so
    assert steps["stream"] == "ok"
    expect(pr.page.get_by_text("Sent to stream backend")).to_be_visible()
    expect(pr.page.locator(".pr-profile[data-current]")).to_have_count(1)
    expect(pr.page.locator(".pr-result")).to_contain_text("Podcast")


def test_platform_and_tier_show_the_resolved_plan(pr: App, rig: Rig) -> None:
    page = pr.page
    tap(page, page.get_by_role("radio", name="YouTube Live"))
    tap(page, page.get_by_role("radio", name="Constrained"))
    state = poll(rig, lambda s: s["presets"]["tier"] == "constrained" and s["presets"]["platform"] == "youtube" and s)
    assert state["presets"]["plan"]["video"]["height"] == 720
    plan = page.locator(".pr-plan")
    expect(plan).to_contain_text("1280×720")
    expect(plan).to_contain_text("-14 LUFS")
    expect(plan.get_by_role("link", name="Source")).to_have_attribute("href", re.compile("^https://"))
    expect(page.get_by_role("radio", name="YouTube Live")).to_have_attribute("aria-checked", "true")
    tap(page, page.get_by_role("radio", name="Ultra"))
    expect(plan).to_contain_text("3840×2160")


def test_uncertain_platform_lists_what_is_uncertain(pr: App) -> None:
    page = pr.page
    tap(page, page.get_by_role("radio", name="TikTok Live"))
    expect(page.locator(".pr-uncertain")).to_be_visible()
    expect(page.locator(".pr-plan")).to_contain_text("1080×1920")


def test_arrow_keys_move_the_tier(pr: App, rig: Rig) -> None:
    page = pr.page
    balanced = page.get_by_role("radio", name="Balanced")
    if rig.state()["presets"]["tier"] != "balanced":
        tap(page, balanced)
    # Both the click and the arrow key post `presets.tier` on their own connection and the threaded server may run
    # them out of order, so the first must have landed (and the page shown it) before the second is sent.
    poll(rig, lambda s: s["presets"]["tier"] == "balanced")
    expect(balanced).to_have_attribute("aria-checked", "true", timeout=LOADED)
    balanced.focus()
    page.keyboard.press("ArrowRight")
    assert poll(rig, lambda s: s["presets"]["tier"] == "constrained")


def test_two_quick_tier_choices_land_in_the_order_they_were_made(pr: App, rig: Rig) -> None:
    """The server answers each request on its own thread: a slow first post must not land after the second one.

    The page holds back the first post (Balanced) for a second, so a second choice (Constrained) made at once would
    overtake it, and the older choice would stand. The page sends posts of one action in the order it made them.
    """
    page = pr.page
    page.evaluate(
        """() => {
          const send = window.fetch.bind(window);
          window.fetch = (url, init) => {
            const body = typeof init?.body === 'string' ? init.body : '';
            const slow = body.includes('"presets.tier"') && body.includes('"balanced"');
            return slow ? new Promise((done) => setTimeout(done, 1000)).then(() => send(url, init)) : send(url, init);
          };
        }"""
    )
    tap(page, page.get_by_role("radio", name="Balanced"))
    page.get_by_role("radio", name="Balanced").focus()
    page.keyboard.press("ArrowRight")
    assert poll(rig, lambda s: s["presets"]["tier"] == "constrained")
    page.wait_for_timeout(1500)  # the held post has long been answered: the later choice must still stand
    assert rig.state()["presets"]["tier"] == "constrained"
    expect(page.get_by_role("radio", name="Constrained")).to_have_attribute("aria-checked", "true")


def test_looks_grid_filters_and_applies(pr: App, rig: Rig) -> None:
    page = pr.page
    looks = page.locator(".pr-look")
    assert looks.count() >= 20
    expect(page.locator(".pr-look img").first).to_have_attribute("src", re.compile(r"^static/presets/looks/.+\.png$"))
    tap(page, page.get_by_role("button", name=re.compile(r"^Film stocks")))
    shown = looks.count()
    assert 0 < shown < 37
    tap(page, page.get_by_role("button", name="Apply look Portra Portrait"))
    state = poll(rig, lambda s: s["video"]["look"]["warmth"] > 0 and s)
    assert state["video"]["look"]["saturation"] != 1.0 or state["video"]["look"]["warmth"] > 0
    expect(page.locator(".pr-look[data-current]")).to_have_count(1)
    expect(page.locator(".pr-look[data-current]")).to_contain_text("Portra Portrait")


def test_thumbnails_load(pr: App) -> None:
    pr.page.locator(".pr-look img").last.scroll_into_view_if_needed()
    pr.page.wait_for_function(
        "() => [...document.querySelectorAll('.pr-look img')].filter(i => i.complete && i.naturalWidth > 0).length > 5",
        timeout=LOADED,
    )


def test_save_export_import_and_delete_a_profile(pr: App, rig: Rig, tmp_path: Path) -> None:
    page = pr.page
    tap(page, page.get_by_role("radio", name="Twitch"))
    # Save takes the platform from the page state, which only has it once the choice is read back from the rig.
    expect(page.get_by_role("radio", name="Twitch")).to_have_attribute("aria-checked", "true", timeout=LOADED)
    page.get_by_label("Profile name").fill("Friday show")
    tap(page, page.get_by_role("button", name="Save profile"))
    mine = poll(rig, lambda s: [p for p in s["presets"]["profiles"] if not p["builtin"]])
    assert mine[0]["name"] == "Friday show" and mine[0]["platform"] == "twitch"
    expect(profile(page, "Friday show")).to_be_visible()

    with page.expect_download() as info:
        tap(page, page.locator('ss-button[aria-label="Export profile Friday show"]'))
    path = tmp_path / "friday.json"
    info.value.save_as(path)
    doc = json.loads(path.read_text())
    assert doc["schema"] == "streamstack.profile/1" and doc["profile"]["name"] == "Friday show"

    hold(page, page.locator('ss-button[aria-label="Hold to delete profile Friday show"]'), 1200)
    poll(rig, lambda s: not [p for p in s["presets"]["profiles"] if not p["builtin"]])
    expect(profile(page, "Friday show")).to_have_count(0)

    doc["profile"]["name"] = "Imported one"
    path.write_text(json.dumps(doc))
    page.locator("input[type=file]").set_input_files(path)
    poll(rig, lambda s: any(p["name"] == "Imported one" for p in s["presets"]["profiles"]))
    expect(profile(page, "Imported one")).to_be_visible()


def test_import_rejects_a_bad_file(pr: App, rig: Rig, tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(
        '{"schema": "streamstack.profile/1", "profile": {"name": "x", "platform": "myspace", "tier": "ultra"}}'
    )
    pr.page.locator("input[type=file]").set_input_files(bad)
    expect(pr.page.locator("ss-toast")).to_contain_text("unknown platform", timeout=EXPECT_TIMEOUT)
    assert not [p for p in rig.state()["presets"]["profiles"] if not p["builtin"]]
    junk = tmp_path / "junk.json"
    junk.write_text("not json {")
    quiet(pr.page)
    pr.page.locator("input[type=file]").set_input_files(junk)
    expect(pr.page.locator("ss-toast")).to_contain_text("not valid JSON", timeout=EXPECT_TIMEOUT)


def test_command_palette_applies_a_profile(pr: App, rig: Rig) -> None:
    pr.press("Control+k")
    pr.page.get_by_role("combobox").fill("Profile: Interview")
    pr.page.keyboard.press("Enter")
    assert poll(rig, lambda s: s["presets"]["profile"] == "Interview")


@pytest.mark.parametrize(("width", "height", "tag"), [(1280, 720, "1280x720"), (390, 844, "390x844")])
def test_layout_has_no_overflow_and_screenshots(
    app: App, page: Page, artifacts: Path, rig: Rig, width: int, height: int, tag: str
) -> None:
    page.set_viewport_size({"width": width, "height": height})
    app.goto("presets")
    rig.act("presets.profile.apply", id="gaming")
    expect(page.locator(".pr-profile[data-current]")).to_have_count(1, timeout=LOADED)
    expect(page.locator(".pr-plan")).to_contain_text("Mbps")
    page.evaluate("() => document.fonts.ready")
    overflow = page.evaluate(
        """() => {
          const bad = [];
          const root = document.querySelector('.pr');
          if (root.scrollWidth > root.clientWidth + 1) bad.push('page ' + root.scrollWidth + '>' + root.clientWidth);
          for (const el of root.querySelectorAll('.pr-profile, .pr-platform, .pr-look, .pr-plan, .pr-spec')) {
            const over = el.scrollWidth - el.clientWidth;
            if (over > 1) bad.push(el.className + ' +' + over);
          }
          return bad;
        }"""
    )
    page.screenshot(path=str(artifacts / f"presets-{tag}.png"))
    for name, ref in (("platform", "streamCard"), ("looks", "looksCard")):
        card = page.locator(f'[data-ref="{ref}"]')
        card.evaluate('(el) => el.scrollIntoView({ block: "start" })')
        page.screenshot(path=str(artifacts / f"presets-{name}-{tag}.png"))
    assert (artifacts / f"presets-{tag}.png").stat().st_size > 5_000
    assert overflow == []
