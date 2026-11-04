"""No tracked file holds a PEM private key outside the allowlisted fixture directories (issue #82).

MediaMTX writes a self-signed `auto.key` / `auto.crt` pair into its working directory; one run of the ingest e2e from
the repository root left the pair behind and it was merged, which made the v0.0.27 tag pipeline fail in gitleaks.
gitleaks only ever blocked on tags (see test_ci_tag_only_jobs.py); this test is the in-repo gate that holds on every
merge request, whatever the pipeline template does.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# The header is built from parts so that this file does not match its own pattern.
PRIVATE_KEY = re.compile(rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE " + rb"KEY(?: BLOCK)?-----")
# Directories whose files are deliberate, obviously fake key fixtures. Empty today: add a path here, with a reason,
# only for a key that has to be committed.
ALLOWED_PREFIXES: tuple[str, ...] = ()
MAX_BYTES = 2_000_000


def tracked_files() -> list[str]:
    try:
        done = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"git ls-files is not available here ({exc})")
    return [x for x in done.stdout.decode().split("\0") if x]


def test_no_tracked_file_holds_a_private_key() -> None:
    found: list[str] = []
    for name in tracked_files():
        if name.startswith(ALLOWED_PREFIXES):
            continue
        path = ROOT / name
        try:
            if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue  # deleted in a working tree that has not committed the removal yet
        if PRIVATE_KEY.search(data):
            found.append(name)
    assert not found, f"PEM private key committed in: {found} (a test key belongs in a tmp_path, never in the tree)"


def test_the_pattern_matches_real_headers_and_not_public_material() -> None:
    for kind in ("EC ", "RSA ", "", "OPENSSH ", "ENCRYPTED "):
        assert PRIVATE_KEY.search(f"-----BEGIN {kind}PRIVATE KEY-----".encode())
    assert not PRIVATE_KEY.search(b"-----BEGIN CERTIFICATE-----")
    assert not PRIVATE_KEY.search(b"-----BEGIN PUBLIC KEY-----")
