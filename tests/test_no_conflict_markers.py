"""Fail when a tracked text file still contains git merge-conflict markers."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Markers git writes (including diff3 style). A bare "=======" is a valid
# Markdown setext underline, so the separator only counts when text follows it.
MARKER = re.compile(r"^(?:(?:<<<<<<<|>>>>>>>|\|\|\|\|\|\|\|)(?: |$)|======= )")


def tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout
    return [ROOT / p for p in out.decode().split("\0") if p]


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
