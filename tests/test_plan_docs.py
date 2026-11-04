"""The plan and audit documents keep their honesty guarantees (catalog #4, #24, #26, #38)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


def read(name: str) -> str:
    return (DOCS / name).read_text()


def test_documents_exist() -> None:
    for name in ("history-audit.md", "security-audit.md", "role-capability-plan.md", "accessibility.md"):
        assert (DOCS / name).is_file(), name


def test_every_script_a_document_names_exists() -> None:
    for name in ("history-audit.md", "security-audit.md", "role-capability-plan.md", "accessibility.md"):
        for ref in re.findall(r"`(scripts/[A-Za-z0-9_./-]+\.(?:py|sh))`", read(name)):
            assert (ROOT / ref).is_file(), f"{name} names missing file {ref}"


def test_accessibility_doc_makes_no_compliance_claim() -> None:
    text = read("accessibility.md")
    assert "NOT a conformance claim" in text
    assert "no assistive-technology testing has been" in text.lower().replace("\n", " ")
    for claim in ("is wcag", "wcag 2.1 aa compliant", "fully accessible", "section 508 compliant"):
        assert claim not in text.lower()
    # the checklist covers each path the issue names
    for heading in ("A. Openbox desktop", "B. GNOME path", "C. Firefox", "D. Kiosk"):
        assert heading in text


def test_history_audit_records_no_values_and_has_a_decision_record() -> None:
    text = read("history-audit.md")
    assert "Decision record" in text
    assert "An agent must not change visibility" in text
    for private in ("developerdojo", "focusapiary", "focuscell", "focuspass", "omlab"):
        assert private not in text.lower()
    assert not re.search(r"\b(?:10|192\.168)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text)
    assert not re.search(r"[\w.]+@[\w.]+\.\w+", text)


def test_security_audit_lists_what_remains() -> None:
    text = read("security-audit.md")
    assert "Remaining (not done in this pass)" in text
    assert "samsung_tv_key_vault_secret" in text


def test_capability_plan_leaves_the_role_whole() -> None:
    text = read("role-capability-plan.md")
    assert "Nothing is split by this change" in text
    assert "Compatibility wrapper" in text
    assert "Measurements: before" in text
