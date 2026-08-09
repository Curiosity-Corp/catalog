"""Public OSS contract checks that do not require a managed host."""

import json
import re
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
    assert defaults["cloud_clis_enabled"] is False
    assert defaults["keycloak_sso_enabled"] is False
    assert defaults["ziti_enabled"] is False
    assert defaults["sunshine_enabled"] is False
    assert defaults["profile_cleanup_enabled"] is False
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
    text = "\n".join(
        path.read_text(errors="replace")
        for root in source_paths
        for path in (root.rglob("*") if root.is_dir() else [root])
        if path.is_file()
    ).lower()
    for marker in old_private_markers:
        assert marker not in text
    assert not list((ROLE / "files").rglob("*.crt"))
    assert 'CLIENT_ID = ""' in (ROLE / "files/teams.py").read_text()


def test_renovate_is_public_and_valid_json() -> None:
    config = json.loads((ROOT / "renovate.json").read_text())
    assert config["$schema"].startswith("https://docs.renovatebot.com/")
    assert "config:recommended" in config["extends"]


def test_mongodb_channel_discovery_covers_desktop_and_workspace_profiles() -> None:
    """Keep the dynamic MongoDB repository lookup valid for both profiles."""

    listing = """
    <Prefix>apt/ubuntu/dists/noble/mongodb-org/</Prefix>
    <Prefix>apt/ubuntu/dists/noble/mongodb-org/8.0/</Prefix>
    <Prefix>apt/ubuntu/dists/noble/mongodb-org/8.2/</Prefix>
    <Prefix>apt/ubuntu/dists/noble/mongodb-org/development/</Prefix>
    """
    channels = re.findall(
        r"<Prefix>apt/ubuntu/dists/noble/mongodb-org/([0-9]+\.[0-9]+)/</Prefix>",
        listing,
    )
    assert sorted(channels) == ["8.0", "8.2"]

    expected_lookup = (
        "regex_findall('<Prefix>apt/ubuntu/dists/' ~ apt_fallback_release"
        " ~ '/mongodb-org/([0-9]+\\\\.[0-9]+)/</Prefix>')"
    )
    for filename in ("repos.yml", "repos-workspace.yml"):
        text = (ROLE / "tasks" / filename).read_text()
        assert expected_lookup in text
        assert "status_code: 200" in text
        assert "retries: 3" in text
        assert 'latest_mongodb_key_version: "{{ latest_mongodb_channel.split' in text
        assert "https://pgp.mongodb.com/server-{{ latest_mongodb_key_version }}.asc" in text
