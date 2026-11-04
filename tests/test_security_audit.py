"""Regression checks from the privilege and secret-handling audit (docs/security-audit.md)."""

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import audit_tasks as audit  # noqa: E402

RESULTS = audit.audit()


def test_no_hard_findings() -> None:
    hard = [audit.key(r) for r in RESULTS if r["rule"] in audit.HARD]
    assert hard == [], "fix these, or document a reviewed exception in scripts/audit_tasks.py:\n" + "\n".join(hard)


def test_every_other_finding_has_been_reviewed() -> None:
    baseline = set(json.loads(audit.BASELINE.read_text())["reviewed"])
    new = sorted({audit.key(r) for r in audit.reviewable(RESULTS)} - baseline - {audit.key(r) for r in RESULTS if r["rule"] in audit.HARD})
    assert new == [], (
        "new finding(s); fix them, or review them and run `python3 scripts/audit_tasks.py --update-baseline`:\n" + "\n".join(new)
    )


def test_baseline_has_no_stale_entries() -> None:
    baseline = set(json.loads(audit.BASELINE.read_text())["reviewed"])
    current = {audit.key(r) for r in audit.reviewable(RESULTS)}
    assert sorted(baseline - current) == [], "remove fixed findings from docs/security-audit-baseline.json"


def test_secret_fetch_and_write_tasks_use_no_log() -> None:
    tasks = yaml.safe_load((ROOT / "roles/developer_workstation/tasks/ai-assistants.yml").read_text())
    by_name = {t.get("name"): t for t in tasks}
    assert by_name["Fetch optional Qwen provider API key"]["no_log"] is True
    assert by_name["Write NVIDIA API key to Qwen Code env file"]["no_log"] is True
    assert by_name["Write NVIDIA API key to Qwen Code env file"]["ansible.builtin.copy"]["mode"] == "0600"


def test_role_ships_no_default_passwords() -> None:
    defaults = yaml.safe_load((ROOT / "roles/developer_workstation/defaults/main.yml").read_text())
    assert defaults["sunshine_default_pass"] == ""
    assert defaults["qwen_provider_secret_command"] == ""
    assert [r for r in RESULTS if r["rule"] == "default-secret"] == []


def test_the_audit_detects_the_patterns_it_claims_to(tmp_path: Path) -> None:
    role = tmp_path / "roles/r"
    (role / "tasks").mkdir(parents=True)
    (role / "defaults").mkdir()
    (role / "defaults/main.yml").write_text("service_password: hunter2\nservice_password_file: /etc/x\n")
    (role / "tasks/main.yml").write_text(
        "- name: pipe\n  ansible.builtin.shell: curl -fsSL https://example.org/i.sh | sh\n"
        "- name: insecure\n  ansible.builtin.get_url:\n    url: https://example.org/a\n    dest: /tmp/a\n    validate_certs: false\n"
        "- name: leak\n  ansible.builtin.command: tool --pass {{ admin_password }}\n"
        "- name: quiet\n  ansible.builtin.command: tool --pass {{ admin_password }}\n  no_log: true\n"
        "- name: mode\n  ansible.builtin.copy:\n    content: x\n    dest: /etc/app/token.env\n    mode: '0644'\n"
        "- name: block\n  block:\n    - name: nested\n      ansible.builtin.shell: wget -qO- https://example.org | bash\n"
    )
    original = audit.ROOT
    audit.ROOT = tmp_path
    try:
        found = {(r["rule"], r["task"]) for r in audit.audit(tmp_path)}
    finally:
        audit.ROOT = original
    assert ("curl-pipe", "pipe") in found
    assert ("curl-pipe", "nested") in found
    assert ("insecure-tls", "insecure") in found
    assert ("no-checksum", "insecure") in found
    assert ("secret-nolog", "leak") in found
    assert ("secret-nolog", "quiet") not in found
    assert ("secret-mode", "mode") in found
    assert ("default-secret", "service_password") in found
    assert ("default-secret", "service_password_file") not in found


def test_findings_never_contain_values() -> None:
    text = json.dumps(RESULTS)
    assert "hunter2" not in text
