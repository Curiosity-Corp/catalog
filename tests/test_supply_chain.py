"""Supply-chain controls for the collection's own build: pinned toolchain, vulnerability
audit gate, credential scanner and artifact verification (docs/supply-chain.md)."""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
TODAY = dt.date(2026, 11, 4)


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


audit = load("run_dependency_audit")
secrets = load("scan_secrets")
verify = load("verify_collection_artifact")
sbom_check = load("verify_sbom")

PIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[0-9][A-Za-z0-9.!+_-]*$")


# --- toolchain lock -------------------------------------------------------------

def lock_lines() -> list[str]:
    text = (ROOT / "ci/requirements.txt").read_text()
    return [line for line in text.splitlines() if line.strip() and not line.startswith("#")]


def test_every_ci_dependency_is_an_exact_pin():
    lines = lock_lines()
    assert len(lines) > 20, "the lock should hold the whole closure, not only the direct tools"
    assert [line for line in lines if not PIN.match(line)] == []
    names = [line.split("==")[0].lower().replace("_", "-") for line in lines]
    assert len(names) == len(set(names)), "a package is pinned twice"


def test_lock_covers_every_direct_tool():
    locked = {line.split("==")[0].lower().replace("_", "-") for line in lock_lines()}
    direct = {
        re.split(r"[<>=!~ ]", line.strip())[0].lower().replace("_", "-")
        for line in (ROOT / "ci/requirements.in").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert direct <= locked, f"not in the lock: {sorted(direct - locked)}"
    assert {"ansible-core", "ansible-lint", "yamllint", "pytest", "pip-audit"} <= direct


def test_ci_collections_are_exact_pins():
    document = yaml.safe_load((ROOT / "ci/requirements.yml").read_text())
    for item in document["collections"]:
        assert re.fullmatch(r"\d+\.\d+\.\d+", str(item["version"])), f"{item['name']} is not an exact pin"


def test_pipeline_installs_only_from_the_lock():
    ci = (ROOT / ".gitlab-ci.yml").read_text()
    assert "pip install -r ci/requirements.txt" in ci
    assert "collection install -r ci/requirements.yml" in ci
    assert not re.search(r"pip install (?!-r )\S", ci), "an unpinned pip install crept back into the pipeline"
    assert not re.search(r"install --upgrade pip", ci)


def test_ci_image_is_pinned_by_digest():
    image = next(line for line in (ROOT / ".gitlab-ci.yml").read_text().splitlines() if line.startswith("image:"))
    assert re.fullmatch(r"image: [\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}", image), image


def test_renovate_covers_lock_collections_and_image_digest():
    config = json.loads((ROOT / "renovate.json").read_text())
    rules = config["packageRules"]
    assert any(rule.get("pinDigests") and "docker" in rule.get("matchDatasources", []) for rule in rules)
    assert any("pip_requirements" in rule.get("matchManagers", []) for rule in rules)
    assert any("ansible-galaxy" in rule.get("matchManagers", []) for rule in rules)
    assert config.get("vulnerabilityAlerts", {}).get("labels")
    # Renovate's default managers find these files only under these names.
    assert (ROOT / "ci/requirements.txt").is_file() and (ROOT / "ci/requirements.yml").is_file()


def test_pipeline_has_supply_chain_jobs():
    ci = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())
    for job in ("supply_chain", "dependency_audit", "secret_scan", "artifact_verify", "provenance"):
        assert job in ci, f"{job} job is missing"
    assert ci["dependency_audit"]["allow_failure"] == {"exit_codes": 3}
    assert "tags" not in str(ci["secret_scan"].get("rules", "")), "secret_scan must run on tag pipelines too"
    assert "evidence/" in ci["provenance"]["artifacts"]["paths"]


# --- vulnerability audit gate ---------------------------------------------------

def report(*vulns: tuple[str, str, str]) -> dict:
    dependencies = [{"name": "alpha", "version": "1.0", "vulns": []}, {"name": "beta", "version": "2.0", "vulns": []}]
    for package, version, advisory in vulns:
        dependencies.append({"name": package, "version": version, "vulns": [{"id": advisory, "fix_versions": ["9.9"]}]})
    return {"dependencies": dependencies}


def run_audit(tmp_path, document, exceptions=None, monkeypatch=None, capsys=None) -> tuple[int, str]:
    saved = tmp_path / "report.json"
    saved.write_text(json.dumps(document))
    exception_file = tmp_path / "exceptions.yml"
    exception_file.write_text(yaml.safe_dump({"exceptions": exceptions or []}))
    argv = ["run_dependency_audit", "--from-json", str(saved), "--exceptions", str(exception_file), "--today", TODAY.isoformat()]
    monkeypatch.setattr(sys, "argv", argv)
    code = audit.main()
    return code, capsys.readouterr().out


def exception(**overrides) -> dict:
    base = {"id": "PYSEC-1", "package": "gamma", "reason": "not reachable", "expires": "2026-12-01", "approved_by": "maintainer"}
    base.update(overrides)
    return base


