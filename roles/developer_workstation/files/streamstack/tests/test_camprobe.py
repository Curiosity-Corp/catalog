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
