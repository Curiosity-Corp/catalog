"""scripts/release.py: the release preparation tool and the CI tag gate (a developer tool, not in the wheel)."""

from __future__ import annotations

import importlib.util
import io
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from email.message import Message
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release.py"

CHANGELOG = """# Changelog

All notable changes are recorded here.

## [Unreleased]

### Fixed

- A thing that was broken.

## [0.3.1] - 2026-10-22

### Fixed

- Older fix.
"""

INIT = '"""StreamStack."""\n\n__version__ = "0.0.8"\n'


@pytest.fixture(scope="module")
def release() -> ModuleType:
    spec = importlib.util.spec_from_file_location("streamstack_release_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeGit:
    """The few git answers `prepare` asks for, so the suite runs on images without git."""

    def __init__(self) -> None:
        self.dirty = ""
        self.branch = "main"
        self.behind = "0"
        self.has_upstream = False
        self.tags: set[str] = set()

    def __call__(self, root: Path, *args: str) -> str:
        if args[:2] == ("status", "--porcelain"):
            return self.dirty
        if args[:2] == ("rev-parse", "--abbrev-ref"):
            return self.branch
        if args[:2] == ("rev-list", "--count"):
            return self.behind
        if args[:2] == ("tag", "--list"):
            return args[2] if args[2] in self.tags else ""
        raise AssertionError(f"unexpected git call: {args}")

    def ok(self, root: Path, *args: str) -> bool:
        assert args[:2] == ("rev-parse", "--verify")
        return self.has_upstream


@pytest.fixture
def fake_git(release: ModuleType, monkeypatch: pytest.MonkeyPatch) -> FakeGit:
    fake = FakeGit()
    monkeypatch.setattr(release, "git", fake)
    monkeypatch.setattr(release, "git_ok", fake.ok)
    return fake


@pytest.fixture
def repo(tmp_path: Path, fake_git: FakeGit) -> Path:
    """The two files the tool edits, on a (faked) clean main."""
    (tmp_path / "src/streamstack").mkdir(parents=True)
    (tmp_path / "src/streamstack/__init__.py").write_text(INIT)
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG)
    return tmp_path


def _prepare(release: ModuleType, root: Path, *args: str) -> int:
    return release.main(["--root", str(root), "prepare", *args])


# --- versions --------------------------------------------------------------------------------------------------


def test_parse_version_rejects_anything_but_three_numbers(release: ModuleType) -> None:
    assert release.parse_version("0.0.8") == (0, 0, 8)
    for bad in ("0.0", "v0.0.8", "0.0.8rc1", "01.0.0", ""):
        with pytest.raises(release.ReleaseError):
            release.parse_version(bad)


def test_patch_bump_and_same_version_are_allowed(release: ModuleType) -> None:
    release.check_bump((0, 0, 8), (0, 0, 9), owner_approved_minor=False)
    release.check_bump((0, 0, 8), (0, 0, 8), owner_approved_minor=False)


@pytest.mark.parametrize("new", [(0, 1, 0), (1, 0, 0), (0, 2, 5)])
def test_minor_and_major_bumps_are_refused_with_the_reason(release: ModuleType, new: tuple[int, int, int]) -> None:
    with pytest.raises(release.ReleaseError, match="PATCH releases only") as err:
        release.check_bump((0, 0, 8), new, owner_approved_minor=False)
    assert "--owner-approved-minor" in str(err.value)


def test_owner_flag_allows_a_minor_bump_but_never_a_downgrade(release: ModuleType) -> None:
    release.check_bump((0, 0, 8), (0, 1, 0), owner_approved_minor=True)
    with pytest.raises(release.ReleaseError, match="older"):
        release.check_bump((0, 1, 0), (0, 0, 9), owner_approved_minor=True)


def test_patch_on_an_approved_minor_needs_no_flag(release: ModuleType) -> None:
    release.check_bump((0, 1, 0), (0, 1, 1), owner_approved_minor=False)


def test_ci_policy_only_passes_0_0_x_without_the_flag(release: ModuleType) -> None:
    release.check_policy((0, 0, 12), owner_approved_minor=False)
    with pytest.raises(release.ReleaseError, match=r"not a 0\.0\.x"):
        release.check_policy((0, 3, 2), owner_approved_minor=False)
    release.check_policy((0, 3, 2), owner_approved_minor=True)


