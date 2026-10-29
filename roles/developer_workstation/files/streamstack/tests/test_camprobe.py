from __future__ import annotations

from streamstack import camprobe

GOOD = {
    "devices": [{"id": "a", "label": "Elgato Cam Link 4K"}, {"id": "b", "label": "StreamStack Camera"}],
    "matched": "StreamStack Camera",
    "opened": True,
    "error": None,
    "settings": {"width": 1280, "height": 720, "frameRate": 30},
    "frames": 90,
    "distinct": 88,
    "seconds": 3.0,
    "luma_mean": 110.0,
    "luma_std": 40.0,
}


def rows(**changes: object):
    return camprobe.evaluate({**GOOD, **changes})


def failed(result) -> list[str]:
    return [r.id for r in result if not r.ok]


def test_a_healthy_camera_passes_every_check() -> None:
    result = rows()
    assert camprobe.passed(result)
    assert [r.id for r in result] == ["listed", "opens", "frames", "moving", "picture"]


def test_an_unfed_node_is_reported_as_not_listed_with_the_fix() -> None:
    result = rows(devices=[{"id": "a", "label": "Elgato Cam Link 4K"}], matched=None, opened=False)
    assert failed(result) == ["listed"] and len(result) == 1
    assert "StreamStack Camera is not listed" in result[0].title
    assert "Elgato Cam Link 4K" in result[0].detail  # what Teams would have shown instead
    assert "exclusive_caps" in result[0].hint and "doctor" in result[0].hint


def test_no_cameras_at_all_is_named() -> None:
    result = rows(devices=[], matched=None, opened=False)
    assert "no cameras at all" in result[0].detail


def test_a_node_that_will_not_open_names_the_error_and_points_at_the_holder() -> None:
    result = rows(opened=False, error="NotReadableError: Device in use")
    assert failed(result) == ["opens"]
    assert "Device in use" in result[1].detail and "loopback guard" in result[1].hint


def test_too_few_frames_fails_with_the_count() -> None:
    result = rows(frames=3, seconds=3.0)
    assert failed(result) == ["frames"]
    assert "3 in 3.0 s" in result[-1].title


def test_a_frozen_picture_is_caught() -> None:
    assert failed(rows(distinct=1)) == ["moving"]


def test_a_black_or_flat_picture_is_caught() -> None:
    assert failed(rows(luma_mean=1.0, luma_std=0.5)) == ["picture"]
    assert failed(rows(luma_mean=120.0, luma_std=0.4)) == ["picture"]


def test_thresholds_are_adjustable() -> None:
    assert camprobe.passed(camprobe.evaluate({**GOOD, "frames": 20, "seconds": 3.0}, min_frames=5, min_fps=5))
    assert not camprobe.passed(camprobe.evaluate({**GOOD, "frames": 20, "seconds": 3.0}, min_frames=5, min_fps=30))


def test_run_without_playwright_says_how_to_install_it(monkeypatch) -> None:
    import builtins

    real = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    import pytest

    with pytest.raises(RuntimeError, match="pip install playwright"):
        camprobe.run(fake=True)


def test_a_solid_card_that_drifts_in_colour_is_live_but_a_held_one_is_flat() -> None:
    """Firefox's fake camera is one solid colour that drifts (#153): spatially flat in every frame, but moving."""
    drifting = {"spatial_std": 0.0, "mean_range": 4.8, "luma_std": 0.3, "luma_peak": 149.7}
    assert camprobe.passed(rows(**drifting))
    held = {"spatial_std": 0.0, "mean_range": 0.0, "luma_std": 0.0, "luma_peak": 149.7}
    assert failed(rows(**held)) == ["picture"]


def test_a_frame_with_content_is_live_even_when_the_colour_never_moves() -> None:
    assert camprobe.passed(rows(spatial_std=12.0, mean_range=0.0, luma_std=12.0, luma_peak=90.0))


def test_a_black_warm_up_does_not_make_a_lit_feed_black_but_an_all_black_feed_still_is() -> None:
    warmed = {"luma_mean": 3.0, "luma_peak": 110.0, "spatial_std": 30.0, "mean_range": 108.0}
    assert camprobe.passed(rows(**warmed))
    black = {"luma_mean": 0.0, "luma_peak": 0.4, "spatial_std": 0.0, "mean_range": 0.4}
    assert failed(rows(**black)) == ["picture"]


def test_picture_spread_falls_back_to_the_pooled_figure_for_an_old_report() -> None:
    assert camprobe.picture_spread({"luma_std": 7.5}) == 7.5
    assert camprobe.picture_spread({"spatial_std": 2.0, "mean_range": 5.0}) == 5.0


class _Timeout(Exception):
    pass


class _FakePage:
    def __init__(self, goto_outcomes: list[Exception | None]) -> None:
        self.outcomes = goto_outcomes
        self.gotos: list[dict] = []
        self.closed = False

    def route(self, url, handler) -> None:
        self.url = url

    def goto(self, url, **kwargs) -> None:
        self.gotos.append({"url": url, **kwargs})
        outcome = self.outcomes.pop(0)
        if outcome is not None:
            raise outcome

    def wait_for_load_state(self, state, **kwargs) -> None:
        self.state = state

    def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self, outcomes: list[Exception | None]) -> None:
        self.outcomes = outcomes
        self.pages: list[_FakePage] = []

    def new_page(self) -> _FakePage:
        page = _FakePage([self.outcomes.pop(0)])
        self.pages.append(page)
        return page


def test_a_cold_browsers_first_navigation_is_retried_on_a_new_page_and_waits_at_commit() -> None:
    """Issue #124: the first Firefox navigation on a stalled runner timed out at 30 s and failed the matrix test."""
    context = _FakeContext([_Timeout("stalled"), None])
    page = camprobe.open_probe_page(context, _Timeout)
    assert page is context.pages[1] and context.pages[0].closed and not page.closed
    assert page.gotos == [{"url": camprobe.PROBE_URL, "wait_until": "commit", "timeout": camprobe.NAV_TIMEOUT_MS}]
    assert camprobe.NAV_TIMEOUT_MS > 30_000, "the allowance must exceed Playwright's 30 s default that tripped in CI"


def test_a_navigation_that_times_out_every_attempt_still_fails() -> None:
    import pytest

    context = _FakeContext([_Timeout("one"), _Timeout("two")])
    with pytest.raises(_Timeout, match="two"):
        camprobe.open_probe_page(context, _Timeout)
    assert len(context.pages) == camprobe.NAV_ATTEMPTS and all(p.closed for p in context.pages)


def test_other_errors_are_not_retried() -> None:
    import pytest

    context = _FakeContext([RuntimeError("boom")])
    with pytest.raises(RuntimeError, match="boom"):
        camprobe.open_probe_page(context, _Timeout)
    assert len(context.pages) == 1
