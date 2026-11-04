#!/usr/bin/env python3
"""curiosity-diagnostics: a redacted, local-only support bundle for the workstation role.

    curiosity-diagnostics fields                  # what is collected, hashed, redacted, never collected
    curiosity-diagnostics preview                 # print the exact bundle; write nothing
    curiosity-diagnostics write [--yes]           # preview, ask, then write a 0600 file under
                                                  #   ~/.local/state/curiosity/diagnostics/
    curiosity-diagnostics delete [--all] [FILE]   # remove bundles this tool wrote

Nothing is ever sent anywhere: this program has no network code. You review the
file and decide whether to attach it to an issue. It only reads the update
manifest, /etc/os-release, tool version output and service/disk state, and it
never reads documents, home directories, shell history or credential stores.

Redaction runs over every string in the bundle. Values that identify a person or
a network (user names, host names, IP addresses, e-mail addresses) become stable
tags such as ``host-1a2b3c4d`` so a report still shows that two lines name the same
machine; the tag is a salted hash with a salt that is thrown away after the run,
so it cannot be reversed or linked to another bundle. Tokens, passwords and
certificate or key blocks are replaced outright (``[REDACTED:token]``).
Redaction is pattern based and cannot be perfect: read the preview.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import hashlib
import ipaddress
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
from pathlib import Path

VERSION = "1"
SCHEMA = "curiosity.workstation.diagnostics/v1"
DEFAULT_MANIFEST = Path("/var/lib/curiosity/workstation-updates/last-run.json")
DEFAULT_OUTPUT = Path.home() / ".local/state/curiosity/diagnostics"
BUNDLE_GLOB = "diagnostics-*.json"
TIMEOUT = 5  # seconds for any single command
SAFE_VERSION_ARGS = {"--version", "-V", "-v", "version"}
STALE_MANIFEST_DAYS = 14
LOW_DISK_PERCENT = 10
LOW_DISK_BYTES = 2 * 1024**3
DISK_PATHS = ("/", "/var/lib/curiosity", "/usr/local")

POLICY = {
    "collected": [
        "tool, schema and collection date",
        "operating system id, version, codename; architecture; kernel major.minor; Python version",
        "ansible and ansible-core versions",
        "role profile, update ring, selected tags (only if you pass --tags)",
        "last update run: status, run id, start and finish time, quarantine state, catalog commit and whether it was verified",
        "failure message and the names of failed or warned health checks",
        "per managed binary: path with the home directory shortened to ~, whether it exists, sha256 before and after the run",
        "upstream release tag names recorded for the run",
        "health re-check now: command present, version output (first line), service state, free disk space, changed or stale artifacts",
    ],
    "hashed": [
        "user names (the current user, accounts under /home, names you pass with --redact)",
        "host name and fully qualified names that end in a common top-level domain",
        "IPv4 and IPv6 addresses, MAC addresses and e-mail addresses",
    ],
    "redacted": [
        "tokens, API keys, passwords, authorization headers and passwords inside URLs",
        "certificate and private key blocks",
        "values of keys named like secret, token, password, credential, apikey",
    ],
    "never_collected": [
        "file contents other than the update manifest and /etc/os-release",
        "home directory listings, shell history, browser or password-manager data, SSH and GPG keys",
        "research data, project files, environment variables, process lists, network topology, package inventories",
        "anything from other users' accounts",
    ],
}

# --- redaction -------------------------------------------------------------------

PEM = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----.*?-----END \1-----", re.S)
TOKEN_SHAPES = re.compile(
    r"\bgh[pousr]_[A-Za-z0-9]{20,}\b|\bglpat-[A-Za-z0-9_-]{15,}\b|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|\bxox[abprs]-[A-Za-z0-9-]{10,}\b"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|(?i:\bbearer\s+)[A-Za-z0-9._~+/=-]{16,}"
)
URL_PASSWORD = re.compile(r"(?P<scheme>\b[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
KEYED_SECRET = re.compile(
    r"(?i)\b(?P<key>[A-Za-z0-9_.-]*(?:secret|token|passw(?:or)?d|credential|api[_-]?key|private[_-]?key)[A-Za-z0-9_.-]*)"
    r"(?P<sep>\s*[:=]\s*)(?P<value>\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\b")
IPV4 = re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])")
IPV6_CANDIDATE = re.compile(r"(?<![\w:.])[0-9A-Fa-f:.]*:[0-9A-Fa-f:.]*:[0-9A-Fa-f:.]*(?![\w:])")
MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")
TLDS = "com|org|net|io|dev|edu|gov|local|lan|internal|corp|home|test|cloud|app|co|us|uk|de|fr|eu|ca|au|info|xyz|ai"
HOSTNAME = re.compile(rf"\b(?:[A-Za-z0-9](?:[A-Za-z0-9-]{{0,61}}[A-Za-z0-9])?\.)+(?:{TLDS})\b", re.I)
PUBLIC_DOMAINS = {
    "github.com", "api.github.com", "githubusercontent.com", "objects.githubusercontent.com", "gitlab.com", "ubuntu.com",
    "archive.ubuntu.com", "debian.org", "deb.debian.org", "pypi.org", "npmjs.org", "registry.npmjs.org", "example.com",
    "example.org", "example.net",
}
LOOPBACK = {"127.0.0.1", "0.0.0.0", "::1", "::"}


class Redactor:
    """Replaces identifying values in strings; counts what it did by kind."""

    def __init__(self, users: set[str] | None = None, hosts: set[str] | None = None, salt: bytes | None = None):
        self.salt = salt or secrets.token_bytes(16)
        self.users = {name for name in (users or set()) if len(name) >= 3}
        self.hosts = {name.lower() for name in (hosts or set()) if len(name) >= 3}
        self.counts: dict[str, int] = {}

    def tag(self, kind: str, value: str) -> str:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        digest = hashlib.sha256(self.salt + kind.encode() + value.lower().encode()).hexdigest()[:8]
        return f"{kind}-{digest}"

    def mask(self, kind: str) -> str:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        return f"[REDACTED:{kind}]"

    def text(self, value: str) -> str:
        value = PEM.sub(lambda m: self.mask("private-key" if "PRIVATE" in m.group(1) else "certificate"), value)
        value = URL_PASSWORD.sub(lambda m: m.group("scheme") + self.mask("url-credentials") + "@", value)
        value = TOKEN_SHAPES.sub(lambda m: self.mask("token"), value)
        value = KEYED_SECRET.sub(lambda m: m.group("key") + m.group("sep") + self.mask("secret"), value)
        value = EMAIL.sub(lambda m: self.tag("email", m.group(0)), value)
        value = MAC.sub(lambda m: self.tag("mac", m.group(0)), value)
        value = IPV6_CANDIDATE.sub(self._ipv6, value)
        value = IPV4.sub(lambda m: m.group(0) if m.group(0) in LOOPBACK else self.tag("ip", m.group(0)), value)
        for host in sorted(self.hosts, key=len, reverse=True):
            value = re.sub(re.escape(host), lambda m: self.tag("host", m.group(0)), value, flags=re.I)
        value = HOSTNAME.sub(lambda m: m.group(0) if m.group(0).lower() in PUBLIC_DOMAINS else self.tag("host", m.group(0)), value)
        value = re.sub(r"/home/([^/\s\"']+)", "/home/~", value)
        for user in sorted(self.users, key=len, reverse=True):
            value = re.sub(rf"(?<![A-Za-z0-9]){re.escape(user)}(?![A-Za-z0-9])", lambda m: self.tag("user", m.group(0)), value)
        return value

    def _ipv6(self, match: re.Match) -> str:
        text = match.group(0)
        try:
            address = ipaddress.IPv6Address(text)
        except ValueError:
            return text  # a clock time or a version string, not an address
        return text if text in LOOPBACK or address.is_loopback else self.tag("ip", text)

    def walk(self, node):
        if isinstance(node, str):
            return self.text(node)
        if isinstance(node, list):
            return [self.walk(item) for item in node]
        if isinstance(node, dict):
            return {self.text(str(key)): self.walk(item) for key, item in node.items()}
        return node


def known_users(extra: list[str]) -> set[str]:
    users = set(extra)
    try:
        users.add(getpass.getuser())
    except (OSError, KeyError):
        pass  # a uid with no passwd entry (some containers); /home below still helps
    try:
        users.update(entry for entry in os.listdir("/home") if not entry.startswith("."))
    except OSError:
        pass
    return users


def known_hosts() -> set[str]:
    names = {socket.gethostname()}
    names.add(names.copy().pop().split(".")[0])
    return names


# --- collection ------------------------------------------------------------------

def run(command: list[str]) -> tuple[int, str]:
    """Run one bounded, read-only command. Returns (returncode, first line of output); 127 if absent."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT, check=False, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return 127, ""
    except (subprocess.TimeoutExpired, OSError):
        return 124, ""
    text = (result.stdout or result.stderr or "").strip()
    return result.returncode, text.splitlines()[0] if text else ""