def test_tag_version_needs_the_v_form(release: ModuleType) -> None:
    assert release.tag_version("v0.0.8") == "0.0.8"
    for bad in ("0.0.8", "v0.0", "v0.0.8-rc1", "release-0.0.8"):
        with pytest.raises(release.ReleaseError):
            release.tag_version(bad)


def test_version_rewrite_touches_only_the_assignment(release: ModuleType) -> None:
    assert release.read_version(INIT) == "0.0.8"
    assert release.write_version(INIT, "0.0.9") == INIT.replace("0.0.8", "0.0.9")


# --- changelog -------------------------------------------------------------------------------------------------


def test_promote_dates_the_section_and_leaves_an_empty_unreleased(release: ModuleType) -> None:
    out = release.promote_unreleased(CHANGELOG, "0.0.8", "2026-10-24")
    assert "## [Unreleased]\n\n## [0.0.8] - 2026-10-24\n\n### Fixed\n\n- A thing that was broken." in out
    assert out.count("## [Unreleased]") == 1
    assert "## [0.3.1] - 2026-10-22" in out
    with pytest.raises(release.ReleaseError, match="empty"):
        release.unreleased_body(out)


def test_promote_refuses_an_empty_unreleased_and_a_repeated_version(release: ModuleType) -> None:
    with pytest.raises(release.ReleaseError, match="empty"):
        release.promote_unreleased("# C\n\n## [Unreleased]\n\n## [0.0.7] - 2026-01-01\n\n- x\n", "0.0.8", "2026-10-24")
    with pytest.raises(release.ReleaseError, match="already has"):
        release.promote_unreleased(CHANGELOG, "0.3.1", "2026-10-24")
    with pytest.raises(release.ReleaseError, match="no ## \\[Unreleased\\]"):
        release.promote_unreleased("# C\n\n## [0.0.7] - 2026-01-01\n\n- x\n", "0.0.8", "2026-10-24")


def test_section_body_is_exactly_that_section(release: ModuleType) -> None:
    body = release.section_body(CHANGELOG, "0.3.1")
    assert body == "### Fixed\n\n- Older fix.\n"
    assert "Unreleased" not in body


def test_last_section_has_no_trailing_heading_to_stop_at(release: ModuleType) -> None:
    assert release.section_body("## [0.0.1] - 2026-01-01\n\n- only\n", "0.0.1") == "- only\n"


def test_default_target_is_the_unreleased_current_version_then_the_next_patch(release: ModuleType) -> None:
    assert release.default_target((0, 0, 8), CHANGELOG) == (0, 0, 8)
    released = release.promote_unreleased(CHANGELOG, "0.0.8", "2026-10-24")
    assert release.default_target((0, 0, 8), released) == (0, 0, 9)


# --- prepare ---------------------------------------------------------------------------------------------------


