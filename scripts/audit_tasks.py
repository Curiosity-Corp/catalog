#!/usr/bin/env python3
"""Static privilege and secret-handling audit of the Ansible roles.

Walks every task file under roles/ (the vendored StreamStack subtree is not
Ansible content and is skipped) and reports, per task, the risky patterns that
issue #24 asks to review:

  exec         command, shell, raw and script tasks (inventory only)
  network      uri and get_url tasks (inventory only)
  secret-nolog a task that handles secret-looking variables without no_log
  secret-mode  a secret-looking destination without a restrictive mode
  curl-pipe    `curl|wget ... | sh` style installers (always a failure)
  no-checksum  get_url without a checksum
  insecure-tls validate_certs false or curl -k (always a failure)
  unpinned     `pip install` without a version, or a git ref of a moving branch
  default-secret  a role default that is a non-empty literal for a secret name

The script reads files only. It never prints the value of a variable, only
file, task name and the rule that fired.

    python3 scripts/audit_tasks.py                 # human summary
    python3 scripts/audit_tasks.py --json          # machine readable
    python3 scripts/audit_tasks.py --update-baseline

tests/test_security_audit.py fails on any "hard" finding and on any non-hard
finding that is not recorded in docs/security-audit-baseline.json, so existing
reviewed items stay visible and new ones need a decision.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "docs" / "security-audit-baseline.json"
HARD = {"curl-pipe", "insecure-tls", "default-secret"}
EXEC = {"command", "shell", "raw", "script"}
NETWORK = {"uri", "get_url"}
MODULE_PREFIXES = ("ansible.builtin.", "ansible.legacy.", "community.general.", "ansible.posix.")
SECRET_WORD = re.compile(r"(password|passphrase|secret|token|api[_-]?key|credential|private[_-]?key)", re.I)
# A secret-bearing variable is a Jinja expression that names one of those words.
SECRET_EXPR = re.compile(r"\{\{[^}]*(password|passphrase|secret|token|api[_-]?key|credential|private[_-]?key)[^}]*\}\}", re.I)
SECRET_DEST = re.compile(r"(\.env\b|token|secret|credential|password|\.key\b)", re.I)
# Role defaults that look secret by name but are reviewed and hold only a name.
REVIEWED_DEFAULTS = {
    "samsung_tv_key_vault_secret": "the NAME of a vault secret, never its value; the default is a private name that "
    "should become empty in a breaking-change release (docs/security-audit.md, remaining)",
}
CURL_PIPE = re.compile(r"(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(ba|z)?sh\b")
INSECURE = re.compile(r"(\bcurl\b[^\n]*\s(-k|--insecure)\b|\bwget\b[^\n]*--no-check-certificate)")
PIP_UNPINNED = re.compile(r"\bpip3?\s+install\s+(?!.*(==|-r\s|-e\s|\.\s))[^\n]*")
MOVING_REFS = {"HEAD", "main", "master", "latest", "develop"}
# Defaults that name a secret but hold something else (a path, a name, a URL).
NOT_A_SECRET_VALUE = re.compile(r"^(/|https?://|\{\{|\s*$)")
SAFE_DEFAULT_NAME = re.compile(r"(_path|_file|_dir|_url|_name|_enabled|_users?|_unit|_service|_keycode|_fingerprint|_uuid|_mapper|_keyring|_key_url|_archive_key)", re.I)


class Loader(yaml.SafeLoader):
    """SafeLoader that tolerates Ansible's custom tags such as !unsafe."""


Loader.add_multi_constructor("!", lambda loader, suffix, node: loader.construct_scalar(node) if isinstance(node, yaml.ScalarNode) else None)


def bare(module: str) -> str:
    for prefix in MODULE_PREFIXES:
        if module.startswith(prefix):
            return module[len(prefix):]
    return module


def tasks_of(data) -> list[dict]:
    found: list[dict] = []
    for task in data if isinstance(data, list) else []:
        if not isinstance(task, dict):
            continue
        for key in ("block", "rescue", "always"):
            if key in task:
                found.extend(tasks_of(task[key]))
        found.append(task)
    return found


def module_of(task: dict) -> str:
    keywords = {"name", "when", "tags", "register", "vars", "loop", "loop_control", "become", "become_user", "no_log",
                "notify", "changed_when", "failed_when", "environment", "retries", "delay", "until", "check_mode",
                "ignore_errors", "args", "with_items", "delegate_to", "run_once", "block", "rescue", "always",
                "listen", "diff", "throttle", "timeout", "async", "poll", "with_dict", "with_fileglob", "any_errors_fatal",
                "no_log", "become_method", "become_flags", "connection", "remote_user", "module_defaults", "collections"}
    for key in task:
        if key not in keywords:
            return bare(key)
    return ""


