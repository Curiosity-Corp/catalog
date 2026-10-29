#!/usr/bin/env python3
"""StreamStack's signed apt repository: index, sign, verify, pull and publish (a developer/CI tool, not in the wheel).

    scripts/apt_repo.py index   DIR              write Packages, Packages.gz and Release for the .deb files in DIR
    scripts/apt_repo.py sign    DIR --key FPR    write InRelease (clearsigned) and the detached Release.gpg with gpg
    scripts/apt_repo.py verify  DIR --keyring K  check what apt checks: signatures, Release, Packages, every .deb
    scripts/apt_repo.py pull    DIR --base URL   fetch the .deb files the published repository already lists
    scripts/apt_repo.py publish DIR --base URL   upload the repository's files (new .deb files only) to the registry
    scripts/apt_repo.py put --base URL FILE...   upload plain files (a release's package files) to a registry URL

The repository is a *flat* apt repository: one directory holding the .deb files and the index, served from the
project's Generic Package Registry at a stable path (``.../packages/generic/streamstack-apt/stable/``), so a
workstation's apt line never changes while the files behind it do. Every released .deb stays in the repository, so
``apt install streamstack=<older>`` is the rollback. The repository key is the OpenPGP key
`streamstack-apt-signing-key` in the org's Azure Key Vault (named by the KEY_VAULT_NAME CI variable); the CI signing
Job imports it, signs, and discards it (docs/release.md). Standard library only; ``dpkg-deb`` reads control files,
``gpg`` signs, ``gpgv`` (apt's own verifier) verifies.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ORIGIN = "StreamStack"
SUITE = "stable"
ARCHITECTURES = "all"
APT_PACKAGE = "streamstack-apt"
INDEX_FILES = ("Packages", "Packages.gz", "Release", "InRelease", "Release.gpg")
HASHES = (("MD5Sum", "md5"), ("SHA1", "sha1"), ("SHA256", "sha256"))
UPLOAD_TIMEOUT = 120


class AptError(Exception):
    """A repository step failed; the message says what to do about it."""


@dataclass(frozen=True)
class DebEntry:
    name: str
    control: dict[str, str]
    size: int
    sha256: str
    md5: str
    sha1: str


def _digest(data: bytes, algorithm: str) -> str:
    return hashlib.new(algorithm, data, usedforsecurity=algorithm == "sha256").hexdigest()


def _run(argv: Sequence[str], **kwargs: str) -> str:
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, check=False, **kwargs)
    except FileNotFoundError as exc:
        raise AptError(f"{argv[0]} is required but not installed") from exc
    if done.returncode != 0:
        raise AptError(f"{' '.join(argv[:2])} failed: {(done.stderr or done.stdout).strip()}")
    return done.stdout


def parse_control(text: str) -> dict[str, str]:
    """A control stanza as {field: value}; continuation lines (a leading space or tab) stay part of the value."""
    fields: dict[str, str] = {}
    last = ""
    for line in text.splitlines():
        if not line.strip():
            continue
        if line[0] in " \t" and last:
            fields[last] += "\n" + line
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise AptError(f"bad control line {line!r}")
        last = key.strip()
        fields[last] = value.strip()
    return fields


def read_deb(path: Path) -> DebEntry:
    control = parse_control(_run(["dpkg-deb", "-f", str(path)]))
    for field in ("Package", "Version", "Architecture"):
        if field not in control:
            raise AptError(f"{path.name} has no {field} field")
    data = path.read_bytes()
    return DebEntry(path.name, control, len(data), _digest(data, "sha256"), _digest(data, "md5"), _digest(data, "sha1"))


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) if part.isdigit() else 0 for part in re.split(r"[.+~:-]", version))


def packages_text(entries: Sequence[DebEntry]) -> str:
    """The Packages index: control fields, then Filename, Size and the three checksums; newest version first."""
    ordered = sorted(entries, key=lambda e: (e.control["Package"], _version_key(e.control["Version"])), reverse=True)
    stanzas = []
    for entry in ordered:
        lines = [f"{key}: {value}" for key, value in entry.control.items()]
        lines += [
            f"Filename: ./{entry.name}",
            f"Size: {entry.size}",
            f"MD5sum: {entry.md5}",
            f"SHA1: {entry.sha1}",
            f"SHA256: {entry.sha256}",
        ]
        stanzas.append("\n".join(lines))
    return "\n\n".join(stanzas) + "\n"


def release_text(files: dict[str, bytes], date: dt.datetime) -> str:
    """A flat-repository Release file listing the checksums of the index files."""
    lines = [
        f"Origin: {ORIGIN}",
        f"Label: {ORIGIN}",
        f"Suite: {SUITE}",
        f"Codename: {SUITE}",
        f"Date: {date.astimezone(dt.UTC).strftime('%a, %d %b %Y %H:%M:%S UTC')}",
        f"Architectures: {ARCHITECTURES}",
        "Description: StreamStack releases (signed; rollback: apt install streamstack=<older version>)",
    ]
    for field, algorithm in HASHES:
        lines.append(f"{field}:")
        lines += [f" {_digest(data, algorithm)} {len(data):>16} {name}" for name, data in sorted(files.items())]
    return "\n".join(lines) + "\n"


def cmd_index(args: argparse.Namespace) -> int:
    root = Path(args.dir)
    debs = sorted(root.glob("*.deb"))
    if not debs:
        raise AptError(f"no .deb files in {root}")
    entries = [read_deb(p) for p in debs]
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        key = (entry.control["Package"], entry.control["Version"])
        if key in seen:
            raise AptError(f"{key[0]} {key[1]} appears twice in {root}")
        seen.add(key)
    packages = packages_text(entries).encode("utf-8")
    gz = gzip.compress(packages, compresslevel=9, mtime=0)
    (root / "Packages").write_bytes(packages)
    (root / "Packages.gz").write_bytes(gz)
    date = dt.datetime.fromisoformat(args.date) if args.date else dt.datetime.now(dt.UTC)
    (root / "Release").write_text(
        release_text({"Packages": packages, "Packages.gz": gz}, date.replace(tzinfo=date.tzinfo or dt.UTC)),
        encoding="utf-8",
    )
    for stale in ("InRelease", "Release.gpg"):
        (root / stale).unlink(missing_ok=True)  # signatures of the previous Release must never outlive it
    print(f"indexed {len(entries)} package(s) in {root}")
    return 0


def cmd_sign(args: argparse.Namespace) -> int:
    root = Path(args.dir)
    release = root / "Release"
    if not release.is_file():
        raise AptError(f"{release} is missing: run `index` first")
    base = ["gpg", "--batch", "--yes", "--pinentry-mode", "loopback", "--local-user", args.key]
    if args.passphrase_file:
        base += ["--passphrase-file", args.passphrase_file]
    else:
        base += ["--passphrase", ""]
    _run([*base, "--clearsign", "--output", str(root / "InRelease"), str(release)])
    _run([*base, "--armor", "--detach-sign", "--output", str(root / "Release.gpg"), str(release)])
    print(f"signed Release with key {args.key}")
    return 0


def _gpgv(keyring: Path, *files: Path) -> None:
    _run(["gpgv", "--keyring", str(keyring), *map(str, files)])


def verify_repository(root: Path, keyring: Path) -> int:
    """What apt checks, in apt's order; returns the number of packages verified, raises AptError otherwise."""
    for name in ("InRelease", "Release.gpg", "Release", "Packages", "Packages.gz"):
        if not (root / name).is_file():
            raise AptError(f"{root / name} is missing")
    with tempfile.TemporaryDirectory(prefix="apt-verify-") as tmp:
        keys = Path(tmp) / "keys.gpg"
        keys.write_bytes(_dearmor(keyring))
        _gpgv(keys, root / "Release.gpg", root / "Release")
        _gpgv(keys, root / "InRelease")
        cleartext = Path(tmp) / "inrelease.txt"
        _run(
            [
                "gpg",
                "--batch",
                "--no-default-keyring",
                "--keyring",
                str(keys),
                "--output",
                str(cleartext),
                "--decrypt",
                str(root / "InRelease"),
            ]
        )
        if _release_fields(cleartext.read_text("utf-8")) != _release_fields((root / "Release").read_text("utf-8")):
            raise AptError("InRelease and Release disagree")
    release = (root / "Release").read_text("utf-8")
    for name in ("Packages", "Packages.gz"):
        data = (root / name).read_bytes()
        want = re.search(rf"^ ([0-9a-f]{{64}}) +{len(data)} {re.escape(name)}$", release, re.MULTILINE)
        if not want or want.group(1) != _digest(data, "sha256"):
            raise AptError(f"{name} does not match the signed Release")
    count = 0
    packages = (root / "Packages").read_text("utf-8")
    for stanza in filter(str.strip, packages.split("\n\n")):
        fields = parse_control(stanza)
        deb = root / fields["Filename"].removeprefix("./")
        data = deb.read_bytes()
        if len(data) != int(fields["Size"]) or _digest(data, "sha256") != fields["SHA256"]:
            raise AptError(f"{deb.name} does not match the signed Packages index")
        count += 1
    if count == 0:
        raise AptError("the repository lists no packages")
    return count


