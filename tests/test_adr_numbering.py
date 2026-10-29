"""Architecture decision records keep one number each, and every link to one resolves (#152)."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ADR = REPO / "docs" / "adr"
NAME = re.compile(r"^(\d{4})-[a-z0-9-]+\.md$")


def records() -> list[Path]:
    return sorted(p for p in ADR.glob("*.md") if NAME.match(p.name))


def test_every_record_has_its_own_number() -> None:
    numbers = Counter(NAME.match(p.name)[1] for p in records())
    assert records(), "no ADRs found"
    assert [n for n, count in numbers.items() if count > 1] == []


def test_a_record_title_carries_its_file_number() -> None:
    for path in records():
        title = path.read_text().splitlines()[0]
        assert title.startswith(f"# ADR {NAME.match(path.name)[1]}:"), f"{path.name}: {title}"


def test_every_link_to_a_record_resolves() -> None:
    link = re.compile(r"\(((?:\.\./)*(?:docs/)?adr/\d{4}-[a-z0-9-]+\.md)\)")
    for path in [*REPO.glob("*.md"), *(REPO / "docs").rglob("*.md")]:
        for target in link.findall(path.read_text()):
            assert (path.parent / target).resolve().is_file(), f"{path.relative_to(REPO)} links to missing {target}"
