"""APT repository and package selection per distribution (catalog#162, #163).

The role's repos and packages steps are rendered for a host's facts without a
host: a small interpreter walks repos.yml, repos-workspace.yml, packages.yml and
packages-workspace.yml (following include_tasks), evaluates `when`, `set_fact`
and `vars` with Jinja, answers `uri` calls from a canned copy of the vendors'
public listings, and records every apt_repository line and apt package list.

- Ubuntu must not change at all: its rendering is compared with
  fixtures/apt_selection_ubuntu_before.json, rendered from main at 98846a44
  (before #162/#163) by `python tests/test_apt_vendor_channels.py <role dir>`.
- Debian 13 gets each vendor's Debian channel where the vendor publishes one
  (listings checked 2026-11-25), skips Azure CLI with a stated reason, and does
  not ask for software-properties-common.
- Policy: no repository line under repos*.yml hard-codes an Ubuntu path or
  codename without a distribution guard.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from jinja2 import StrictUndefined
from jinja2.nativetypes import NativeEnvironment, native_concat

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles" / "developer_workstation"
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "apt_selection_ubuntu_before.json"

PROFILES = {
    "desktop": ("repos.yml", "packages.yml"),
    "workspace": ("repos-workspace.yml", "packages-workspace.yml"),
}

# --- Canned vendor listings ------------------------------------------------

# MongoDB's S3 index: channels per (family, codename), as listed 2026-11-25.
MONGODB_CHANNELS = {
    ("ubuntu", "noble"): ["7.0", "8.0", "8.2", "8.3", "9.0"],
    ("debian", "trixie"): ["7.0", "8.0", "8.2", "8.3", "9.0"],
}
# Release files: 200 where the vendor publishes the suite, 404 where not.
RELEASE_STATUS = {
    "https://packages.microsoft.com/debian/13/prod/dists/trixie/Release": 200,
    "https://packages.microsoft.com/repos/azure-cli/dists/trixie/Release": 404,
    "https://repo.mongodb.org/apt/debian/dists/trixie/mongodb-org/8.3/Release": 200,
    "https://repo.mongodb.org/apt/debian/dists/trixie/mongodb-org/9.0/Release": 200,
}


def vendor_http(channels: Mapping[tuple[str, str], list[str]]) -> Callable[[str], dict[str, Any]]:
    def fetch(url: str) -> dict[str, Any]:
        index = re.search(r"prefix=apt/(\w+)/dists/(\w+)/mongodb-org/&", url)
        if index:
            family, codename = index.groups()
            base = f"apt/{family}/dists/{codename}/mongodb-org/"
            prefixes = [base] + [f"{base}{c}/" for c in channels.get((family, codename), [])]
            body = "".join(f"<CommonPrefixes><Prefix>{p}</Prefix></CommonPrefixes>" for p in prefixes)
            return {"status": 200, "content": f"<ListBucketResult>{body}</ListBucketResult>"}
        if url in RELEASE_STATUS:
            return {"status": RELEASE_STATUS[url], "content": ""}
        raise AssertionError(f"the role fetched a URL the test has no listing for: {url}")

    return fetch


# --- A minimal task interpreter ---------------------------------------------


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y", "t"}
    return bool(value)


ENV = NativeEnvironment(undefined=StrictUndefined)
ENV.filters.update(
    {
        "bool": _bool,
        # Ansible's regex filters take the text form of their input.
        "regex_replace": lambda s, pattern, repl="": re.sub(pattern, repl, str(s)),
        "regex_findall": lambda s, pattern: re.findall(pattern, str(s)),
        "regex_escape": lambda s: re.escape(str(s)),
    }
)
ENV.tests["skipped"] = lambda result: isinstance(result, Mapping) and bool(result.get("skipped"))


class Scope(Mapping[str, Any]):
    """Variables in Ansible precedence order, rendered lazily on lookup."""

    def __init__(self, layers: list[Mapping[str, Any]]):
        self.layers = layers

    def child(self, extra: Mapping[str, Any] | None) -> "Scope":
        return Scope([extra or {}, *self.layers])

    def _raw(self, key: str) -> Any:
        for layer in self.layers:
            if key in layer:
                return layer[key]
        raise KeyError(key)

    def __getitem__(self, key: str) -> Any:
        return self.render(self._raw(key))

    def __iter__(self) -> Iterator[str]:
        return iter({k for layer in self.layers for k in layer})

    def __len__(self) -> int:
        return len(set(self))

    def render(self, value: Any) -> Any:
        if isinstance(value, Mapping) and not isinstance(value, LazyDict):
            return LazyDict(value, self)
        if isinstance(value, list):
            return [self.render(v) for v in value]
        if isinstance(value, str) and ("{{" in value or "{%" in value):
            template = ENV.from_string(value)
            rendered = native_concat(template.root_render_func(template.new_context(self, shared=True)))
            return self.render(rendered) if isinstance(rendered, str) else rendered
        return value

    def condition(self, when: Any) -> bool:
        conditions = when if isinstance(when, list) else [when]
        return all(_bool(self.render("{{ " + str(c) + " }}")) for c in conditions)


class LazyDict(Mapping[str, Any]):
    def __init__(self, data: Mapping[str, Any], scope: Scope):
        self.data, self.scope = data, scope

    def __getitem__(self, key: str) -> Any:
        return self.scope.render(self.data[key])

    def __iter__(self) -> Iterator[str]:
        return iter(self.data)

    def __len__(self) -> int:
        return len(self.data)


def materialize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: materialize(value[k]) for k in value}
    if isinstance(value, list):
        return [materialize(v) for v in value]
    return value


def module_of(task: Mapping[str, Any]) -> str:
    keys = [k for k in task if k.startswith("ansible.builtin.") or k.startswith("community.")]
    return keys[0] if keys else ""


class Run:
    def __init__(self, role: Path, facts: Mapping[str, Any], fetch: Callable[[str], dict[str, Any]]):
        self.role = role
        self.fetch = fetch
        self.runtime: dict[str, Any] = {}
        defaults = yaml.safe_load((role / "defaults" / "main.yml").read_text())
        self.scope = Scope([self.runtime, dict(facts), defaults])
        self.repositories: list[dict[str, str]] = []
        self.removed_lines: list[dict[str, str]] = []
        self.messages: list[str] = []
        self.packages: list[list[str]] = []

    def tasks(self, name: str) -> list[dict[str, Any]]:
        return yaml.safe_load((self.role / "tasks" / name).read_text()) or []

    def run_file(self, name: str, scope: Scope | None = None) -> None:
        self.run(self.tasks(name), scope or self.scope)

    def run(self, tasks: list[dict[str, Any]], scope: Scope) -> None:
        for task in tasks:
            inner = scope.child(task.get("vars"))
            if "when" in task and not inner.condition(task["when"]):
                if "register" in task:
                    self.runtime[task["register"]] = {"skipped": True}
                continue
            if "block" in task:
                self.run(task["block"], inner)
                continue
            if "loop" in task:
                continue  # only file removals of find results loop here
            self.step(module_of(task), task, inner)

    def step(self, module: str, task: dict[str, Any], scope: Scope) -> None:
        args = task.get(module)
        result: dict[str, Any] = {}
        if module == "ansible.builtin.include_tasks":
            if args != "apt-keyring.yml":
                self.run_file(args, scope)
        elif module == "ansible.builtin.uri":
            result = self.fetch(str(scope.render(args["url"])).strip())
        elif module == "ansible.builtin.set_fact":
            values = {k: materialize(scope.render(v)) for k, v in args.items()}
            self.runtime.update(values)
        elif module == "ansible.builtin.apt_repository":
            self.repositories.append(
                {"filename": scope.render(args["filename"]), "repo": scope.render(args["repo"]),
                 "state": args.get("state", "present")})
        elif module == "ansible.builtin.lineinfile" and args.get("state") == "absent":
            path = scope.render(args["path"])
            if path.startswith("/etc/apt/sources.list.d/"):
                self.removed_lines.append({"path": path, "regexp": scope.render(args["regexp"])})
        elif module == "ansible.builtin.debug":
            self.messages.append(" ".join(str(scope.render(args["msg"])).split()))
        elif module == "ansible.builtin.apt" and isinstance(args.get("name"), (list, str)):
            names = materialize(scope.render(args["name"]))
            if isinstance(names, list) and len(names) > 1:
                self.packages.append(names)
        else:
            result = {"stdout": "", "rc": 0, "files": []}
        if "register" in task:
            self.runtime[task["register"]] = result


def facts(distribution: str, release: str, major: str) -> dict[str, str]:
    return {"ansible_distribution": distribution, "ansible_distribution_release": release,
            "ansible_distribution_major_version": major, "ansible_architecture": "x86_64"}


UBUNTU = {"noble": facts("Ubuntu", "noble", "24"), "resolute": facts("Ubuntu", "resolute", "26")}
DEBIAN13 = facts("Debian", "trixie", "13")


def select(role: Path, host: Mapping[str, Any], profile: str,
           channels: Mapping[tuple[str, str], list[str]] = MONGODB_CHANNELS) -> Run:
    run = Run(role, host, vendor_http(channels))
    repos, packages = PROFILES[profile]
    run.run_file(repos)
    run.run_file(packages)
    return run


def ubuntu_selection(role: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for release, host in UBUNTU.items():
        for profile in PROFILES:
            run = select(role, host, profile)
            out[f"{release}/{profile}"] = {"repositories": run.repositories, "packages": run.packages}
    return out


# --- Tests -----------------------------------------------------------------


def test_ubuntu_selection_is_unchanged() -> None:
    before = json.loads(GOLDEN.read_text())
    after = ubuntu_selection(ROLE)
    assert after.keys() == before.keys()
    for key in before:
        assert after[key] == before[key], f"Ubuntu {key} repositories or packages changed"


@pytest.mark.parametrize("release", sorted(UBUNTU))
@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_ubuntu_does_no_debian_work(release: str, profile: str) -> None:
    run = select(ROLE, UBUNTU[release], profile)
    assert not VENDOR_FILES & {r["path"] for r in run.removed_lines}
    assert not [m for m in run.messages if "Skipping" in m]
    assert "software-properties-common" in run.packages[0]


VENDOR_FILES = {f"/etc/apt/sources.list.d/{n}.list" for n in ("microsoft-dotnet", "azure-cli", "mongodb-org")}
ARCH = "arch=amd64"
MS_KEY = "signed-by=/usr/share/keyrings/microsoft-archive-keyring.gpg"
MONGO_KEY = "signed-by=/usr/share/keyrings/mongodb-archive-keyring.gpg"


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_debian13_uses_vendor_debian_channels(profile: str) -> None:
    run = select(ROLE, DEBIAN13, profile)
    lines = {r["filename"]: r["repo"] for r in run.repositories}
    assert lines["microsoft-dotnet"] == (
        f"deb [{ARCH} {MS_KEY}] https://packages.microsoft.com/debian/13/prod trixie main")
    expected_mongo_channel = "8.3" if profile == "desktop" else "9.0"
    assert lines["mongodb-org"] == (
        f"deb [{ARCH} {MONGO_KEY}] https://repo.mongodb.org/apt/debian "
        f"trixie/mongodb-org/{expected_mongo_channel} main")
    # Microsoft publishes no trixie suite for azure-cli: skipped, with the reason.
    assert "azure-cli" not in lines
    assert any(m.startswith("Skipping the azure_cli repository on Debian trixie")
               and "dists/trixie returned HTTP 404" in m for m in run.messages), run.messages
    for repo in lines.values():
        assert "/ubuntu" not in repo and "noble" not in repo, repo
    # Lines an earlier run wrote with the Ubuntu noble channel are removed.
    assert VENDOR_FILES <= {r["path"] for r in run.removed_lines}


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_debian13_package_list_drops_only_what_trixie_lacks(profile: str) -> None:
    debian = select(ROLE, DEBIAN13, profile).packages[0]
    ubuntu = select(ROLE, UBUNTU["noble"], profile).packages[0]
    assert "software-properties-common" not in debian
    assert [p for p in ubuntu if p != "software-properties-common"] == debian


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_debian_without_a_mongodb_channel_skips_repo_and_mongosh(profile: str) -> None:
    run = select(ROLE, DEBIAN13, profile, channels={})
    assert "mongodb-org" not in {r["filename"] for r in run.repositories}
    assert "mongodb-mongosh" not in run.packages[0]
    assert any(m.startswith("Skipping the mongodb repository on Debian trixie") for m in run.messages)


def _repository_tasks(tasks: list[dict[str, Any]], guard: str = "") -> Iterator[tuple[dict[str, Any], str]]:
    for task in tasks or []:
        when = guard + " " + str(task.get("when", ""))
        if "block" in task:
            yield from _repository_tasks(task["block"], when)
        elif "ansible.builtin.apt_repository" in task:
            yield task, when


UBUNTU_ONLY = re.compile(r"/ubuntu\b|\b(noble|jammy|focal|resolute|questing|plucky)\b|apt_fallback_release")
DISTRIBUTION_GUARD = re.compile(r"ansible_distribution|apt_vendor_family")


@pytest.mark.parametrize("name", sorted(p.name for p in (ROLE / "tasks").glob("repos*.yml"))
                         + ["apt-vendor-repo.yml"])
def test_no_unguarded_ubuntu_repository_line(name: str) -> None:
    tasks = yaml.safe_load((ROLE / "tasks" / name).read_text())
    for task, when in _repository_tasks(tasks):
        repo = task["ansible.builtin.apt_repository"]["repo"]
        if UBUNTU_ONLY.search(repo):
            assert DISTRIBUTION_GUARD.search(when), (
                f"{name}: '{task['name']}' hard-codes an Ubuntu path or codename without a "
                f"distribution guard; use apt_vendor_repos and apt-vendor-repo.yml (catalog#163)")


def test_vendor_debian_channels_name_no_ubuntu_path() -> None:
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
    for key, channels in defaults["apt_vendor_repos"].items():
        assert set(channels) == {"ubuntu", "debian"}, key
        debian = " ".join(str(v) for v in channels["debian"].values())
        assert not re.search(r"/ubuntu\b|\bnoble\b", debian), key


if __name__ == "__main__":
    # Regenerate the "before" fixture from a role checkout: python ... <role dir>
    print(json.dumps(ubuntu_selection(Path(sys.argv[1])), indent=2, sort_keys=True))
