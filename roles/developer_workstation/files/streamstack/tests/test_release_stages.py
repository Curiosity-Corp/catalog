"""docs/release.md's stage-to-job table against .gitlab-ci.yml (issue #102): a job added or moved must be documented."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")
DOC = (ROOT / "docs" / "release.md").read_text(encoding="utf-8")

# The stages of the launch-sequence python-package pipeline, in order, each of which the table must have a row for.
STAGES = [
    ".pre",
    "preflight",
    "source-security",
    "source-evidence",
    "supply-chain",
    "build",
    "supply-chain-vex",
    "supply-chain-evidence",
    "artifact-security",
    "policy-gate",
    "attest",
    "artifact-promote",
    "compliance",
    "release-create",
    "release-evidence",
    "pipeline-attestation",
]
# Jobs the template owns (they are not in .gitlab-ci.yml) that the table must keep naming, with their stage.
TEMPLATE_JOBS = {
    "offboarded-identity-guard": ".pre",
    "pipeline-canary": "preflight",
    "sast": "source-security",
    "source-evidence": "source-evidence",
    "lockfile-verify": "supply-chain",
    "python-build": "build",
    "python-sbom": "supply-chain-vex",
    "supply-chain-evidence": "supply-chain-evidence",
    "python-vuln-scan": "artifact-security",
    "python-license": "policy-gate",
    "python-sign": "attest",
    "python-publish": "artifact-promote",
    "defectdojo": "compliance",
    "release-evidence": "release-evidence",
    "pipeline-attestation": "pipeline-attestation",
}


def table() -> dict[str, str]:
    """{stage: the row's job cell and description} from the first table of the tag pipeline section."""
    rows: dict[str, str] = {}
    for line in DOC.splitlines():
        match = re.match(r"^\| ([a-z.-]+) \| (.+) \|$", line)
        if match and match.group(1) in STAGES:
            rows[match.group(1)] = match.group(2)
    return rows


def local_jobs() -> dict[str, str]:
    """{job: stage} for every job .gitlab-ci.yml defines with its own stage."""
    jobs: dict[str, str] = {}
    for match in re.finditer(r"^([a-z][a-z0-9-]*):\n((?:[ \t].*\n|\n)+)", CI, re.MULTILINE):
        stage = re.search(r"^  stage: ([a-z.-]+)$", match.group(2), re.MULTILINE)
        if stage:
            jobs[match.group(1)] = stage.group(1)
    return jobs


def test_every_stage_has_a_row() -> None:
    assert list(table()) == STAGES


def test_every_job_of_this_repository_is_in_its_stage_row() -> None:
    rows = table()
    for job, stage in local_jobs().items():
        assert f"`{job}`" in rows[stage] or f"`{job}` " in rows[stage], f"{job} is not listed under {stage}"


def test_the_template_jobs_stay_documented_in_their_stage() -> None:
    rows = table()
    for job, stage in TEMPLATE_JOBS.items():
        assert f"`{job}`" in rows[stage], f"{job} is not listed under {stage}"


def test_the_job_definitions_this_repository_adds_are_not_missing() -> None:
    jobs = local_jobs()
    for name in ("deb-build", "apt-index", "deb-sign", "apt-publish", "release-verify", "release-notes"):
        assert name in jobs, name