def _release_fields(text: str) -> dict[str, str]:
    return parse_control(
        text.split("\n\n", 1)[0].replace("-----BEGIN PGP SIGNED MESSAGE-----", "").replace("Hash: SHA512", "")
    )


def _dearmor(path: Path) -> bytes:
    data = path.read_bytes()
    if b"BEGIN PGP PUBLIC KEY BLOCK" not in data:
        return data
    return _run_bytes(["gpg", "--batch", "--dearmor"], data)


def _run_bytes(argv: Sequence[str], data: bytes) -> bytes:
    done = subprocess.run(list(argv), input=data, capture_output=True, check=False)
    if done.returncode != 0:
        raise AptError(f"{argv[0]} failed: {done.stderr.decode('utf-8', 'replace').strip()}")
    return done.stdout


def cmd_verify(args: argparse.Namespace) -> int:
    count = verify_repository(Path(args.dir), Path(args.keyring))
    print(f"repository verified: signatures, Release, Packages and {count} package file(s)")
    return 0


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def _request(url: str, token: tuple[str, str], data: bytes | None = None, method: str = "GET") -> bytes:
    request = urllib.request.Request(url, data=data, method=method, headers={token[0]: token[1]})
    last = ""
    for attempt in range(1, 4):
        if attempt > 1:
            time.sleep(2**attempt)
        try:
            with urllib.request.urlopen(request, timeout=UPLOAD_TIMEOUT) as response:
                body: bytes = response.read()
                return body
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and method == "GET":
                raise FileNotFoundError(url) from exc
            last = f"HTTP {exc.code}"
            if exc.code < 500 and exc.code != 429:
                break
        except urllib.error.URLError as exc:
            last = str(exc.reason)
    raise AptError(f"{method} {url} failed: {last}")


