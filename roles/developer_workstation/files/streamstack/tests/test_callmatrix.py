from __future__ import annotations

from streamstack import callmatrix, camprobe

GOOD = {
    "devices": [{"id": "a", "label": "fake_device_0"}],
    "matched": "fake_device_0",
    "opened": True,
    "settings": {"width": 640, "height": 480},
    "frames": 60,
    "distinct": 60,
    "seconds": 2.0,
    "luma_mean": 90.0,
    "luma_std": 20.0,
}


def runner(missing: tuple[str, ...] = (), broken: str = "", unlisted: str = ""):
    def run(label, *, seconds, fake, channel, browser):
        name = channel or browser
        if name in missing:
            raise camprobe.BrowserMissing(f"{name} is not installed here")
        if name == broken:
            raise RuntimeError("the browser could not run the probe: boom")
        if name == unlisted:
            return {**GOOD, "matched": None, "opened": False}
        return GOOD

    return run


def statuses(results):
    return {r.name: r.status for r in results}


def test_every_entry_passes_when_every_browser_delivers() -> None:
    results = callmatrix.run_matrix(fake=True, runner=runner())
    assert statuses(results) == {
        "Chromium": "pass", "Chrome": "pass", "Edge": "pass", "Teams in Edge": "pass", "Firefox": "pass",
    }  # fmt: skip
    assert callmatrix.exit_code(results) == 0


def test_a_browser_that_is_not_installed_is_a_skip_not_a_failure() -> None:
    results = callmatrix.run_matrix(fake=True, runner=runner(missing=("chrome", "msedge")))
    assert statuses(results)["Chrome"] == "skip" and statuses(results)["Teams in Edge"] == "skip"
    assert callmatrix.exit_code(results) == 0
    assert callmatrix.exit_code(results, require_all=True) == 2


def test_a_browser_that_cannot_list_the_camera_fails_the_matrix_with_the_reason() -> None:
    results = callmatrix.run_matrix(label="StreamStack Camera", runner=runner(unlisted="firefox"))
    failed = [r for r in results if r.status == "fail"]
    assert [r.name for r in failed] == ["Firefox"] and "not listed" in failed[0].note
    assert callmatrix.exit_code(results) == 1


def test_a_crashing_browser_is_a_failure_with_its_message() -> None:
    results = callmatrix.run_matrix(runner=runner(broken="msedge"))
    assert [r.name for r in results if r.status == "fail"] == ["Edge", "Teams in Edge"]
    assert "boom" in results[2].note


def test_nothing_ran_is_exit_two() -> None:
    results = callmatrix.run_matrix(runner=runner(missing=("chromium", "chrome", "msedge", "firefox")))
    assert callmatrix.exit_code(results) == 2


def test_only_selects_by_name_and_the_default_label_is_each_browsers_test_camera() -> None:
    seen = []

    def run(label, **kwargs):
        seen.append((kwargs["browser"], label))
        return GOOD

    callmatrix.run_matrix(fake=True, only=("firefox", "Chromium"), runner=run)
    assert seen == [("chromium", "fake_device_0"), ("firefox", "Default Video Device")]


def test_the_table_has_one_row_per_browser() -> None:
    text = callmatrix.table(callmatrix.run_matrix(fake=True, runner=runner(missing=("chrome",))))
    assert text.count("\n") == 6 and "| Chrome | skip | chrome is not installed here |" in text
