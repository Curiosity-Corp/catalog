"""The package and apt repository jobs in .gitlab-ci.yml (issues #99, #101): static checks, no CI needed."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CI = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")


def job(name: str) -> str:
    """The text of one top-level job."""
    match = re.search(rf"^{re.escape(name)}:\n((?:[ \t].*\n|\n)+)", CI, re.MULTILINE)
    assert match, f"{name} is not a job in .gitlab-ci.yml"
    return match.group(1)


def test_the_package_jobs_exist_in_the_stages_the_pipeline_documents() -> None:
    stages = {
        "deb-build": "build",
        "apt-index": "supply-chain-vex",
        "deb-sign": "attest",
        "apt-publish": "artifact-promote",
    }
    for name, stage in stages.items():
        assert f"stage: {stage}" in job(name), name


def test_the_deb_build_job_runs_the_apt_tests_and_refuses_a_silent_skip() -> None:
    text = job("deb-build")
    assert 'REQUIRE_APT_TOOLS: "1"' in text
    assert "tests/test_apt_repo.py" in text
    assert "build_deb.py" in text and "deb_provenance.py" in text


def test_signing_runs_on_the_dispatch_runner_and_is_required_on_tags_only() -> None:
    text = job("deb-sign")
    assert "tags: [ci-dispatch]" in text
    tag_rule = text.index("$CI_COMMIT_TAG")
    main_rule = text.index("$CI_DEFAULT_BRANCH")
    assert "allow_failure" not in text[tag_rule:main_rule]  # a tag without its signatures must fail
    assert "allow_failure: true" in text[main_rule:]


def test_publishing_is_for_tags_and_waits_for_the_signatures() -> None:
    text = job("apt-publish")
    assert "- if: $CI_COMMIT_TAG" in text
    assert "job: deb-sign" in text
    assert text.index("apt_repo.py verify") < text.index("apt_repo.py publish")  # verified before anything is uploaded


def test_the_release_waits_for_the_apt_repository() -> None:
    assert "job: apt-publish" in job("release")


def test_the_signing_key_fingerprint_in_ci_is_the_committed_public_key() -> None:
    declared = re.fullmatch(r"([0-9A-F]{40})\n?", (ROOT / "packaging" / "apt-public.fingerprint").read_text())
    assert declared
    if shutil.which("gpg") is None:
        pytest.skip("gpg not installed")
    shown = subprocess.run(
        ["gpg", "--show-keys", "--with-colons", str(ROOT / "packaging" / "apt-public.asc")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    fingerprints = [line.split(":")[9] for line in shown.splitlines() if line.startswith("fpr")]
    assert fingerprints[0] == declared.group(1)


def test_the_committed_signing_material_is_public_only() -> None:
    assert "PRIVATE KEY" not in (ROOT / "packaging" / "apt-public.asc").read_text(encoding="utf-8")
    assert "PRIVATE KEY" not in (ROOT / "packaging" / "signing-cert.pem").read_text(encoding="utf-8")


@pytest.mark.parametrize("script", ["deb-sign.sh", "deb-sign-pod.sh"])
def test_the_signing_scripts_are_valid_posix_shell(script: str) -> None:
    done = subprocess.run(
        ["sh", "-n", str(ROOT / "scripts" / "ci" / script)], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr


def test_the_signing_scripts_never_print_secret_material() -> None:
    pod = (ROOT / "scripts" / "ci" / "deb-sign-pod.sh").read_text(encoding="utf-8")
    assert "set -x" not in pod
    assert not re.search(r"echo[^\n]*(CLIENT_SECRET|TOKEN|apt-key)", pod)
    dispatcher = (ROOT / "scripts" / "ci" / "deb-sign.sh").read_text(encoding="utf-8")
    assert "set -x" not in dispatcher


def test_no_job_pipes_dpkg_deb_into_head() -> None:
    """`dpkg-deb -c | head` closes the pipe early; under pipefail dpkg-deb dies of SIGPIPE (pipeline 96708)."""
    assert not re.search(r"dpkg-deb[^\n]*\|\s*head", CI)
