#!/usr/bin/env python3
"""Sweep the whole Git history (every branch, tag and blob) for sensitive material.

Read-only. Run it in a bare mirror or a clone that has every ref fetched:

    git clone --mirror <url> history.git
    python3 scripts/history_audit.py --repo history.git
    python3 scripts/history_audit.py --repo history.git --markers private-markers.txt --paths

What it looks for, in every blob ever committed (text blobs up to 3 MB):

  private-key-block, certificate-block, rfc1918-ip, email-address,
  aws-access-key-id, github-token, gitlab-token, jwt
  marker:<n>  each line of the optional --markers file (a private list of
              organisation, host or project names that must not be public)

and, by path, files whose names look sensitive (key, certificate, env, vault).

It reports COUNTS by default. --paths adds the file paths that matched. It never
prints matched text, so its output is safe to paste into an issue. Marker names
are reported as marker:1, marker:2 ... in file order, so the private list never
appears in output or in this repository: keep the markers file outside the repo.

When `gitleaks` is installed it is also run over all refs and only its rule,
file and commit are shown (never its match text). A clean sweep is evidence, not
a guarantee: treat rotation and the visibility decision as separate human steps
(docs/history-audit.md).
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_BLOB = 3_000_000
PATTERNS = {
    "private-key-block": re.compile(rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY"),
    "certificate-block": re.compile(rb"-----BEGIN CERTIFICATE-----"),
    "rfc1918-ip": re.compile(
        rb"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
    ),
    "email-address": re.compile(rb"\b[A-Za-z0-9._%+-]+@(?!example\.|users\.noreply)[A-Za-z0-9.-]+\.[a-z]{2,}\b"),
    "aws-access-key-id": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    "github-token": re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "gitlab-token": re.compile(rb"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    "jwt": re.compile(rb"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\."),
}
SENSITIVE_PATH = re.compile(
    r"\.(crt|pem|key|p12|pfx|jks|kdbx|env|ovpn|gpg|asc)$|(^|/)(\.env|id_rsa|id_ed25519|credentials|\.netrc|secrets?\.ya?ml)$",
    re.I,
)


def git(repo: str, *args: str, data: bytes | None = None) -> bytes:
    return subprocess.run(["git", "-C", repo, *args], input=data, capture_output=True, check=True).stdout


def load_markers(path: str | None) -> list[str]:
    if not path:
        return []
    return [line.strip() for line in Path(path).read_text().splitlines() if line.strip() and not line.startswith("#")]


def sweep(repo: str, markers: list[str]) -> dict:
    patterns = dict(PATTERNS)
    for index, marker in enumerate(markers, 1):
        patterns[f"marker:{index}"] = re.compile(re.escape(marker.encode()), re.I)
    blob_paths: dict[str, set[str]] = collections.defaultdict(set)
    for line in git(repo, "rev-list", "--objects", "--all").decode(errors="replace").splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2:
            blob_paths[parts[0]].add(parts[1])
    listing = git(repo, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype) %(objectsize)")
    blobs = [(o, int(s)) for o, t, s in (x.split() for x in listing.decode().splitlines()) if t == "blob"]
    hits: dict[str, dict[str, set[str]]] = collections.defaultdict(lambda: collections.defaultdict(set))
    proc = subprocess.Popen(["git", "-C", repo, "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        for oid, size in blobs:
            if size > MAX_BLOB:
                continue
            proc.stdin.write((oid + "\n").encode())
            proc.stdin.flush()
            header = proc.stdout.readline().split()
            data = proc.stdout.read(int(header[2]))
            proc.stdout.read(1)
            if b"\0" in data[:8000]:
                continue
            for name, rx in patterns.items():
                if rx.search(data):
                    for path in blob_paths.get(oid, {"?"}):
                        hits[name][path].add(oid[:10])
    finally:
        proc.stdin.close()
        proc.wait()
    sensitive = sorted({p for paths in blob_paths.values() for p in paths if SENSITIVE_PATH.search(p)})
    refs = git(repo, "for-each-ref", "--format=%(refname)").decode().split()
    return {
        "commits": int(git(repo, "rev-list", "--all", "--count").decode()),
        "refs": len(refs),
        "tags": sum(r.startswith("refs/tags/") for r in refs),
        "blobs": len(blobs),
        "skipped_large_blobs": sum(1 for _, s in blobs if s > MAX_BLOB),
        "categories": {k: {"paths": len(v), "blobs": len({b for s in v.values() for b in s})} for k, v in sorted(hits.items())},
        "paths": {k: sorted(v) for k, v in sorted(hits.items())},
        "sensitive_looking_paths": sensitive,
    }


def run_gitleaks(repo: str) -> list[dict] | None:
    if not shutil.which("gitleaks"):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        subprocess.run(
            ["gitleaks", "git", repo, "--report-format", "json", "--report-path", str(report), "--no-banner", "--redact"],
            capture_output=True,
            check=False,
        )
        try:
            findings = json.loads(report.read_text() or "[]")
        except (OSError, ValueError):
            return []
    return [{"rule": f["RuleID"], "file": f["File"], "commit": f["Commit"][:8], "line": f["StartLine"]} for f in findings]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True, help="path to a bare mirror or clone with all refs")
    parser.add_argument("--markers", help="file with one private name per line (keep it outside this repository)")
    parser.add_argument("--paths", action="store_true", help="also list the matching file paths")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-gitleaks", action="store_true")
    args = parser.parse_args(argv)
    result = sweep(args.repo, load_markers(args.markers))
    if not args.paths:
        result.pop("paths")
        result["sensitive_looking_paths"] = len(result["sensitive_looking_paths"])
    result["gitleaks"] = None if args.no_gitleaks else run_gitleaks(args.repo)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print(f"commits {result['commits']}, refs {result['refs']} (tags {result['tags']}), blobs {result['blobs']}")
    for name, count in result["categories"].items():
        print(f"{name}: {count['paths']} paths, {count['blobs']} blobs")
        if args.paths:
            for path in result["paths"][name]:
                print(f"    {path}")
    print("sensitive-looking file names:", result["sensitive_looking_paths"])
    if result["gitleaks"] is None:
        print("gitleaks: not run")
    else:
        print(f"gitleaks findings: {len(result['gitleaks'])}")
        for f in result["gitleaks"]:
            print(f"    {f['rule']} {f['file']}:{f['line']} {f['commit']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
