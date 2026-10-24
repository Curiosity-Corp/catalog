"""Fail when a tracked text file still contains git merge-conflict markers."""

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Markers git writes (including diff3 style). A bare "=======" is a valid
# Markdown setext underline, so the separator only counts when text follows it.
MARKER = re.compile(r"^(?:(?:<<<<<<<|>>>>>>>|\|\|\|\|\|\|\|)(?: |$)|======= )")


SKIP_DIRS = {".git", ".ci-venv", ".venv", "node_modules", "__pycache__", ".pytest_cache"}


def tracked_files() -> list[Path]:
    """Walk the checkout; the CI image has no git binary."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        found.extend(Path(dirpath) / name for name in filenames)
    return found


def test_no_leftover_conflict_markers() -> None:
    offenders: list[str] = []
    for path in tracked_files():
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(lines, start=1):
            if MARKER.match(line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line[:60]}")
    assert not offenders, "leftover conflict markers:\n" + "\n".join(offenders)
