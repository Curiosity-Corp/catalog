#!/usr/bin/env python3
"""Release tooling for StreamStack (a developer tool; it is not part of the wheel).

    scripts/release.py prepare [VERSION]   promote "Unreleased", bump __version__, print the tag command
    scripts/release.py check-tag TAG       CI gate: tag == __version__, CHANGELOG section, patch-only policy
    scripts/release.py notes TAG           print the CHANGELOG section of TAG (the GitLab Release notes)
    scripts/release.py upload-assets ...   CI: upload the built files to the project's Generic Package Registry

Versioning rule (owner decision): StreamStack ships 0.0.x PATCH releases. A release whose major or minor differs
from the current version is refused unless the owner approved it and ``--owner-approved-minor`` is passed (in CI
the owner sets ``RELEASE_OWNER_APPROVED_MINOR=true`` on the tag pipeline). The standard library is all this needs.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INIT_FILE = Path("src/streamstack/__init__.py")
CHANGELOG_FILE = Path("CHANGELOG.md")
DEFAULT_BRANCH = "main"
GENERIC_PACKAGE = "streamstack-release"

SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
VERSION_LINE = re.compile(r'^(__version__\s*=\s*")([^"]+)(")', re.MULTILINE)
UNRELEASED_HEADING = re.compile(r"^## \[Unreleased\][ \t]*$", re.MULTILINE)
SECTION_HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\](?: - (?P<date>\d{4}-\d{2}-\d{2}))?[ \t]*$", re.MULTILINE)

Version = tuple[int, int, int]


class ReleaseError(Exception):
    """A release precondition failed; the message says what to do about it."""


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


def parse_version(text: str) -> Version:
    match = SEMVER.match(text.strip())
    if not match:
        raise ReleaseError(f"{text!r} is not a MAJOR.MINOR.PATCH version")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def format_version(version: Version) -> str:
    return ".".join(str(part) for part in version)


def tag_version(tag: str) -> str:
    """The version a ``vX.Y.Z`` tag names."""
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ReleaseError(f"tag {tag!r} is not of the form vX.Y.Z")
    return tag[1:]


def check_bump(current: Version, new: Version, owner_approved_minor: bool) -> None:
    """Allow the same version (already bumped) or a higher patch; anything else needs the owner's flag."""
    if new < current:
        raise ReleaseError(f"{format_version(new)} is older than the current version {format_version(current)}")
    if new[:2] != current[:2] and not owner_approved_minor:
        raise ReleaseError(
            f"{format_version(current)} -> {format_version(new)} is a "
            f"{'major' if new[0] != current[0] else 'minor'} bump. StreamStack ships 0.0.x PATCH releases only; "
            "minor and major versions are set by the owner. If the owner approved this one, "
            "pass --owner-approved-minor (CI: RELEASE_OWNER_APPROVED_MINOR=true)."
        )


def check_policy(version: Version, owner_approved_minor: bool) -> None:
    """The CI form of the rule: with no current version to compare, only 0.0.x passes without the flag."""
    if version[:2] != (0, 0) and not owner_approved_minor:
        raise ReleaseError(
            f"{format_version(version)} is not a 0.0.x patch release. StreamStack ships 0.0.x PATCH releases only; "
            "minor and major versions are set by the owner. If the owner approved this one, "
            "pass --owner-approved-minor (CI: RELEASE_OWNER_APPROVED_MINOR=true)."
        )


def read_version(init_text: str) -> str:
    match = VERSION_LINE.search(init_text)
    if not match:
        raise ReleaseError(f"no __version__ assignment found in {INIT_FILE}")
    return match.group(2)


def write_version(init_text: str, version: str) -> str:
    if not VERSION_LINE.search(init_text):
        raise ReleaseError(f"no __version__ assignment found in {INIT_FILE}")
    return VERSION_LINE.sub(lambda m: f"{m.group(1)}{version}{m.group(3)}", init_text, count=1)


# ---------------------------------------------------------------------------
# CHANGELOG
# ---------------------------------------------------------------------------


def _sections(text: str) -> list[tuple[str, int, int]]:
    """(heading name, start of the heading line, end of its body) for every ``## [..]`` section."""
    matches = list(SECTION_HEADING.finditer(text))
    out = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        out.append((match.group("version"), match.start(), end))
    return out


