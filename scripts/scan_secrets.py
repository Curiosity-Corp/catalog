#!/usr/bin/env python3
"""Small, dependency-free credential scanner for the files Git tracks.

It complements the GitLab secret-detection job: it runs locally and in the
policy tests with no network and no analyzer image, and it only flags
high-confidence shapes (private key blocks, well-known token prefixes, a
password inside a URL). A line that carries ``secret-scan: allow`` is skipped,
and a line holding only an obviously fake value (``example``, ``changeme``,
``<placeholder>``, ``REDACTED``) is skipped.

    python scripts/scan_secrets.py [PATH ...]      # default: git ls-files, or every file under
    the current directory when git is not installed

Findings print as ``path:line: rule`` and never include the matched text, so a
report cannot leak the value it found. Exit status 1 when anything is found.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

RULES = {
    "private-key-block": re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
    "github-token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "gitlab-token": re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    "aws-access-key-id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "slack-token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "password-in-url": re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:(?!//)[^/\s@{}$<]{4,}@[A-Za-z0-9.-]+"),
}
FAKE = re.compile(r"example|changeme|placeholder|redacted|<[a-z_ -]+>|\bx{4,}\b|:(?:pass|password|secret)@|your[-_ ]?(?:token|password|key)", re.I)
ALLOW = "secret-scan: allow"
MAX_BYTES = 2_000_000
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico", ".gz", ".tar", ".zip", ".woff", ".woff2", ".ttf", ".pdf", ".deb"}


def scan_text(text: str) -> list[tuple[int, str]]:
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        if ALLOW in line:
            continue
        for rule, pattern in RULES.items():
            match = pattern.search(line)
            if match and not (rule != "private-key-block" and FAKE.search(match.group(0))):
                found.append((number, rule))
    return found


SKIP_DIRS = {".git", ".ci-venv", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".cache", "evidence"}


def tracked(root: Path) -> list[Path]:
    """Files Git tracks; a plain directory walk when git is not installed (CI image)."""
    try:
        output = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout
    except (FileNotFoundError, subprocess.CalledProcessError):
        return sorted(
            path
            for path in root.rglob("*")
            if path.is_file() and not SKIP_DIRS.intersection(path.relative_to(root).parts)
        )
    return [root / name for name in output.decode().split("\0") if name]


def main(argv: list[str]) -> int:
    root = Path(".")
    paths = [Path(arg) for arg in argv] if argv else tracked(root)
    total = 0
    for path in paths:
        if path.suffix.lower() in SKIP_SUFFIXES or not path.is_file() or path.stat().st_size > MAX_BYTES:
            continue
        data = path.read_bytes()
        if b"\0" in data[:4096]:
            continue
        for number, rule in scan_text(data.decode("utf-8", errors="replace")):
            print(f"{path}:{number}: {rule}")
            total += 1
    if total:
        print(f"{total} finding(s); values are not printed. Add `{ALLOW}` only for a reviewed synthetic fixture.")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
