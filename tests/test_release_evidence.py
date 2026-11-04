"""Checksums, toolchain record and unsigned provenance that travel with the artifact."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


evidence = load("release_evidence")
COMMIT = "0123456789abcdef0123456789abcdef01234567"
CI_ENV = {
    "CI_COMMIT_SHA": COMMIT,
    "CI_COMMIT_REF_NAME": "main",
    "CI_PROJECT_URL": "https://git.example.test/group/catalog",
    "CI_PIPELINE_ID": "42",
    "CI_JOB_URL": "https://git.example.test/group/catalog/-/jobs/7",
    "CI_RUNNER_DESCRIPTION": "runner-1",
}


@pytest.fixture
def built(tmp_path, monkeypatch):
    for key, value in CI_ENV.items():
        monkeypatch.setenv(key, value)
    archive = tmp_path / "curiosity-workstation-1.2.3.tar.gz"
    archive.write_bytes(b"archive bytes")
    sbom = tmp_path / "collection.cdx.json"
    sbom.write_text("{}")
    out = tmp_path / "evidence"
    assert evidence.main(["build", str(archive), str(sbom), str(out), "--root", str(ROOT)]) == 0
    shutil.copy(archive, out)
    shutil.copy(sbom, out)
    return out


def problems(directory: Path, commit: str = COMMIT, fingerprint: str = "") -> list[str]:
    return evidence.verify_dir(directory, commit, fingerprint)


def test_build_then_verify_agree(built):
    assert problems(built) == []
    statement = json.loads((built / "provenance.intoto.json").read_text())
    assert statement["_type"] == evidence.STATEMENT_TYPE
    assert statement["predicateType"] == evidence.PREDICATE_TYPE
    assert statement["predicate"]["signed"] is False
    assert {subject["name"] for subject in statement["subject"]} == {"curiosity-workstation-1.2.3.tar.gz", "collection.cdx.json"}


def test_provenance_records_commit_pipeline_and_lock_digests(built):
    statement = json.loads((built / "provenance.intoto.json").read_text())
    resolved = statement["predicate"]["buildDefinition"]["resolvedDependencies"]
    assert resolved[0]["digest"] == {"gitCommit": COMMIT}
    assert any(item.get("name") == "ci/requirements.txt" and len(item["digest"]["sha256"]) == 64 for item in resolved)
    assert statement["predicate"]["runDetails"]["metadata"]["pipelineId"] == "42"
    toolchain = json.loads((built / "toolchain.json").read_text())
    assert toolchain["locks"]["ci/requirements.txt"] and toolchain["python"]


def test_a_modified_artifact_is_detected(built):
    (built / "curiosity-workstation-1.2.3.tar.gz").write_bytes(b"swapped")
    found = problems(built)
    assert any("does not match SHA256SUMS" in problem for problem in found)
    assert any("modified after the build" in problem for problem in found)


def test_a_modified_sbom_is_detected(built):
    (built / "collection.cdx.json").write_text('{"tampered": true}')
    assert any("collection.cdx.json" in problem for problem in problems(built))


def test_a_missing_file_or_statement_is_detected(built):
    (built / "provenance.intoto.json").unlink()
    assert "provenance.intoto.json is missing" in problems(built)
    (built / "SHA256SUMS").unlink()
    assert problems(built) == ["SHA256SUMS is missing"]


def test_wrong_commit_is_detected(built):
    assert any("does not name commit" in problem for problem in problems(built, commit="f" * 40))


def test_provenance_without_a_commit_is_rejected(tmp_path, monkeypatch):
    for key in CI_ENV:
        monkeypatch.delenv(key, raising=False)
    archive = tmp_path / "x-1.0.0.tar.gz"
    archive.write_bytes(b"a")
    sbom = tmp_path / "s.json"
    sbom.write_text("{}")
    out = tmp_path / "evidence"
    evidence.main(["build", str(archive), str(sbom), str(out), "--root", str(ROOT)])
    shutil.copy(archive, out)
    shutil.copy(sbom, out)
    assert "provenance does not record a source commit" in problems(out, commit="")


def test_a_malformed_sums_line_is_reported(built):
    (built / "SHA256SUMS").write_text("not a checksum line\n")
    assert any("malformed SHA256SUMS line" in problem for problem in problems(built))


def test_a_fingerprint_without_a_signature_fails_verification(built):
    assert "a fingerprint was given but no .asc/.sig signature is present" in problems(built, fingerprint="A" * 40)


def test_sha256sum_can_check_the_sums_file(built):
    result = subprocess.run(["sha256sum", "--check", "SHA256SUMS"], cwd=built, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("gpg") is None, reason="gpg is not installed")
def test_signature_by_the_expected_key_is_accepted_and_others_rejected(built, tmp_path):
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    env = {"GNUPGHOME": str(home), "PATH": "/usr/bin:/bin:/usr/local/bin"}
    batch = "%no-protection\nKey-Type: EdDSA\nKey-Curve: ed25519\nName-Real: Test Signer\nName-Email: signer@example.test\nExpire-Date: 0\n%commit\n"
    generated = subprocess.run(["gpg", "--batch", "--gen-key"], input=batch, env=env, capture_output=True, text=True, check=False)
    if generated.returncode != 0:
        pytest.skip("gpg could not create a throwaway key here")
    listing = subprocess.run(["gpg", "--batch", "--with-colons", "--list-keys"], env=env, capture_output=True, text=True, check=True).stdout
    fingerprint = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))
    target = built / "SHA256SUMS"
    subprocess.run(["gpg", "--batch", "--armor", "--detach-sign", str(target)], env=env, check=True, capture_output=True)

    import os

    saved = dict(os.environ)
    os.environ.update(env)
    try:
        assert problems(built, fingerprint=fingerprint) == []
        wrong = problems(built, fingerprint="B" * 40)
        assert any("not by the expected key" in problem for problem in wrong)
        target.write_text(target.read_text() + "\n")
        assert any("no valid signature" in problem for problem in problems(built, fingerprint=fingerprint))
    finally:
        os.environ.clear()
        os.environ.update(saved)