def has_section(text: str, version: str) -> bool:
    return any(name == version for name, _, _ in _sections(text))


def section_body(text: str, version: str) -> str:
    """The body of ``## [version]`` without its heading; raises when the section is missing or empty."""
    for name, start, end in _sections(text):
        if name == version:
            newline = text.find("\n", start, end)
            heading_end = end if newline == -1 else newline + 1
            body = text[heading_end:end].strip("\n")
            if not body.strip():
                raise ReleaseError(f"CHANGELOG.md has a ## [{version}] section but it is empty")
            return body + "\n"
    raise ReleaseError(f"CHANGELOG.md has no ## [{version}] section")


def unreleased_body(text: str) -> str:
    return section_body(text, "Unreleased")


def promote_unreleased(text: str, version: str, date: str) -> str:
    """Rename the Unreleased section to a dated version heading and leave a fresh empty Unreleased above it."""
    if has_section(text, version):
        raise ReleaseError(f"CHANGELOG.md already has a ## [{version}] section")
    unreleased_body(text)  # raises when Unreleased is missing or has nothing in it
    if len(UNRELEASED_HEADING.findall(text)) != 1:
        raise ReleaseError("CHANGELOG.md must have exactly one ## [Unreleased] heading")
    return UNRELEASED_HEADING.sub(f"## [Unreleased]\n\n## [{version}] - {date}", text, count=1)


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def git_ok(root: Path, *args: str) -> bool:
    """Whether a git command succeeds (for questions whose answer is the exit status)."""
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False).returncode == 0


def check_tree(root: Path, allow_branch: bool, version: str) -> None:
    dirty = git(root, "status", "--porcelain")
    if dirty:
        raise ReleaseError("the working tree is not clean; commit or stash first:\n" + dirty)
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch != DEFAULT_BRANCH and not allow_branch:
        raise ReleaseError(f"on branch {branch!r}; release from {DEFAULT_BRANCH} (or pass --allow-branch)")
    upstream = f"origin/{DEFAULT_BRANCH}"
    if git_ok(root, "rev-parse", "--verify", "-q", upstream):
        behind = git(root, "rev-list", "--count", f"HEAD..{upstream}")
        if behind != "0":
            raise ReleaseError(f"HEAD is {behind} commit(s) behind {upstream}; pull first")
    if git(root, "tag", "--list", f"v{version}"):
        raise ReleaseError(f"tag v{version} already exists")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def default_target(current: Version, changelog: str) -> Version:
    """The version being prepared: the current one while it has no CHANGELOG section yet, else the next patch."""
    if not has_section(changelog, format_version(current)):
        return current
    return (current[0], current[1], current[2] + 1)


def tag_commands(version: str) -> list[str]:
    return [
        f'git commit -am "chore(release): {version}"',
        "# merge that commit to main through its merge request, then on the merged main:",
        f'git tag -a v{version} -m "StreamStack {version}"',
        f"git push origin v{version}",
    ]


def cmd_prepare(args: argparse.Namespace) -> int:
    root = Path(args.root)
    init_path, changelog_path = root / INIT_FILE, root / CHANGELOG_FILE
    init_text, changelog = init_path.read_text(encoding="utf-8"), changelog_path.read_text(encoding="utf-8")
    current = parse_version(read_version(init_text))
    target = parse_version(args.version) if args.version else default_target(current, changelog)
    version = format_version(target)
    check_bump(current, target, args.owner_approved_minor)
    if not args.allow_dirty:
        check_tree(root, args.allow_branch, version)
    date = args.date or dt.date.today().isoformat()
    try:
        dt.date.fromisoformat(date)
    except ValueError as exc:
        raise ReleaseError(f"--date {date!r} is not YYYY-MM-DD") from exc
    new_changelog = promote_unreleased(changelog, version, date)
    new_init = write_version(init_text, version)
    if args.dry_run:
        print(f"dry run: would release {version} ({date}); {format_version(current)} -> {version}")
    else:
        changelog_path.write_text(new_changelog, encoding="utf-8")
        if new_init != init_text:
            init_path.write_text(new_init, encoding="utf-8")
        print(
            f"prepared {version} ({date}): CHANGELOG.md promoted"
            + ("" if new_init != init_text else ", __version__ already current")
        )
    print("next:")
    for line in tag_commands(version):
        print(f"  {line}")
    return 0


