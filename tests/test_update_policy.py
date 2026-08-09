"""Regression checks for the workstation's no-release-pin update policy."""

import json
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
DEFAULTS = ROLE / "defaults/main.yml"
RENOVATE = ROOT / "renovate.json"


def task_text(*names: str) -> str:
    return "\n".join((ROLE / "tasks" / name).read_text() for name in names)


def main() -> None:
    defaults_text = DEFAULTS.read_text()
    defaults = yaml.safe_load(defaults_text)
    renovate = json.loads(RENOVATE.read_text())
    all_tasks = task_text(*[path.name for path in (ROLE / "tasks").glob("*.yml")])

    # Application releases are discovered at run time. A checked-in *_version
    # default or numeric GitHub release URL would reintroduce the old rollback
    # behavior that put m365-cli and Codex back on stale versions.
    version_defaults = sorted(
        match.group(1)
        for line in defaults_text.splitlines()
        if (match := re.match(r"^([a-z][a-z0-9_]*_version):", line))
    )
    if version_defaults:
        raise SystemExit(
            "Release-version defaults are not allowed: " + ", ".join(version_defaults)
        )

    numeric_release_urls = re.findall(
        r"releases/download/(?:v)?\d+(?:\.\d+)+", all_tasks
    )
    if numeric_release_urls:
        raise SystemExit(
            "Numeric release URLs are not allowed: " + ", ".join(numeric_release_urls)
        )

    if "npm_global_update_policy" in defaults_text:
        raise SystemExit("The obsolete npm compatibility-pin policy is still present")

    # Every API-backed binary installer must be covered by the common latest
    # release discovery task, and every discovered repository must be consumed.
    repositories = defaults.get("latest_github_repositories", {})
    if not repositories:
        raise SystemExit("latest_github_repositories must not be empty")

    contracts = defaults.get("latest_release_contracts", {})
    if set(repositories) != set(contracts):
        raise SystemExit(
            "Every latest release source must have exactly one application contract"
        )
    for name, contract in contracts.items():
        for field in ("installer", "verification", "healthcheck"):
            if not contract.get(field):
                raise SystemExit(f"Application contract {name} is missing {field}")

    additional_contracts = defaults.get("workstation_application_contracts", {})
    for name, contract in additional_contracts.items():
        for field in ("installer", "verification", "healthcheck"):
            if not contract.get(field):
                raise SystemExit(f"Application contract {name} is missing {field}")
    if not {"m365-cli", "codex"}.issubset(additional_contracts):
        raise SystemExit("The universal application contract must cover m365-cli and Codex")

    latest_task = (ROLE / "tasks/latest-releases.yml").read_text()
    if (
        "releases/latest" not in latest_task
        or "Require latest metadata" not in latest_task
        or "asset_checksums" not in latest_task
        or "checksum" not in latest_task
    ):
        raise SystemExit("latest-releases.yml must query and require current upstream metadata")
    if (
        "latest_github_verified_assets" not in latest_task
        or "| zip(" not in latest_task
        or "with_subelements:" in latest_task
    ):
        raise SystemExit(
            "latest-releases.yml must build its digest index in one pass; "
            "the old per-asset set_fact loop is too slow for ansible-pull"
        )

    kubernetes_tasks = (ROLE / "tasks/kubernetes.yml").read_text()
    if (
        "helm-{{ latest_github_releases.helm.tag_name }}.tar.gz" not in kubernetes_tasks
        or "helm-{{ latest_github_releases.helm.tag_name }}.archive" in kubernetes_tasks
    ):
        raise SystemExit(
            "Helm's cached artifact must retain its .tar.gz suffix for reliable extraction"
        )

    referenced = {
        match.group(1)
        for match in re.finditer(r"latest_github_releases\.([a-z0-9_]+)", all_tasks)
    }
    missing_references = sorted(set(repositories) - referenced)
    if missing_references:
        raise SystemExit(
            "Latest-release repositories with no installer consumer: "
            + ", ".join(missing_references)
        )

    # Renovate may still monitor CI image tags, but it must not own workstation
    # application versions because there are no application versions to pin.
    release_managers = [
        manager
        for manager in renovate.get("customManagers", [])
        if "defaults/main" in " ".join(manager.get("managerFilePatterns", []))
    ]
    if release_managers:
        raise SystemExit("Renovate must not manage workstation release defaults")
    if any(
        rule.get("groupName") == "workstation pinned releases"
        for rule in renovate.get("packageRules", [])
    ):
        raise SystemExit("The obsolete workstation pinned-release Renovate rule remains")
    if renovate.get("$schema") != "https://docs.renovatebot.com/renovate-schema.json":
        raise SystemExit("Renovate must use the public schema")
    if "config:recommended" not in renovate.get("extends", []):
        raise SystemExit("Renovate must use the public recommended preset")
    if any("developerdojo" in json.dumps(renovate) for _ in [0]):
        raise SystemExit("Renovate must not contain organization-private hosts")

    # npm globals must be installed in the user's actual prefix and refreshed
    # from the registry's latest dist-tags.
    for relative in (
        "tasks/node-packages.yml",
        "tasks/ai-assistants.yml",
        "tasks/testing.yml",
        "tasks/m365-cli.yml",
    ):
        text = (ROLE / relative).read_text()
        if 'become_user: "{{ dev_user }}"' not in text:
            raise SystemExit(f"{relative} is not user-scoped")
        if 'NPM_CONFIG_PREFIX: "{{ npm_global_prefix }}"' not in text:
            raise SystemExit(f"{relative} does not target npm_global_prefix")
        if "state: latest" not in text:
            raise SystemExit(f"{relative} does not request latest packages")

    if "npm" not in set(defaults.get("npm_global_packages", [])):
        raise SystemExit("npm must be in the user-scoped latest package set")
    # m365-cli is deliberately NOT in npm_global_packages: it lives in its own
    # m365_cli_packages list (installed by tasks/m365-cli.yml, tag: m365) so a
    # consumer can select it independently of the rest of the Node toolchain.
    if "@pnp/cli-microsoft365" in set(defaults.get("npm_global_packages", [])):
        raise SystemExit(
            "m365-cli must not be folded into npm_global_packages; "
            "it belongs in m365_cli_packages (tag: m365) instead"
        )
    if "@pnp/cli-microsoft365" not in set(defaults.get("m365_cli_packages", [])):
        raise SystemExit("m365-cli must be in the user-scoped latest m365_cli_packages set")
    if "@openai/codex" not in defaults.get("ai_assistant_packages", []):
        raise SystemExit("Codex must be in the user-scoped latest AI package set")

    for relative in (
        "tasks/user-env.yml",
        "tasks/user-shell-fallback.yml",
    ):
        if ".npm-global/bin" not in (ROLE / relative).read_text():
            raise SystemExit(f"{relative} does not put the managed npm prefix on PATH")

    # Package-manager and timer coverage is the fleet-wide part of the policy;
    # these checks prevent future app additions from silently becoming stale.
    required_fragments = {
        "tasks/system-updates.yml": ("upgrade: safe", "snap refresh"),
        "tasks/python-tools.yml": ("state: latest", "name: uv"),
        "tasks/science-tools.yml": ("state: latest",),
        "tasks/hardware-drivers.yml": ("state: latest",),
        "tasks/ansible-pull-timer.yml": (
            "OnCalendar=*-*-* 02:00:00",
            "--verify-commit",
            "RandomizedDelaySec=",
        ),
    }
    for relative, fragments in required_fragments.items():
        text = (ROLE / relative).read_text()
        for fragment in fragments:
            if fragment not in text:
                raise SystemExit(f"{relative} is missing update coverage: {fragment}")

    # Workspace bootstrap applies the package tags directly. The update
    # foundation and release discovery must therefore be reachable through
    # that same tag, otherwise the first unrelated installer can abort before
    # user-scoped packages (including Codex) are refreshed.
    main_tasks = (ROLE / "tasks/main.yml").read_text()
    for fragment in (
        "tags: [updates, releases, provenance, rollback, packages]",
        "tags: [updates, releases, rings, packages]",
        "Refresh user-scoped npm packages before upstream release discovery",
        "tags: [updates, packages, node, ai]",
    ):
        if fragment not in main_tasks:
            raise SystemExit(
                "tasks/main.yml must make update prerequisites reachable via packages: "
                + fragment
            )

    rollback_exclusions = defaults.get("workstation_update_rollback_excluded_paths", [])
    if '"{{ npm_global_prefix }}/bin/codex"' not in defaults_text:
        raise SystemExit("Codex must be excluded from binary rollback after npm refresh")
    if "{{ npm_global_prefix }}/bin/codex" not in rollback_exclusions:
        raise SystemExit("Codex rollback exclusion must use the managed npm prefix")

    # The eight workstation-local controls remain structural parts of the role
    # even before a central fleet service exists.
    control_fragments = {
        "tasks/update-foundation.yml": (
            "workstation_update_snapshot_dir",
            "workstation_update_artifact_cache_dir",
            "before.json",
        ),
        "tasks/update-ring.yml": ("minimum_age_seconds", "install_allowed"),
        "tasks/update-health.yml": (
            "healthcheck_failures",
            "Post-update healthchecks failed",
        ),
        "tasks/update-rollback.yml": (
            "Restore managed binaries",
            "quarantine.json",
        ),
        "tasks/update-manifest.yml": (
            "curiosity.workstation.update/v1",
            "CycloneDX",
            "catalog_commit",
        ),
        "tasks/managed-updates.yml": ("Software update transaction",),
    }
    for relative, fragments in control_fragments.items():
        text = (ROLE / relative).read_text()
        for fragment in fragments:
            if fragment not in text:
                raise SystemExit(f"{relative} is missing control-plane feature: {fragment}")

    # Direct-release installers must verify their selected artifact and use the
    # local digest-addressed cache. Package managers have their own signatures.
    direct_release_tasks = (
        "tasks/cli-tools.yml",
        "tasks/kubernetes.yml",
        "tasks/devops.yml",
        "tasks/security.yml",
        "tasks/streaming.yml",
        "tasks/fonts.yml",
        "tasks/languages.yml",
        "tasks/collaboration.yml",
        "tasks/dotfiles.yml",
    )
    for relative in direct_release_tasks:
        text = (ROLE / relative).read_text()
        if "checksum:" not in text or "workstation_update_artifact_cache_dir" not in text:
            raise SystemExit(f"{relative} is missing verified cached artifact handling")

    # Moving vendor URLs must be revalidated even when their URL never changes.
    cloud_text = (ROLE / "tasks/cloud-clis.yml").read_text()
    if "awscli-exe-linux-x86_64.zip" not in cloud_text or "    force: true" not in cloud_text:
        raise SystemExit("AWS CLI's moving latest URL must not be permanently cached")

    # Do not regress to mutable remote installer pipes in the update paths.
    script_paths = (
        "tasks/cloud-clis.yml",
        "tasks/kubernetes.yml",
        "tasks/python-tools.yml",
        "tasks/user-env.yml",
        "tasks/languages.yml",
    )
    forbidden_script_pattern = re.compile(
        r"curl[^\n|]*\|\s*(bash|sh)|curl[^\n]*install\.sh"
    )
    for relative in script_paths:
        if forbidden_script_pattern.search((ROLE / relative).read_text()):
            raise SystemExit(f"Mutable remote installer pipe remains in {relative}")

    print(
        f"update policy ok: {len(repositories)} upstream release sources are dynamic; "
        "Renovate has no workstation release pins; package managers and nightly pull are active"
    )


if __name__ == "__main__":
    main()