def _token() -> tuple[str, str]:
    job = os.environ.get("CI_JOB_TOKEN", "").strip()
    if job:
        return "JOB-TOKEN", job
    personal = os.environ.get("GITLAB_TOKEN", "").strip()
    if personal:
        return "PRIVATE-TOKEN", personal
    raise AptError("CI_JOB_TOKEN (CI) or GITLAB_TOKEN is required")


def _base(args: argparse.Namespace) -> str:
    base = args.base or os.environ.get("STREAMSTACK_APT_BASE", "")
    if not base:
        api, project = os.environ.get("CI_API_V4_URL", ""), os.environ.get("CI_PROJECT_ID", "")
        if not (api and project):
            raise AptError("pass --base URL (CI: CI_API_V4_URL and CI_PROJECT_ID give it)")
        base = f"{api.rstrip('/')}/projects/{project}/packages/generic/{APT_PACKAGE}/{SUITE}"
    return base.rstrip("/")


def published_packages(base: str, token: tuple[str, str]) -> list[dict[str, str]]:
    """The stanzas of the published Packages index; empty when the repository does not exist yet."""
    try:
        text = _request(f"{base}/Packages", token).decode("utf-8")
    except FileNotFoundError:
        return []
    return [parse_control(stanza) for stanza in text.split("\n\n") if stanza.strip()]


