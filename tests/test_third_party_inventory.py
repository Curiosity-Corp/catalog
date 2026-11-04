"""The third-party inventory must match what the BUILT collection really contains."""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts/check_third_party_inventory.py"
INVENTORY = ROOT / "THIRD_PARTY_INVENTORY.yml"


def load_checker():
    spec = importlib.util.spec_from_file_location("check_third_party_inventory", CHECKER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = load_checker()


@pytest.fixture(scope="module")
def built_artifact():
    """Build the real collection into a temporary directory, as a release would."""
    if shutil.which("ansible-galaxy") is None:
        pytest.fail("ansible-galaxy is required: this test scans the built collection tarball")
    with tempfile.TemporaryDirectory(prefix="collection-licenses-test-") as tmp:
        yield checker.read_artifact(checker.build_artifact(ROOT, Path(tmp)))


def real_inventory() -> tuple[dict, bytes]:
    raw = INVENTORY.read_bytes()
    return yaml.safe_load(raw), raw


# --- the real collection ----------------------------------------------------


def test_built_collection_matches_inventory(built_artifact) -> None:
    inventory, raw = real_inventory()
    errors, _ = checker.check(built_artifact, inventory, raw)
    assert errors == []


def test_excluded_material_is_not_in_the_built_collection(built_artifact) -> None:
    inventory, _ = real_inventory()
    excluded = [e for e in inventory["entries"] if e["status"] == "excluded"]
    assert excluded, "the inventory should list what it keeps out of the artifact"
    for entry in excluded:
        patterns = [checker.glob_regex(p) for p in entry["paths"]]
        leaked = [n for n in built_artifact if any(p.match(n) for p in patterns)]
        assert leaked == [], f"{entry['id']} leaked into the artifact"
        # and the files really exist in the repository, so the exclusion is doing work
        assert any((ROOT / p.rstrip("/")).exists() for p in entry["paths"]), f"{entry['id']} no longer exists in git"


def test_every_excluded_path_is_in_build_ignore() -> None:
    inventory, _ = real_inventory()
    build_ignore = set(yaml.safe_load((ROOT / "galaxy.yml").read_text())["build_ignore"])
    for entry in inventory["entries"]:
        if entry["status"] == "excluded":
            for path in entry["paths"]:
                assert path.rstrip("/") in build_ignore, f"{path} is excluded in the inventory but not in galaxy.yml build_ignore"


def test_unresolved_provenance_is_never_shipped() -> None:
    inventory, _ = real_inventory()
    for entry in inventory["entries"]:
        if entry["provenance"] == "unresolved":
            assert entry["status"] == "excluded", entry["id"]


def test_artifact_carries_the_notice_files(built_artifact) -> None:
    for name in ("LICENSE", "NOTICE", "THIRD_PARTY.md", "THIRD_PARTY_INVENTORY.yml", "LICENSES/GPL-3.0-only.txt", "LICENSES/GPL-2.0-or-later.txt"):
        assert built_artifact.get(name, b"").strip(), f"{name} is missing from the built collection"


def test_release_checklist_reviews_the_inventory() -> None:
    text = (ROOT / "docs/release-checklist.md").read_text()
    assert "THIRD_PARTY_INVENTORY.yml" in text
    assert "make licenses" in text


def test_ci_runs_the_artifact_check() -> None:
    ci = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())
    scripts = "\n".join(ci["third_party_inventory"]["script"])
    assert "check_third_party_inventory.py --build" in scripts
    assert "tests/test_third_party_inventory.py" in scripts


# --- the checker itself, on small synthetic artifacts -------------------------

BASE = {
    "LICENSE": b"Apache License",
    "NOTICE": b"Example notice: Copyright (c) 2022 Jane Roe, wrapped\nacross lines",
    "THIRD_PARTY.md": b"notes",
    "LICENSES/GPL-3.0-only.txt": b"GNU GENERAL PUBLIC LICENSE",
    "roles/r/files/ok.py": b"print('first party')\n",
}
INVENTORY_BYTES = b"inventory"


