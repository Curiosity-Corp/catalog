"""Community, pilot, evidence and upstream pages stay true to the repository."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMMUNITY = ROOT / "docs/community"
EVIDENCE = ROOT / "docs/evidence"
PRIVATE_MARKERS = (
    "developerdojo.org", "focusapiary", "focuscell.org", "focuspass.com", "focusbuzz.org", "dreamhive.org",
    "dreamdecks.org", "hardmagic.com", "hypersight.net", "slidee.net", "omlab-secrets", "harbor-internal-ca",
)
NEW_EVIDENCE = ("pilot-feedback-template.md", "contributor-session-notes-template.md", "downloads-dependents-template.md")


def read(path: Path) -> str:
    return path.read_text()


def test_community_pages_name_no_private_service_or_person() -> None:
    for path in [*COMMUNITY.glob("*.md"), *(EVIDENCE / name for name in NEW_EVIDENCE), ROOT / ".gitlab/issue_templates/Good_first_issue.md"]:
        text = read(path).lower()
        for marker in PRIVATE_MARKERS:
            if path.name == "good-first-issues.md" and marker == "developerdojo.org":
                continue  # the issue links point at the project's own tracker
            assert marker not in text, f"{marker} in {path.relative_to(ROOT)}"
        assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), f"an email address in {path.name}"


def test_review_area_paths_exist() -> None:
    text = read(COMMUNITY / "maintainer-onboarding.md")
    rows = [line for line in text.splitlines() if line.startswith("| ") and "`" in line and "Paths" not in line and "---" not in line]
    assert len(rows) >= 7
    checked = 0
    for row in rows:
        directory = ""
        for token in re.findall(r"`([^`]+)`", row.split("|")[2]):
            if not re.search(r"[./*]", token) or token.endswith(" handling"):
                continue
            if "/" in token:
                directory = token.rsplit("/", 1)[0] + "/" if not token.endswith("/") else token
                pattern = token
            else:
                pattern = directory + token
            matches = list(ROOT.glob(pattern)) or ([] if "/" in token else list(ROOT.glob(token)))
            assert matches, f"review area path {pattern} matches nothing"
            checked += 1
    assert checked >= 15


def test_every_review_area_that_changes_privileged_behaviour_needs_two_maintainers() -> None:
    rows = {}
    for line in read(COMMUNITY / "maintainer-onboarding.md").splitlines():
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) == 4 and cells[0] not in ("Area", "---", ""):
            rows[cells[0]] = cells[3]
    for area in ("Update control plane", "Privileged system changes", "Defaults and public API", "Identity, certificates, secrets",
                 "CI and supply chain", "Security documents"):
        assert rows[area].startswith("Yes"), area
    assert rows["Documentation, examples, tests only"].startswith("No")


def test_onboarding_path_is_ordered_and_ends_with_a_recorded_decision() -> None:
    text = read(COMMUNITY / "maintainer-onboarding.md")
    steps = re.findall(r"^\d\. \*\*(\w[^*]+)\*\*", text, re.MULTILINE)
    assert steps == ["Contribute", "Triage", "Review", "Shadow a release", "Review a privileged change", "Propose"]
    assert "decision record" in text.split("**Propose**")[1]


def test_backlog_lists_at_least_ten_open_scoped_issues_with_valid_links() -> None:
    text = read(COMMUNITY / "good-first-issues.md")
    numbers = re.findall(r"\| \[#(\d+)\]\(https://git\.developerdojo\.org/Curiosity-Corp/catalog/-/issues/(\d+)\)", text)
    assert len(numbers) >= 10
    assert all(a == b for a, b in numbers) and len({a for a, _ in numbers}) == len(numbers)
    for needle in ("good first issue", "help wanted", "7-day", "Monthly stale review", "Credit"):
        assert needle in text, needle


def test_good_first_template_has_the_sections_the_backlog_promises() -> None:
    template = read(ROOT / ".gitlab/issue_templates/Good_first_issue.md")
    for heading in ("## Context", "## Acceptance criteria", "## Test guidance", "## Depends on"):
        assert heading in template
    assert 'good first issue' in template


@pytest.mark.parametrize(
    ("name", "required"),
    [
        ("pilot-feedback-template.md", ("## Permission", "## What they tried", "## Issues and requests", "withdraw")),
        ("contributor-session-notes-template.md", ("## Credit", "Substantive?", "Security reports")),
        ("downloads-dependents-template.md", ("append-only", "not available", "threshold", "Source of the measurement")),
    ],
)
def test_evidence_templates_have_their_required_fields(name: str, required: tuple[str, ...]) -> None:
    text = " ".join(read(EVIDENCE / name).split())
    for needle in required:
        assert needle in text, f"{name} lacks {needle}"
    assert "YYYY" in text, "a template stays a blank record"


def test_monthly_template_requires_rechecks_before_external_use() -> None:
    text = read(EVIDENCE / "monthly-template.md")
    section = text.split("## Rechecks before any external use")[1]
    assert section.count("- [ ]") >= 3
    for needle in ("contributor worksheet", "registry", "private deployment"):
        assert needle in section
    for needle in ("Substantive external merged contributors", "Window used", "Pilot feedback links", "Security policy"):
        assert needle in text


def test_pilot_and_session_runbooks_forbid_gaming() -> None:
    pilot = " ".join(read(COMMUNITY / "pilot-kit.md").split())
    assert "at least five unrelated projects" in pilot
    assert "separate file" in pilot and "access-controlled" in pilot
    session = " ".join(read(COMMUNITY / "contributor-session.md").split())
    assert "do not ask anyone to split a change" in session.lower()
    assert "make quickstarts" in session
    assert "does not pick the date" in session


def test_cohort_page_matches_the_script() -> None:
    page = read(COMMUNITY / "contributor-cohorts.md")
    script = read(ROOT / "scripts/contributor_cohort.py")
    assert "scripts/contributor_cohort.py" in page and "--internal-file" in page and "--internal-file" in script
    for rule in ("Bots and AI agents", "Token changes", "split into several merge requests"):
        assert rule in page
    assert "12-month" in page


def test_upstream_inventory_matches_third_party_notices_and_claims_no_outreach() -> None:
    page = read(COMMUNITY / "upstream-stewardship.md")
    third_party = read(ROOT / "THIRD_PARTY.md")
    for name in re.findall(r"`roles/developer_workstation/files/([\w.-]+)`", third_party):
        assert (ROOT / "roles/developer_workstation/files" / name).exists(), name
        assert name in page, f"{name} is in THIRD_PARTY.md but not in the upstream inventory"
    assert "records no outreach" in page
    assert "has **not been measured**" in page
    assert "Not reviewed" in page


def test_top_level_documents_link_the_community_pages() -> None:
    readme = read(ROOT / "README.md")
    contributing = read(ROOT / "CONTRIBUTING.md")
    for target in ("docs/community/good-first-issues.md", "docs/community/maintainer-onboarding.md", "docs/index.md"):
        assert target in readme, target
    assert "docs/community/good-first-issues.md" in contributing
