"""The signed apt repository: scripts/build_deb.py, scripts/apt_repo.py and scripts/deb_provenance.py.

The index and signature tests need dpkg-deb, gpg and gpgv; the update and rollback tests drive the real apt-get
against a signed local repository. Where a tool is missing they skip, except in CI's deb-build job, which sets
REQUIRE_APT_TOOLS=1 so a missing tool is a failure there, never a silent pass.
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
REQUIRED_TOOLS = os.environ.get("REQUIRE_APT_TOOLS") == "1"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


apt_repo = load("apt_repo")
build_deb = load("build_deb")
deb_provenance = load("deb_provenance")


def need(*tools: str) -> None:
    missing = [t for t in tools if shutil.which(t) is None]
    if missing and REQUIRED_TOOLS:
        pytest.fail(f"REQUIRE_APT_TOOLS=1 but {', '.join(missing)} is missing")
    if missing:
        pytest.skip(f"{', '.join(missing)} not installed")


# ---------------------------------------------------------------------------
# A tiny source tree that builds a real .deb, in any version
# ---------------------------------------------------------------------------


def fake_tree(base: Path, version: str) -> Path:
    root = base / f"tree-{version}"
    (root / "src" / "streamstack").mkdir(parents=True)
    (root / "src" / "streamstack" / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (root / "launcher").write_text(f"#!/bin/sh\necho streamstack {version}\n", encoding="utf-8")
    (root / "Makefile").write_text(
        "install:\n\tinstall -D -m 755 launcher $(DESTDIR)$(PREFIX)/bin/streamstack\n"
        "\tinstall -D -m 644 launcher $(DESTDIR)/etc/streamstack.conf\n",
        encoding="utf-8",
    )
    return root


def make_deb(base: Path, version: str, out: Path) -> Path:
    need("make", "dpkg-deb")
    os.environ["SOURCE_DATE_EPOCH"] = "1700000000"
    try:
        return build_deb.build(fake_tree(base, version), out)  # type: ignore[no-any-return]
    finally:
        os.environ.pop("SOURCE_DATE_EPOCH", None)


@pytest.fixture
def gpg_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway OpenPGP key standing in for the repository key (the real one lives in Key Vault)."""
    need("gpg", "gpgv")
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("GNUPGHOME", str(home))
    done = subprocess.run(
        ["gpg", "--batch", "--passphrase", "", "--quick-gen-key", "test <t@example.org>", "ed25519", "sign", "never"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return home


def key_fpr() -> str:
    out = subprocess.run(["gpg", "--list-keys", "--with-colons"], capture_output=True, text=True, check=True).stdout
    return next(line.split(":")[9] for line in out.splitlines() if line.startswith("fpr"))


def export_public(path: Path) -> Path:
    path.write_bytes(subprocess.run(["gpg", "--export"], capture_output=True, check=True).stdout)
    return path


def run(*argv: str) -> int:
    return int(apt_repo.main(list(argv)))


@pytest.fixture
def signed_repo(tmp_path: Path, gpg_home: Path) -> tuple[Path, Path]:
    """A repository holding 0.0.11 and 0.0.12, indexed and signed; returns (repository, public keyring)."""
    repo = tmp_path / "repo"
    make_deb(tmp_path, "0.0.11", repo)
    make_deb(tmp_path, "0.0.12", repo)
    assert run("index", str(repo), "--date", "2026-10-27T12:00:00+00:00") == 0
    assert run("sign", str(repo), "--key", key_fpr()) == 0
    return repo, export_public(tmp_path / "apt-public.gpg")


# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------


def test_control_parsing_keeps_continuation_lines() -> None:
    fields = apt_repo.parse_control("Package: streamstack\nDescription: one\n two\n three\nVersion: 0.0.1\n")
    assert fields["Package"] == "streamstack"
    assert fields["Description"] == "one\n two\n three"
    assert fields["Version"] == "0.0.1"


def test_a_bad_control_line_is_refused() -> None:
    with pytest.raises(apt_repo.AptError):
        apt_repo.parse_control("not a field\n")


def entry(version: str) -> object:
    data = version.encode()
    return apt_repo.DebEntry(
        f"streamstack_{version}_all.deb",
        {"Package": "streamstack", "Version": version, "Architecture": "all"},
        len(data),
        hashlib.sha256(data).hexdigest(),
        hashlib.md5(data, usedforsecurity=False).hexdigest(),
        hashlib.sha1(data, usedforsecurity=False).hexdigest(),
    )


def test_packages_lists_every_version_newest_first_with_checksums() -> None:
    text = apt_repo.packages_text([entry("0.0.9"), entry("0.0.12"), entry("0.0.10")])
    versions = [line.split(": ")[1] for line in text.splitlines() if line.startswith("Version: ")]
    assert versions == ["0.0.12", "0.0.10", "0.0.9"]  # numeric, not lexical: 0.0.9 sorts below 0.0.10
    assert "Filename: ./streamstack_0.0.12_all.deb" in text
    assert f"SHA256: {hashlib.sha256(b'0.0.12').hexdigest()}" in text


def test_release_lists_the_checksums_and_size_of_each_index_file() -> None:
    files = {"Packages": b"abc\n", "Packages.gz": gzip.compress(b"abc\n", mtime=0)}
    text = apt_repo.release_text(files, dt.datetime(2026, 10, 27, 12, 0, tzinfo=dt.UTC))
    assert "Date: Tue, 27 Oct 2026 12:00:00 UTC" in text
    assert "Architectures: all" in text
    digest = hashlib.sha256(b"abc\n").hexdigest()
    assert f" {digest} {4:>16} Packages" in text
    assert text.count("Packages.gz") == 3  # MD5Sum, SHA1 and SHA256 blocks


def test_registry_commands_need_a_token_and_a_location(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("CI_JOB_TOKEN", "GITLAB_TOKEN", "CI_API_V4_URL", "CI_PROJECT_ID", "STREAMSTACK_APT_BASE"):
        monkeypatch.delenv(name, raising=False)
    assert run("pull", str(tmp_path / "x")) == 1
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    assert run("pull", str(tmp_path / "x")) == 1  # a token but nowhere to pull from
    assert not (tmp_path / "x" / "anything").exists()


def test_provenance_binds_the_package_digest_to_the_ci_job(tmp_path: Path) -> None:
    manifest = tmp_path / "DEB-SHA256SUMS"
    manifest.write_text(f"{'a' * 64}  streamstack_0.0.12_all.deb\n", encoding="utf-8")
    env = {
        "CI_PROJECT_URL": "https://git.example.org/group/streamstack",
        "CI_COMMIT_SHA": "c" * 40,
        "CI_COMMIT_TAG": "v0.0.12",
        "CI_PIPELINE_URL": "https://git.example.org/group/streamstack/-/pipelines/1",
        "CI_JOB_URL": "https://git.example.org/group/streamstack/-/jobs/2",
        "CI_JOB_NAME": "deb-build",
    }
    doc = deb_provenance.statement(deb_provenance.read_subjects(manifest), env)
    assert doc["_type"] == "https://in-toto.io/Statement/v1"
    assert doc["predicateType"] == "https://slsa.dev/provenance/v1"
    assert doc["subject"] == [{"name": "streamstack_0.0.12_all.deb", "digest": {"sha256": "a" * 64}}]
    deps = doc["predicate"]["buildDefinition"]["resolvedDependencies"]  # type: ignore[index]
    assert deps[0]["digest"] == {"gitCommit": "c" * 40}
    assert doc["predicate"]["runDetails"]["builder"]["id"].endswith("/pipelines/1#deb-build")  # type: ignore[index]


def test_provenance_refuses_to_be_made_outside_ci(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = tmp_path / "DEB-SHA256SUMS"
    manifest.write_text(f"{'a' * 64}  x.deb\n", encoding="utf-8")
    assert deb_provenance.main([str(manifest)], env={}) == 1
    assert "CI_PROJECT_URL" in capsys.readouterr().err
    manifest.write_text("garbage\n", encoding="utf-8")
    assert deb_provenance.main([str(manifest)], env={"CI_PROJECT_URL": "x"}) == 1


def test_provenance_cli_prints_the_statement(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest = tmp_path / "DEB-SHA256SUMS"
    manifest.write_text(f"{'b' * 64}  x.deb\n", encoding="utf-8")
    env = {k: "https://h/x" for k in deb_provenance.REQUIRED}
    assert deb_provenance.main([str(manifest)], env=env) == 0
    assert json.loads(capsys.readouterr().out)["subject"][0]["name"] == "x.deb"


# ---------------------------------------------------------------------------
# The package
# ---------------------------------------------------------------------------


def test_the_package_is_reproducible_and_carries_its_metadata(tmp_path: Path) -> None:
    first = make_deb(tmp_path / "a", "0.0.12", tmp_path / "out-a")
    second = make_deb(tmp_path / "b", "0.0.12", tmp_path / "out-b")
    assert first.read_bytes() == second.read_bytes()
    control = apt_repo.parse_control(
        subprocess.run(["dpkg-deb", "-f", str(first)], capture_output=True, text=True).stdout
    )
    assert control["Package"] == "streamstack"
    assert control["Version"] == "0.0.12"
    assert "python3-gi" in control["Depends"]
    listing = subprocess.run(["dpkg-deb", "-c", str(first)], capture_output=True, text=True).stdout
    assert "./usr/bin/streamstack" in listing
    assert (tmp_path / "out-a" / "DEB-SHA256SUMS").read_text().split()[0] == hashlib.sha256(
        first.read_bytes()
    ).hexdigest()


# ---------------------------------------------------------------------------
# The repository
# ---------------------------------------------------------------------------


def test_a_signed_repository_verifies_the_way_apt_checks_it(signed_repo: tuple[Path, Path]) -> None:
    repo, keyring = signed_repo
    assert run("verify", str(repo), "--keyring", str(keyring)) == 0
    assert (repo / "InRelease").read_text().startswith("-----BEGIN PGP SIGNED MESSAGE-----")


def test_the_packages_index_lists_both_releases(signed_repo: tuple[Path, Path]) -> None:
    repo, _ = signed_repo
    text = (repo / "Packages").read_text()
    assert text.index("Version: 0.0.12") < text.index("Version: 0.0.11")


def test_a_tampered_index_is_refused(signed_repo: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    repo, keyring = signed_repo
    with (repo / "Packages").open("a") as handle:
        handle.write("\n")
    assert run("verify", str(repo), "--keyring", str(keyring)) == 1
    assert "Packages does not match the signed Release" in capsys.readouterr().err


def test_a_tampered_package_is_refused(signed_repo: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    repo, keyring = signed_repo
    deb = repo / "streamstack_0.0.12_all.deb"
    deb.write_bytes(deb.read_bytes() + b"x")
    assert run("verify", str(repo), "--keyring", str(keyring)) == 1
    assert "streamstack_0.0.12_all.deb does not match" in capsys.readouterr().err


def test_a_tampered_release_is_refused(signed_repo: tuple[Path, Path]) -> None:
    repo, keyring = signed_repo
    release = repo / "Release"
    release.write_text(release.read_text().replace("Suite: stable", "Suite: evil"), encoding="utf-8")
    assert run("verify", str(repo), "--keyring", str(keyring)) == 1


def test_a_signature_by_another_key_is_refused(
    signed_repo: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ = signed_repo
    other = tmp_path / "other-home"
    other.mkdir(mode=0o700)
    monkeypatch.setenv("GNUPGHOME", str(other))
    subprocess.run(
        ["gpg", "--batch", "--passphrase", "", "--quick-gen-key", "other <o@example.org>", "ed25519", "sign", "never"],
        capture_output=True,
        check=True,
    )
    assert run("verify", str(repo), "--keyring", str(export_public(tmp_path / "other.gpg"))) == 1


def test_reindexing_drops_the_signatures_of_the_old_release(signed_repo: tuple[Path, Path]) -> None:
    repo, keyring = signed_repo
    assert run("index", str(repo), "--date", "2026-10-28T12:00:00+00:00") == 0
    assert not (repo / "InRelease").exists() and not (repo / "Release.gpg").exists()
    assert run("verify", str(repo), "--keyring", str(keyring)) == 1  # unsigned until signed again


def test_two_files_of_one_version_are_refused(
    signed_repo: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _ = signed_repo
    shutil.copy(repo / "streamstack_0.0.12_all.deb", repo / "streamstack_0.0.12_copy_all.deb")
    assert run("index", str(repo)) == 1
    assert "appears twice" in capsys.readouterr().err


def test_an_empty_directory_is_not_a_repository(tmp_path: Path) -> None:
    assert run("index", str(tmp_path)) == 1


# ---------------------------------------------------------------------------
# apt itself: update and rollback
# ---------------------------------------------------------------------------


class Apt:
    """The real apt-get, fenced into a directory: its own state, sources, keyring and a fake dpkg status."""

    def __init__(self, base: Path, repo: Path, keyring: Path) -> None:
        self.base = base
        for sub in ("state/lists/partial", "cache/archives/partial", "etc"):
            (base / sub).mkdir(parents=True)
        (base / "etc" / "sources.list").write_text(f"deb [signed-by={keyring}] file:{repo} ./\n", encoding="utf-8")
        self.status = base / "status"
        self.set_installed(None)

    def set_installed(self, version: str | None) -> None:
        """What dpkg says is installed: the package's dependencies always, StreamStack itself at ``version``."""
        stanzas = []
        for dep in build_deb.DEPENDS:
            name = dep.split()[0]
            stanzas.append(f"Package: {name}\nStatus: install ok installed\nArchitecture: all\nVersion: 99.0\n")
        if version:
            stanzas.append(
                f"Package: streamstack\nStatus: install ok installed\nArchitecture: all\nVersion: {version}\n"
            )
        self.status.write_text("\n".join(stanzas) + "\n", encoding="utf-8")

    def get(self, *args: str) -> subprocess.CompletedProcess[str]:
        options = [
            f"-oDir::State={self.base / 'state'}",
            f"-oDir::State::status={self.status}",
            f"-oDir::Cache={self.base / 'cache'}",
            f"-oDir::Etc::sourcelist={self.base / 'etc' / 'sources.list'}",
            "-oDir::Etc::sourceparts=/dev/null",
            "-oDir::Etc::trustedparts=/dev/null",
            "-oDebug::NoLocking=1",
            "-oAPT::Sandbox::User=",
            "-oAPT::Architecture=amd64",
            "-oAPT::Architectures::=all",
        ]
        return subprocess.run(
            ["apt-get", *options, *args], capture_output=True, text=True, check=False, env={**os.environ, "LC_ALL": "C"}
        )


@pytest.fixture
def apt(signed_repo: tuple[Path, Path], tmp_path: Path) -> Apt:
    need("apt-get")
    repo, keyring = signed_repo
    return Apt(tmp_path / "apt", repo, keyring)


def test_apt_trusts_the_repository_and_installs_the_newest(apt: Apt) -> None:
    updated = apt.get("update")
    assert updated.returncode == 0, updated.stdout + updated.stderr
    simulated = apt.get("-s", "install", "streamstack")
    assert simulated.returncode == 0, simulated.stdout + simulated.stderr
    assert "Inst streamstack (0.0.12 " in simulated.stdout


def test_update_moves_an_installed_older_release_forward(apt: Apt) -> None:
    apt.set_installed("0.0.11")
    assert apt.get("update").returncode == 0
    upgraded = apt.get("-s", "install", "--only-upgrade", "streamstack")
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    assert "Inst streamstack [0.0.11] (0.0.12 " in upgraded.stdout


def test_rollback_installs_the_previous_release_over_the_newest(apt: Apt) -> None:
    apt.set_installed("0.0.12")
    assert apt.get("update").returncode == 0
    back = apt.get("-s", "--allow-downgrades", "install", "streamstack=0.0.11")
    assert back.returncode == 0, back.stdout + back.stderr
    assert "Inst streamstack [0.0.12] (0.0.11 " in back.stdout
    assert "DOWNGRADED" in back.stdout  # apt names it a downgrade; nothing is hidden from the operator


def test_apt_refuses_an_unsigned_repository(apt: Apt, signed_repo: tuple[Path, Path]) -> None:
    repo, _ = signed_repo
    (repo / "InRelease").unlink()
    (repo / "Release.gpg").unlink()
    failed = apt.get("update")
    assert failed.returncode != 0
    assert "is not signed" in failed.stdout + failed.stderr


def test_apt_refuses_a_repository_that_changed_after_signing(apt: Apt, signed_repo: tuple[Path, Path]) -> None:
    repo, _ = signed_repo
    (repo / "Release").write_text((repo / "Release").read_text().replace("stable", "stable2"), encoding="utf-8")
    (repo / "InRelease").unlink()  # leave only the detached signature, which no longer matches the file
    failed = apt.get("update")
    assert failed.returncode != 0, failed.stdout + failed.stderr
