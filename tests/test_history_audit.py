"""scripts/history_audit.py finds planted material in old commits and never prints it."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/history_audit.py"
SENTINEL_IP = "10.9.8.7"
SENTINEL_MAIL = "someone-sentinel@corp.example.net"
SENTINEL_MARKER = "acme-private-sentinel"
FAKE_KEY = "-----BEGIN " + "RSA PRIVATE KEY-----\nAAAA\n-----END " + "RSA PRIVATE KEY-----\n"


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True, capture_output=True,
    )


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text(f"host {SENTINEL_IP}\nmail {SENTINEL_MAIL}\nproject {SENTINEL_MARKER}\n")
    (repo / "old.pem").write_text(FAKE_KEY)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "one")
    git(repo, "tag", "v1")
    # The material is deleted afterwards, so only history still holds it.
    git(repo, "rm", "-q", "a.txt", "old.pem")
    (repo / "b.txt").write_text("clean\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "two")
    return repo


def run(repo: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo), "--no-gitleaks", *extra],
        capture_output=True, text=True, timeout=60, check=False,
    )


def test_deleted_material_is_found_in_history_and_counted(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    markers = tmp_path / "markers.txt"
    markers.write_text("# private names\n" + SENTINEL_MARKER + "\n")
    result = run(repo, "--json", "--paths", "--markers", str(markers))
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["commits"] == 2 and data["tags"] == 1
    cats = data["categories"]
    for name in ("rfc1918-ip", "email-address", "private-key-block", "marker:1"):
        assert cats[name]["blobs"] == 1, name
    assert data["sensitive_looking_paths"] == ["old.pem"]
    assert data["paths"]["rfc1918-ip"] == ["a.txt"]


def test_output_never_contains_matched_text_or_marker_names(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    markers = tmp_path / "markers.txt"
    markers.write_text(SENTINEL_MARKER + "\n")
    for args in ([], ["--paths"], ["--json", "--paths"]):
        out = run(repo, "--markers", str(markers), *args).stdout
        for secret in (SENTINEL_IP, SENTINEL_MAIL, SENTINEL_MARKER, "PRIVATE KEY"):
            assert secret not in out, (args, secret)
        assert "marker:1" in out


def test_clean_repo_reports_nothing(tmp_path: Path) -> None:
    repo = tmp_path / "c"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "x.txt").write_text("nothing here\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "x")
    data = json.loads(run(repo, "--json").stdout)
    assert data["categories"] == {}


def test_script_contains_no_organisation_names() -> None:
    text = SCRIPT.read_text().lower()
    for private in ("developerdojo", "focusapiary", "focuscell", "focuspass", "omlab"):
        assert private not in text
