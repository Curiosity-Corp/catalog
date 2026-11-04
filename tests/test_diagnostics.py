"""Redacted diagnostic bundle and health re-check (catalog #18, #25, #41)."""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
import re
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "roles/developer_workstation/files/curiosity_diagnostics.py"


def load():
    spec = importlib.util.spec_from_file_location("curiosity_diagnostics", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["curiosity_diagnostics"] = module
    spec.loader.exec_module(module)
    return module


diag = load()

# Secret-shaped values are assembled at run time so the repository holds no fixture
# that the secret scanners would flag.
GITHUB_TOKEN = "gh" + "p_" + "a1B2c3D4e5F6g7H8i9J0" * 2
GITLAB_TOKEN = "gl" + "pat-" + "x1y2z3w4v5u6t7s8r9q0"
AWS_KEY = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
JWT = "ey" + "JhbGciOiJIUzI1NiJ9." + "ey" + "JzdWIiOiIxMjM0NTY3ODkwIn0." + "c2lnbmF0dXJlLXZhbHVl"
PEM_BODY = "MIIBVgIBADANBgkqhkiG9w0BAQEFAASCAUAwggE8AgEAAkEA"
CERT = "-----BEGIN " + "CERTIFICATE-----\n" + PEM_BODY + "\n-----END " + "CERTIFICATE-----"
KEY = "-----BEGIN " + "PRIVATE KEY-----\n" + PEM_BODY + "\n-----END " + "PRIVATE KEY-----"
PASSWORD = "hunter2-correct-horse"


@pytest.fixture
def redactor():
    return diag.Redactor(users={"alice", "researcher42"}, hosts={"lab-node-7", "lab-node-7.corp.example-lab.org"}, salt=b"fixed-salt")


# --- redaction (#25): hostnames, usernames, IPs, tokens, certificates -------------

def test_hostnames_are_hashed_and_stay_correlatable(redactor):
    out = redactor.text("failed on lab-node-7 and lab-node-7.corp.example-lab.org, retried on lab-node-7; fetched github.com")
    assert "lab-node-7" not in out and "example-lab.org" not in out
    first, second = re.findall(r"host-[0-9a-f]{8}", out)[:2]
    assert out.count(first) == 2, "the same host must get the same tag within a bundle"
    assert "github.com" in out, "well known public hosts are kept so a report stays useful"


def test_unknown_internal_hostnames_are_hashed_too(redactor):
    out = redactor.text("pulling from build01.dept.university.edu and nas.home")
    assert "university.edu" not in out and "nas.home" not in out


def test_usernames_and_home_paths_are_removed(redactor):
    out = redactor.text("/home/alice/.local/bin/tool owned by alice, run by researcher42; also /home/someoneelse/data")
    assert "alice" not in out and "researcher42" not in out and "someoneelse" not in out
    assert "/home/~/.local/bin/tool" in out
    assert out.count("user-") == 2


def test_username_inside_a_longer_word_is_left_alone(redactor):
    assert redactor.text("malice and alicexyz") == "malice and alicexyz"


def test_ip_addresses_are_hashed_but_loopback_is_kept(redactor):
    out = redactor.text("peer 10.20.30.40 and 8.8.8.8, v6 2001:db8:85a3::8a2e:370:7334, local 127.0.0.1")
    for value in ("10.20.30.40", "8.8.8.8", "2001:db8:85a3", "7334"):
        assert value not in out
    assert "127.0.0.1" in out
    assert out.count("ip-") == 3


def test_versions_that_look_like_addresses_survive(redactor):
    assert redactor.text("tool 1.2.3 and 10.4 and 1.2.3.4.5") == "tool 1.2.3 and 10.4 and 1.2.3.4.5"


def test_tokens_and_keys_are_replaced(redactor):
    text = f"gh={GITHUB_TOKEN} gl={GITLAB_TOKEN} aws={AWS_KEY} jwt={JWT} Authorization: Bearer abcdef0123456789abcdef"
    out = redactor.text(text)
    for secret in (GITHUB_TOKEN, GITLAB_TOKEN, AWS_KEY, JWT, "abcdef0123456789abcdef"):
        assert secret not in out
    assert "[REDACTED:" in out


def test_keyed_secrets_and_url_passwords_are_replaced(redactor):
    out = redactor.text(f'password={PASSWORD} api_key: "{PASSWORD}" https://user:{PASSWORD}@git.example.test/x.git')
    assert PASSWORD not in out
    assert "password=[REDACTED:secret]" in out and "https://[REDACTED:url-credentials]@" in out


def test_certificate_and_private_key_blocks_are_replaced(redactor):
    out = redactor.text(f"trust:\n{CERT}\nkey:\n{KEY}\nend")
    assert PEM_BODY not in out and "BEGIN" not in out
    assert "[REDACTED:certificate]" in out and "[REDACTED:private-key]" in out


def test_email_addresses_are_hashed(redactor):
    out = redactor.text("contact alice.smith@lab.example-lab.org")
    assert "alice.smith" not in out and "email-" in out


def test_tags_are_not_linkable_across_bundles():
    a = diag.Redactor(salt=b"one").text("host.corp.example-lab.org")
    b = diag.Redactor(salt=b"two").text("host.corp.example-lab.org")
    assert a != b


def test_walk_redacts_keys_and_nested_values_and_counts(redactor):
    data = {"host lab-node-7": ["alice", {"token": f"token={GITHUB_TOKEN}"}], "n": 3}
    out = redactor.walk(data)
    assert "lab-node-7" not in json.dumps(out) and "alice" not in json.dumps(out) and GITHUB_TOKEN not in json.dumps(out)
    assert out["n"] == 3
    assert redactor.counts["host"] >= 1 and redactor.counts["user"] >= 1


# --- the bundle: collected, never collected (#25, #41) -----------------------------

def manifest(tmp_path: Path, tool: Path, **overrides) -> Path:
    digest = hashlib.sha256(tool.read_bytes()).hexdigest()
    data = {
        "schema": "curiosity.workstation.update/v1",
        "run_id": "1793000000-abc",
        "status": "success",
        "started_at": "2026-11-01T10:00:00Z",
        "finished_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host": "lab-node-7.corp.example-lab.org",
        "profile": "desktop",
        "update_ring": "stable",
        "quarantine_active": False,
        "install_allowed": True,
        "catalog_commit": "0123456789abcdef0123456789abcdef01234567",
        "catalog_commit_verified": False,
        "catalog_integrity_mode": "warn",
        "application_contracts": {"tool": {"healthcheck": [str(tool), "--version"], "services": ["tool.service"]}},
        "upstream_releases": {"tool": {"tag_name": "v1.0.0"}},
        "before": [{"path": str(tool), "exists": True, "checksum": "0" * 64}],
        "after": [{"path": str(tool), "exists": True, "checksum": digest}],
        "healthcheck_failures": [],
        "healthcheck_warnings": [],
        "failure": {},
        "package_inventory": {"dpkg": f"secret-package\t1.0 owner alice {GITHUB_TOKEN}", "npm": "", "pipx": ""},
        "sbom": "/var/lib/curiosity/workstation-updates/sbom.cdx.json",
    }
    data.update(overrides)
    path = tmp_path / "last-run.json"
    path.write_text(json.dumps(data))
    return path


@pytest.fixture
def tool(tmp_path):
    path = tmp_path / "bin" / "tool"
    path.parent.mkdir()
    path.write_text("#!/bin/sh\necho 'tool 1.0.0'\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def fake_systemctl(tmp_path, monkeypatch):
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    script = stubs / "systemctl"
    script.write_text('#!/bin/sh\ncase "$2" in tool.service) echo active; exit 0;; broken.service) echo failed; exit 3;; esac\necho inactive; exit 3\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stubs}:{os.environ['PATH']}")


def bundle_for(path: Path, **kwargs):
    return diag.build_bundle(path, kwargs.get("profile", ""), kwargs.get("tags", []), kwargs.get("services", []), kwargs.get("users", []))[0]


def test_a_uid_with_no_username_does_not_crash(monkeypatch):
    def nobody():
        raise OSError("No username set in the environment")

    monkeypatch.setattr(diag.getpass, "getuser", nobody)
    assert isinstance(diag.known_users(["alice"]), set)
    assert "alice" in diag.known_users(["alice"])


def test_bundle_has_the_requested_fields(tmp_path, tool, fake_systemctl):
    bundle = bundle_for(manifest(tmp_path, tool), tags=["packages", "update"])
    assert bundle["schema"] == diag.SCHEMA
    env = bundle["environment"]
    assert env["architecture"] and env["python"] and env["profile"] == "desktop" and env["tags"] == ["packages", "update"]
    assert set(env["ansible"]) == {"installed", "version_line"}
    update = bundle["update"]
    assert update["status"] == "success" and update["catalog_commit"].startswith("0123456789")
    assert update["upstream_release_tags"] == {"tool": "v1.0.0"}
    assert bundle["summary"] == []
    assert bundle["redaction"]["note"]


def test_nothing_identifying_or_secret_reaches_the_rendered_bundle(tmp_path, tool, fake_systemctl, monkeypatch):
    monkeypatch.setattr(diag.getpass, "getuser", lambda: "researcher42")
    monkeypatch.setattr(diag.socket, "gethostname", lambda: "lab-node-7")
    path = manifest(tmp_path, tool, failure={"message": f"download from 10.1.2.3 failed for alice@lab.example-lab.org token={GITHUB_TOKEN} {CERT}"})
    text = diag.render(bundle_for(path, users=["alice"]))
    for leaked in ("researcher42", "lab-node-7", "example-lab.org", "10.1.2.3", "alice", GITHUB_TOKEN, PEM_BODY, "secret-package"):
        assert leaked not in text, f"{leaked!r} leaked"


def test_package_inventories_and_contract_environment_are_never_collected(tmp_path, tool, fake_systemctl):
    text = diag.render(bundle_for(manifest(tmp_path, tool)))
    for key in ("package_inventory", "application_contracts", "asset_checksums", "dpkg", "npm", "pipx"):
        assert key not in text
    assert set(diag.POLICY) == {"collected", "hashed", "redacted", "never_collected"}


def test_a_missing_manifest_still_yields_a_useful_bundle(tmp_path):
    bundle = bundle_for(tmp_path / "absent.json")
    assert "no update manifest" in bundle["update"]["error"]
    assert bundle["checks"]["commands"] == [] and bundle["checks"]["disk"]


def test_a_corrupt_manifest_is_reported_without_its_content(tmp_path):
    path = tmp_path / "last-run.json"
    path.write_text("{not json " + GITHUB_TOKEN)
    text = diag.render(bundle_for(path))
    assert "unreadable" in text and GITHUB_TOKEN not in text


# --- health re-check (#18): missing binary, failed service, stale artifact ---------

def test_a_missing_binary_is_reported(tmp_path, tool, fake_systemctl):
    path = manifest(tmp_path, tool)
    tool.unlink()
    bundle = bundle_for(path)
    command = bundle["checks"]["commands"][0]
    assert command["state"] == "missing" and command["present"] is False
    assert any(line.startswith("command tool: missing") for line in bundle["summary"])
    assert any(line.startswith("artifact") and "missing" in line for line in bundle["summary"])


def test_a_failing_version_command_is_reported(tmp_path, tool, fake_systemctl):
    path = manifest(tmp_path, tool)
    tool.write_text("#!/bin/sh\necho broken >&2\nexit 1\n")
    bundle = bundle_for(path)
    assert bundle["checks"]["commands"][0]["state"] == "failed"


def test_a_failed_service_is_reported(tmp_path, tool, fake_systemctl):
    bundle = bundle_for(manifest(tmp_path, tool), services=["broken.service"])
    states = {row["unit"]: row["state"] for row in bundle["checks"]["services"]}
    assert states == {"tool.service": "active", "broken.service": "failed"}
    assert "service broken.service: failed" in bundle["summary"]


def test_a_stale_artifact_is_reported(tmp_path, tool, fake_systemctl):
    path = manifest(tmp_path, tool)
    tool.write_text("#!/bin/sh\necho 'tool 0.9.0 (manually replaced)'\n")
    bundle = bundle_for(path)
    states = [row["state"] for row in bundle["checks"]["artifacts"] if row["path"].endswith("/bin/tool")]
    assert states == ["changed-since-last-run"]
    assert any("changed-since-last-run" in line for line in bundle["summary"])


def test_an_old_manifest_is_reported_stale(tmp_path, tool, fake_systemctl):
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    bundle = bundle_for(manifest(tmp_path, tool, finished_at=old))
    assert any(row["state"] == "stale" and row["age_days"] >= 40 for row in bundle["checks"]["artifacts"])


def test_low_disk_is_reported(monkeypatch):
    usage = type("U", (), {"total": 100 * 1024**3, "free": 1 * 1024**3})
    monkeypatch.setattr(diag.shutil, "disk_usage", lambda path: usage)
    rows = diag.check_disk(["/"])
    assert rows[0]["low"] is True and rows[0]["free_percent"] == 1.0


def test_failed_update_and_failed_checks_are_summarised(tmp_path, tool, fake_systemctl):
    path = manifest(tmp_path, tool, status="failed", healthcheck_failures=["tool"], failure={"message": "rolled back"})
    summary = bundle_for(path)["summary"]
    assert "last update: failed" in summary and "health check failed in last update: tool" in summary


def test_healthchecks_with_other_arguments_are_not_executed(tmp_path):
    marker = tmp_path / "ran"
    script = tmp_path / "danger"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n")
    script.chmod(0o755)
    row = diag.check_command("danger", [str(script), "--delete-everything"])
    assert row["state"] == "present" and not marker.exists()


def test_commands_are_bounded_by_a_timeout(tmp_path, monkeypatch):
    slow = tmp_path / "slow"
    slow.write_text("#!/bin/sh\nsleep 30\n")
    slow.chmod(0o755)
    monkeypatch.setattr(diag, "TIMEOUT", 0.5)
    assert diag.run([str(slow)]) == (124, "")


# --- commands: preview, write, delete, opt-in ---------------------------------------

def run_cli(args, tmp_path, tool, stdin=None):
    argv = ["--manifest", str(manifest(tmp_path, tool)), "--output-dir", str(tmp_path / "out"), *args]
    return diag.main(argv, stdin=stdin)


class Tty(io.StringIO):
    def isatty(self):
        return True


def test_preview_writes_nothing(tmp_path, tool, fake_systemctl, capsys):
    assert run_cli(["preview"], tmp_path, tool) == 0
    assert not (tmp_path / "out").exists()
    assert json.loads(capsys.readouterr().out)["schema"] == diag.SCHEMA


def test_write_refuses_without_a_terminal_or_yes(tmp_path, tool, fake_systemctl):
    assert run_cli(["write"], tmp_path, tool, stdin=io.StringIO("y\n")) == 2
    assert not (tmp_path / "out").exists()


def test_write_asks_and_a_no_writes_nothing(tmp_path, tool, fake_systemctl):
    assert run_cli(["write"], tmp_path, tool, stdin=Tty("n\n")) == 1
    assert not list((tmp_path / "out").glob("*")) if (tmp_path / "out").exists() else True


def test_confirmed_write_is_private(tmp_path, tool, fake_systemctl):
    assert run_cli(["write"], tmp_path, tool, stdin=Tty("y\n")) == 0
    [bundle] = list((tmp_path / "out").glob("diagnostics-*.json"))
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "out").stat().st_mode) == 0o700
    assert json.loads(bundle.read_text())["schema"] == diag.SCHEMA


def test_delete_removes_only_bundles_written_by_the_tool(tmp_path, tool, fake_systemctl):
    assert run_cli(["write", "--yes"], tmp_path, tool) == 0
    out = tmp_path / "out"
    keep = out / "notes.txt"
    keep.write_text("mine")
    outside = tmp_path / "diagnostics-elsewhere.json"
    outside.write_text("{}")
    with pytest.raises(SystemExit):
        diag.main(["--output-dir", str(out), "delete"])
    with pytest.raises(SystemExit):
        diag.main(["--output-dir", str(out), "delete", str(outside)])
    assert diag.main(["--output-dir", str(out), "delete", "--all"]) == 0
    assert keep.exists() and outside.exists() and not list(out.glob("diagnostics-*.json"))


def test_fields_lists_the_policy(capsys):
    assert diag.main(["fields"]) == 0
    assert set(json.loads(capsys.readouterr().out)) == {"collected", "hashed", "redacted", "never_collected"}


def test_the_tool_has_no_network_or_upload_code():
    """Submission is opt-in because the tool cannot submit anything."""
    source = SCRIPT.read_text()
    for forbidden in ("urllib", "http.client", "requests", "smtplib", "ftplib", "socket.socket", "create_connection", "curl", "wget", "paramiko"):
        assert forbidden not in source, forbidden
    assert re.findall(r"^(?:import|from) (\w+)", source, re.M) and "socket" in source  # only gethostname is used
    assert source.count("socket.") == source.count("socket.gethostname")


# --- role wiring ----------------------------------------------------------------------

import yaml  # noqa: E402

ROLE = ROOT / "roles/developer_workstation"


def test_role_installs_the_command_only_when_enabled():
    tasks = yaml.safe_load((ROLE / "tasks/diagnostics-cli.yml").read_text())
    install, remove = tasks
    copy = install["ansible.builtin.copy"]
    assert copy["src"] == SCRIPT.name and (ROLE / "files" / copy["src"]).is_file()
    assert copy["dest"] == "/usr/local/bin/curiosity-diagnostics" and copy["mode"] == "0755" and copy["owner"] == "root"
    assert install["when"] == "diagnostics_cli_enabled | bool"
    assert remove["ansible.builtin.file"]["state"] == "absent" and "not (diagnostics_cli_enabled | bool)" in remove["when"]
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults["diagnostics_cli_enabled"] is True


def test_main_includes_the_diagnostics_tasks_with_a_tag():
    main = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
    def target(task):
        value = task.get("ansible.builtin.include_tasks")
        return value.get("file") if isinstance(value, dict) else value

    [include] = [task for task in main if target(task) == "diagnostics-cli.yml"]
    assert include["tags"] == ["diagnostics"]