def entry(**overrides):
    base = {
        "id": "e",
        "name": "Example",
        "kind": "vendored",
        "status": "shipped",
        "provenance": "resolved",
        "paths": ["roles/r/files/vendored.py"],
        "source": "https://example.invalid/e",
        "version": "1.0",
        "license": "GPL-3.0-only",
        "redistribution": "keep the notice",
    }
    base.update(overrides)
    return base


def run_check(entries, files=None, first_party=None):
    artifact = dict(BASE)
    artifact["THIRD_PARTY_INVENTORY.yml"] = INVENTORY_BYTES
    artifact.update(files or {})
    inventory = {"entries": entries, "first_party_paths": first_party or ["LICENSE", "NOTICE", "THIRD_PARTY.md", "LICENSES/*"]}
    return checker.check(artifact, inventory, INVENTORY_BYTES)


def messages(result) -> str:
    return "\n".join(result[0])


def test_checker_accepts_a_consistent_artifact() -> None:
    errors, _ = run_check([entry()], {"roles/r/files/vendored.py": b"# vendored\n"})
    assert errors == []


def test_checker_fails_when_an_excluded_file_is_in_the_artifact() -> None:
    result = run_check(
        [entry(status="excluded", provenance="unresolved", paths=["roles/r/docs/evidence/"])],
        {"roles/r/docs/evidence/shot.png": b"\x89PNG"},
    )
    assert "excluded file is in the artifact: roles/r/docs/evidence/shot.png" in messages(result)


def test_checker_fails_when_unresolved_material_ships() -> None:
    result = run_check([entry(provenance="unresolved")], {"roles/r/files/vendored.py": b"x\n"})
    assert "provenance is unresolved" in messages(result)


def test_checker_flags_needs_review_without_failing() -> None:
    errors, notes = run_check([entry(provenance="needs-review")], {"roles/r/files/vendored.py": b"x\n"})
    assert errors == []
    assert "needs-review (shipped): e" in notes


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("roles/r/files/fonts/Brand.woff2", b"wOF2\0\0"),
        ("roles/r/files/installer.run", b"#!/bin/sh\n"),
        ("roles/r/files/logo.png", b"\x89PNG\r\n"),
        ("roles/r/files/lib.so", b"\x7fELF\0"),
        ("roles/r/files/tool.py", b"# Copyright (c) 2019 Someone Else\nprint(1)\n"),
        ("roles/r/files/tool.sh", b"#!/bin/sh\n# SPDX-License-Identifier: GPL-2.0-only\n"),
        ("roles/r/files/theme", b"!! Based on Onyx by Dana Jansens\n"),
        ("roles/r/files/plugin.py", b"SCRIPT_LICENSE = 'GPL3'\n"),
        ("roles/r/files/key.asc", b"-----BEGIN PGP PUBLIC KEY BLOCK-----\nabc\n"),
    ],
)
def test_checker_flags_uninventoried_third_party_looking_files(name: str, data: bytes) -> None:
    result = run_check([], {name: data})
    assert f"uninventoried third-party-looking file in the artifact: {name}" in messages(result)


def test_checker_ignores_a_key_header_mentioned_inside_a_command() -> None:
    result = run_check([], {"roles/r/tasks/k.yml": b'- "--regexp=-----BEGIN PGP PUBLIC KEY BLOCK-----"\n'})
    assert result[0] == []


def test_inventory_entry_covers_a_flagged_file() -> None:
    result = run_check(
        [entry(paths=["roles/r/files/logo.png"])],
        {"roles/r/files/logo.png": b"\x89PNG"},
    )
    assert result[0] == []


def test_except_removes_files_from_an_entry() -> None:
    result = run_check(
        [entry(paths=["roles/r/files/tree/"], **{"except": ["**/*.png"]})],
        {"roles/r/files/tree/a.py": b"x\n", "roles/r/files/tree/b.png": b"\x89PNG"},
    )
    assert "uninventoried third-party-looking file in the artifact: roles/r/files/tree/b.png" in messages(result)


