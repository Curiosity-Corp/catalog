"""Governance, security-process and identity documents stay true and current."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")


def read(path: str) -> str:
    return (ROOT / path).read_text()


# --- governance and decision records ---------------------------------------


def test_annual_review_is_recorded_and_not_overdue() -> None:
    text = read("GOVERNANCE.md")
    due = re.search(r"Next review due: (\d{4}-\d{2}-\d{2})", text)
    assert due, "GOVERNANCE.md must record the next review date"
    rows = re.findall(r"^\| (\d{4}-\d{2}-\d{2}) \|", text, flags=re.M)
    assert rows, "the review table needs at least one dated row"
    last = max(dt.date.fromisoformat(r) for r in rows)
    assert (dt.date.today() - last).days <= 400, (
        f"the annual governance review was last recorded on {last}; review and add a row"
    )
    assert dt.date.fromisoformat(due.group(1)) > last


def test_governance_covers_the_required_topics() -> None:
    text = read("GOVERNANCE.md")
    for heading in (
        "## Maintainer responsibilities",
        "## Review expectations",
        "## Backup contact route",
        "## Who approves what",
        "## Decision records",
        "## Annual review",
    ):
        assert heading in text, heading
    for decision in ("Security report", "Breaking change", "Release", "Emergency security release"):
        assert decision in text, decision


def test_decision_records_are_indexed_and_complete() -> None:
    folder = ROOT / "docs/decisions"
    index = (folder / "README.md").read_text()
    records = sorted(p for p in folder.glob("[0-9][0-9][0-9][0-9]-*.md") if not p.name.startswith("0000"))
    assert records
    template = (folder / "0000-template.md").read_text()
    headings = re.findall(r"^## .+$", template, flags=re.M)
    for record in records:
        assert f"]({record.name})" in index, f"{record.name} missing from the index"
        body = record.read_text()
        for heading in headings:
            assert heading in body, f"{record.name} lacks {heading}"
        assert re.search(r"^- Status: (proposed|accepted|superseded by \d{4})", body, flags=re.M), record.name
    numbers = [r.name[:4] for r in records]
    assert numbers == sorted(set(numbers)), "decision numbers must be unique"


def test_decision_0002_matches_the_defaults_it_describes() -> None:
    body = read("docs/decisions/0002-optional-integrations-default-off.md")
    for name in re.findall(r"`([a-z_]+)`", body.split("## Context")[0]):
        if name in DEFAULTS:
            assert DEFAULTS[name] in (False, [], ""), name


def test_merge_request_template_asks_for_decision_and_threat_model_review() -> None:
    template = read(".gitlab/merge_request_templates/Default.md")
    assert "docs/decisions" in template
    assert "docs/threat-model.md" in template


# --- threat model ----------------------------------------------------------


def test_threat_model_evidence_points_at_real_tests() -> None:
    text = read("docs/threat-model.md")
    refs = set(re.findall(r"`(tests/test_[a-z0-9_]+\.py)(?:::([A-Za-z0-9_]+))?`", text))
    assert refs
    for path, func in refs:
        file = ROOT / path
        assert file.is_file(), f"threat model cites missing {path}"
        if func:
            assert f"def {func}" in file.read_text(), f"{path} has no {func}"
    for path in re.findall(r"`(docs/evidence/[a-z0-9._-]+)`", text):
        assert (ROOT / path).is_file(), path


def test_threat_model_covers_the_required_areas_and_every_row_has_evidence() -> None:
    text = read("docs/threat-model.md")
    for area in ("Supply chain", "Privilege escalation", "Private CA", "Identity", "Network", "Rollback", "Data loss"):
        assert f"| {area} |" in text, area
    for heading in ("## Trust boundaries", "## Risks, controls and evidence", "## Revisiting the model"):
        assert heading in text, heading
    for boundary in ("Upstream release APIs", "Package managers", "Host-local secrets"):
        assert boundary in text, boundary
    table = text.split("## Risks, controls and evidence")[1].split("## What is out of scope")[0]
    rows = [line for line in table.splitlines() if line.startswith("| ") and not line.startswith("| Area") and "---" not in line]
    assert len(rows) >= 14
    for row in rows:
        cells = [c.strip() for c in row.strip("|").split("|")]
        assert len(cells) == 4, row
        evidence = cells[3]
        assert "tests/" in evidence or "CI job" in evidence or "none yet" in evidence or "Limitation" in evidence, row


# --- vulnerability response ------------------------------------------------


def test_vulnerability_runbook_defines_every_required_step() -> None:
    text = read("docs/vulnerability-response.md")
    for step in ("Acknowledge", "Triage", "Disclose", "Credit", "Emergency release", "Changelog and advisory linkage"):
        assert step in text, step
    assert "### Security" in text or "`### Security`" in text
    security = read("SECURITY.md")
    assert "docs/vulnerability-response.md" in security
    assert "docs/security-tabletop.md" in read("docs/vulnerability-response.md").replace("(security-tabletop.md)", "docs/security-tabletop.md")


def test_tabletop_uses_a_synthetic_scenario_and_a_record_template() -> None:
    text = read("docs/security-tabletop.md")
    assert "synthetic" in text.lower() and "no real vulnerability" in text.lower()
    assert (ROOT / "docs/evidence/tabletop-template.md").is_file()
    assert "example-lab-ca.crt" in text


def test_changelog_states_supported_versions_and_security_notes() -> None:
    changelog = read("CHANGELOG.md")
    assert "Security and supported versions" in changelog
    assert "advisory" in changelog.lower()


# --- project identity and links --------------------------------------------


def test_identity_page_agrees_with_collection_metadata() -> None:
    galaxy = yaml.safe_load(read("galaxy.yml"))
    page = read("docs/project-identity.md")
    for key in ("repository", "issues", "homepage", "documentation"):
        assert galaxy[key] in page, f"docs/project-identity.md does not list galaxy.yml {key}: {galaxy[key]}"
    advisory = galaxy["repository"] + "/security/advisories/new"
    assert advisory in page
    assert advisory in read("SECURITY.md")
    assert advisory in read(".github/ISSUE_TEMPLATE/config.yml")
    assert galaxy["version"] in page


def test_identity_states_mission_without_vendor_affiliation() -> None:
    readme = read("README.md").lower()
    assert "non-profit" in readme
    assert "does not require a curiosity account" in readme
    page = read("docs/project-identity.md").lower()
    assert "non-profit" in page
    assert "no endorsement by or affiliation with" in page
    assert "apache-2.0" in page
    assert "no curiosity account" in page
    assert "separate" in page and "private downstream" in page
    governance = " ".join(read("GOVERNANCE.md").lower().split())
    assert "nothing here implies endorsement by, or affiliation with" in governance


def test_relative_links_in_public_documents_resolve() -> None:
    documents = [ROOT / name for name in (
        "README.md", "CONTRIBUTING.md", "SECURITY.md", "SUPPORT.md", "CODE_OF_CONDUCT.md", "GOVERNANCE.md",
    )]
    documents += sorted((ROOT / "docs").rglob("*.md"))
    broken = []
    for doc in documents:
        for target in LINK.findall(doc.read_text()):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path = target.split("#")[0]
            if path and not (doc.parent / path).resolve().exists():
                broken.append(f"{doc.relative_to(ROOT)} -> {target}")
    assert not broken, broken


def test_top_level_documents_link_the_new_governance_pages() -> None:
    readme = read("README.md")
    for target in ("GOVERNANCE.md", "docs/project-identity.md", "docs/threat-model.md", "docs/vulnerability-response.md"):
        assert target in readme, target
    assert "GOVERNANCE.md" in read("CONTRIBUTING.md")


def test_release_checklist_checks_identity_and_security_notes() -> None:
    checklist = read("docs/release-checklist.md")
    assert "docs/project-identity.md" in checklist
    assert "### Security" in checklist or "Security" in checklist


# --- 1.0 contract draft ------------------------------------------------------


def test_contract_draft_lists_the_profiles_the_role_accepts() -> None:
    main = (ROLE / "tasks/main.yml").read_text()
    accepted = re.search(r"workstation_profile in \[([^\]]+)\]", main).group(1)
    profiles = re.findall(r"'([a-z-]+)'", accepted)
    draft = read("docs/stable-contract-1.0-draft.md")
    for profile in profiles:
        assert f"`{profile}`" in draft, profile
    assert "DRAFT" in draft
    assert "has not been reviewed" in draft


def test_contract_default_safety_variables_really_default_off() -> None:
    draft = read("docs/stable-contract-1.0-draft.md")
    block = draft.split("### Default-safety guarantees")[1].split("## Deprecation windows")[0]
    named = [n for n in re.findall(r"`([a-z_]+)`", block) if n in DEFAULTS]
    assert len(named) >= 8
    for name in named:
        assert DEFAULTS[name] in (False, [], ""), name
    assert "curiosity.workstation.update/v1" in draft
    assert "curiosity.workstation.update/v1" in (ROLE / "tasks/update-manifest.yml").read_text()


# --- generic infrastructure pattern ---------------------------------------


def test_lab_pull_example_uses_real_variables_and_no_private_values() -> None:
    path = ROOT / "examples/lab-pull-maintenance.yml"
    variables = yaml.safe_load(path.read_text())
    for name in variables:
        assert name in DEFAULTS, f"{name} is not a role variable"
    assert variables["workstation_profile"] in ("desktop", "thin-client", "byod-kiosk")
    assert variables["catalog_integrity_mode"] in ("off", "warn", "enforce")
    assert variables["workstation_update_ring"] in ("canary", "early", "general")
    assert "example.edu" in variables["ansible_pull_repo"]
    assert variables["failed_sweep_webhook_url"] == ""
    text = path.read_text().lower()
    for marker in ("developerdojo.org", "focuscell", "focuspass", "curiosity-corp/catalog-internal"):
        assert marker not in text


def test_infrastructure_note_names_internal_only_boundary_and_diagram() -> None:
    note = read("docs/infrastructure-patterns.md")
    assert "```mermaid" in note
    assert "## Internal-only components" in note
    assert "## Real deployments" in note
    assert "examples/lab-pull-maintenance.yml" in note