def test_audit_passes_when_nothing_is_vulnerable(tmp_path, monkeypatch, capsys):
    code, out = run_audit(tmp_path, report(), monkeypatch=monkeypatch, capsys=capsys)
    assert code == 0 and "no unexcepted vulnerabilities" in out


def test_audit_fails_on_an_unexcepted_finding(tmp_path, monkeypatch, capsys):
    code, out = run_audit(tmp_path, report(("gamma", "3.0", "PYSEC-1")), monkeypatch=monkeypatch, capsys=capsys)
    assert code == 1
    assert "gamma 3.0 has PYSEC-1; fixed in 9.9" in out


def test_audit_accepts_an_unexpired_exception(tmp_path, monkeypatch, capsys):
    code, out = run_audit(tmp_path, report(("gamma", "3.0", "PYSEC-1")), [exception()], monkeypatch, capsys)
    assert code == 0 and "excepted  gamma 3.0 PYSEC-1" in out


def test_audit_fails_when_an_exception_has_expired(tmp_path, monkeypatch, capsys):
    code, out = run_audit(tmp_path, report(("gamma", "3.0", "PYSEC-1")), [exception(expires="2026-11-03")], monkeypatch, capsys)
    assert code == 1 and "expired on 2026-11-03" in out and "has PYSEC-1" in out


def test_audit_rejects_a_long_lived_or_incomplete_exception(tmp_path, monkeypatch, capsys):
    code, out = run_audit(tmp_path, report(), [exception(expires="2027-06-01")], monkeypatch, capsys)
    assert code == 1 and "more than 90 days" in out
    code, out = run_audit(tmp_path, report(), [exception(reason="")], monkeypatch, capsys)
    assert code == 1 and "missing reason" in out
    code, out = run_audit(tmp_path, report(), [exception(approved_by="")], monkeypatch, capsys)
    assert code == 1 and "missing approved_by" in out


def test_audit_reports_but_tolerates_an_unused_exception(tmp_path, monkeypatch, capsys):
    code, out = run_audit(tmp_path, report(), [exception()], monkeypatch, capsys)
    assert code == 0 and "matches no current finding" in out


def test_audit_exits_3_when_the_database_is_unreachable(monkeypatch, capsys):
    monkeypatch.setattr(audit, "run_pip_audit", lambda lock: None)
    monkeypatch.setattr(sys, "argv", ["run_dependency_audit"])
    assert audit.main() == audit.NETWORK_ERROR == 3
    assert "nothing was checked" in capsys.readouterr().out


def test_committed_exceptions_file_is_valid():
    active, problems = audit.load_exceptions(ROOT / "ci/vulnerability-exceptions.yml", dt.date.today())
    assert problems == [], "an exception in ci/vulnerability-exceptions.yml expired or is malformed"


# --- credential scanner ---------------------------------------------------------
# Secret-shaped strings are assembled at run time so this file holds none itself.

def fake(prefix: str, body: str) -> str:
    return prefix + body


@pytest.mark.parametrize(
    "line, rule",
    [
        ("-----BEGIN " + "RSA PRIVATE KEY-----", "private-key-block"),
        ("token = " + fake("ghp_", "a1B2" * 9), "github-token"),
        ("token: " + fake("glpat-", "abcdefghij0123456789"), "gitlab-token"),
        ("id = " + fake("AKIA", "ABCDEFGHIJKLMNOP"), "aws-access-key-id"),
        ("hook " + fake("xoxb-", "123456789012-abcdefghijkl"), "slack-token"),
        ("url = postgres://admin:" + "s3cretvalue" + "@db.internal", "password-in-url"),
    ],
)
def test_scanner_flags_credential_shapes(line, rule):
    assert [found[1] for found in secrets.scan_text(line)] == [rule]


@pytest.mark.parametrize(
    "line",
    [
        "url: https://github.com/example/repo.git",
        "dsn = postgres://user:{{ db_password }}@db",
        "dsn = postgres://user:$DB_PASSWORD@db",
        "see scheme://user:pass@host in the docs",
        "export TOKEN=<your-token-here>",
        "token = " + fake("ghp_", "x" * 36) + "  # secret-scan: allow",
        "plain prose about a private key and passwords",
    ],
)
def test_scanner_ignores_placeholders_and_prose(line):
    assert secrets.scan_text(line) == []


def test_scanner_output_never_contains_the_value(tmp_path, capsys):
    value = fake("ghp_", "q9Z8" * 9)
    sample = tmp_path / "leak.txt"
    sample.write_text(f"token={value}\n")
    assert secrets.main([str(sample)]) == 1
    out = capsys.readouterr().out
    assert "leak.txt:1: github-token" in out and value not in out


def test_tracked_files_hold_no_credentials():
    assert subprocess.run([sys.executable, str(ROOT / "scripts/scan_secrets.py")], cwd=ROOT).returncode == 0


