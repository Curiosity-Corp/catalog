"""Sunshine .deb selection must follow the host release and upstream asset names."""

from pathlib import Path

import jinja2
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
TASKS = yaml.safe_load(
    (ROOT / "roles/developer_workstation/tasks/streaming.yml").read_text()
)

CURRENT_ASSETS = [
    "sunshine_2026.914.233613-1+debiantrixie_amd64.deb",
    "sunshine_2026.914.233613-1+debiantrixie_arm64.deb",
    "sunshine_2026.914.233613-1+ubuntu22.04_amd64.deb",
    "sunshine_2026.914.233613-1+ubuntu24.04_amd64.deb",
    "sunshine_2026.914.233613-1+ubuntu24.04_arm64.deb",
    "sunshine_2026.914.233613-1+ubuntu26.04_amd64.deb",
    "sunshine_2026.914.233613-1+ubuntu26.04_arm64.deb",
    "sunshine_2026.914.233613-1+ubuntu26.10_amd64.deb",
    "sunshine_2026.914.233613-1+ubuntu26.04_amd64.deb.sha256",
]
LEGACY_ASSETS = [
    "sunshine-debian-trixie-amd64.deb",
    "sunshine-ubuntu-22.04-amd64.deb",
    "sunshine-ubuntu-24.04-amd64.deb",
]


def _task(name: str) -> dict:
    return next(t for t in TASKS if t["name"] == name)


def _environment() -> jinja2.Environment:
    from ansible.plugins.filter.core import FilterModule
    from ansible.plugins.test.core import TestModule

    env = jinja2.Environment()
    env.filters.update(FilterModule().filters())
    env.tests.update(TestModule().tests())
    return env


def _select(assets: list[str], distribution: str, version: str, release: str, arch: str) -> str:
    env = _environment()
    facts = {
        "ansible_distribution": distribution,
        "ansible_distribution_version": version,
        "ansible_distribution_release": release,
        "ansible_architecture": arch,
    }
    for key, template in _task(
        "Select latest Sunshine Debian package for this distribution"
    )["ansible.builtin.set_fact"].items():
        facts[key] = env.from_string(template).render(**facts).strip()
    facts["latest_github_releases"] = {
        "sunshine": {"assets": [{"name": name} for name in assets]}
    }
    template = _task("Resolve latest Sunshine Debian package asset")[
        "ansible.builtin.set_fact"
    ]["_sunshine_latest_deb_asset"]
    expression = template.strip().removeprefix("{{").removesuffix("}}")
    selected = env.compile_expression(expression)(**facts)
    # Ansible's default() renders {} for an empty match; outside Ansible's
    # templar the expression collapses to None. Both mean "nothing selected".
    return (selected or {}).get("name", "")


@pytest.mark.parametrize(
    ("distribution", "version", "release", "arch", "expected"),
    [
        ("Ubuntu", "26.04", "resolute", "x86_64", "sunshine_2026.914.233613-1+ubuntu26.04_amd64.deb"),
        ("Ubuntu", "24.04", "noble", "aarch64", "sunshine_2026.914.233613-1+ubuntu24.04_arm64.deb"),
        ("Debian", "13", "trixie", "x86_64", "sunshine_2026.914.233613-1+debiantrixie_amd64.deb"),
    ],
)
def test_current_asset_names_follow_host_release(distribution, version, release, arch, expected) -> None:
    assert _select(CURRENT_ASSETS, distribution, version, release, arch) == expected


def test_legacy_asset_names_still_match() -> None:
    assert _select(LEGACY_ASSETS, "Ubuntu", "24.04", "noble", "x86_64") == "sunshine-ubuntu-24.04-amd64.deb"


def test_unpublished_release_selects_nothing_so_the_assert_fails() -> None:
    assert _select(CURRENT_ASSETS, "Ubuntu", "27.04", "next", "x86_64") == ""
    assert _select(LEGACY_ASSETS, "Ubuntu", "26.04", "resolute", "x86_64") == ""
