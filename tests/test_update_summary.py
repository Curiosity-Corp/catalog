"""The maintenance summary helper: content, schema, redaction and wiring."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
SCRIPT = ROLE / "files/curiosity-update-summary"


def load_module():
    loader = importlib.machinery.SourceFileLoader("curiosity_update_summary", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


MOD = load_module()


def manifest(**overrides) -> dict:
    base = {
        "schema": "curiosity.workstation.update/v1",
        "run_id": "20260704T020000Z",
        "status": "success",
        "started_at": "2026-07-04T02:00:00Z",
        "finished_at": "2026-07-04T02:09:41Z",
        "host": "private-host.internal.example",
        "profile": "desktop",
        "update_ring": "general",
        "ring_blockers": [],
        "quarantine_active": False,
        "install_allowed": True,
        "catalog_commit": "0123456789abcdef0123456789abcdef01234567",
        "catalog_commit_verified": True,
        "catalog_signer_fingerprint": "AAAABBBBCCCCDDDDEEEEFFFF0000111122223333",
        "catalog_integrity_mode": "enforce",
        "catalog_trusted_fingerprint": "AAAABBBBCCCCDDDDEEEEFFFF0000111122223333",
        "application_contracts": {"sunshine": {"installer": "github-deb", "enabled_when": "sunshine_enabled"}},
        "upstream_releases": {
            "lazygit": {"repository": "jesseduffield/lazygit", "tag_name": "v0.50.0", "assets": ["a.tar.gz"]},
            "deno": {"repository": "denoland/deno", "tag_name": "v2.4.0", "assets": []},
        },
        "asset_checksums": {"https://example.invalid/a": "sha256:" + "0" * 64},
        "before": [
            {"path": "/usr/local/bin/lazygit", "exists": True, "checksum": "aaa"},
            {"path": "/usr/local/bin/deno", "exists": True, "checksum": "bbb"},
            {"path": "/home/alice/.local/bin/uv", "exists": False, "checksum": ""},
            {"path": "/usr/local/bin/old", "exists": True, "checksum": "ccc"},
        ],
        "after": [
            {"path": "/usr/local/bin/lazygit", "exists": True, "checksum": "a2"},
            {"path": "/usr/local/bin/deno", "exists": True, "checksum": "bbb"},
            {"path": "/home/alice/.local/bin/uv", "exists": True, "checksum": "ddd"},
            {"path": "/usr/local/bin/old", "exists": False, "checksum": ""},
        ],
        "healthcheck_failures": [],
        "healthcheck_warnings": ["sunshine"],
        "skipped_integrations": ["sunshine", "coder"],
        "failure": {},
        "package_inventory": {"dpkg": "secret-package\t1.0\n", "npm": "{}", "pipx": "{}"},
        "sbom": "/var/lib/curiosity/workstation-updates/sbom.cdx.json",
    }
    base.update(overrides)
    return base


def test_success_summary_reports_changes_checks_and_skips() -> None:
    summary = MOD.summarize(manifest())
    assert summary["schema"] == "curiosity.workstation.update-summary/v1"
    assert summary["status"] == "success"
    changes = {c["path"]: c["change"] for c in summary["tools"]["changed"]}
    assert changes == {
        "/usr/local/bin/lazygit": "updated",
        "~/.local/bin/uv": "added",
        "/usr/local/bin/old": "removed",
    }
    assert summary["tools"]["unchanged"] == 1
    assert summary["upstream_releases"] == {"deno": "v2.4.0", "lazygit": "v0.50.0"}
    assert summary["skipped_integrations"] == ["coder", "sunshine"]
    assert summary["health"] == {"failures": [], "warnings": ["sunshine"]}
    assert summary["rollback"]["performed"] is False
    assert summary["needs_attention"] == []
    assert summary["catalog"] == {"commit": "0123456789ab", "signature_verified": True, "integrity_mode": "enforce"}


def test_rolled_back_run_says_so_and_asks_for_attention() -> None:
    summary = MOD.summarize(
        manifest(
            status="rolled_back",
            healthcheck_failures=["lazygit"],
            failure={"task": "Install lazygit", "message": "checksum mismatch for lazygit", "recorded_at": "x"},
        )
    )
    assert summary["rollback"] == {
        "performed": True,
        "failed_task": "Install lazygit",
        "reason": "checksum mismatch for lazygit",
    }
    joined = " ".join(summary["needs_attention"])
    assert "Install lazygit" in joined and "restored" in joined and "lazygit" in joined


def test_held_run_lists_what_it_is_waiting_for() -> None:
    summary = MOD.summarize(manifest(status="held", install_allowed=False, ring_blockers=["deno", "bun"]))
    assert summary["install_allowed"] is False
    assert summary["ring_blockers"] == ["bun", "deno"]
    assert any("age past the ring delay: bun, deno" in item for item in summary["needs_attention"])


def test_unsigned_catalog_commit_is_flagged_by_mode() -> None:
    warn = MOD.summarize(manifest(catalog_commit_verified=False, catalog_integrity_mode="warn"))
    enforce = MOD.summarize(manifest(catalog_commit_verified=False, catalog_integrity_mode="enforce"))
    off = MOD.summarize(manifest(catalog_commit_verified=False, catalog_integrity_mode="off"))
    assert any("not verified" in item for item in warn["needs_attention"])
    assert any("not signed by the trusted key" in item for item in enforce["needs_attention"])
    assert off["needs_attention"] == []


def test_output_never_carries_private_or_secret_values() -> None:
    raw = manifest(
        status="rolled_back",
        failure={
            "task": "Download from https://deploy:hunter2@git.internal.example/x",
            "message": (
                "401 Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz0123456789 "
                "api_key=sk-live-123456 token: abcdef /home/alice/.config/x password=hunter2 "
                "AKIAABCDEFGHIJKLMNOP"
            ),
        },
    )
    summary = MOD.summarize(raw)
    for text in (json.dumps(summary), MOD.render_text(summary)):
        for forbidden in (
            "private-host.internal.example",
            "hunter2",
            "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
            "sk-live-123456",
            "abcdef /home",
            "/home/alice",
            "AKIAABCDEFGHIJKLMNOP",
            "secret-package",
            "AAAABBBBCCCCDDDDEEEEFFFF0000111122223333",
            "sha256:",
            "sbom.cdx.json",
        ):
            assert forbidden not in text, forbidden


def test_long_failure_messages_are_truncated() -> None:
    summary = MOD.summarize(manifest(status="failed", failure={"task": "t", "message": "x" * 5000}))
    assert max(len(item) for item in summary["needs_attention"]) < 450


def test_wrong_schema_is_rejected() -> None:
    try:
        MOD.summarize(manifest(schema="curiosity.workstation.update/v2"))
    except ValueError as exc:
        assert "unsupported manifest schema" in str(exc)
    else:
        raise AssertionError("a v2 manifest must not be summarised as v1")


def test_text_format_is_stable_and_short() -> None:
    text = MOD.render_text(MOD.summarize(manifest()))
    assert text.splitlines()[0] == "Workstation update 20260704T020000Z: success"
    assert "Tools changed (3):" in text
    assert "Optional integrations skipped: coder, sunshine" in text
    assert "Rollback: not needed" in text
    assert text.rstrip().endswith("Needs attention: nothing")
    assert len(text.splitlines()) < 30


def test_summary_fields_are_all_allow_listed_from_the_manifest() -> None:
    written = yaml.safe_load((ROLE / "tasks/update-manifest.yml").read_text())
    text = (ROLE / "tasks/update-manifest.yml").read_text()
    for key in MOD.MANIFEST_KEYS:
        assert f"'{key}'" in text, f"update-manifest.yml no longer writes {key}"
    assert written  # the task file parses
    assert "curiosity.workstation.update/v1" in text
    assert MOD.SOURCE_SCHEMA == "curiosity.workstation.update/v1"


def run_cli(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, **kwargs)


def test_cli_text_json_and_write_modes(tmp_path: Path) -> None:
    source = tmp_path / "last-run.json"
    source.write_text(json.dumps(manifest()))
    out = run_cli("--manifest", str(source))
    assert out.returncode == 0 and out.stdout.startswith("Workstation update ")
    as_json = run_cli("--manifest", str(source), "--format", "json")
    assert json.loads(as_json.stdout)["schema"] == "curiosity.workstation.update-summary/v1"

    target = tmp_path / "summary"
    written = run_cli("--manifest", str(source), "--summary-dir", str(target), "--write")
    assert written.returncode == 0 and written.stdout == ""
    for name in ("last-run-summary.txt", "last-run-summary.json"):
        mode = stat.S_IMODE((target / name).stat().st_mode)
        assert mode == 0o644, (name, oct(mode))
    assert not list(target.glob("*.tmp"))
    saved = json.loads((target / "last-run-summary.json").read_text())
    assert saved == json.loads(as_json.stdout)


def test_cli_exit_codes_for_missing_and_invalid_manifests(tmp_path: Path) -> None:
    missing = run_cli("--manifest", str(tmp_path / "nope.json"))
    assert missing.returncode == 2 and "no run manifest" in missing.stderr
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert run_cli("--manifest", str(bad)).returncode == 2
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps(manifest(schema="other/v1")))
    result = run_cli("--manifest", str(wrong))
    assert result.returncode == 2 and "unsupported manifest schema" in result.stderr


def test_unreadable_manifest_falls_back_to_the_saved_summary(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        return  # root can read mode 000 files, so the fallback cannot be exercised
    source = tmp_path / "last-run.json"
    source.write_text(json.dumps(manifest()))
    target = tmp_path / "summary"
    assert run_cli("--manifest", str(source), "--summary-dir", str(target), "--write").returncode == 0
    source.chmod(0)
    try:
        result = run_cli("--manifest", str(source), "--summary-dir", str(target))
        assert result.returncode == 0 and result.stdout.startswith("Workstation update ")
        empty = run_cli("--manifest", str(source), "--summary-dir", str(tmp_path / "none"))
        assert empty.returncode == 2
    finally:
        source.chmod(0o600)


def test_role_installs_and_runs_the_helper_without_failing_updates() -> None:
    tasks = yaml.safe_load((ROLE / "tasks/update-manifest.yml").read_text())
    by_name = {t["name"]: t for t in tasks}
    install = by_name["Install the maintenance summary helper"]
    assert install["ansible.builtin.copy"]["src"] == "curiosity-update-summary"
    assert install["ansible.builtin.copy"]["mode"] == "0755"
    assert install["failed_when"] is False
    run = by_name["Save the plain-language maintenance summary"]
    assert run["failed_when"] is False
    names = [t["name"] for t in tasks]
    assert names.index("Write the local workstation update manifest") < names.index("Save the plain-language maintenance summary")
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults["workstation_update_summary_dir"] == "/var/lib/curiosity/update-summary"
    assert SCRIPT.stat().st_mode & stat.S_IXUSR


def test_skipped_integrations_use_the_same_rule_as_the_health_check() -> None:
    manifest_tasks = (ROLE / "tasks/update-manifest.yml").read_text()
    health = (ROLE / "tasks/update-health.yml").read_text()
    rule = "lookup('vars', item.value.enabled_when, default=true)"
    assert rule in manifest_tasks and rule in health
    assert re.search(r"'skipped_integrations': workstation_update_skipped_integrations", manifest_tasks)


def test_skipped_integration_task_runs_as_written(tmp_path: Path) -> None:
    """Execute the role's own task text with synthetic contracts and flags."""
    tasks = yaml.safe_load((ROLE / "tasks/update-manifest.yml").read_text())
    task = next(t for t in tasks if t["name"] == "List optional integrations that are switched off on this host")
    out = tmp_path / "skipped.json"
    play = [
        {
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "vars": {
                "latest_release_contracts": {
                    "sunshine": {"enabled_when": "sunshine_enabled"},
                    "litra": {"enabled_when": "litra_cli_enabled"},
                    "aws": {"enabled_when": "cloud_clis_enabled"},
                    "undefined_flag": {"enabled_when": "flag_nobody_defined"},
                    "always": {},
                },
                "sunshine_enabled": False,
                "litra_cli_enabled": "false",
                "cloud_clis_enabled": True,
            },
            "tasks": [
                task,
                {
                    "ansible.builtin.copy": {
                        "dest": str(out),
                        "content": "{{ workstation_update_skipped_integrations | default([]) | to_json }}",
                    }
                },
            ],
        }
    ]
    playbook = tmp_path / "skipped.yml"
    playbook.write_text(yaml.safe_dump(play))
    env = {**os.environ, "ANSIBLE_LOCALHOST_WARNING": "False", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "False"}
    done = subprocess.run(
        ["ansible-playbook", "-i", "localhost,", str(playbook)], capture_output=True, text=True, env=env, cwd=ROOT
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert sorted(json.loads(out.read_text())) == ["litra", "sunshine"]
