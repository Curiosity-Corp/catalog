"""The face finder behind SUBJECT_FINDERS: a real detector when available, the skin heuristic when not."""

from __future__ import annotations

from pathlib import Path

import pytest

from streamstack.director import faces, imagefeatures
from streamstack.framing import detector
from streamstack.framing.engine import Face

real_install = faces.install  # the conftest fixture replaces the module attribute


class FakeDetector:
    backend = "fake"
    device = "CPU"

    def __init__(self, found: list[Face] | Exception) -> None:
        self.found = found
        self.closed = False
        self.seen: list[bytes] = []

    def detect(self, jpeg: bytes) -> list[Face]:
        self.seen.append(jpeg)
        if isinstance(self.found, Exception):
            raise self.found
        return self.found

    def close(self) -> None:
        self.closed = True


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


def scene(tmp_path: Path) -> imagefeatures.Scene:
    still = tmp_path / "still-1.jpg"
    still.write_bytes(b"JPEGDATA")
    return imagefeatures.Scene([], 0, 0, still)


def test_the_largest_confident_face_becomes_the_subject(tmp_path: Path) -> None:
    fake = FakeDetector(
        [Face(0.1, 0.1, 0.05, 0.05, 0.99), Face(0.4, 0.2, 0.2, 0.3, 0.9), Face(0.8, 0.8, 0.3, 0.3, 0.2)]
    )
    opened: list[str] = []
    finder = faces.FaceFinder(lambda device: opened.append(device) or fake)
    subject = finder(scene(tmp_path))
    assert subject["source"] == "yunet" and subject["box"] == [0.4, 0.2, 0.2, 0.3]
    assert subject["headroom"] == 0.2 and subject["off_centre"] == 0.0 and subject["confidence"] == "high"
    assert opened == ["cpu"] and fake.seen == [b"JPEGDATA"]  # the director never takes the framing engine's GPU
    assert finder.describe()["available"] is True


def test_a_still_without_a_face_gives_none_so_the_next_finder_runs(tmp_path: Path) -> None:
    assert faces.FaceFinder(lambda d: FakeDetector([]))(scene(tmp_path)) is None
    assert faces.FaceFinder(lambda d: FakeDetector([Face(0.4, 0.2, 0.2, 0.3, 0.3)]))(scene(tmp_path)) is None


def test_a_missing_model_falls_back_and_is_retried_later(tmp_path: Path) -> None:
    clock, attempts = Clock(), []

    def opener(device: str) -> FakeDetector:
        attempts.append(device)
        if len(attempts) == 1:
            raise detector.DetectorUnavailable("the YuNet model is missing")
        return FakeDetector([Face(0.3, 0.2, 0.2, 0.3, 0.9)])

    finder = faces.FaceFinder(opener, clock=clock, retry=600.0)
    assert finder(scene(tmp_path)) is None and "missing" in finder.describe()["reason"]
    assert finder.describe()["available"] is False
    assert finder(scene(tmp_path)) is None and len(attempts) == 1  # not hammered
    clock.now = 601.0
    assert finder(scene(tmp_path))["source"] == "yunet" and len(attempts) == 2


def test_a_failing_detection_is_dropped_and_reopened_later(tmp_path: Path) -> None:
    clock = Clock()
    fake = FakeDetector(RuntimeError("backend blew up"))
    finder = faces.FaceFinder(lambda d: fake, clock=clock)
    assert finder(scene(tmp_path)) is None and fake.closed and "blew up" in finder.reason


def test_an_unreadable_still_gives_none(tmp_path: Path) -> None:
    finder = faces.FaceFinder(lambda d: FakeDetector([Face(0.3, 0.2, 0.2, 0.3, 0.9)]))
    assert finder(imagefeatures.Scene([], 0, 0, tmp_path / "gone.jpg")) is None
    assert finder(imagefeatures.Scene([], 0, 0, None)) is None


def test_faces_are_clipped_to_the_picture() -> None:
    subject = faces.subject_from_face(Face(-0.1, 0.9, 0.5, 0.5, 0.7))
    assert subject["box"] == [0.0, 0.9, 0.5, 0.1] and subject["confidence"] == "medium"


def test_install_puts_the_finder_first_once_and_only_when_ready(monkeypatch) -> None:
    finders: list = []
    monkeypatch.setattr(faces.health, "readiness", lambda: (False, "needs numpy and opencv", "pip install"))
    finder, row = real_install({}, finders)
    assert finder is None and finders == [] and not row["available"] and "numpy" in row["reason"]
    monkeypatch.setattr(faces.health, "readiness", lambda: (True, "", ""))
    first, row = real_install({}, finders)
    again, _ = real_install({}, finders)
    assert finders == [first] and again is first and row["available"]
    off, row = real_install({"STREAMSTACK_AUTO_FACES": "false"}, [])
    assert off is None and "STREAMSTACK_AUTO_FACES" in row["reason"]


def test_the_skin_heuristic_still_answers_when_the_detector_does_not(monkeypatch, tmp_path: Path) -> None:
    def unavailable(device: str):
        raise detector.DetectorUnavailable("no model")

    finder = faces.FaceFinder(unavailable)
    monkeypatch.setattr(imagefeatures, "SUBJECT_FINDERS", [finder, imagefeatures._skin_finder])
    cells = [[0] * 8 for _ in range(8)]
    for r in range(2, 6):
        for c in range(3, 6):
            cells[r][c] = 20
    subject = imagefeatures.find_subject(imagefeatures.Scene(cells, 64 * 20, 240, scene(tmp_path).source))
    assert subject is not None and subject["source"] == "skin-blob" and finder.reason == "no model"


def test_a_detected_face_wins_over_the_skin_blob(monkeypatch, tmp_path: Path) -> None:
    finder = faces.FaceFinder(lambda d: FakeDetector([Face(0.3, 0.2, 0.2, 0.3, 0.9)]))
    monkeypatch.setattr(imagefeatures, "SUBJECT_FINDERS", [finder, imagefeatures._skin_finder])
    cells = [[20] * 8 for _ in range(8)]
    subject = imagefeatures.find_subject(imagefeatures.Scene(cells, 64 * 20, 1000, scene(tmp_path).source))
    assert subject["source"] == "yunet"


@pytest.mark.parametrize("name", ["yunet"])
def test_subject_records_keep_the_keys_the_judge_reads(name: str) -> None:
    subject = faces.subject_from_face(Face(0.3, 0.1, 0.2, 0.3, 0.95))
    assert {"box", "headroom", "off_centre", "source"} <= set(subject) and subject["source"] == name
