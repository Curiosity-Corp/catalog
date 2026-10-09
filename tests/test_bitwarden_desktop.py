"""Bitwarden desktop release selection, verification, and gating.

bitwarden/clients publishes desktop-, cli-, browser- and web- releases from one
repository, so the installer must not trust /releases/latest. These tests render
the real task expressions with Ansible's own filters and tests.
"""

from pathlib import Path

import jinja2
import pytest
import yaml


ROLE = Path(__file__).resolve().parents[1] / "roles/developer_workstation"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
LATEST = yaml.safe_load((ROLE / "tasks/latest-releases.yml").read_text())
INSTALL = yaml.safe_load((ROLE / "tasks/bitwarden-desktop.yml").read_text())
MANAGED = yaml.safe_load((ROLE / "tasks/managed-updates.yml").read_text())

DIGEST = "sha256:" + "ab" * 32


def _task(tasks: list[dict], name: str) -> dict:
    return next(t for t in tasks if t.get("name") == name)


def _environment() -> jinja2.Environment:
    from ansible.plugins.filter.core import FilterModule
    from ansible.plugins.test.core import TestModule

    env = jinja2.Environment()
    env.filters.update(FilterModule().filters())
    env.tests.update(TestModule().tests())
    # Outside Ansible's templar its `default` filter returns Undefined for an
    # undefined variable; use Jinja's so `| default(true)` behaves as in a play.
    env.filters["default"] = jinja2.filters.do_default
    return env


def _release(tag: str, *, draft: bool = False, prerelease: bool = False, assets: list[str] | None = None) -> dict:
    names = [f"{tag}-asset.bin"] if assets is None else assets
    return {
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "assets": [
            {
                "name": name,
                "browser_download_url": f"https://example.invalid/{tag}/{name}",
                "digest": DIGEST,
            }
            for name in names
        ],
    }


DESKTOP_ASSETS = [
    "Bitwarden-2026.9.1-x86_64.AppImage",
    "Bitwarden-2026.9.1-x86_64.rpm",
    "Bitwarden-2026.9.1-arm64-store.appx",
    "Bitwarden-2026.9.1-amd64.deb",
    "bitwarden_2026.9.1_amd64.snap",
    "bitwarden_2026.9.1_x64.tar.gz",
    "latest-linux.yml",
]


def _select_release(releases: list[dict], status: int = 200) -> dict | None:
    """Apply the tag-prefix indexing task to one query result."""
    task = _task(LATEST, "Index newest stable GitHub release for each tag prefix")
    env = _environment()
    context = {
        "latest_github_release_tag_prefixes": DEFAULTS["latest_github_release_tag_prefixes"],
        "latest_github_releases": {},
        "item": {"item": {"key": "bitwarden_desktop"}, "json": releases, "status": status},
    }
    for name, template in task["vars"].items():
        context[name] = yaml.safe_load(env.from_string(template).render(**context))
    if not all(
        env.compile_expression(str(condition).strip())(**context)
        for condition in task["when"]
    ):
        return None
    rendered = env.from_string(
        task["ansible.builtin.set_fact"]["latest_github_releases"]
    ).render(**context)
    return yaml.safe_load(rendered).get("bitwarden_desktop")


def _select_asset(release: dict) -> dict:
    env = _environment()
    facts = {"latest_github_releases": {"bitwarden_desktop": release}}
    for key, template in _task(
        INSTALL, "Resolve the current Bitwarden desktop release and amd64 package"
    )["ansible.builtin.set_fact"].items():
        value = env.from_string(template).render(**facts).strip()
        facts[key] = yaml.safe_load(value) if key.endswith("asset") else value
    return facts["_bitwarden_desktop_asset"] or {}


def test_desktop_tag_wins_over_newer_cli_browser_and_web_releases() -> None:
    releases = [
        _release("web-v2026.9.1", assets=["web-vault.zip"]),
        _release("browser-v2026.9.3", assets=["browser-chrome.zip"]),
        _release("cli-v2026.9.1", assets=["bw-linux.zip"]),
        _release("desktop-v2026.9.1", assets=DESKTOP_ASSETS),
        _release("desktop-v2026.9.0", assets=DESKTOP_ASSETS),
    ]
    assert _select_release(releases)["tag_name"] == "desktop-v2026.9.1"


def test_prereleases_and_drafts_are_never_selected() -> None:
    releases = [
        _release("desktop-v2026.10.0-beta.1", prerelease=True, assets=DESKTOP_ASSETS),
        _release("desktop-v2026.10.0", draft=True, assets=DESKTOP_ASSETS),
        _release("desktop-v2026.9.1", assets=DESKTOP_ASSETS),
    ]
    assert _select_release(releases)["tag_name"] == "desktop-v2026.9.1"


def test_release_without_assets_is_skipped() -> None:
    releases = [
        _release("desktop-v2026.10.0", assets=[]),
        _release("desktop-v2026.9.1", assets=DESKTOP_ASSETS),
    ]
    assert _select_release(releases)["tag_name"] == "desktop-v2026.9.1"