def shorten_home(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def read_manifest(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"_error": "no update manifest found; the role has not completed a run on this machine"}
    except (OSError, ValueError) as error:
        return {"_error": f"update manifest unreadable: {type(error).__name__}"}


def os_release() -> dict:
    fields = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key in {"ID", "VERSION_ID", "VERSION_CODENAME"}:
                fields[key.lower()] = value.strip('"')
    except OSError:
        pass
    return fields


def environment(profile: str, tags: list[str]) -> dict:
    code, first = run(["ansible", "--version"])
    ansible = {"installed": code == 0, "version_line": first if code == 0 else ""}
    kernel = ".".join(platform.release().split(".")[:2])
    return {
        "os": os_release(),
        "architecture": platform.machine(),
        "kernel": kernel,
        "python": platform.python_version(),
        "ansible": ansible,
        "profile": profile,
        "tags": tags,
    }


def update_summary(manifest: dict) -> dict:
    if "_error" in manifest:
        return {"error": manifest["_error"]}
    keep = (
        "schema", "run_id", "status", "started_at", "finished_at", "update_ring", "quarantine_active", "install_allowed",
        "catalog_commit", "catalog_commit_verified", "catalog_integrity_mode", "healthcheck_failures", "healthcheck_warnings",
    )
    summary = {key: manifest[key] for key in keep if key in manifest}
    summary["failure"] = (manifest.get("failure") or {}).get("message", "")
    summary["profile"] = manifest.get("profile", "")
    summary["artifacts"] = [
        {"path": shorten_home(entry.get("path", "")), "exists": entry.get("exists"), "sha256_after": entry.get("checksum", "")}
        for entry in manifest.get("after", [])
    ]
    before = {entry.get("path"): entry.get("checksum", "") for entry in manifest.get("before", [])}
    for artifact in summary["artifacts"]:
        artifact["sha256_before"] = before.get(artifact["path"], before.get(artifact["path"].replace("~", str(Path.home()), 1), ""))
    summary["upstream_release_tags"] = {
        name: (value.get("tag_name") or value.get("tag") or "") if isinstance(value, dict) else str(value)
        for name, value in (manifest.get("upstream_releases") or {}).items()
    }
    return summary


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError:
        return ""
    return digest.hexdigest()


def healthchecks_from(manifest: dict) -> list[tuple[str, list[str], list[str]]]:
    """(name, healthcheck argv, services) for every contract that declares them."""
    found = []
    for name, contract in sorted((manifest.get("application_contracts") or {}).items()):
        if isinstance(contract, dict) and isinstance(contract.get("healthcheck"), list) and contract["healthcheck"]:
            found.append((name, [str(part) for part in contract["healthcheck"]], [str(s) for s in contract.get("services", [])]))
    return found


def check_command(name: str, argv: list[str]) -> dict:
    result = {"name": name, "path": shorten_home(argv[0]), "present": False, "executable": False, "version": "", "state": "missing"}
    path = shutil.which(argv[0]) or (argv[0] if os.path.isfile(argv[0]) else "")
    if not path:
        return result
    result["present"] = True
    result["executable"] = os.access(path, os.X_OK)
    if not result["executable"]:
        result["state"] = "not-executable"
        return result
    if argv[1:] and set(argv[1:]) <= SAFE_VERSION_ARGS:
        code, first = run([path, *argv[1:]])
        result["version"] = first
        result["state"] = "ok" if code == 0 else "failed"
    else:
        result["state"] = "present"  # a healthcheck with other arguments is not executed here
    return result


def check_service(unit: str) -> dict:
    code, first = run(["systemctl", "is-active", unit])
    if code == 127:
        return {"unit": unit, "state": "unknown", "detail": "systemctl not available"}
    return {"unit": unit, "state": first or "unknown", "ok": first == "active"}


def check_disk(paths=DISK_PATHS) -> list[dict]:
    rows = []
    for path in paths:
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        percent = round(100 * usage.free / usage.total, 1) if usage.total else 0.0
        rows.append({"path": path, "free_bytes": usage.free, "free_percent": percent, "low": percent < LOW_DISK_PERCENT or usage.free < LOW_DISK_BYTES})
    return rows


def check_artifacts(manifest: dict, now: dt.datetime | None = None, max_age_days: int = STALE_MANIFEST_DAYS) -> list[dict]:
    """Compare the digests the last run recorded with what is on disk now."""
    rows = []
    for entry in manifest.get("after", []) if "_error" not in manifest else []:
        path, recorded = entry.get("path", ""), entry.get("checksum", "")
        if not entry.get("exists") or not recorded:
            continue
        current = sha256_file(path)
        state = "missing" if not os.path.exists(path) else ("unchanged" if current == recorded else "changed-since-last-run")
        rows.append({"path": shorten_home(path), "state": state})
    finished = manifest.get("finished_at", "")
    if finished:
        try:
            when = dt.datetime.fromisoformat(finished.replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt.timezone.utc)
            age = ((now or dt.datetime.now(dt.timezone.utc)) - when).days
            rows.append({"path": "update manifest", "state": "stale" if age > max_age_days else "recent", "age_days": age})
        except ValueError:
            pass
    return rows


def collect_checks(manifest: dict, services: list[str]) -> dict:
    declared = healthchecks_from(manifest)
    units = sorted({*services, *(unit for _, _, listed in declared for unit in listed)})
    return {
        "commands": [check_command(name, argv) for name, argv, _ in declared],
        "services": [check_service(unit) for unit in units],
        "disk": check_disk(),
        "artifacts": check_artifacts(manifest),
    }


def problems(bundle: dict) -> list[str]:
    """One line per finding, for the summary at the top of a report."""
    found = []
    checks = bundle.get("checks", {})
    found += [f"command {c['name']}: {c['state']}" for c in checks.get("commands", []) if c["state"] in {"missing", "not-executable", "failed"}]
    found += [f"service {s['unit']}: {s['state']}" for s in checks.get("services", []) if s.get("ok") is False]
    found += [f"disk {d['path']}: {d['free_percent']}% free" for d in checks.get("disk", []) if d["low"]]
    found += [f"artifact {a['path']}: {a['state']}" for a in checks.get("artifacts", []) if a["state"] in {"missing", "changed-since-last-run", "stale"}]
    update = bundle.get("update", {})
    if update.get("status") not in (None, "success"):
        found.append(f"last update: {update.get('status')}")
    found += [f"health check failed in last update: {name}" for name in update.get("healthcheck_failures", [])]
    return found


def build_bundle(manifest_path: Path, profile: str, tags: list[str], services: list[str], extra_users: list[str]) -> tuple[dict, Redactor]:
    manifest = read_manifest(manifest_path)
    bundle = {
        "schema": SCHEMA,
        "tool_version": VERSION,
        "created": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "environment": environment(profile or manifest.get("profile", ""), tags),
        "update": update_summary(manifest),
        "checks": collect_checks(manifest, services),
    }
    bundle["summary"] = problems(bundle)
    redactor = Redactor(known_users(extra_users), known_hosts() | ({manifest["host"]} if isinstance(manifest.get("host"), str) else set()))
    clean = redactor.walk(bundle)
    clean["redaction"] = {"counts": dict(sorted(redactor.counts.items())), "note": "pattern based; review before sharing"}
    return clean, redactor


# --- commands --------------------------------------------------------------------

def render(bundle: dict) -> str:
    return json.dumps(bundle, indent=2, sort_keys=True) + "\n"


def write_bundle(text: str, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"diagnostics-{stamp}.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def delete_bundles(directory: Path, target: str | None, everything: bool) -> list[Path]:
    """Remove only files this tool wrote (diagnostics-*.json directly inside the output directory)."""
    candidates = sorted(directory.glob(BUNDLE_GLOB)) if directory.is_dir() else []
    if target:
        wanted = Path(target)
        candidates = [path for path in candidates if path.resolve() == wanted.resolve()]
        if not candidates:
            raise SystemExit(f"{target} is not a bundle written by this tool in {directory}")
    elif not everything:
        raise SystemExit("name a bundle to delete, or pass --all")
    for path in candidates:
        path.unlink()
    return candidates


def main(argv: list[str] | None = None, stdin=None) -> int:
    parser = argparse.ArgumentParser(prog="curiosity-diagnostics", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--profile", default="")
    parser.add_argument("--tags", default=os.environ.get("CURIOSITY_DIAG_TAGS", ""), help="comma separated tags of the run you are reporting")
    parser.add_argument("--service", action="append", default=[], help="extra systemd unit to check (repeatable)")
    parser.add_argument("--redact", action="append", default=[], help="extra name to hash, such as a project or lab name (repeatable)")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("fields")
    sub.add_parser("preview")
    write = sub.add_parser("write")
    write.add_argument("--yes", action="store_true", help="write without asking (after you have used preview)")
    delete = sub.add_parser("delete")
    delete.add_argument("file", nargs="?")
    delete.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "fields":
        print(json.dumps(POLICY, indent=2))
        return 0
    if args.command == "delete":
        removed = delete_bundles(args.output_dir, args.file, args.all)
        print(f"removed {len(removed)} bundle(s) from {args.output_dir}")
        return 0

    tags = [tag for tag in args.tags.split(",") if tag]
    bundle, _ = build_bundle(args.manifest, args.profile, tags, args.service, args.redact)
    text = render(bundle)
    if args.command == "preview":
        sys.stdout.write(text)
        print("\nNothing was written or sent. `curiosity-diagnostics write` saves this to a private file.", file=sys.stderr)
        return 0
    sys.stdout.write(text)
    if not args.yes:
        stream = stdin or sys.stdin
        if not stream.isatty():
            print("Refusing to write without a terminal to confirm; review the preview above and pass --yes.", file=sys.stderr)
            return 2
        print("\nWrite this to a private file? [y/N] ", end="", file=sys.stderr, flush=True)
        if stream.readline().strip().lower() not in {"y", "yes"}:
            print("Nothing written.", file=sys.stderr)
            return 1
    path = write_bundle(text, args.output_dir)
    print(f"\nWrote {path} (mode 0600). Nothing was sent. Share it only if you choose to; delete it with:\n"
          f"  curiosity-diagnostics delete {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
