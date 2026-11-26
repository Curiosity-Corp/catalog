"""Public OSS contract checks that do not require a managed host."""

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"


def test_required_public_files_exist() -> None:
    required = (
        "README.md",
        "LICENSE",
        "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md",
        "SECURITY.md",
        "SUPPORT.md",
        "CHANGELOG.md",
        "galaxy.yml",
        "docs/architecture.md",
        "docs/compatibility.md",
        "docs/configuration.md",
        "docs/release-checklist.md",
    )
    assert all((ROOT / path).is_file() for path in required)
    assert (ROLE / "meta/main.yml").is_file()


def test_collection_metadata_is_publishable() -> None:
    metadata = yaml.safe_load((ROOT / "galaxy.yml").read_text())
    assert metadata["namespace"] == "curiosity"
    assert metadata["name"] == "workstation"
    assert metadata["version"] == "0.1.0"
    assert "Apache-2.0" in metadata["license"]
    assert metadata["dependencies"]["community.general"]


def test_defaults_are_safe_for_unrelated_machines() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults["coder_cli_enabled"] is False
    assert defaults["cloud_clis_enabled"] is True
    assert defaults["keycloak_sso_enabled"] is False
    assert defaults["ziti_enabled"] is False
    assert defaults["sunshine_enabled"] is False
    assert defaults["profile_cleanup_enabled"] is False
    assert defaults["fleetd_enabled"] is False
    assert defaults["ziti_etc_hosts_pins"] == []
    assert defaults["ziti_intercept_domains"] == []
    assert defaults["custom_ca_certificates"] == []
    assert defaults["sunshine_default_pass"] == ""


def test_private_material_is_not_in_the_public_surface() -> None:
    source_paths = [ROOT / "roles", ROOT / "playbooks", ROOT / "renovate.json"]
    old_private_markers = (
        "developerdojo.org",
        "focusapiary",
        "focuscell.org",
        "focuspass.com",
        "focusbuzz.org",
        "dreamhive.org",
        "dreamdecks.org",
        "hardmagic.com",
        "hypersight.net",
        "slidee.net",
        "omlab-secrets",
        "harbor-internal-ca",
    )
    # The vendored StreamStack project (files/streamstack, a squashed subtree of its
    # release) is upstream CI, build and install code that names its own GitLab and
    # registry hosts. Those two host markers are allowed only inside that tree, until
    # StreamStack makes them configuration-only (tracked in the StreamStack project).
    # Every other file, every other marker, and every credential-like marker inside
    # the tree stays under the gate.
    vendored = ROLE / "files/streamstack"
    host_markers = {"developerdojo.org", "focuscell.org"}
    text = "\n".join(
        path.read_text(errors="replace")
        for root in source_paths
        for path in (root.rglob("*") if root.is_dir() else [root])
        if path.is_file() and vendored not in path.parents
    ).lower()
    for marker in old_private_markers:
        assert marker not in text
    vendored_text = "\n".join(
        path.read_text(errors="replace") for path in vendored.rglob("*") if path.is_file()
    ).lower()
    for marker in old_private_markers:
        if marker not in host_markers:
            assert marker not in vendored_text, f"{marker} in the vendored StreamStack tree"
    # No private certificates ship in the role. The one allowed exception is the
    # public CA that PIA publishes for its WireGuard API (catalog#145); it must
    # stay a single certificate with no key material next to it.
    public_vendor_cas = {"pia-ca.rsa.4096.crt"}
    certificates = {path.name for path in (ROLE / "files").rglob("*.crt")}
    assert certificates <= public_vendor_cas
    for name in certificates:
        pem = (ROLE / "files" / name).read_text()
        assert pem.count("BEGIN CERTIFICATE") == 1 and "PRIVATE KEY" not in pem
    assert 'CLIENT_ID = ""' in (ROLE / "files/teams.py").read_text()


def test_renovate_is_public_and_valid_json() -> None:
    config = json.loads((ROOT / "renovate.json").read_text())
    assert config["$schema"].startswith("https://docs.renovatebot.com/")
    assert "config:recommended" in config["extends"]


def test_ci_images_are_not_pulled_from_docker_hub() -> None:
    """Every CI image names its registry host (catalog#97).

    A bare ``python:3.13-slim`` resolves to Docker Hub, whose anonymous pull
    limit returns 429 to the shared runners.
    """
    ci = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())
    images = [ci["image"]] + [
        job["image"]
        for job in ci.values()
        if isinstance(job, dict) and isinstance(job.get("image"), str)
    ]
    for image in images:
        first = image.split("/", 1)[0]
        assert "/" in image and ("." in first or ":" in first), f"{image} has no registry host"


def test_ci_toolchain_is_locked() -> None:
    """CI images are digest-pinned and tooling installs come from the lock (catalog#22).

    An image whose tag is a CI variable (the research-profile matrix) cannot
    carry a digest; docs/ci-toolchain.md records that exception.
    """
    ci_text = (ROOT / ".gitlab-ci.yml").read_text()
    ci = yaml.safe_load(ci_text)
    images = [ci["image"]] + [
        job["image"]
        for job in ci.values()
        if isinstance(job, dict) and isinstance(job.get("image"), str)
    ]
    for image in images:
        assert "${" in image or "@sha256:" in image, f"{image} is not pinned by digest"
    assert "--require-hashes -r ci/requirements.txt" in ci_text
    assert "collection install -r ci/requirements.yml" in ci_text
    for line in ci_text.splitlines():
        if "pip install" in line and ".ci-cloud-init/src/requirements.txt" not in line:
            assert "ci/requirements.txt" in line, f"unlocked install: {line.strip()}"
    lock = (ROOT / "ci/requirements.txt").read_text()
    for line in (ROOT / "ci/requirements.in").read_text().splitlines():
        if line and not line.startswith("#"):
            assert line.lower() in lock.lower(), f"{line} is not in the lock"
    collections = yaml.safe_load((ROOT / "ci/requirements.yml").read_text())["collections"]
    for collection in collections:
        assert all(ch.isdigit() or ch == "." for ch in collection["version"]), collection
