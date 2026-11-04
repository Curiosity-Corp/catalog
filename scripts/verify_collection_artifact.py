#!/usr/bin/env python3
"""Verify a built collection artifact before it is recorded or released.

Checks, in order (each failure is listed; the exit status is 1 if any fail):

* contents: only intended top-level names, nothing from the deny-list (tests,
  CI, VCS, caches, private-key and environment files, other archives);
* integrity: FILES.json lists exactly the members present and every digest
  matches;
* metadata: MANIFEST.json agrees with galaxy.yml (namespace, name, version,
  license, dependency constraints); every role has complete ``meta/main.yml``;
  CHANGELOG.md has an ``[Unreleased]`` section and, with ``--release``, a
  section for the artifact's version that is not marked "planned";
* reproducibility (``--rebuild SOURCE_DIR``): a second normalized build from a
  clean copy of the source tree has the same SHA-256;
* clean controller (``--install-check``): the artifact installs into an empty
  collections path and ``ansible-playbook --syntax-check`` plus ``--list-tasks``
  accept a playbook that uses each role by its fully qualified name.

``--report FILE`` writes the checks, the digest and the member count as JSON,
and ``--sha256-file FILE`` writes ``<digest>  <name>`` for ``sha256sum -c``.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import yaml

ALLOWED_TOP_LEVEL_FILES = {
    "FILES.json",
    "MANIFEST.json",
    "README.md",
    "LICENSE",
    "NOTICE",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "SECURITY.md",
    "SUPPORT.md",
    "THIRD_PARTY.md",
    "THIRD_PARTY_INVENTORY.yml",
    "ansible.cfg",
    "requirements.yml",
}
ALLOWED_TOP_LEVEL_DIRS = {"docs", "examples", "playbooks", "roles", "LICENSES", "meta", "plugins"}

# Names that must not appear anywhere in the artifact (build leftovers, caches,
# other archives, credential-shaped files).
DENIED_ANYWHERE = (
    ".ci-venv",
    ".venv",
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
    ".mypy_cache",
    ".ansible",
    "*.tar.gz",
    "*.tgz",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.kdbx",
    "id_rsa*",
    "id_ed25519*",
    ".env",
    ".env.*",
    ".netrc",
    "collection.cdx.json",
    "collection.sha256",
)
# Repository plumbing: forbidden at the top level only, because the vendored
# StreamStack subtree legitimately carries its own tests and CI files.
DENIED_TOP_LEVEL = ("tests", "ci", "scripts", ".git*", ".gitlab*", ".github", "Makefile", "renovate.json")
DENIED_PATH_PREFIXES = ("docs/evidence/",)
PRIVATE_KEY_BLOCK = re.compile(rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")

REQUIRED_ROLE_META = ("author", "description", "license", "min_ansible_version", "platforms")
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_denied(name: str) -> str | None:
    if any(name.startswith(prefix) for prefix in DENIED_PATH_PREFIXES):
        return f"{name} is under a deny-listed path"
    parts = name.split("/")
    for pattern in DENIED_TOP_LEVEL:
        if fnmatch.fnmatchcase(parts[0], pattern):
            return f"{name} is repository plumbing ({pattern!r}) and must stay out of the artifact"
    for part in parts:
        for pattern in DENIED_ANYWHERE:
            if fnmatch.fnmatchcase(part, pattern):
                return f"{name} matches deny-list pattern {pattern!r}"
    return None


def check_contents(names: list[str]) -> list[str]:
    problems = []
    for name in names:
        top = name.split("/", 1)[0]
        is_dir_member = "/" in name
        if is_dir_member and top not in ALLOWED_TOP_LEVEL_DIRS:
            problems.append(f"unexpected top-level directory {top!r} (member {name})")
        elif not is_dir_member and top not in ALLOWED_TOP_LEVEL_FILES | ALLOWED_TOP_LEVEL_DIRS:
            problems.append(f"unexpected top-level file {top!r}")
        reason = is_denied(name)
        if reason:
            problems.append(reason)
    return problems


def check_files_json(archive: tarfile.TarFile) -> list[str]:
    problems = []
    members = {m.name: m for m in archive.getmembers() if m.isfile() or m.isdir()}
    try:
        listing = json.load(archive.extractfile("FILES.json"))  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError) as error:
        return [f"FILES.json is missing or unreadable: {error}"]
    listed = {}
    for entry in listing.get("files", []):
        listed[entry["name"]] = entry
    for name, entry in sorted(listed.items()):
        if name == ".":
            continue
        member = members.get(name)
        if member is None:
            problems.append(f"FILES.json lists {name} but the archive does not contain it")
            continue
        if entry.get("ftype") == "file":
            body = archive.extractfile(member)
            data = body.read() if body else b""
            if sha256_bytes(data) != entry.get("chksum_sha256"):
                problems.append(f"{name} does not match its FILES.json digest")
            if PRIVATE_KEY_BLOCK.search(data):
                problems.append(f"{name} contains a private key block")
    for name, member in members.items():
        if name in ("FILES.json", "MANIFEST.json"):
            continue
        if name not in listed and member.isfile():
            problems.append(f"{name} is in the archive but not in FILES.json")
    return problems


def check_metadata(archive: tarfile.TarFile, galaxy: dict, changelog: str | None, release: bool) -> list[str]:
    problems = []
    try:
        manifest = json.load(archive.extractfile("MANIFEST.json"))  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError) as error:
        return [f"MANIFEST.json is missing or unreadable: {error}"]
    info = manifest.get("collection_info", {})
    for key in ("namespace", "name", "version"):
        if info.get(key) != galaxy.get(key):
            problems.append(f"MANIFEST {key} {info.get(key)!r} differs from galaxy.yml {galaxy.get(key)!r}")
    if not SEMVER.match(str(info.get("version", ""))):
        problems.append(f"collection version {info.get('version')!r} is not semantic")
    if sorted(info.get("license", [])) != sorted(galaxy.get("license", [])):
        problems.append("MANIFEST license differs from galaxy.yml")
    if (info.get("dependencies") or {}) != (galaxy.get("dependencies") or {}):
        problems.append("MANIFEST dependencies differ from galaxy.yml")
    for dependency, constraint in (info.get("dependencies") or {}).items():
        if not re.match(r"^[a-z0-9_]+\.[a-z0-9_]+$", dependency):
            problems.append(f"dependency {dependency!r} is not namespace.name")
        if not str(constraint).strip() or str(constraint).strip() == "*":
            problems.append(f"dependency {dependency} has no version constraint")

    role_dirs = sorted(
        {m.name.split("/")[1] for m in archive.getmembers() if m.name.startswith("roles/") and m.name.count("/") >= 2}
    )
    if not role_dirs:
        problems.append("the artifact contains no roles")
    for role in role_dirs:
        try:
            meta = yaml.safe_load(archive.extractfile(f"roles/{role}/meta/main.yml"))  # type: ignore[arg-type]
        except (KeyError, TypeError, yaml.YAMLError) as error:
            problems.append(f"role {role}: meta/main.yml is missing or invalid: {error}")
            continue
        galaxy_info = (meta or {}).get("galaxy_info") or {}
        for key in REQUIRED_ROLE_META:
            if not galaxy_info.get(key):
                problems.append(f"role {role}: galaxy_info.{key} is missing")

    version = str(info.get("version", ""))
    if changelog is None:
        problems.append("CHANGELOG.md is missing from the source tree")
    else:
        if not re.search(r"^## \[Unreleased\]", changelog, re.M):
            problems.append("CHANGELOG.md has no [Unreleased] section")
        section = re.search(rf"^## \[{re.escape(version)}\][^\n]*", changelog, re.M)
        if release and not section:
            problems.append(f"CHANGELOG.md has no section for {version}")
        elif release and "planned" in section.group(0).lower():
            problems.append(f"CHANGELOG.md still marks {version} as planned")
    return problems


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, check=False, **kwargs)


def rebuild_digest(source: Path, work: Path) -> tuple[str | None, str]:
    """Build the collection a second time from a clean copy and return its digest."""
    copy = work / "source"
    shutil.copytree(
        source,
        copy,
        ignore=shutil.ignore_patterns(".git", ".ci-venv", ".venv", "*.tar.gz", "__pycache__", ".pytest_cache"),
    )
    out = work / "rebuild"
    out.mkdir()
    env = {**os.environ, "SOURCE_DATE_EPOCH": os.environ.get("SOURCE_DATE_EPOCH", "0")}
    build = run(["ansible-galaxy", "collection", "build", "--force", "--output-path", str(out)], cwd=copy, env=env)
    if build.returncode != 0:
        return None, build.stdout + build.stderr
    archives = sorted(out.glob("*.tar.gz"))
    if len(archives) != 1:
        return None, f"expected one rebuilt archive, found {len(archives)}"
    normalizer = Path(__file__).with_name("normalize_collection_artifact.py")
    normalized = run([sys.executable, str(normalizer), str(archives[0])], env=env)
    if normalized.returncode != 0:
        return None, normalized.stdout + normalized.stderr
    return sha256_file(archives[0]), ""


def install_check(archive: Path, roles: list[str], namespace: str, name: str, requirements: Path | None, work: Path) -> list[str]:
    """Install into an empty collections path and let Ansible resolve every role."""
    problems = []
    collections = work / "collections"
    collections.mkdir()
    # Point Ansible at the empty path only; otherwise galaxy reports "nothing to do"
    # when a copy of the collection is already installed in the user's own paths.
    install_env = {**os.environ, "ANSIBLE_COLLECTIONS_PATH": str(collections), "ANSIBLE_HOME": str(work / "ansible-home")}
    if requirements and requirements.is_file():
        pre = run(["ansible-galaxy", "collection", "install", "-r", str(requirements), "-p", str(collections), "--force"], env=install_env)
        if pre.returncode != 0:
            return [f"could not install the pinned dependencies: {(pre.stdout + pre.stderr)[-400:]}"]
    install = run(["ansible-galaxy", "collection", "install", str(archive), "-p", str(collections), "--no-deps", "--force"], env=install_env)
    if install.returncode != 0:
        return [f"ansible-galaxy could not install the artifact: {(install.stdout + install.stderr)[-400:]}"]
    play = work / "controller-check.yml"
    play.write_text(
        yaml.safe_dump(
            [
                {
                    "name": "clean controller invocation check",
                    "hosts": "localhost",
                    "connection": "local",
                    "gather_facts": False,
                    "roles": [f"{namespace}.{name}.{role}" for role in roles],
                }
            ]
        )
    )
    env = {
        **os.environ,
        "ANSIBLE_COLLECTIONS_PATH": str(collections),
        "ANSIBLE_HOME": str(work / "ansible-home"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_ROLES_PATH": str(work / "no-roles"),
    }
    for flag in ("--syntax-check", "--list-tasks"):
        result = run(["ansible-playbook", "-i", "localhost,", flag, str(play)], env=env, cwd=work)
        if result.returncode != 0:
            problems.append(f"ansible-playbook {flag} failed against the installed artifact: {(result.stdout + result.stderr)[-600:]}")
        elif flag == "--list-tasks" and "TASK" not in result.stdout and "tasks:" not in result.stdout:
            problems.append("the installed roles listed no tasks")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--source", type=Path, default=Path("."), help="source tree holding galaxy.yml and CHANGELOG.md")
    parser.add_argument("--release", action="store_true", help="require a dated CHANGELOG section for this version")
    parser.add_argument("--rebuild", action="store_true", help="rebuild from a clean copy of --source and compare digests")
    parser.add_argument("--install-check", action="store_true", help="install into an empty collections path and resolve each role")
    parser.add_argument("--requirements", type=Path, default=Path("ci/requirements.yml"), help="pinned dependency collections for --install-check")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--sha256-file", type=Path)
    args = parser.parse_args()

    galaxy = yaml.safe_load((args.source / "galaxy.yml").read_text())
    changelog_path = args.source / "CHANGELOG.md"
    changelog = changelog_path.read_text() if changelog_path.is_file() else None
    digest = sha256_file(args.archive)
    results: dict[str, list[str]] = {}

    with tarfile.open(args.archive, "r:gz") as archive:
        names = sorted(m.name for m in archive.getmembers() if m.name != ".")
        results["contents"] = check_contents(names)
        results["integrity"] = check_files_json(archive)
        results["metadata"] = check_metadata(archive, galaxy, changelog, args.release)
        roles = sorted(
            {m.name.split("/")[1] for m in archive.getmembers() if m.name.startswith("roles/") and m.name.count("/") >= 2}
        )

    with tempfile.TemporaryDirectory(prefix="collection-verify-") as scratch:
        work = Path(scratch)
        if args.rebuild:
            second, error = rebuild_digest(args.source, work)
            if second is None:
                results["reproducible"] = [f"rebuild failed: {error[-400:]}"]
            elif second != digest:
                results["reproducible"] = [f"a clean rebuild has digest {second}, the artifact has {digest}"]
            else:
                results["reproducible"] = []
        if args.install_check:
            results["clean-controller"] = install_check(
                args.archive, roles, galaxy["namespace"], galaxy["name"], args.requirements, work
            )

    failed = {key: value for key, value in results.items() if value}
    report = {
        "archive": args.archive.name,
        "sha256": digest,
        "members": len(names),
        "version": galaxy["version"],
        "checks": {key: ("fail" if value else "pass") for key, value in results.items()},
        "problems": failed,
    }
    if args.report:
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if args.sha256_file:
        args.sha256_file.write_text(f"{digest}  {args.archive.name}\n")
    for key, value in results.items():
        print(f"{'FAIL' if value else 'ok  '} {key}")
        for problem in value:
            print(f"     - {problem}")
    print(f"sha256 {digest}  {args.archive.name}  ({len(names)} members)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