def cmd_pull(args: argparse.Namespace) -> int:
    root, token, base = Path(args.dir), _token(), _base(args)
    root.mkdir(parents=True, exist_ok=True)
    fetched = 0
    for fields in published_packages(base, token):
        name = fields["Filename"].removeprefix("./")
        target = root / name
        if target.exists():
            continue
        data = _request(f"{base}/{urllib.parse.quote(name)}", token)
        if _digest(data, "sha256") != fields["SHA256"]:
            raise AptError(f"{name} from the registry does not match its published checksum")
        target.write_bytes(data)
        fetched += 1
    print(f"pulled {fetched} published package file(s)")
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    root, token, base = Path(args.dir), _token(), _base(args)
    for name in INDEX_FILES:
        if not (root / name).is_file():
            raise AptError(f"{root / name} is missing: index and sign before publishing")
    already = {f["Filename"].removeprefix("./"): f["SHA256"] for f in published_packages(base, token)}
    uploads = []
    for deb in sorted(root.glob("*.deb")):
        digest = _digest(deb.read_bytes(), "sha256")
        if already.get(deb.name) == digest:
            continue  # published with these exact bytes: never upload a second copy
        if deb.name in already:
            raise AptError(f"{deb.name} is published with different bytes; a version is never overwritten")
        uploads.append(deb)
    # .deb files first and the signed index last: a client never sees an index that lists a file that is not there yet
    uploads += [root / name for name in ("Packages", "Packages.gz", "Release", "Release.gpg", "InRelease")]
    extra = [Path(p) for p in args.extra or []]
    for path in [*extra, *uploads]:
        _request(f"{base}/{urllib.parse.quote(path.name)}", token, data=path.read_bytes(), method="PUT")
        print(f"uploaded {path.name}")
    return 0


def cmd_put(args: argparse.Namespace) -> int:
    """Upload plain files to a Generic Package Registry URL (the package files of a release)."""
    token, base = _token(), args.base.rstrip("/")
    for name in args.files:
        path = Path(name)
        _request(f"{base}/{urllib.parse.quote(path.name)}", token, data=path.read_bytes(), method="PUT")
        print(f"uploaded {path.name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="apt_repo.py", description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    index = sub.add_parser("index", help="write Packages, Packages.gz and Release")
    index.add_argument("dir")
    index.add_argument("--date", help="Release Date, ISO 8601 (default: now; for tests)")
    index.set_defaults(func=cmd_index)
    sign = sub.add_parser("sign", help="write InRelease and Release.gpg with gpg")
    sign.add_argument("dir")
    sign.add_argument("--key", required=True, help="key id or fingerprint in the current GNUPGHOME")
    sign.add_argument("--passphrase-file", help="file holding the key's passphrase (default: an unprotected key)")
    sign.set_defaults(func=cmd_sign)
    verify = sub.add_parser("verify", help="verify signatures and checksums the way apt does")
    verify.add_argument("dir")
    verify.add_argument("--keyring", required=True, help="the public key (armored or binary)")
    verify.set_defaults(func=cmd_verify)
    for name, func, text in (
        ("pull", cmd_pull, "download the published .deb files into DIR"),
        ("publish", cmd_publish, "upload the repository to the Generic Package Registry"),
    ):
        step = sub.add_parser(name, help=text)
        step.add_argument("dir")
        step.add_argument("--base", help="registry URL of the repository (default: from the CI variables)")
        step.set_defaults(func=func)
        if name == "publish":
            step.add_argument("--extra", nargs="*", help="further files to upload beside the index (the public key)")
    put = sub.add_parser("put", help="upload files to a Generic Package Registry URL")
    put.add_argument("--base", required=True, help="registry URL (package and version) to upload to")
    put.add_argument("files", nargs="+")
    put.set_defaults(func=cmd_put)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (AptError, OSError) as exc:
        print(f"apt_repo.py: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
