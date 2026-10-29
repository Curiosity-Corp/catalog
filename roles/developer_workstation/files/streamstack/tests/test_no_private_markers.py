"""This tree is vendored, as a squashed subtree, into the public catalog, whose public-contract test fails any file that
names the org's private infrastructure. A StreamStack release that does that reaches the catalog as a red merge request
after the tag exists (v0.0.19's catalog bump failed on the Key Vault name). Fail here instead, at the source.

The markers are the catalog's private ones (the two GitLab and registry host markers it tolerates are not listed). They
are assembled from pieces so that this file does not contain them itself.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

MARKERS = tuple(
    "".join(parts)
    for parts in (
        ("omlab", "-secrets"),
        ("harbor", "-internal-ca"),
        ("focus", "apiary"),
        ("focus", "pass.com"),
        ("focus", "buzz.org"),
        ("dream", "hive.org"),
        ("dream", "decks.org"),
        ("hard", "magic.com"),
        ("hyper", "sight.net"),
        ("slid", "ee.net"),
    )
)


SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".ruff_cache", ".pytest_cache", "dist", "build"}


def walked_files() -> list[Path]:
    files: list[Path] = []
    for folder, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        files.extend(Path(folder) / name for name in names)
    return files


def source_files() -> list[Path]:
    """The tracked files. The unit-test images have no git (v0.0.17 burned on exactly that), so walk the tree there."""
    if shutil.which("git"):
        result = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, text=True, check=False)
        if result.returncode == 0:
            names = [name for name in result.stdout.split("\0") if name]
            return [ROOT / name for name in names if (ROOT / name).is_file()]
    return walked_files()


def test_the_scan_works_where_git_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    names = {path.relative_to(ROOT).as_posix() for path in source_files()}
    assert "scripts/ci/deb-sign.sh" in names and "tests/test_no_private_markers.py" in names
    assert not any(name.startswith(".git/") for name in names)


def test_no_file_names_private_infrastructure() -> None:
    hits: list[str] = []
    for path in source_files():
        try:
            text = path.read_text(encoding="utf-8").lower()
        except UnicodeDecodeError:
            continue  # a binary file (an icon, a fixture); the catalog gate reads text
        hits.extend(f"{path.relative_to(ROOT)}: {marker}" for marker in MARKERS if marker in text)
    assert not hits, "private markers in the vendored tree (make them CI variables or configuration): " + "; ".join(
        hits
    )
