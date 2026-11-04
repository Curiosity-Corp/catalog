#!/usr/bin/env python3
"""List candidate external contributors from git history, for a human to review.

The monthly review in docs/community/contributor-cohorts.md asks how many unique
external human contributors made substantive changes in the last 12 months. A
program cannot decide "substantive" or "human", so this one does not produce a
count to report. It produces a worksheet:

* every author in the window, grouped by identity (git's .mailmap is honoured);
* bots, AI agents and maintainers/internal accounts listed separately and excluded;
* for each remaining person, their commits split into code, tests, docs and
  other, and how many were tiny (two changed lines or fewer), which is a signal
  to look at, not a verdict.

Only a person who has read the changes may move a name from "candidates" to the
evidence log. Nothing here contacts a service; it reads `git log`.

    python3 scripts/contributor_cohort.py --internal-file ~/maintainers.txt
    python3 scripts/contributor_cohort.py --as-of 2026-12-01 --format json

The internal file lists one email address or @domain per line (comments start
with #). Keep it outside the repository: it names people.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

BOT = re.compile(
    r"\[bot\]|dependabot|renovate|github-actions|gitlab-bot|project_\d+_bot|^bot$|-bot$|noreply.*bot"
    r"|anthropic|\bclaude\b|copilot|openai|codex|devin",
    re.IGNORECASE,
)
TINY_LINES = 2
CODE_PREFIXES = ("roles/", "playbooks/", "scripts/", "plugins/", "galaxy.yml", "requirements.yml", "ansible.cfg")
SEP = "\x1f"
START = "\x1e"


def category(path: str) -> str:
    if path.startswith("tests/"):
        return "tests"
    if path.startswith("docs/") or path.endswith(".md") or path.startswith("examples/"):
        return "docs"
    if path.startswith(CODE_PREFIXES):
        return "code"
    return "other"


def read_internal(path: Path | None) -> tuple[set[str], set[str]]:
    emails: set[str] = set()
    domains: set[str] = set()
    if path is None:
        return emails, domains
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip().lower()
        if not line:
            continue
        (domains if line.startswith("@") else emails).add(line)
    return emails, domains


def is_bot(name: str, email: str) -> bool:
    return bool(BOT.search(name) or BOT.search(email))


def is_internal(email: str, emails: set[str], domains: set[str]) -> bool:
    email = email.lower()
    return email in emails or any(email.endswith(domain) for domain in domains)


def git_log(repo: Path, since: date, until: date) -> list[dict]:
    fmt = f"{START}%aN{SEP}%aE{SEP}%aI{SEP}%h{SEP}%s"
    out = subprocess.run(
        ["git", "-C", str(repo), "log", "--no-merges", f"--since={since.isoformat()}", f"--until={until.isoformat()}T23:59:59",
         "--numstat", f"--format={fmt}"],
        capture_output=True, text=True, check=True,
    ).stdout
    commits = []
    for block in out.split(START)[1:]:
        head, _, body = block.partition("\n")
        name, email, when, sha, subject = head.split(SEP, 4)
        files = []
        for line in body.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                added = int(parts[0]) if parts[0].isdigit() else 0
                removed = int(parts[1]) if parts[1].isdigit() else 0
                files.append((parts[2], added + removed))
        commits.append({"name": name, "email": email.lower(), "date": when[:10], "sha": sha, "subject": subject, "files": files})
    return commits


def build(commits: list[dict], internal_emails: set[str], internal_domains: set[str]) -> dict:
    people: dict[str, dict] = defaultdict(lambda: {"name": "", "commits": 0, "code": 0, "tests": 0, "docs": 0, "other": 0, "tiny": 0, "first": "", "last": ""})
    bots: dict[str, int] = defaultdict(int)
    internal: dict[str, int] = defaultdict(int)
    for commit in commits:
        email = commit["email"]
        if is_bot(commit["name"], email):
            bots[email] += 1
            continue
        if is_internal(email, internal_emails, internal_domains):
            internal[email] += 1
            continue
        person = people[email]
        person["name"] = commit["name"]
        person["commits"] += 1
        for path, _ in commit["files"]:
            person[category(path)] += 1
        if sum(changed for _, changed in commit["files"]) <= TINY_LINES:
            person["tiny"] += 1
        person["first"] = min(filter(None, [person["first"], commit["date"]]))
        person["last"] = max(person["last"], commit["date"])
    return {
        "candidates": [{"email": email, **data} for email, data in sorted(people.items())],
        "bots_excluded": dict(sorted(bots.items())),
        "internal_excluded": dict(sorted(internal.items())),
    }


def render_text(result: dict, since: date, until: date) -> str:
    lines = [
        f"Contributor worksheet for {since} to {until}",
        "This is a list to review, not a count to report. A person must read each",
        "change and confirm it is substantive and made by a human before it is",
        "recorded as evidence (docs/community/contributor-cohorts.md).",
        "",
        f"Candidate external contributors: {len(result['candidates'])}",
    ]
    for person in result["candidates"]:
        lines.append(
            f"  {person['email']}  {person['commits']} commits ({person['first']} to {person['last']}): "
            f"code {person['code']}, tests {person['tests']}, docs {person['docs']}, other {person['other']}; "
            f"tiny commits {person['tiny']}"
        )
    lines += ["", f"Bots excluded: {len(result['bots_excluded'])}"]
    lines += [f"  {email}  {count} commits" for email, count in result["bots_excluded"].items()]
    lines += ["", f"Maintainer or internal accounts excluded: {len(result['internal_excluded'])}"]
    lines += [f"  {email}  {count} commits" for email, count in result["internal_excluded"].items()]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--months", type=int, default=12, help="rolling window length in months (default 12)")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today(), help="end of the window, YYYY-MM-DD (default today)")
    parser.add_argument("--internal-file", type=Path, help="emails or @domains of maintainers and internal accounts to exclude")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    since = args.as_of - timedelta(days=round(args.months * 30.4375))
    emails, domains = read_internal(args.internal_file)
    result = build(git_log(args.repo, since, args.as_of), emails, domains)
    if args.format == "json":
        print(json.dumps({"window": {"since": since.isoformat(), "until": args.as_of.isoformat()}, "review_required": True, **result}, indent=2))
    else:
        print(render_text(result, since, args.as_of))
    return 0


if __name__ == "__main__":
    sys.exit(main())
