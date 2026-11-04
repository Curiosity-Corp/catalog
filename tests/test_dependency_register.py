"""The dependency register (docs/dependency-register.yml) must describe what the role installs."""

from __future__ import annotations

import copy
import datetime as dt
import importlib.util
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TODAY = dt.date(2026, 11, 4)


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sources = load("external_sources")
register_check = load("check_dependency_register")
REGISTER = yaml.safe_load((ROOT / "docs/dependency-register.yml").read_text())
SCAN = sources.scan_tree(ROOT)


# --- the scanner -----------------------------------------------------------------

def scan(text: str) -> list[dict]:
    return sources.scan_tasks("roles/r/tasks/t.yml", text)


def test_scan_finds_apt_repositories_and_downloads():
    rows = scan(
        """
- name: add repo
  ansible.builtin.apt_repository:
    repo: "deb [signed-by=/k.gpg] https://packages.example-vendor.test/apt stable main"
- name: fetch tool
  ansible.builtin.get_url:
    url: https://downloads.vendor.test/tool.tar.gz
    dest: /tmp/tool.tar.gz
"""
    )
    kinds = {(row["host"], row["kind"]) for row in rows}
    assert ("packages.example-vendor.test", "apt-source") in kinds
    assert ("downloads.vendor.test", "download") in kinds


def test_scan_flags_pipe_to_shell_and_installer_scripts():
    rows = scan(
        """
- name: install
  ansible.builtin.shell: curl -fsSL https://get.vendor.test/install | sh
- name: script
  ansible.builtin.get_url:
    url: https://vendor.test/install.sh
    dest: /tmp/install.sh
"""
    )
    by_host = {row["host"]: row for row in rows}
    assert by_host["get.vendor.test"]["pipes_to_shell"] is True
    assert by_host["get.vendor.test"]["kind"] == "installer-script"
    assert by_host["vendor.test"]["kind"] == "installer-script"
    assert by_host["vendor.test"]["pipes_to_shell"] is False


def test_scan_descends_into_blocks_and_ignores_templated_hosts():
    rows = scan(
        """
- block:
    - ansible.builtin.git:
        repo: https://github.com/org/project.git
        dest: /opt/project
  rescue:
    - ansible.builtin.uri:
        url: "https://{{ mirror }}/x"
"""
    )
    assert [(row["host"], row["kind"]) for row in rows] == [("github.com", "source-clone")]


def test_scan_ignores_placeholder_hosts_and_non_fetching_tasks():
    assert scan("- ansible.builtin.debug:\n    msg: see https://real.vendor.test\n") == []
    assert scan("- ansible.builtin.get_url:\n    url: https://example.org/x\n    dest: /x\n") == []


def test_scan_reads_contracts_from_defaults():
    parsed = sources.scan_defaults(
        """
latest_github_repositories:
  tool: org/tool
latest_release_contracts:
  tool: {installer: github-archive, verification: github-asset-digest}
workstation_application_contracts:
  other: {installer: apt, verification: apt-signature}
"""
    )
    assert parsed["github_releases"] == [{"name": "tool", "repository": "org/tool", "installer": "github-archive", "verification": "github-asset-digest"}]
    assert parsed["other_contracts"][0]["name"] == "other"


def test_scan_of_the_real_role_finds_the_expected_surface():
    names = {item["name"] for item in SCAN["github_releases"]}
    assert {"lazygit", "cosign", "sops"} <= names
    hosts = {item["host"] for item in SCAN["hosts"]}
    assert {"github.com", "pypi.org", "packages.microsoft.com"} <= hosts


# --- the register against the real role ------------------------------------------

def run_check(register: dict, **kwargs) -> tuple[list[str], list[str]]:
    return register_check.check(register, SCAN, TODAY, **kwargs)


def test_committed_register_matches_the_role():
    errors, _ = run_check(REGISTER)
    assert errors == [], "docs/dependency-register.yml is out of step with the role: " + "; ".join(errors)


def test_every_tool_and_source_has_the_required_fields():
    installer_types = REGISTER["installer_types"]
    contracts = {item["name"]: item["installer"] for item in SCAN["github_releases"] + SCAN["other_contracts"]}
    for name, entry in REGISTER["tools"].items():
        record = register_check.effective(entry, installer_types, contracts[name])
        missing = [field for field in register_check.REQUIRED if not str(record.get(field, "")).strip()]
        assert missing == [], f"tools.{name} lacks {missing}"
    for host, entry in REGISTER["sources"].items():
        missing = [field for field in register_check.REQUIRED if not str(entry.get(field, "")).strip()]
        assert missing == [], f"sources.{host} lacks {missing}"


