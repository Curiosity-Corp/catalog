#!/usr/bin/env python3
"""Check the laptop quickstart's APT steps on Debian without root (catalog#135).

Runs ONLY in the debian13-quickstart CI job, as the runner's non-root uid. It
reads what the role would do for the quickstart tags on a ``desktop`` profile,
straight from the role's task files:

- the package list of the first ``ansible.builtin.apt`` task in packages.yml,
  plus the newest ``openjdk-N-jdk`` and ``dotnet-sdk-N.0`` the role selects;
- every ``ansible.builtin.apt_repository`` line in repos.yml and the signing key
  each one uses (from the apt-keyring.yml includes).

It then asks apt, with user-owned state and cache directories and no lock,
what would happen: ``apt-get update`` and ``apt-get install --simulate``, first
against the image's Debian archive only, then with the role's vendor
repositories added. Nothing is installed and nothing outside ``--work`` is
written. Findings go to a Markdown section on stdout; raw apt output stays in
``--out``.

Exit status: 0 when the report was produced (findings are data, not a job
failure); 2 when the harness itself cannot run (role files unreadable, the
Debian archive unreachable).
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import jinja2
import yaml

ROLE_TASKS = Path("roles/developer_workstation/tasks")
ROLE_DEFAULTS = Path("roles/developer_workstation/defaults/main.yml")
ARMOR_BEGIN = "-----BEGIN PGP PUBLIC KEY BLOCK-----"
HTTP_TIMEOUT = 30


class HarnessError(RuntimeError):
    """The check itself could not run; this is not a finding about the role."""


@dataclass
class Repo:
    filename: str
    line: str
    keyring: str | None


@dataclass
class AptRun:
    name: str
    work: Path
    out: Path
    sources: Path
    preferences: Path
    log_lines: list[str] = field(default_factory=list)

    def options(self) -> list[str]:
        return [
            "-o", f"Dir::State::Lists={self.work / 'lists'}",
            "-o", f"Dir::Cache={self.work / 'cache'}",
            "-o", f"Dir::Etc::SourceParts={self.sources}",
            "-o", "Dir::Etc::SourceList=/dev/null",
            "-o", f"Dir::Etc::PreferencesParts={self.preferences}",
            "-o", "Debug::NoLocking=1",
            "-o", "APT::Get::List-Cleanup=0",
        ]

    def run(self, tool: str, *args: str, log: str) -> subprocess.CompletedProcess[str]:
        for sub in ("lists/partial", "cache/archives/partial"):
            (self.work / sub).mkdir(parents=True, exist_ok=True)
        cmd = [tool, *self.options(), *args]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        with (self.out / f"{self.name}-{log}.log").open("a", encoding="utf-8") as fh:
            fh.write(f"$ {' '.join(cmd)}\n# exit {proc.returncode}\n{proc.stdout}{proc.stderr}\n")
        return proc


def load_tasks(name: str) -> list[dict]:
    path = ROLE_TASKS / name
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise HarnessError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, list):
        raise HarnessError(f"{path} is not a task list")
    return data


def role_packages() -> list[str]:
    for task in load_tasks("packages.yml"):
        apt = task.get("ansible.builtin.apt")
        if isinstance(apt, dict) and apt.get("state") == "present" and isinstance(apt.get("name"), list):
            return [str(p) for p in apt["name"]]
    raise HarnessError("packages.yml has no apt task with a package list")


def role_repos(variables: dict[str, str]) -> list[Repo]:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    keyrings: dict[str, str] = {}
    repos: list[Repo] = []
    for task in load_tasks("repos.yml"):
        if task.get("ansible.builtin.include_tasks") == "apt-keyring.yml":
            tv = task.get("vars", {})
            keyrings[tv["apt_keyring_dest"]] = env.from_string(tv["apt_keyring_url"]).render(variables)
        repo = task.get("ansible.builtin.apt_repository")
        if isinstance(repo, dict) and repo.get("state") == "present":
            line = env.from_string(repo["repo"]).render(variables)
            match = re.search(r"signed-by=([^\s\]]+)", line)
            dest = match.group(1) if match else None
            repos.append(Repo(repo["filename"], line, keyrings.get(dest) if dest else None))
    if not repos:
        raise HarnessError("repos.yml declares no APT repositories")
    return repos


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "catalog-ci-debian-check"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310 - fixed vendor URLs
        return resp.read()


def dearmor(data: bytes) -> bytes:
    text = data.decode("ascii", errors="replace")
    body = text.split(ARMOR_BEGIN, 1)[1].split("-----END", 1)[0]
    lines = [ln.strip() for ln in body.strip().splitlines()]
    # Skip armor headers (up to the first blank line) and the CRC line.
    if "" in lines:
        lines = lines[lines.index("") + 1:]
    payload = "".join(ln for ln in lines if ln and not ln.startswith("="))
    return base64.b64decode(payload)


def mongodb_channel(release: str) -> str:
    url = (
        "https://s3.amazonaws.com/repo.mongodb.org?list-type=2"
        f"&prefix=apt/ubuntu/dists/{release}/mongodb-org/&delimiter=/"
    )
    try:
        listing = fetch(url).decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        return f"unresolved ({exc})"
    channels = sorted(re.findall(r"mongodb-org/(8[.][0-9]+)/</Prefix>", listing),
                      key=lambda v: [int(x) for x in v.split(".")])
    return channels[-1] if channels else "unresolved (no 8.x channel listed)"


def candidates(apt: AptRun, packages: list[str]) -> dict[str, str | None]:
    """Map each package to its candidate version, or None when apt has none."""
    proc = apt.run("apt-cache", "policy", *packages, log="policy")
    found: dict[str, str | None] = {p: None for p in packages}
    current = None
    for line in proc.stdout.splitlines():
        if line and not line.startswith(" ") and line.endswith(":"):
            current = line[:-1]
        elif current in found and line.strip().startswith("Candidate:"):
            value = line.split(":", 1)[1].strip()
            found[current] = None if value == "(none)" else value
    return found


def simulate(apt: AptRun, packages: list[str], log: str) -> tuple[int, int, list[str]]:
    proc = apt.run("apt-get", "install", "--simulate", "-y", *packages, log=log)
    inst = sum(1 for ln in proc.stdout.splitlines() if ln.startswith("Inst "))
    errors = [ln.strip() for ln in (proc.stdout + proc.stderr).splitlines()
              if re.match(r"^\s*(E:|.* : Depends:|.* : Breaks:|.* : Conflicts:)", ln)]
    return proc.returncode, inst, errors


def newest(names: list[str], pattern: str) -> str | None:
    matches = [n for n in names if re.fullmatch(pattern, n)]
    return max(matches, key=lambda n: [int(x) for x in re.findall(r"\d+", n)], default=None)


def update_problems(proc: subprocess.CompletedProcess[str]) -> list[str]:
    return [ln.strip() for ln in (proc.stdout + proc.stderr).splitlines()
            if ln.startswith(("Err:", "E:", "W:"))]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    findings: dict = {"repositories": [], "debian_only": {}, "with_vendor_repos": {}}
    md: list[str] = []

    defaults = yaml.safe_load(ROLE_DEFAULTS.read_text(encoding="utf-8"))
    release = str(defaults.get("apt_fallback_release", ""))
    variables = {
        "ansible_architecture": "x86_64",
        "apt_fallback_release": release,
        "latest_mongodb_channel": mongodb_channel(release),
        "latest_mongodb_key_url": "https://pgp.mongodb.com/server-8.0.asc",
    }
    packages = role_packages()
    repos = role_repos(variables)

    # --- Pass 1: the image's own Debian archive only -------------------------
    debian_sources = Path("/etc/apt/sources.list.d")
    prefs = args.work / "preferences.d"
    prefs.mkdir(parents=True, exist_ok=True)
    base = AptRun("1-debian", args.work / "debian", args.out, debian_sources, prefs)
    upd = base.run("apt-get", "update", log="update")
    if upd.returncode != 0:
        raise HarnessError("apt-get update against the Debian archive failed: "
                           + "; ".join(update_problems(upd)[:5]))
    cand = candidates(base, packages)
    missing = [p for p, v in cand.items() if v is None]
    resolvable = [p for p in packages if cand[p] is not None]
    rc, inst, errors = simulate(base, resolvable, "simulate")
    findings["debian_only"] = {"packages": len(packages), "missing": missing,
                               "simulate_exit": rc, "would_install": inst, "errors": errors}

    # --- Pass 2: the role's vendor repositories added ------------------------
    vendor_sources = args.work / "sources.d"
    keys = args.work / "keys"
    vendor_sources.mkdir(parents=True, exist_ok=True)
    keys.mkdir(parents=True, exist_ok=True)
    for src in debian_sources.iterdir():
        if src.is_file():
            shutil.copy(src, vendor_sources / src.name)
    # The role's Mozilla pin (repos.yml), so Firefox resolves as the role intends.
    (prefs / "mozilla.pref").write_text(
        "Package: firefox*\nPin: origin packages.mozilla.org\nPin-Priority: 1001\n", encoding="utf-8")
    for repo in repos:
        entry = {"file": repo.filename, "line": repo.line, "key": repo.keyring, "problem": None}
        line = repo.line
        if repo.keyring:
            try:
                data = fetch(repo.keyring)
            except (urllib.error.URLError, TimeoutError) as exc:
                entry["problem"] = f"signing key download failed: {exc}"
                findings["repositories"].append(entry)
                continue
            path = keys / f"{repo.filename}.gpg"
            path.write_bytes(dearmor(data) if ARMOR_BEGIN.encode() in data else data)
            line = re.sub(r"signed-by=[^\s\]]+", f"signed-by={path}", line)
        (vendor_sources / f"{repo.filename}.list").write_text(line + "\n", encoding="utf-8")
        findings["repositories"].append(entry)
    vend = AptRun("2-vendor", args.work / "vendor", args.out, vendor_sources, prefs)
    upd = vend.run("apt-get", "update", log="update")
    problems = update_problems(upd)
    for entry in findings["repositories"]:
        host = re.search(r"https?://([^\s]+)", entry["line"])
        hits = [p for p in problems if host and host.group(1).split("/")[0] in p]
        if hits and not entry["problem"]:
            entry["problem"] = " | ".join(hits[:3])
    cand = candidates(vend, packages)
    names = vend.run("apt-cache", "pkgnames", log="pkgnames").stdout.split()
    extra = [p for p in (newest(names, r"openjdk-\d+-jdk"), newest(names, r"dotnet-sdk-\d+\.0")) if p]
    extra_cand = candidates(vend, extra) if extra else {}
    missing = [p for p, v in cand.items() if v is None]
    resolvable = [p for p in packages if cand[p] is not None]
    rc, inst, errors = simulate(vend, resolvable, "simulate")
    per_extra = {}
    for pkg in extra:
        xrc, xinst, xerr = simulate(vend, [pkg], f"simulate-{pkg}")
        per_extra[pkg] = {"candidate": extra_cand.get(pkg), "simulate_exit": xrc,
                          "would_install": xinst, "errors": xerr}
    findings["with_vendor_repos"] = {
        "update_exit": upd.returncode, "update_problems": problems, "missing": missing,
        "simulate_exit": rc, "would_install": inst, "errors": errors, "selected": per_extra,
    }
    (args.out / "apt-findings.json").write_text(json.dumps(findings, indent=2), encoding="utf-8")

    d, v = findings["debian_only"], findings["with_vendor_repos"]
    md += ["", "## APT simulation (non-root, user-owned apt state)", "",
           f"Role package list: {d['packages']} packages from `packages.yml`; "
           f"`apt_fallback_release` is `{release}`.", "",
           "### Debian archive only", "",
           f"- Packages with no candidate: {', '.join(d['missing']) or 'none'}",
           f"- `apt-get install --simulate` of the rest: exit {d['simulate_exit']}, "
           f"{d['would_install']} packages would be installed"]
    md += [f"  - `{e}`" for e in d["errors"][:20]]
    md += ["", "### With the role's vendor repositories", "",
           "| File | Repository line | Problem |", "| --- | --- | --- |"]
    md += [f"| {r['file']} | `{r['line']}` | {r['problem'] or 'none'} |" for r in findings["repositories"]]
    md += ["", f"- `apt-get update` exit {v['update_exit']}"]
    md += [f"  - `{p}`" for p in v["update_problems"][:20]]
    md += [f"- Packages with no candidate: {', '.join(v['missing']) or 'none'}",
           f"- `apt-get install --simulate` of the rest: exit {v['simulate_exit']}, "
           f"{v['would_install']} packages would be installed"]
    md += [f"  - `{e}`" for e in v["errors"][:20]]
    for pkg, res in v["selected"].items():
        md.append(f"- Newest-version pick `{pkg}` (candidate {res['candidate']}): simulate exit "
                  f"{res['simulate_exit']}, {res['would_install']} packages")
        md += [f"  - `{e}`" for e in res["errors"][:10]]
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except HarnessError as exc:
        print(f"\n- Harness error: {exc}")
        sys.exit(2)
