"""The System page's "Video in use" card: which GPU each video job runs on, and the cameras (ADR 0003).

The demo rig replays the recording of each studio, so both layouts are checked without either machine: on
dm313red the Arc is the only GPU and does everything; on deskmeet1 the UHD 770 enumerates first (renderD128) but the
Arc (renderD129) must still take grade, blur and the stream encode, while the Canon decode goes to the iGPU.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from support import LOADED, App, Rig, poll


@pytest.fixture
def video(app: App, page: Page) -> Page:
    app.goto("system")
    expect(page.locator("[data-testid=hw-rig]")).not_to_have_text("Probing…", timeout=LOADED)
    return page


def job(page: Page, consumer: str):
    return page.locator(f".hw-job[data-consumer='{consumer}']")


def camera(page: Page, ident: str):
    return page.locator(f".hw-cam[data-camera='{ident}']")


def show(page: Page, rig: Rig, host: str) -> None:
    page.get_by_role("button", name=host, exact=True).click()
    poll(rig, lambda s: s["hardware"]["host"] == host)


def test_dm313red_runs_every_video_job_on_the_only_gpu(video: Page, rig: Rig) -> None:
    expect(video.locator(".hw-job")).to_have_count(4)
    for consumer in ("engine", "blur", "stream", "canon"):
        expect(job(video, consumer)).to_have_attribute("data-node", "/dev/dri/renderD128")
        expect(job(video, consumer)).to_have_attribute("data-pci", "8086:56a5")
    expect(camera(video, "cam-link-4k")).to_have_attribute("data-usb", "0fd9:0067")
    expect(camera(video, "canon-eos-50d")).to_have_attribute("data-usb", "04a9:319b")
    loop = video.locator("[data-testid=hw-loopback]")
    expect(loop).to_have_attribute("data-camera", "/dev/video42")
    expect(loop).to_have_attribute("data-program", "/dev/video43")
    expect(loop).to_contain_text("module options match the rig")
    assert rig.state()["hardware"]["video"]["jobs"][0]["via"] == "rig"


def test_deskmeet1_keeps_the_arc_for_grade_encode_and_blur_and_gives_the_igpu_the_canon_decode(
    video: Page, rig: Rig
) -> None:
    show(video, rig, "deskmeet1")
    for consumer in ("engine", "blur", "stream"):
        expect(job(video, consumer)).to_have_attribute("data-node", "/dev/dri/renderD129")
        expect(job(video, consumer)).to_have_attribute("data-pci", "8086:56a5")
    expect(job(video, "canon")).to_have_attribute("data-node", "/dev/dri/renderD128")
    expect(job(video, "canon")).to_have_attribute("data-pci", "8086:a780")
    expect(camera(video, "cam-link-4k")).to_have_attribute("data-usb", "0fd9:0067")
    expect(camera(video, "cam-link-4k")).to_have_attribute("data-node", "/dev/video0")
    expect(camera(video, "canon-eos-50d")).to_have_attribute("data-state", "present")
    jobs = {j["consumer"]: j["node"] for j in rig.state()["hardware"]["video"]["jobs"]}
    assert jobs == {
        "engine": "/dev/dri/renderD129",
        "blur": "/dev/dri/renderD129",
        "stream": "/dev/dri/renderD129",
        "canon": "/dev/dri/renderD128",
    }


def test_the_video_card_follows_the_recorded_host(video: Page, rig: Rig) -> None:
    show(video, rig, "deskmeet1")
    expect(job(video, "engine")).to_have_attribute("data-node", "/dev/dri/renderD129")
    show(video, rig, "dm313red")
    expect(job(video, "engine")).to_have_attribute("data-node", "/dev/dri/renderD128")
    expect(job(video, "canon")).to_have_attribute("data-pci", "8086:56a5")
