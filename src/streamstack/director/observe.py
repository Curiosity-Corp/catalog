"""One observation: a burst of stills, a contact sheet, local image measurements and a local voice analysis.

Everything here degrades instead of failing. Without ffmpeg (or when a still cannot be decoded) the director keeps
working from the single middle still; without a microphone or enough speech the voice is simply "not measured".
Stills are what goes to Claude; audio is analysed here and only the resulting numbers are kept.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from streamstack.director import imagefeatures

MAX_BURST = 6


@dataclass
class Observation:
    """What one pass saw. `sheet` is the image Claude is sent: the contact sheet, or the single still."""

    source: str
    stills: list[Path]
    sheet: Path
    spacing: float
    analysis: dict | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def middle(self) -> Path:
        """The still the local exposure meter and the face box refer to."""
        return self.stills[len(self.stills) // 2]


def burst_size(cfg: dict[str, str]) -> int:
    try:
        return max(1, min(MAX_BURST, int(float(cfg.get("STREAMSTACK_AUTO_BURST", "4")))))
    except ValueError:
        return 4


def burst_spacing(cfg: dict[str, str]) -> float:
    try:
        return max(0.2, min(5.0, float(cfg.get("STREAMSTACK_AUTO_BURST_SECONDS", "1.0"))))
    except ValueError:
        return 1.0


def capture_burst(
    shoot: Callable[[Path], Path], folder: Path, count: int, spacing: float, sleep: Callable[[float], None] = time.sleep
) -> tuple[list[Path], list[str]]:
    """`count` stills `spacing` seconds apart. The first still must succeed; a later failure shortens the burst."""
    stills: list[Path] = []
    problems: list[str] = []
    for index in range(count):
        if index:
            sleep(spacing)
        path = folder / f"still-{index + 1}.jpg"
        try:
            stills.append(shoot(path))
        except Exception as exc:
            if not stills:
                raise
            problems.append(f"still {index + 1}: {exc}")
            break
    return stills, problems


def observe(
    source: str,
    shoot: Callable[[Path], Path],
    folder: Path,
    count: int,
    spacing: float,
    sleep: Callable[[float], None] = time.sleep,
) -> Observation:
    """Take the burst, compose the contact sheet and measure every still."""
    stills, problems = capture_burst(shoot, folder, count, spacing, sleep)
    middle = stills[len(stills) // 2]
    sheet = middle
    analysis: dict | None = None
    try:
        analysis = imagefeatures.analyze_stills(stills)
        problems += analysis.get("problems", [])
    except imagefeatures.ImageAnalysisError as exc:
        problems.append(f"image analysis: {exc}")
    if len(stills) > 1:
        try:
            sheet = imagefeatures.contact_sheet(stills, folder / "sheet.jpg")
        except imagefeatures.ImageAnalysisError as exc:
            problems.append(f"contact sheet: {exc}")
    return Observation(source, stills, sheet, spacing, analysis, problems)
