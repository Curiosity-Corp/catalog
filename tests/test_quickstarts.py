"""The researcher quickstarts and their example files stay true to the role.

Every example variable must be a real role variable, every documented tag set
must preview with `ansible-playbook --list-tasks`, links and anchors in the
guides must resolve, and the cloud-init and Jupyter examples must keep their
security posture (key-only SSH, loopback-only notebook server).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
DOCS = ROOT / "docs/quickstarts"
EXAMPLES = ROOT / "examples"

PRIVATE_MARKERS = (
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

EXAMPLE_VARS = (
    "laptop.yml",
    "research-vm.yml",
    "thin-client.yml",
    "shared-lab.yml",
)

# Each preview is (example vars file, guide, regex that finds the --tags value
# in the guide, tags used when the guide documents a command with a fixed list).
PREVIEWS = (
    ("laptop.yml", "laptop.md", r"TAGS=(\S+)"),
    ("research-vm.yml", "research-vm.md", r"TAGS=(\S+)"),
    ("thin-client.yml", "thin-client-and-shared-lab.md", r"examples/thin-client.yml --tags (\S+)"),
    ("shared-lab.yml", "thin-client-and-shared-lab.md", r"examples/shared-lab.yml --tags (\S+)"),
)


def _defaults() -> dict:
    merged: dict = {}
    for role in ("developer_workstation", "workstation_storage"):
        merged.update(yaml.safe_load((ROOT / "roles" / role / "defaults/main.yml").read_text()))
    return merged


DEFAULTS = _defaults()


def _slug(heading: str) -> str:
    text = heading.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    in_fence = False
    anchors = set()
    for line in path.read_text().splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        elif not in_fence and line.startswith("#"):
            anchors.add(_slug(line.lstrip("#")))
    return anchors


@pytest.mark.parametrize("name", EXAMPLE_VARS)
def test_example_variables_are_real_role_variables(name: str) -> None:
    data = yaml.safe_load((EXAMPLES / name).read_text())
    unknown = sorted(set(data) - set(DEFAULTS))
    assert not unknown, f"{name} sets variables the role does not define: {unknown}"
    assert data["workstation_profile"] in ("desktop", "workspace", "thin-client", "byod-kiosk")
    assert data["hardware_profile"] in DEFAULTS["hardware_profiles"]
    assert data["dev_user"] in {u["name"] for u in data.get("dev_users", [{"name": data["dev_user"]}])}
    assert data["dev_user_home"] == f"/home/{data['dev_user']}"


@pytest.mark.parametrize("name", EXAMPLE_VARS)
def test_examples_keep_private_integrations_off(name: str) -> None:
    data = yaml.safe_load((EXAMPLES / name).read_text())
    for key in ("keycloak_sso_enabled", "ziti_enabled", "sunshine_enabled", "coder_cli_enabled"):
        assert data.get(key, False) is False, f"{name}: {key} must stay false in a public example"
    for ca in data.get("custom_ca_certificates", []):
        assert ca["src"].startswith("/etc/ansible/private/"), "certificate paths stay outside the repository"


def test_guides_and_examples_name_no_private_service() -> None:
    files = [p for p in list(DOCS.glob("*.md")) + list(EXAMPLES.rglob("*")) if p.is_file()]
    assert files
    for path in files:
        text = path.read_text().lower()
        for marker in PRIVATE_MARKERS:
            assert marker not in text, f"{marker} in {path.relative_to(ROOT)}"


def test_relative_links_and_anchors_resolve() -> None:
    link = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
    checked = 0
    for guide in sorted(DOCS.glob("*.md")):
        in_fence = False
        for line in guide.read_text().splitlines():
            if line.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            for target in link.findall(line):
                if re.match(r"^[a-z]+:", target):
                    continue
                path_part, _, anchor = target.partition("#")
                dest = (guide.parent / path_part).resolve() if path_part else guide
                assert dest.exists(), f"{guide.name}: broken link {target}"
                if anchor and dest.suffix == ".md":
                    assert anchor in _anchors(dest), f"{guide.name}: no heading for #{anchor} in {dest.name}"
                checked += 1
    assert checked > 20


def test_every_guide_is_listed_in_the_index() -> None:
    index = (DOCS / "README.md").read_text()
    for guide in DOCS.glob("*.md"):
        if guide.name != "README.md":
            assert f"]({guide.name})" in index, f"{guide.name} missing from docs/quickstarts/README.md"


def test_cloud_init_user_data_is_key_only_and_does_not_apply_the_role() -> None:
    raw = (EXAMPLES / "cloud-init/user-data").read_text()
    assert raw.splitlines()[0] == "#cloud-config"
    data = yaml.safe_load(raw)
    assert data["ssh_pwauth"] is False
    assert data["disable_root"] is True
    for user in data["users"]:
        assert user["lock_passwd"] is True
        assert "passwd" not in user and "plain_text_passwd" not in user
        assert all("EXAMPLE" in key for key in user["ssh_authorized_keys"]), "keys in the example stay placeholders"
    runcmd = " ".join(" ".join(c) if isinstance(c, list) else c for c in data["runcmd"])
    assert "ansible-playbook" not in runcmd, "the example prepares the VM; applying the role is a manual step"
    assert "--branch" in runcmd, "the example pins a tag instead of a moving branch"
    assert "chrony" in data["packages"]
    local_vars = next(f for f in data["write_files"] if f["path"] == "/etc/ansible/local-vars.yml")
    unknown = sorted(set(yaml.safe_load(local_vars["content"])) - set(DEFAULTS))
    assert not unknown, unknown
    meta = yaml.safe_load((EXAMPLES / "cloud-init/meta-data").read_text())
    assert meta["instance-id"] and meta["local-hostname"]


def test_notebook_unit_binds_loopback_and_keeps_authentication() -> None:
    unit = (EXAMPLES / "notebooks/jupyter-loopback.service").read_text()
    exec_start = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    assert "--ServerApp.ip=127.0.0.1" in exec_start
    for forbidden in ("0.0.0.0", "--ip=*", "token=''", 'token=""', "password=''", "allow_origin", "--allow-root"):
        assert forbidden not in exec_start
    assert "--no-browser" in exec_start
    assert "Restart=on-failure" in unit
    assert "UMask=0077" in unit


needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")


def _playbook(*args: str, extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, ANSIBLE_ROLES_PATH=str(ROOT / "roles"), ANSIBLE_NOCOLOR="1", ANSIBLE_FORCE_COLOR="0")
    env.pop("ANSIBLE_STDOUT_CALLBACK", None)
    return subprocess.run(
        ["ansible-playbook", "-i", "localhost,", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )


@needs_ansible
@pytest.mark.parametrize(("vars_file", "guide", "pattern"), PREVIEWS)
def test_documented_tag_sets_preview_with_the_example_variables(vars_file: str, guide: str, pattern: str) -> None:
    text = (DOCS / guide).read_text()
    tags = re.findall(pattern, text)
    assert tags, f"{guide} no longer documents a tag set for {vars_file}"
    result = _playbook(
        "-c",
        "local",
        "playbooks/site.yml",
        "-e",
        f"@examples/{vars_file}",
        "--tags",
        tags[0],
        "--list-tasks",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    listed = result.stdout
    assert "tasks:" in listed
    if vars_file == "laptop.yml":
        assert "Include APT package installation (desktop)" in listed
        assert "Include ansible-pull timer" not in listed
        assert "Run workstation software update transaction" in listed
    if vars_file == "research-vm.yml":
        assert "Include APT package installation (workspace)" in listed
        assert "Include ansible-pull timer" not in listed
    if vars_file == "thin-client.yml":
        assert "Include firewall (UFW)" in listed
        assert "Include keyboard layout enforcement" in listed
    if vars_file == "shared-lab.yml":
        assert "Include /tmp hygiene" in listed
        assert "Include profile cleanup timer" in listed


@needs_ansible
def test_restricted_network_playbook_parses_and_exports_proxy_environment() -> None:
    play = yaml.safe_load((EXAMPLES / "restricted-network-site.yml").read_text())[0]
    assert play["environment"] == {
        "http_proxy": "{{ lab_http_proxy }}",
        "https_proxy": "{{ lab_https_proxy }}",
        "no_proxy": "{{ lab_no_proxy }}",
    }
    site = yaml.safe_load((ROOT / "playbooks/site.yml").read_text())[0]
    assert play["roles"] == site["roles"], "keep the proxy wrapper in step with playbooks/site.yml"
    result = _playbook(
        "--syntax-check",
        "examples/restricted-network-site.yml",
        "-e",
        "lab_http_proxy=http://proxy.example.edu:3128 lab_https_proxy=http://proxy.example.edu:3128 lab_no_proxy=localhost",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_update_ring_policy_matches_the_role_defaults() -> None:
    doc = (ROOT / "docs/update-rings.md").read_text()
    assert DEFAULTS["workstation_update_ring"] == "canary"
    assert "The default is `canary`" in doc
    ages = DEFAULTS["workstation_update_ring_min_age_seconds"]
    delays = DEFAULTS["workstation_update_ring_randomized_delay_seconds"]
    assert ages == {"canary": 0, "early": 21600, "general": 86400}
    assert delays == {"canary": 900, "early": 21600, "general": 86400}
    for ring, age_h in (("canary", "0 h"), ("early", "6 h"), ("general", "24 h")):
        row = next(line for line in doc.splitlines() if line.startswith(f"| `{ring}` |"))
        assert age_h in row, row
    assert f"`workstation_update_quarantine_seconds` ({DEFAULTS['workstation_update_quarantine_seconds']})" in doc
    override = yaml.safe_load((EXAMPLES / "update-ring-early.yml").read_text())
    assert set(override) <= set(DEFAULTS)
    assert override["workstation_update_ring"] in ages


def test_update_gate_still_covers_every_source_and_the_apt_upgrade() -> None:
    """The doc says one young release holds the whole transaction, APT included."""
    ring = (ROLE / "tasks/update-ring.yml").read_text()
    assert "latest_github_releases | dict2items" in ring
    assert "workstation_update_ring_blockers" in ring
    managed = (ROLE / "tasks/managed-updates.yml").read_text()
    first_include = managed.split("- name: Include Coder CLI installation")[0]
    assert "system-updates.yml" in first_include
    assert "workstation_update_install_allowed" in first_include