@pytest.mark.parametrize(
    "releases",
    [
        [_release("cli-v2026.9.1"), _release("web-v2026.9.1"), _release("browser-v2026.9.3")],
        [_release("desktop-v2026.9.1-beta.1", prerelease=True, assets=DESKTOP_ASSETS)],
        [_release("not-desktop-v2026.9.1", assets=DESKTOP_ASSETS)],
        [],
    ],
)
def test_no_desktop_release_selects_nothing_so_discovery_fails_closed(releases) -> None:
    assert _select_release(releases) is None
    # The shared assertion then refuses to continue without current metadata.
    required = _task(LATEST, "Require latest metadata for every binary release source")
    assert "item in (latest_github_releases | default({}))" in required["ansible.builtin.assert"]["that"]


def test_failed_release_query_selects_nothing() -> None:
    assert _select_release([_release("desktop-v2026.9.1", assets=DESKTOP_ASSETS)], status=500) is None


def test_amd64_deb_is_chosen_over_every_other_desktop_artifact() -> None:
    release = _release("desktop-v2026.9.1", assets=DESKTOP_ASSETS)
    assert _select_asset(release)["name"] == "Bitwarden-2026.9.1-amd64.deb"


def test_deb_for_another_version_or_architecture_is_not_selected() -> None:
    other = [
        "Bitwarden-2026.9.0-amd64.deb",
        "Bitwarden-2026.9.1-arm64.deb",
        "Bitwarden-2026.9.1-x86_64.rpm",
        "Bitwarden-2026.9.1-x86_64.AppImage",
    ]
    assert _select_asset(_release("desktop-v2026.9.1", assets=other)) == {}


def test_install_requires_digest_and_verifies_the_download_with_it() -> None:
    gate = _task(INSTALL, "Require a verified Bitwarden desktop package")
    conditions = " ".join(gate["ansible.builtin.assert"]["that"])
    assert "_bitwarden_desktop_asset.digest" in conditions
    assert "^sha256:[0-9a-f]{64}$" in conditions
    assert "^desktop-v" in conditions
    download = _task(INSTALL, "Download the Bitwarden desktop .deb")["ansible.builtin.get_url"]
    assert download["checksum"] == "{{ _bitwarden_desktop_asset.digest }}"
    assert download["dest"].startswith("{{ workstation_update_artifact_cache_dir }}/")
    install = _task(INSTALL, "Install the Bitwarden desktop .deb (with dependencies)")["ansible.builtin.apt"]
    assert install["deb"] == download["dest"]
    assert install["state"] == "present"

    # The same expression must reject an asset that has no digest.
    env = _environment()
    digest_check = next(c for c in gate["ansible.builtin.assert"]["that"] if "digest" in c)
    expression = env.compile_expression(digest_check)
    assert expression(_bitwarden_desktop_asset={"digest": DIGEST}) is True
    assert expression(_bitwarden_desktop_asset={"name": "Bitwarden-2026.9.1-amd64.deb"}) is False
    assert expression(_bitwarden_desktop_asset={"digest": "sha1:" + "ab" * 20}) is False


def _include_conditions() -> list[str]:
    include = _task(MANAGED, "Include Bitwarden desktop app")
    assert include["ansible.builtin.include_tasks"]["file"] == "bitwarden-desktop.yml"
    return include["when"]


def _installs(**variables) -> bool:
    env = _environment()
    context = {
        "workstation_profile": "desktop",
        "ansible_architecture": "x86_64",
        **variables,
    }
    return all(env.compile_expression(str(c).strip())(**context) for c in _include_conditions())


def test_installs_on_x86_64_desktop() -> None:
    assert _installs()


@pytest.mark.parametrize("profile", ["byod-kiosk", "thin-client", "workspace"])
def test_skipped_for_non_desktop_profiles_including_byod_kiosk(profile) -> None:
    assert not _installs(workstation_profile=profile)


def test_skipped_on_other_architectures_and_when_disabled_or_held() -> None:
    assert not _installs(ansible_architecture="aarch64")
    assert not _installs(bitwarden_desktop_enabled=False)
    assert not _installs(workstation_update_install_allowed=False)


def test_discovery_tracks_the_monorepo_without_using_releases_latest() -> None:
    assert DEFAULTS["latest_github_repositories"]["bitwarden_desktop"] == "bitwarden/clients"
    assert DEFAULTS["latest_github_release_tag_prefixes"] == {"bitwarden_desktop": "desktop-v"}
    primary = _task(LATEST, "Query latest GitHub releases for binary applications")
    assert "latest_github_release_tag_prefixes" in primary["loop"]
    tagged = _task(LATEST, "Query recent GitHub releases for tag-prefixed monorepo sources")
    assert "releases?per_page=50" in tagged["ansible.builtin.uri"]["url"]
    assert "releases/latest" not in tagged["ansible.builtin.uri"]["url"]


def test_contract_participates_in_health_rollback_and_tags() -> None:
    contract = DEFAULTS["latest_release_contracts"]["bitwarden_desktop"]
    assert contract["installer"] == "github-deb"
    assert contract["verification"] == "github-asset-digest"
    assert contract["enabled_when"] == "bitwarden_desktop_enabled"
    assert DEFAULTS["bitwarden_desktop_enabled"] is True
    # Never launch the Electron app from the root, display-less health gate.
    assert contract["healthcheck"][0] == "/opt/Bitwarden/chrome_crashpad_handler"
    assert "/usr/bin/bitwarden" in DEFAULTS["workstation_update_managed_paths"]
    assert "/usr/bin/bitwarden" in DEFAULTS["workstation_update_non_removable_paths"]
    assert "bitwarden" in DEFAULTS["workstation_update_transaction_tags"]
