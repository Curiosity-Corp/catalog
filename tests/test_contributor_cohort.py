"""The contributor worksheet never turns git history into a claim by itself."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/contributor_cohort.py"
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed (the CI image has none; the pure tests below still run)")

_spec = importlib.util.spec_from_file_location("contributor_cohort", SCRIPT)
MOD = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(MOD)


def commit(repo: Path, name: str, email: str, when: str, files: dict[str, str], message: str = "change") -> None:
    for path, content in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    env = {**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name,
           "GIT_COMMITTER_EMAIL": email, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message], check=True, env=env)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
    commit(repo, "Old Timer", "old@example.org", "2024-01-05T12:00:00Z", {"docs/a.md": "x\n"})            # outside the window
    commit(repo, "Ada Researcher", "ada@lab.example.edu", "2026-03-01T12:00:00Z",
           {"tests/test_a.py": "def test():\n    assert 1\n", "docs/guide.md": "one\ntwo\nthree\n"})
    commit(repo, "Ada Researcher", "ada@lab.example.edu", "2026-03-02T12:00:00Z", {"docs/guide.md": "one\ntwo\nthree\nfour\n"})
    commit(repo, "Tiny Fixer", "tiny@example.net", "2026-04-01T12:00:00Z", {"docs/typo.md": "a\n"})
    commit(repo, "renovate[bot]", "renovate@example.org", "2026-04-02T12:00:00Z", {"renovate.json": "{}\n"})
    commit(repo, "Claude", "noreply@anthropic.com", "2026-04-03T12:00:00Z", {"roles/x/tasks/main.yml": "---\n- debug: {}\n"})
    commit(repo, "Maintainer One", "maint@curiosity.example", "2026-05-01T12:00:00Z", {"roles/x/tasks/other.yml": "---\n"})
    return repo


def run(repo: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), "--repo", str(repo), "--as-of", "2026-11-04", *extra],
                          capture_output=True, text=True, check=False)


@needs_git
def test_window_bots_and_internal_accounts_are_excluded(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    internal = tmp_path / "maintainers.txt"
    internal.write_text("# kept outside the repository\n@curiosity.example\n")
    result = run(repo, "--internal-file", str(internal), "--format", "json")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["review_required"] is True
    candidates = {c["email"]: c for c in data["candidates"]}
    assert set(candidates) == {"ada@lab.example.edu", "tiny@example.net"}, "the 2024 author, bots and maintainers are not candidates"
    assert set(data["bots_excluded"]) == {"renovate@example.org", "noreply@anthropic.com"}
    assert data["internal_excluded"] == {"maint@curiosity.example": 1}


@needs_git
def test_worksheet_splits_work_by_kind_and_flags_tiny_commits(tmp_path: Path) -> None:
    data = json.loads(run(make_repo(tmp_path), "--format", "json").stdout)
    people = {c["email"]: c for c in data["candidates"]}
    ada, tiny = people["ada@lab.example.edu"], people["tiny@example.net"]
    assert (ada["commits"], ada["tests"], ada["docs"], ada["tiny"]) == (2, 1, 2, 1)
    assert (tiny["commits"], tiny["tiny"]) == (1, 1)
    assert "maint@curiosity.example" in people, "without an internal file nobody is assumed to be a maintainer"


@needs_git
def test_text_output_says_it_is_a_review_list_not_a_count(tmp_path: Path) -> None:
    text = run(make_repo(tmp_path)).stdout
    assert "not a count to report" in text
    assert "Candidate external contributors:" in text and "Bots excluded: 2" in text


def test_script_only_reads_git_and_the_optional_file() -> None:
    source = SCRIPT.read_text()
    for forbidden in ("urllib", "requests", "http.client", "socket", "smtplib"):
        assert forbidden not in source


def fake(name: str, email: str, files: list[tuple[str, int]], date: str = "2026-05-01") -> dict:
    return {"name": name, "email": email, "date": date, "sha": "abc", "subject": "x", "files": files}


def test_classification_bots_and_internal_without_git() -> None:
    commits = [
        fake("Ada", "ada@lab.example.edu", [("tests/test_x.py", 10), ("docs/a.md", 4)]),
        fake("Ada", "ada@lab.example.edu", [("roles/r/tasks/main.yml", 1)], "2026-06-01"),
        fake("dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com", [("requirements.yml", 1)]),
        fake("Claude", "noreply@anthropic.com", [("roles/r/tasks/main.yml", 9)]),
        fake("GitLab project bot", "project_12_bot_abc@noreply.example", [("renovate.json", 1)]),
        fake("Maintainer", "m@curiosity.example", [("README.md", 3)]),
    ]
    result = MOD.build(commits, set(), {"@curiosity.example"})
    assert [c["email"] for c in result["candidates"]] == ["ada@lab.example.edu"]
    ada = result["candidates"][0]
    assert (ada["commits"], ada["tests"], ada["docs"], ada["code"], ada["tiny"]) == (2, 1, 1, 1, 1)
    assert (ada["first"], ada["last"]) == ("2026-05-01", "2026-06-01")
    assert len(result["bots_excluded"]) == 3
    assert result["internal_excluded"] == {"m@curiosity.example": 1}


def test_internal_file_accepts_emails_domains_and_comments(tmp_path: Path) -> None:
    listing = tmp_path / "internal.txt"
    listing.write_text("# private\nA@Example.org\n\n@Staff.example  # domain\n")
    emails, domains = MOD.read_internal(listing)
    assert emails == {"a@example.org"} and domains == {"@staff.example"}
    assert MOD.is_internal("x@staff.example", emails, domains) and MOD.is_internal("A@example.org", emails, domains)
    assert not MOD.is_internal("x@other.example", emails, domains)


def test_text_report_never_reports_a_bare_count() -> None:
    result = MOD.build([fake("Ada", "ada@lab.example.edu", [("docs/a.md", 3)])], set(), set())
    text = MOD.render_text(result, MOD.date(2025, 11, 4), MOD.date(2026, 11, 4))
    assert "list to review, not a count to report" in text.replace("\n", " ")
    assert "A person must read each" in text