def text(value) -> str:
    return json.dumps(value, default=str)


def findings_for(path: Path, task: dict) -> list[dict]:
    module = module_of(task)
    name = str(task.get("name", "(unnamed)"))
    where = path.relative_to(ROOT).as_posix()
    body = next((v for k, v in task.items() if bare(k) == module), None)
    blob = text(body)
    out: list[dict] = []

    def add(rule: str) -> None:
        out.append({"rule": rule, "file": where, "task": name, "module": module})

    if module in EXEC:
        add("exec")
    if module in NETWORK:
        add("network")
    refs_secret = bool(SECRET_EXPR.search(blob + text(task.get("environment", ""))))
    if refs_secret and not task.get("no_log") and module in EXEC | NETWORK | {"copy", "template", "lineinfile", "blockinfile", "user", "git_config", "git"}:
        add("secret-nolog")
    if module in {"copy", "template", "file", "lineinfile", "blockinfile"} and isinstance(body, dict):
        dest = str(body.get("dest") or body.get("path") or "")
        mode = str(body.get("mode", ""))
        if SECRET_DEST.search(dest) and body.get("state") != "absent":
            if not mode or not re.fullmatch(r"0?[0-7]?[0-7][0-7]0", mode.replace("'", "")) and mode not in {"0600", "0640", "0400", "0440", "0700", "0750"}:
                add("secret-mode")
    if CURL_PIPE.search(blob.replace("\\n", "\n")):
        add("curl-pipe")
    if INSECURE.search(blob) or (isinstance(body, dict) and str(body.get("validate_certs", "")).lower() in {"false", "no", "0"}):
        add("insecure-tls")
    if module == "get_url" and isinstance(body, dict) and not body.get("checksum"):
        add("no-checksum")
    if module in EXEC and PIP_UNPINNED.search(blob):
        add("unpinned")
    if module == "git" and isinstance(body, dict) and str(body.get("version", "")) in MOVING_REFS:
        add("unpinned")
    return out


def default_secret_findings(role: Path) -> list[dict]:
    path = role / "defaults" / "main.yml"
    if not path.is_file():
        return []
    data = yaml.load(path.read_text(), Loader=Loader) or {}
    out = []
    for key, value in data.items():
        if (
            SECRET_WORD.search(key)
            and not SAFE_DEFAULT_NAME.search(key)
            and isinstance(value, str)
            and not NOT_A_SECRET_VALUE.match(value)
            and key not in REVIEWED_DEFAULTS
        ):
            out.append({"rule": "default-secret", "file": path.relative_to(ROOT).as_posix(), "task": key, "module": "defaults"})
    return out


def audit(root: Path = ROOT) -> list[dict]:
    results: list[dict] = []
    for role in sorted((root / "roles").iterdir()):
        for path in sorted((role / "tasks").glob("*.yml")) if (role / "tasks").is_dir() else []:
            data = yaml.load(path.read_text(), Loader=Loader)
            for task in tasks_of(data):
                results.extend(findings_for(path, task))
        results.extend(default_secret_findings(role))
    return results


def key(finding: dict) -> str:
    return f"{finding['rule']}|{finding['file']}|{finding['task']}"


def summary(results: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in results:
        counts[item["rule"]] = counts.get(item["rule"], 0) + 1
    return dict(sorted(counts.items()))


def reviewable(results: list[dict]) -> list[dict]:
    """Findings that need a decision: everything except plain inventory."""
    return [r for r in results if r["rule"] not in {"exec", "network"}]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--update-baseline", action="store_true", help="record current non-hard findings as reviewed")
    args = parser.parse_args(argv)
    results = audit()
    if args.update_baseline:
        keep = sorted({key(r) for r in reviewable(results) if r["rule"] not in HARD})
        BASELINE.write_text(json.dumps({"schema": "curiosity.security-audit-baseline/v1", "reviewed": keep}, indent=2) + "\n")
        print(f"recorded {len(keep)} reviewed findings in {BASELINE.relative_to(ROOT)}")
        return 0
    if args.json:
        print(json.dumps({"summary": summary(results), "findings": reviewable(results)}, indent=2))
        return 0
    for rule, count in summary(results).items():
        print(f"{count:5d}  {rule}")
    for item in reviewable(results):
        print(f"{item['rule']}: {item['file']}: {item['task']}")
    return 1 if any(r["rule"] in HARD for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