def mutated(change) -> dict:
    register = copy.deepcopy(REGISTER)
    change(register)
    return register


def test_a_tool_without_a_row_is_an_error():
    errors, _ = run_check(mutated(lambda r: r["tools"].pop("lazygit")))
    assert any("lazygit is installed by the role but has no register entry" in error for error in errors)


def test_a_row_for_a_removed_tool_is_an_error():
    def change(register):
        register["tools"]["ghost"] = copy.deepcopy(register["tools"]["lazygit"])

    errors, _ = run_check(mutated(change))
    assert any("ghost has a register entry but the role no longer installs it" in error for error in errors)


def test_a_new_host_without_a_row_is_an_error():
    errors, _ = run_check(mutated(lambda r: r["sources"].pop("pypi.org")))
    assert any("pypi.org is used by the role but has no register entry" in error for error in errors)


def test_missing_owner_or_license_is_an_error():
    errors, _ = run_check(mutated(lambda r: r["tools"]["lazygit"].pop("owner")))
    assert any("tools: lazygit is missing owner" in error for error in errors)
    errors, _ = run_check(mutated(lambda r: r["sources"]["go.dev"].update(license="")))
    assert any("sources: go.dev is missing license" in error for error in errors)


def test_disagreeing_verification_is_an_error():
    errors, _ = run_check(mutated(lambda r: r["tools"]["lazygit"].update(verification="none")))
    assert any("lists verification 'none' but the role uses" in error for error in errors)


def test_an_abandoned_tool_still_installed_is_an_error():
    errors, _ = run_check(mutated(lambda r: r["tools"]["lazygit"].update(status="abandoned")))
    assert any("lazygit is abandoned upstream but still installed" in error for error in errors)


def test_at_risk_and_watch_entries_alert_but_do_not_fail():
    def change(register):
        register["tools"]["lazygit"].update(status="at-risk", note="maintainer stepped down")

    errors, alerts = run_check(mutated(change))
    assert errors == []
    assert any("lazygit is marked at-risk: maintainer stepped down" in alert for alert in alerts)


def test_security_critical_without_verification_is_an_error():
    def change(register):
        register["sources"]["go.dev"].update(criticality="security-critical", verification="none")

    errors, _ = run_check(mutated(change))
    assert any("go.dev is security-critical and has no verification method" in error for error in errors)


def test_an_apt_repository_needs_signature_verification():
    host = next(item["host"] for item in SCAN["hosts"] if "apt-source" in item["kinds"])
    errors, _ = run_check(mutated(lambda r: r["sources"][host].update(verification="TLS only")))
    assert any(f"{host} is an apt repository" in error for error in errors)


def test_stale_reviews_fail_only_when_a_limit_is_given():
    old = mutated(lambda r: r["tools"]["lazygit"].update(last_reviewed="2026-01-01"))
    assert run_check(old)[0] == []
    errors, _ = run_check(old, max_age_days=45)
    assert any("lazygit was last reviewed" in error and "limit 45" in error for error in errors)


def test_review_date_in_the_future_is_an_error():
    errors, _ = run_check(mutated(lambda r: r["tools"]["lazygit"].update(last_reviewed="2027-01-01")))
    assert any("last_reviewed is in the future" in error for error in errors)


def test_archived_upstream_is_an_error_unless_already_recorded():
    lookup = lambda repository: repository == "jesseduffield/lazygit"  # noqa: E731
    errors, _ = run_check(REGISTER, archived_lookup=lookup)
    assert any("lazygit upstream jesseduffield/lazygit is archived but the register says active" in error for error in errors)

    recorded = mutated(lambda r: r["tools"]["lazygit"].update(status="at-risk", note="archived"))
    errors, alerts = run_check(recorded, archived_lookup=lookup)
    assert errors == [] and any("already recorded as at-risk" in alert for alert in alerts)


def test_network_failure_is_reported_not_hidden():
    errors, alerts = run_check(REGISTER, archived_lookup=lambda repository: None)
    assert errors == []
    assert any("could not ask GitHub" in alert for alert in alerts)


def test_the_recorded_archived_upstreams_are_flagged_for_a_decision():
    flagged = {name for name, entry in REGISTER["tools"].items() if entry.get("status") in {"at-risk", "abandoned"}}
    assert {"filebrowser", "talhelper"} <= flagged, "archived upstreams must stay visible until the role drops them"