def test_checker_fails_on_a_stale_path() -> None:
    result = run_check([entry(paths=["roles/r/files/gone.py"])])
    assert "matches nothing in the artifact" in messages(result)


def test_checker_requires_the_notice_text() -> None:
    result = run_check(
        [entry(notice=True, notice_text=["Copyright (c) 2022 Jane Roe", "John Doe"], license_file="LICENSES/GPL-3.0-only.txt")],
        {"roles/r/files/vendored.py": b"x\n"},
    )
    text = messages(result)
    assert "NOTICE does not contain: 'John Doe'" in text
    assert "Jane Roe" not in text  # present, even though NOTICE wraps it across lines


def test_checker_requires_the_licence_text_file() -> None:
    result = run_check(
        [entry(notice=True, notice_text=["Jane Roe"], license_file="LICENSES/MIT.txt")],
        {"roles/r/files/vendored.py": b"x\n"},
    )
    assert "licence text LICENSES/MIT.txt is missing" in messages(result)


def test_checker_detects_version_drift() -> None:
    files = {"roles/r/files/vendored.py": b'__version__ = "2.0"\n', "roles/r/defaults/main.yml": b"tool_release: '3.1'\n"}
    drift = run_check(
        [entry(pins=[{"file": "roles/r/files/vendored.py", "regex": '__version__ = "([^"]+)"'}])],
        files,
    )
    assert "records version '1.0' but roles/r/files/vendored.py pins '2.0'" in messages(drift)
    yaml_drift = run_check([entry(version="3.0", pins=[{"file": "roles/r/defaults/main.yml", "key": "tool_release"}])], files)
    assert "pins '3.1'" in messages(yaml_drift)
    ok = run_check([entry(version="3.1", pins=[{"file": "roles/r/defaults/main.yml", "key": "tool_release"}])], files)
    assert ok[0] == []


def test_checker_requires_the_notice_files_and_the_same_inventory() -> None:
    artifact = {"roles/r/files/ok.py": b"x\n", "THIRD_PARTY_INVENTORY.yml": b"an older copy"}
    errors, _ = checker.check(artifact, {"entries": [], "first_party_paths": []}, INVENTORY_BYTES)
    text = "\n".join(errors)
    assert "artifact is missing LICENSE" in text
    assert "artifact is missing NOTICE" in text
    assert "differs from the inventory that was checked" in text


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"license": ""}, "missing license"),
        ({"kind": "mystery"}, "kind must be one of"),
        ({"status": "gone"}, "status must be one of"),
        ({"provenance": "hope"}, "provenance must be one of"),
        ({"status": "runtime", "paths": ["a/b"]}, "must not list paths"),
        ({"status": "excluded", "provenance": "resolved"}, "only unresolved or needs-review material is excluded"),
        ({"notice": True}, "notice: true needs notice_text"),
    ],
)
def test_inventory_schema_is_enforced(change: dict, expected: str) -> None:
    _, errors = checker.validate_schema({"entries": [entry(**change)]})
    assert any(expected in error for error in errors), errors


def test_duplicate_ids_are_rejected() -> None:
    _, errors = checker.validate_schema({"entries": [entry(), entry()]})
    assert any("duplicate id" in error for error in errors)


def test_glob_semantics() -> None:
    match = lambda pattern, name: bool(checker.glob_regex(pattern).match(name))  # noqa: E731
    assert match("a/b/", "a/b/c/d.txt")
    assert match("a/*.png", "a/x.png")
    assert not match("a/*.png", "a/b/x.png")
    assert match("**/*.png", "a/b/x.png")
    assert match("**/*.png", "x.png")
    assert not match("a/b", "a/bc")


def test_read_artifact_reads_a_tarball(tmp_path: Path) -> None:
    source = tmp_path / "src"
    (source / "d").mkdir(parents=True)
    (source / "d/f.txt").write_text("hello")
    archive = tmp_path / "c.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(source / "d/f.txt", arcname="d/f.txt")
    assert checker.read_artifact(archive) == {"d/f.txt": b"hello"}