# --- artifact verification ------------------------------------------------------

def test_artifact_contents_allow_the_real_layout():
    names = ["FILES.json", "MANIFEST.json", "README.md", "roles/x/tasks/main.yml", "roles/x/files/streamstack/tests/t.py", "docs/a.md"]
    assert verify.check_contents(names) == []


@pytest.mark.parametrize(
    "name",
    [
        "tests/test_x.py",
        "ci/requirements.txt",
        "scripts/build.py",
        ".gitlab-ci.yml",
        "renovate.json",
        "Makefile",
        "roles/x/.env",
        "roles/x/files/id_ed25519",
        "roles/x/__pycache__/m.pyc",
        "roles/x/.ansible/cache",
        "docs/evidence/run.json",
        "collection.cdx.json",
        "stray.txt",
        "other/dir/file",
    ],
)
def test_artifact_contents_reject_plumbing_secrets_and_strays(name):
    assert verify.check_contents([name]), f"{name} should be rejected"


def make_archive(files: dict[str, bytes], listing: dict[str, str] | None = None) -> tarfile.TarFile:
    buffer = io.BytesIO()
    entries = []
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            entries.append({"name": name, "ftype": "file", "chksum_sha256": (listing or {}).get(name, verify.sha256_bytes(data))})
        body = json.dumps({"files": entries}).encode()
        info = tarfile.TarInfo("FILES.json")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    buffer.seek(0)
    return tarfile.open(fileobj=buffer, mode="r:gz")


def test_files_json_accepts_a_consistent_archive():
    assert verify.check_files_json(make_archive({"README.md": b"hello"})) == []


def test_files_json_detects_a_tampered_file():
    archive = make_archive({"README.md": b"hello"}, listing={"README.md": "0" * 64})
    assert any("does not match its FILES.json digest" in problem for problem in verify.check_files_json(archive))


def test_files_json_detects_an_embedded_private_key():
    archive = make_archive({"roles/x/files/k.txt": b"-----BEGIN " + b"OPENSSH PRIVATE KEY-----\nabc"})
    assert any("private key block" in problem for problem in verify.check_files_json(archive))


def galaxy_manifest(**info) -> dict:
    base = {"namespace": "curiosity", "name": "workstation", "version": "1.2.3", "license": ["Apache-2.0"], "dependencies": {"community.general": ">=9.0.0"}}
    base.update(info)
    return base


def metadata_archive(manifest: dict) -> tarfile.TarFile:
    role_meta = yaml.safe_dump(
        {"galaxy_info": {"author": "a", "description": "d", "license": "l", "min_ansible_version": "2.15", "platforms": [{"name": "Ubuntu"}]}}
    ).encode()
    return make_archive({"MANIFEST.json": json.dumps({"collection_info": manifest}).encode(), "roles/x/meta/main.yml": role_meta})


CHANGELOG = "## [Unreleased]\n\n## [1.2.3] - 2026-11-04\n"


def test_metadata_accepts_a_matching_release():
    assert verify.check_metadata(metadata_archive(galaxy_manifest()), galaxy_manifest(), CHANGELOG, release=True) == []


def test_metadata_detects_version_drift_and_missing_changelog_section():
    problems = verify.check_metadata(metadata_archive(galaxy_manifest(version="1.2.4")), galaxy_manifest(), CHANGELOG, release=True)
    assert any("version" in problem and "differs" in problem for problem in problems)
    problems = verify.check_metadata(metadata_archive(galaxy_manifest()), galaxy_manifest(), "## [Unreleased]\n", release=True)
    assert any("no section for 1.2.3" in problem for problem in problems)
    problems = verify.check_metadata(metadata_archive(galaxy_manifest()), galaxy_manifest(), "## [Unreleased]\n## [1.2.3] - planned\n", release=True)
    assert any("still marks" in problem for problem in problems)


def test_metadata_detects_an_unconstrained_dependency():
    manifest = galaxy_manifest(dependencies={"community.general": "*"})
    problems = verify.check_metadata(metadata_archive(manifest), galaxy_manifest(dependencies={"community.general": "*"}), CHANGELOG, release=False)
    assert any("no version constraint" in problem for problem in problems)


def test_published_source_tree_passes_its_own_metadata_rules():
    galaxy = yaml.safe_load((ROOT / "galaxy.yml").read_text())
    assert re.fullmatch(r"\d+\.\d+\.\d+", galaxy["version"])
    assert {".yamllint", "ci", "renovate.json"} <= set(galaxy["build_ignore"])
    assert (ROOT / "CHANGELOG.md").read_text().count("## [Unreleased]") == 1


def test_sbom_verifier_rejects_a_hollow_document():
    problems = sbom_check.verify({"bomFormat": "CycloneDX", "specVersion": "1.5", "components": []}, None)
    assert any("metadata.component.name" in problem for problem in problems)
    for needed in sbom_check.REQUIRED_CATEGORIES:
        assert any(needed in problem for problem in problems)
