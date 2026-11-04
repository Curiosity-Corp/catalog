#!/usr/bin/env python3
"""Print the variables a run would use, with secrets redacted.

This is a read-only preview helper. It never contacts a host and never runs
Ansible. It reads variables from three places, lowest precedence first, which
mirrors how Ansible orders them:

1. role defaults (``roles/*/defaults/main.yml`` unless ``--role-defaults`` is used),
2. ``--vars-file`` files (for example ``/etc/ansible/local-vars.yml``),
3. ``-e KEY=VALUE`` overrides.

``--stdin`` additionally merges a JSON or YAML mapping from standard input, for
example ``ansible-inventory -i hosts --host myvm | scripts/preview_vars.py --stdin``.

Values are printed as written (unrendered Jinja stays as ``{{ ... }}``), so the
output shows what is configured, not what a template will evaluate to on the
host. Anything that looks like a secret is replaced by ``***REDACTED***``:

* values whose variable name looks sensitive (password, token, secret, psk,
  private key, ``*_key``, ...), when they are non-empty;
* values that look like credentials wherever they appear (PEM private keys,
  well-known API token prefixes, JWTs, ``user:password@`` in URLs).

Redaction is deliberately over-eager. Review the output before pasting it into
an issue; a heuristic cannot promise that nothing sensitive remains.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

REDACTED = "***REDACTED***"

# Matched against the variable name, lower-cased.
SENSITIVE_NAME = re.compile(
    r"pass(word|wd|phrase)|(^|_)pass($|_)|secret|token|(^|_)psk($|_)|credential"
    r"|private[_-]?key|api[_-]?key|apikey|(^|_)key$|(^|_)key_file$|cookie|bearer"
    r"|authorization|(^|_)otp($|_)|(^|_)pat$|vault_",
)

# Matched against string values anywhere in the data. A match replaces the
# whole value, except for URL credentials where only the password is replaced.
SENSITIVE_VALUE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}"
    r"|\bglpat-[A-Za-z0-9_-]{10,}"
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bsk-[A-Za-z0-9_-]{20,}"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
)
URL_CREDENTIALS = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)(?P<user>[^/\s:@]+):(?P<pw>[^/\s@]+)@")


def is_sensitive_name(name: object) -> bool:
    return bool(SENSITIVE_NAME.search(str(name).lower()))


def _is_empty(value: Any) -> bool:
    # Booleans such as ``pro_mimic_ssh_password_auth: true`` are flags, not secrets.
    return value is None or isinstance(value, bool) or value == "" or value == [] or value == {}


def redact(value: Any, name: object = "") -> Any:
    """Return a copy of ``value`` with secrets replaced by REDACTED."""
    if is_sensitive_name(name) and not _is_empty(value):
        return REDACTED
    if isinstance(value, dict):
        return {key: redact(item, key) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item, name) for item in value]
    if isinstance(value, str):
        if SENSITIVE_VALUE.search(value):
            return REDACTED
        return URL_CREDENTIALS.sub(r"\g<scheme>\g<user>:" + REDACTED + "@", value)
    return value


def load_mapping(text: str, source: str) -> dict[str, Any]:
    data = yaml.safe_load(text)  # JSON is valid YAML
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{source}: expected a mapping of variable names to values")
    return data


def default_role_files(root: Path) -> list[Path]:
    return sorted(root.glob("roles/*/defaults/main.yml"))


def parse_extra(items: list[str]) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    for item in items:
        key, sep, raw = item.partition("=")
        if not sep or not key:
            raise ValueError(f"-e expects KEY=VALUE, got {item!r}")
        # Same convenience as ansible-playbook -e: scalars keep their YAML type.
        extra[key] = yaml.safe_load(raw) if raw else ""
    return extra


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--role-defaults", action="append", default=[], metavar="FILE",
                        help="defaults file to load (repeatable); default: roles/*/defaults/main.yml")
    parser.add_argument("--vars-file", action="append", default=[], metavar="FILE",
                        help="extra vars file merged over the defaults (repeatable)")
    parser.add_argument("-e", "--extra-var", action="append", default=[], metavar="KEY=VALUE",
                        help="override a single variable (repeatable)")
    parser.add_argument("--stdin", action="store_true",
                        help="merge a JSON or YAML mapping read from standard input")
    parser.add_argument("--match", metavar="REGEX",
                        help="only print variables whose name matches this regular expression")
    parser.add_argument("--format", choices=("yaml", "json"), default="yaml")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        merged: dict[str, Any] = {}
        defaults = [Path(p) for p in args.role_defaults] or default_role_files(root)
        for path in [*defaults, *(Path(p) for p in args.vars_file)]:
            merged.update(load_mapping(path.read_text(), str(path)))
        if args.stdin:
            merged.update(load_mapping(sys.stdin.read(), "<stdin>"))
        merged.update(parse_extra(args.extra_var))
        pattern = re.compile(args.match) if args.match else None
    except (OSError, ValueError, yaml.YAMLError, re.error) as exc:
        print(f"preview_vars: {exc}", file=sys.stderr)
        return 2

    selected = {k: v for k, v in sorted(merged.items()) if pattern is None or pattern.search(str(k))}
    safe = {key: redact(value, key) for key, value in selected.items()}
    if args.format == "json":
        print(json.dumps(safe, indent=2, sort_keys=True, default=str))
    else:
        print(yaml.safe_dump(safe, sort_keys=True, default_flow_style=False, width=120), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
