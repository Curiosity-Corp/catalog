"""Opt-in telemetry and crash reports on the System page: the switch, the scrape address, the hidden token."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from support import HTTP_TIMEOUT, LOADED, App, Rig, poll

REPO = Path(__file__).resolve().parents[2]

STREAM_KEY = "live_123456789_AbCdEfGhIjKlMnOpQrStUvWxYz"


@pytest.fixture(autouse=True)
def tidy(rig: Rig) -> Iterator[None]:
    """Telemetry and crash reports are process-wide on the shared demo server; leave them as found."""
    yield
    rig.act("telemetry.set", on=False)
    rig.act("crash.clear")


def scrape(rig: Rig, headers: dict[str, str]) -> tuple[int, str]:
    req = urllib.request.Request(rig.base + "metrics", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as res:
            return res.status, res.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode(errors="replace")


def panel(page: Page):
    return page.get_by_role("region", name="Telemetry and crash reports")


def test_telemetry_is_off_until_the_owner_turns_it_on(app: App, page: Page, rig: Rig) -> None:
    app.goto("system")
    section = panel(page)
    expect(section).to_be_visible(timeout=LOADED)
    switch = section.get_by_role("switch", name="Prometheus metrics")
    expect(switch).to_have_attribute("aria-checked", "false")
    expect(section.get_by_role("button", name="Copy the metrics address")).to_be_hidden()
    assert scrape(rig, {"X-StreamStack-Token": rig.token})[0] == 404

    switch.click()
    expect(switch).to_have_attribute("aria-checked", "true", timeout=LOADED)
    expect(section.locator(".diag-url")).to_have_text(f"{rig.base}metrics".rstrip("/"), timeout=LOADED)
    status, body = scrape(rig, {"X-StreamStack-Token": rig.token})
    assert status == 200 and "streamstack_build_info" in body and "streamstack_service_up" in body

    switch.click()
    expect(switch).to_have_attribute("aria-checked", "false", timeout=LOADED)
    assert scrape(rig, {"X-StreamStack-Token": rig.token})[0] == 404


def test_the_scrape_config_reaches_the_clipboard_but_the_token_never_the_page(
    app: App, page: Page, rig: Rig, browser_name: str
) -> None:
    if browser_name == "chromium":
        page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    app.goto("system")
    section = panel(page)
    switch = section.get_by_role("switch", name="Prometheus metrics")
    expect(section).to_be_visible(timeout=LOADED)
    switch.click()
    expect(switch).to_have_attribute("aria-checked", "true", timeout=LOADED)

    copy = section.get_by_role("button", name="Copy a Prometheus scrape config with the scrape token")
    copy.click()
    expect(copy).to_contain_text("Copied", timeout=LOADED)
    page_text = page.content()
    if browser_name == "chromium":
        config_text = page.evaluate("navigator.clipboard.readText()")
        match = re.search(r"credentials: (\S+)", config_text)
        assert match and "job_name: streamstack" in config_text
        token = match[1]
        assert token not in page_text and token not in rig.state()["diagnostics"].__str__()
        assert scrape(rig, {"Authorization": f"Bearer {token}"})[0] == 200
        section.get_by_role("button", name="Create a new scrape token").click()
        poll(rig, lambda s: scrape(rig, {"Authorization": f"Bearer {token}"})[0] == 401)
    assert "credentials:" not in page_text


def crash_a_process(rig: Rig) -> None:
    assert rig.home is not None
    env = {
        **os.environ,
        "HOME": str(rig.home),
        "XDG_STATE_HOME": str(rig.home / ".local/state"),
        "PYTHONPATH": os.pathsep.join(filter(None, [str(REPO / "src"), os.environ.get("PYTHONPATH", "")])),
    }
    code = (
        "from streamstack import crash\n"
        "crash.install('tray')\n"
        f"raise RuntimeError('lost the link to rtmp://live.example.com/app/{STREAM_KEY} token=abcdef0123456789')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 1


def test_a_crash_is_listed_and_its_report_holds_no_secrets(app: App, page: Page, rig: Rig) -> None:
    app.goto("system")
    section = panel(page)
    expect(section).to_be_visible(timeout=LOADED)
    expect(section).to_contain_text("Nothing has crashed")

    crash_a_process(rig)
    crashes = poll(rig, lambda s: s["diagnostics"]["crashes"]["reports"])
    assert crashes[0]["component"] == "tray" and STREAM_KEY not in json.dumps(crashes)
    item = section.locator(".diag-crash")
    expect(item).to_have_count(1, timeout=LOADED)
    expect(item).to_contain_text("RuntimeError")
    expect(item).to_contain_text("tray")
    assert STREAM_KEY not in section.inner_text()

    link = item.get_by_role("link", name=re.compile("Open the report"))
    href = link.get_attribute("href")
    assert href and href.startswith("api/crashes/")
    report = (
        urllib.request.urlopen(
            urllib.request.Request(rig.base + href, headers={"X-StreamStack-Token": rig.token}), timeout=15
        )
        .read()
        .decode()
    )
    assert STREAM_KEY not in report and "abcdef0123456789" not in report
    assert json.loads(report)["error"]["type"] == "RuntimeError"

    section.get_by_role("button", name="Remove all crash reports").click()
    expect(section).to_contain_text("Nothing has crashed", timeout=LOADED)
    assert rig.state()["diagnostics"]["crashes"]["reports"] == []


def test_the_crash_report_endpoint_wants_the_token(rig: Rig) -> None:
    crash_a_process(rig)
    (summary,) = poll(rig, lambda s: s["diagnostics"]["crashes"]["reports"])[:1]
    assert rig.request("GET", f"api/crashes/{summary['id']}.json")[0] == 401
    assert rig.request("GET", f"api/crashes/{summary['id']}.json", token=rig.token)[0] == 200
    assert rig.request("GET", "api/crashes/..%2F..%2Fetc%2Fpasswd.json", token=rig.token)[0] == 404