def cmd_check_tag(args: argparse.Namespace) -> int:
    root = Path(args.root)
    version = tag_version(args.tag)
    in_tree = read_version((root / INIT_FILE).read_text(encoding="utf-8"))
    if in_tree != version:
        raise ReleaseError(f"tag {args.tag} names {version} but streamstack.__version__ is {in_tree}")
    check_policy(parse_version(version), args.owner_approved_minor)
    section_body((root / CHANGELOG_FILE).read_text(encoding="utf-8"), version)
    print(f"release {version}: tag, __version__ and CHANGELOG agree")
    return 0


def cmd_notes(args: argparse.Namespace) -> int:
    version = tag_version(args.tag)
    sys.stdout.write(section_body((Path(args.root) / CHANGELOG_FILE).read_text(encoding="utf-8"), version))
    return 0


def upload_url(api: str, project: str, version: str, name: str) -> str:
    quoted = urllib.parse.quote(name, safe="")
    base = f"{api.rstrip('/')}/projects/{project}/packages/generic/{GENERIC_PACKAGE}/{version}"
    return f"{base}/{quoted}?select=package_file"


def upload_file(url: str, path: Path, token: str, attempts: int = 3) -> None:
    data = path.read_bytes()
    last = ""
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            time.sleep(2**attempt)
        request = urllib.request.Request(url, data=data, method="PUT", headers={"JOB-TOKEN": token})
        try:
            with urllib.request.urlopen(request, timeout=120):
                return
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
            if exc.code == 400 and "already" in exc.read().decode("utf-8", "replace").lower():
                return  # idempotent: the file is already published under this version
            if exc.code < 500 and exc.code != 429:
                break
        except urllib.error.URLError as exc:
            last = str(exc.reason)
    raise ReleaseError(f"uploading {path.name} failed: {last}")


def cmd_upload_assets(args: argparse.Namespace) -> int:
    token = os.environ.get("CI_JOB_TOKEN", "")
    api, project = os.environ.get("CI_API_V4_URL", ""), os.environ.get("CI_PROJECT_ID", "")
    if not (token and api and project):
        raise ReleaseError("CI_JOB_TOKEN, CI_API_V4_URL and CI_PROJECT_ID must be set (this runs in CI)")
    version = tag_version(args.tag)
    files = sorted(p for p in Path(args.dist).iterdir() if p.is_file())
    if not any(p.suffix == ".whl" for p in files) or not any(p.name.endswith(".tar.gz") for p in files):
        raise ReleaseError(f"{args.dist} needs a wheel and an sdist")
    for path in files:
        upload_file(upload_url(api, project, version, path.name), path, token)
        print(f"uploaded {path.name} to {GENERIC_PACKAGE}/{version}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="release.py", description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="promote Unreleased, bump __version__, print the tag command")
    prepare.add_argument("version", nargs="?", help="default: the unreleased current version, else the next patch")
    prepare.add_argument("--owner-approved-minor", action="store_true", help="the owner approved a minor/major bump")
    prepare.add_argument("--date", help="release date, YYYY-MM-DD (default: today)")
    prepare.add_argument("--dry-run", action="store_true", help="check everything, change nothing")
    prepare.add_argument(
        "--allow-branch", action="store_true", help=f"release from a branch other than {DEFAULT_BRANCH}"
    )
    prepare.add_argument("--allow-dirty", action="store_true", help="skip the working tree checks")
    prepare.set_defaults(func=cmd_prepare)

    check_tag = sub.add_parser("check-tag", help="CI gate for a vX.Y.Z tag")
    check_tag.add_argument("tag")
    check_tag.add_argument(
        "--owner-approved-minor",
        action="store_true",
        default=os.environ.get("RELEASE_OWNER_APPROVED_MINOR", "").lower() == "true",
    )
    check_tag.set_defaults(func=cmd_check_tag)

    notes = sub.add_parser("notes", help="print the CHANGELOG section of a tag")
    notes.add_argument("tag")
    notes.set_defaults(func=cmd_notes)

    upload = sub.add_parser("upload-assets", help="CI: upload dist/ to the Generic Package Registry")
    upload.add_argument("tag")
    upload.add_argument("--dist", default="dist")
    upload.set_defaults(func=cmd_upload_assets)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ReleaseError as exc:
        print(f"release.py: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"release.py: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