def test_prepare_promotes_the_changelog_and_prints_the_tag_command(
    release: ModuleType, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _prepare(release, repo, "--date", "2026-10-24") == 0
    out = capsys.readouterr().out
    assert 'git tag -a v0.0.8 -m "StreamStack 0.0.8"' in out
    assert "git push origin v0.0.8" in out
    assert "## [0.0.8] - 2026-10-24" in (repo / "CHANGELOG.md").read_text()
    assert release.read_version((repo / "src/streamstack/__init__.py").read_text()) == "0.0.8"


def test_prepare_bumps_to_the_next_patch(release: ModuleType, repo: Path) -> None:
    assert _prepare(release, repo, "0.0.9", "--date", "2026-10-24") == 0
    assert release.read_version((repo / "src/streamstack/__init__.py").read_text()) == "0.0.9"
    assert "## [0.0.9] - 2026-10-24" in (repo / "CHANGELOG.md").read_text()


def test_prepare_dry_run_changes_nothing(release: ModuleType, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _prepare(release, repo, "0.0.9", "--dry-run") == 0
    assert "dry run" in capsys.readouterr().out
    assert (repo / "CHANGELOG.md").read_text() == CHANGELOG
    assert (repo / "src/streamstack/__init__.py").read_text() == INIT


def test_prepare_refuses_a_minor_bump_and_changes_nothing(
    release: ModuleType, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _prepare(release, repo, "0.1.0") == 1
    err = capsys.readouterr().err
    assert "minor bump" in err
    assert "PATCH releases only" in err
    assert "--owner-approved-minor" in err
    assert (repo / "CHANGELOG.md").read_text() == CHANGELOG


def test_prepare_accepts_a_minor_bump_with_the_owner_flag(release: ModuleType, repo: Path) -> None:
    assert _prepare(release, repo, "0.1.0", "--owner-approved-minor", "--date", "2026-10-24") == 0
    assert release.read_version((repo / "src/streamstack/__init__.py").read_text()) == "0.1.0"


def test_prepare_refuses_a_dirty_tree(
    release: ModuleType, repo: Path, fake_git: FakeGit, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_git.dirty = "?? stray.txt"
    assert _prepare(release, repo) == 1
    assert "not clean" in capsys.readouterr().err


def test_prepare_refuses_another_branch_and_an_existing_tag(
    release: ModuleType, repo: Path, fake_git: FakeGit, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_git.tags.add("v0.0.8")
    assert _prepare(release, repo) == 1
    assert "already exists" in capsys.readouterr().err
    fake_git.tags.clear()
    fake_git.branch = "feature"
    assert _prepare(release, repo) == 1
    assert "release from main" in capsys.readouterr().err
    assert _prepare(release, repo, "--allow-branch", "--date", "2026-10-24") == 0


def test_prepare_refuses_a_checkout_behind_origin(
    release: ModuleType, repo: Path, fake_git: FakeGit, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_git.has_upstream = True
    fake_git.behind = "2"
    assert _prepare(release, repo) == 1
    assert "2 commit(s) behind origin/main" in capsys.readouterr().err
    fake_git.behind = "0"
    assert _prepare(release, repo, "--date", "2026-10-24") == 0


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_prepare_against_a_real_checkout(release: ModuleType, tmp_path: Path) -> None:
    root = tmp_path / "real"
    (root / "src/streamstack").mkdir(parents=True)
    (root / "src/streamstack/__init__.py").write_text(INIT)
    (root / "CHANGELOG.md").write_text(CHANGELOG)
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "t@example.org"),
        ("config", "user.name", "T"),
        ("add", "."),
        ("commit", "-q", "-m", "init"),
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    assert _prepare(release, root, "--date", "2026-10-24") == 0
    (root / "stray.txt").write_text("x")
    assert _prepare(release, root, "0.0.9") == 1  # dirty tree


def test_prepare_rejects_a_bad_date(release: ModuleType, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _prepare(release, repo, "--date", "24-10-2026") == 1
    assert "YYYY-MM-DD" in capsys.readouterr().err


# --- CI gate and notes -----------------------------------------------------------------------------------------


def _released(release: ModuleType, repo: Path) -> None:
    _prepare(release, repo, "--date", "2026-10-24")


def test_check_tag_passes_when_tag_version_and_changelog_agree(release: ModuleType, repo: Path) -> None:
    _released(release, repo)
    assert release.main(["--root", str(repo), "check-tag", "v0.0.8"]) == 0


def test_check_tag_fails_on_a_version_mismatch(
    release: ModuleType, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _released(release, repo)
    assert release.main(["--root", str(repo), "check-tag", "v0.0.9"]) == 1
    assert "__version__ is 0.0.8" in capsys.readouterr().err


def test_check_tag_fails_without_a_changelog_section(
    release: ModuleType, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert release.main(["--root", str(repo), "check-tag", "v0.0.8"]) == 1
    assert "no ## [0.0.8] section" in capsys.readouterr().err


def test_check_tag_refuses_a_minor_tag_unless_the_owner_approved(
    release: ModuleType, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _prepare(release, repo, "0.1.0", "--owner-approved-minor", "--date", "2026-10-24")
    assert release.main(["--root", str(repo), "check-tag", "v0.1.0"]) == 1
    assert "PATCH releases only" in capsys.readouterr().err
    assert release.main(["--root", str(repo), "check-tag", "v0.1.0", "--owner-approved-minor"]) == 0


def test_check_tag_reads_the_owner_flag_from_the_pipeline_variable(
    release: ModuleType, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare(release, repo, "0.1.0", "--owner-approved-minor", "--date", "2026-10-24")
    monkeypatch.setenv("RELEASE_OWNER_APPROVED_MINOR", "true")
    assert release.main(["--root", str(repo), "check-tag", "v0.1.0"]) == 0


def test_notes_print_the_section_for_the_release_body(
    release: ModuleType, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _released(release, repo)
    capsys.readouterr()
    assert release.main(["--root", str(repo), "notes", "v0.0.8"]) == 0
    assert capsys.readouterr().out == "### Fixed\n\n- A thing that was broken.\n"


# --- upload ----------------------------------------------------------------------------------------------------


def test_upload_url_targets_the_generic_registry(release: ModuleType) -> None:
    url = release.upload_url("https://gl.example/api/v4/", "2122", "0.0.8", "streamstack-0.0.8-py3-none-any.whl")
    assert url == (
        "https://gl.example/api/v4/projects/2122/packages/generic/streamstack-release/0.0.8/"
        "streamstack-0.0.8-py3-none-any.whl?select=package_file"
    )


def test_upload_assets_needs_the_ci_environment_and_a_wheel_and_sdist(
    release: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("CI_JOB_TOKEN", "CI_API_V4_URL", "CI_PROJECT_ID"):
        monkeypatch.delenv(name, raising=False)
    assert release.main(["upload-assets", "v0.0.8", "--dist", str(tmp_path)]) == 1
    assert "CI_JOB_TOKEN" in capsys.readouterr().err
    monkeypatch.setenv("CI_JOB_TOKEN", "t")
    monkeypatch.setenv("CI_API_V4_URL", "https://gl.example/api/v4")
    monkeypatch.setenv("CI_PROJECT_ID", "1")
    (tmp_path / "streamstack-0.0.8-py3-none-any.whl").write_bytes(b"w")
    assert release.main(["upload-assets", "v0.0.8", "--dist", str(tmp_path)]) == 1
    assert "wheel and an sdist" in capsys.readouterr().err


def test_upload_puts_every_file_with_the_job_token(
    release: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, data in (("streamstack-0.0.8-py3-none-any.whl", b"w"), ("streamstack-0.0.8.tar.gz", b"s")):
        (tmp_path / name).write_bytes(data)
    (tmp_path / "SHA256SUMS").write_text("sums")
    monkeypatch.setenv("CI_JOB_TOKEN", "tok")
    monkeypatch.setenv("CI_API_V4_URL", "https://gl.example/api/v4")
    monkeypatch.setenv("CI_PROJECT_ID", "2122")
    seen: list[tuple[str, str, str | None]] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def fake_urlopen(request: urllib.request.Request, timeout: float = 0) -> Response:
        seen.append((request.full_url, request.get_method(), request.get_header("Job-token")))
        return Response()

    monkeypatch.setattr(release.urllib.request, "urlopen", fake_urlopen)
    assert release.main(["upload-assets", "v0.0.8", "--dist", str(tmp_path)]) == 0
    assert [name.split("/")[-1].split("?")[0] for name, _, _ in seen] == [
        "SHA256SUMS",
        "streamstack-0.0.8-py3-none-any.whl",
        "streamstack-0.0.8.tar.gz",
    ]
    assert {method for _, method, _ in seen} == {"PUT"}
    assert {token for _, _, token in seen} == {"tok"}


def test_upload_retries_a_server_error_then_gives_up_on_a_client_error(
    release: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"x")
    monkeypatch.setattr(release.time, "sleep", lambda _s: None)
    calls: list[int] = []

    def failing(code: int) -> Callable[..., None]:
        def fake(request: urllib.request.Request, timeout: float = 0) -> None:
            calls.append(code)
            raise urllib.error.HTTPError("u", code, "err", Message(), io.BytesIO(b""))

        return fake

    monkeypatch.setattr(release.urllib.request, "urlopen", failing(503))
    with pytest.raises(release.ReleaseError, match="HTTP 503"):
        release.upload_file("https://gl.example/x", path, "tok")
    assert calls == [503, 503, 503]
    calls.clear()
    monkeypatch.setattr(release.urllib.request, "urlopen", failing(401))
    with pytest.raises(release.ReleaseError, match="HTTP 401"):
        release.upload_file("https://gl.example/x", path, "tok")
    assert calls == [401]
