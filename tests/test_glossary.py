"""catalog#130: the glossary's links resolve and its terms are used elsewhere.

Each `## Term` heading in docs/glossary.md is a defined term. Every link on the
page must point at an existing file (and an existing heading when it carries an
anchor), and every term must appear on at least one other Markdown page, so a
term nobody uses any more fails here until it is removed.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs/glossary.md"
TEXT = PAGE.read_text()
CI = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())

TERMS = re.findall(r"^## (.+)$", TEXT, flags=re.MULTILINE)
OTHER_PAGES = [
    p for p in [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))] if p != PAGE
]


def _anchors(path: Path) -> set[str]:
    """GitLab heading anchors: lower-case, punctuation dropped, spaces to hyphens."""
    anchors = set()
    for heading in re.findall(r"^#+ (.+)$", path.read_text(), flags=re.MULTILINE):
        slug = re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")
        anchors.add(slug)
    return anchors


def _entries() -> dict[str, str]:
    blocks = re.split(r"^## ", TEXT, flags=re.MULTILINE)[1:]
    return {b.splitlines()[0]: b for b in blocks}


def test_issue_terms_are_defined() -> None:
    for term in (
        "Profile",
        "Ring",
        "Tag",
        "Manifest",
        "Snapshot",
        "Quarantine",
        "Hardware profile",
    ):
        assert term in TERMS, term


def test_terms_are_sorted() -> None:
    assert TERMS == sorted(TERMS, key=str.lower)


def test_every_entry_links_to_a_page() -> None:
    for term, block in _entries().items():
        assert re.search(r"\]\([^)\s]+\)", block), (
            f"{term}: no link to an in-depth page"
        )


def test_page_links_resolve() -> None:
    targets = re.findall(r"\]\(([^)\s]+)\)", TEXT)
    assert targets
    for target in targets:
        path_part, _, anchor = target.partition("#")
        dest = (PAGE.parent / path_part).resolve()
        assert dest.is_file(), target
        if anchor:
            assert anchor in _anchors(dest), target


def test_every_term_appears_on_another_page() -> None:
    for term in TERMS:
        pattern = re.compile(rf"\b{re.escape(term)}s?\b", flags=re.IGNORECASE)
        users = [p for p in OTHER_PAGES if pattern.search(p.read_text())]
        assert users, f"'{term}' is used on no page but the glossary: remove it"


def test_an_unused_term_would_fail() -> None:
    pattern = re.compile(r"\bflux capacitor\b", flags=re.IGNORECASE)
    assert not any(pattern.search(p.read_text()) for p in OTHER_PAGES)


def test_readme_links_the_glossary() -> None:
    assert "](docs/glossary.md)" in (ROOT / "README.md").read_text()


def test_quickstarts_index_links_the_glossary() -> None:
    assert "](../glossary.md)" in (ROOT / "docs/quickstarts/README.md").read_text()


def test_this_file_runs_in_the_policy_job() -> None:
    script = " ".join(CI["policy"]["script"])
    assert "tests/test_glossary.py" in script
