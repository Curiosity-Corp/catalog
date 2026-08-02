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
        or "gh" not in latest_task
        or "GITHUB_TOKEN" not in latest_task
        or "GH_TOKEN" not in latest_task
    ):
        raise SystemExit(
            "latest-releases.yml must use available GitHub auth and require "
            "current upstream metadata"
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

    # These expressions are Jinja single-quoted regexes. A doubled backslash
    # reaches Python's regex engine as a literal backslash and silently makes
    # valid release assets disappear from the selector.
    overescaped_asset_patterns = [
        line.strip()
        for line in all_tasks.splitlines()
        if "selectattr('name', 'match'" in line and "\\\\." in line
    ]
    if overescaped_asset_patterns:
        raise SystemExit(
            "Release asset selectors contain doubled regex escapes: "
            + "; ".join(overescaped_asset_patterns[:3])
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
    required_npm_links = {
        (entry.get("package"), entry.get("command"), entry.get("relative_path"))
        for entry in defaults.get("npm_global_command_links", [])
    }
    for required_link in (
        ("npm", "npm", "bin/npm-cli.js"),
        ("pnpm", "pnpm", "bin/pnpm.mjs"),
    ):
        if required_link not in required_npm_links:
            raise SystemExit(
                "The user package-manager launcher contract is missing "
                + repr(required_link)
            )
    if "Repair user-scoped package-manager launchers" not in (
        ROLE / "tasks/node-packages.yml"
    ).read_text():
        raise SystemExit("User package-manager launchers must be repaired after npm updates")
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
    ai_command_links = defaults.get("ai_assistant_command_links", [])
    linked_packages = {entry.get("package") for entry in ai_command_links}
    if linked_packages != set(defaults.get("ai_assistant_packages", [])):
        raise SystemExit("Every AI package must have an explicit user command launcher")
    codex_link = next(
        (entry for entry in ai_command_links if entry.get("command") == "codex"),
        None,
    )
    if not codex_link or codex_link.get("relative_path") != "bin/codex.js":
        raise SystemExit("The Codex launcher must target the package's bin/codex.js")

    # Dynamic include tags are a dependency boundary: a focused component
    # pull must still enter the update foundation and transaction wrappers so
    # rollback/manifest facts exist before the component task runs.
    update_tags = set(defaults.get("workstation_update_transaction_tags", []))
    required_update_tags = {"packages", "node", "ai", "updates", "rollback"}
    if not required_update_tags.issubset(update_tags):
        raise SystemExit(
            "workstation_update_transaction_tags is missing: "
            + ", ".join(sorted(required_update_tags - update_tags))
        )
    main_tasks_text = (ROLE / "tasks/main.yml").read_text()
    if main_tasks_text.count("{{ workstation_update_transaction_tags }}") < 3:
        raise SystemExit(
            "main.yml must apply transaction tags to the foundation, update, "
            "and health/rollback wrapper"
        )
    if main_tasks_text.index("Include workspace launcher repairs") > main_tasks_text.index(
        "Resolve upstream releases and evaluate the update ring"
    ):
        raise SystemExit(
            "Workspace launcher repairs must run before upstream discovery can fail"
        )
    if "file: workspace-launchers.yml\n    apply:\n      tags: [workspace]" not in main_tasks_text:
        raise SystemExit(
            "Workspace launcher repairs must apply their workspace tag to child tasks"
        )

    for relative in (
        "tasks/user-env.yml",
        "tasks/user-shell-fallback.yml",
    ):
        if ".npm-global/bin" not in (ROLE / relative).read_text():
            raise SystemExit(f"{relative} does not put the managed npm prefix on PATH")

    workspace_launcher_text = (ROLE / "tasks/workspace-launchers.yml").read_text()
    for fragment in (
        "Repair catalog-managed npm launchers",
        "Repair the AWS CLI launchers",
        "outside the quarantined software transaction",
    ):
        if fragment not in workspace_launcher_text:
            raise SystemExit(
                "workspace-launchers.yml is missing quarantine-safe repair coverage: "
                + fragment
            )

    # Package-manager and timer coverage is the fleet-wide part of the policy;
    # these checks prevent future app additions from silently becoming stale.
    required_fragments = {
        "tasks/system-updates.yml": ("upgrade: safe", "snap refresh"),
        "tasks/python-tools.yml": (
            "state: latest",
            "name: uv",
            '      - "pipx>=1.7.0"',
            "Check whether pip is importable in Ansible's Python environment",
            "Bootstrap pip in Ansible's Python environment when absent",
            "Install a current pipx into Ansible's Python environment",
            '      - -m\n      - pip\n      - install',
            "Ensure the user pipx binary directory exists",
        ),
        "tasks/science-tools.yml": ("state: latest",),
        "tasks/hardware-drivers.yml": ("state: latest",),
        "tasks/languages.yml": (
            "https://go.dev/dl/?mode=json",
            "sha256:{{ _go_latest_asset.sha256 }}",
            "Merge pyenv into the existing user directory",
            ".pyenv.catalog-staging",
            "https://api.sdkman.io/2/candidates/all",
            "Install the SDKMAN shell launcher",
        ),
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
    if ".tar.gz.sha256" in (ROLE / "tasks/languages.yml").read_text():
        raise SystemExit("Go updates must use the structured release metadata checksum")

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
        "tasks/update-transaction.yml": (
            "managed-updates.yml",
            "update-health.yml",
            "update-rollback.yml",
            "update-manifest.yml",
            "tags: [always]",
        ),
        "tasks/managed-updates.yml": ("Software update transaction",),
    }
    for relative, fragments in control_fragments.items():
        text = (ROLE / relative).read_text()
        for fragment in fragments:
            if fragment not in text:
                raise SystemExit(f"{relative} is missing control-plane feature: {fragment}")

    if "file: ai-assistants.yml" not in main_tasks_text or "tags: [packages, ai]" not in main_tasks_text:
        raise SystemExit("main.yml must propagate the ai tag into ai-assistants.yml")
    managed_updates_text = (ROLE / "tasks/managed-updates.yml").read_text()
    component_tag_applies = {
        "system-updates.yml": "[updates, packages]",
        "coder-cli.yml": "[coder]",
        "cloud-clis.yml": "[cloud]",
        "kubernetes.yml": "[kubernetes]",
        "languages.yml": "[languages]",
        "devops.yml": "[devops]",
        "python-tools.yml": "[python]",
        "science-tools.yml": "[science, data-science]",
        "cli-tools.yml": "[cli]",
        "testing.yml": "[testing]",
        "m365-cli.yml": "[m365]",
        "missionctl.yml": "[missionctl]",
        "security.yml": "[security]",
        "maintenance.yml": "[maintenance]",
        "self-healing.yml": "[self-healing]",
        "vscode-extensions.yml": "[vscode, extensions]",
        "collaboration.yml": "[collaboration]",
        "weechat.yml": "[weechat, chat]",
        "hardware-drivers.yml": "[hardware]",
        "fonts.yml": "[fonts]",
        "streaming.yml": "[streaming, sunshine]",
    }
    for filename, tags in component_tag_applies.items():
        fragment = f"file: {filename}\n    apply:\n      tags: {tags}"
        if fragment not in managed_updates_text:
            raise SystemExit(f"managed-updates.yml must apply component tags to {filename}")
    health_text = (ROLE / "tasks/update-health.yml").read_text()
    if health_text.count("map(attribute='item.item.name')") < 2:
        raise SystemExit("update-health.yml must address nested Ansible loop results correctly")

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

    kubernetes_text = (ROLE / "tasks/kubernetes.yml").read_text()
    for fragment in (
        "Resolve latest Helm artifact checksum",
        "checksum_algorithm: sha256",
        "Remove an invalid cached Helm artifact",
        'include: ["linux-amd64/helm"]',
        "krew-{{ _krew_latest_asset.digest | regex_replace('^sha256:', '') }}.tar.gz",
        "include: [./krew-linux_amd64]",
    ):
        if fragment not in kubernetes_text:
            raise SystemExit(
                "tasks/kubernetes.yml must validate the cached Helm artifact: "
                + fragment
            )

    # Moving vendor URLs must be revalidated even when their URL never changes.
    cloud_text = (ROLE / "tasks/cloud-clis.yml").read_text()
    if "awscli-exe-linux-x86_64.zip" not in cloud_text or "    force: true" not in cloud_text:
        raise SystemExit("AWS CLI's moving latest URL must not be permanently cached")
    if "Inspect AWS CLI launchers without following symlinks" not in cloud_text or "Remove legacy copied AWS CLI launchers" not in cloud_text:
        raise SystemExit("AWS CLI installation must repair legacy copied launchers before updating")
    if "BEGIN PGP PUBLIC KEY BLOCK" not in cloud_text or "FB5DB77FD5C118B80511ADA8A6310ACC4672475C" not in cloud_text:
        raise SystemExit("AWS CLI installation must carry its pinned signing key and fingerprint")
    if "VALIDSIGFB5DB77FD5C118B80511ADA8A6310ACC4672475C" not in cloud_text:
        raise SystemExit("AWS CLI signature validation must normalize GPG status output correctly")
    if "Check if the AWS CLI install tree is present" not in cloud_text or "aws_cli_install_tree.stat.exists" not in cloud_text:
        raise SystemExit("AWS CLI installation must preserve update mode when repairing a launcher")
    if "^glab_.*_linux_amd64[.]tar[.]gz$" not in cloud_text:
        raise SystemExit("GitLab CLI selection must match the current Linux amd64 archive")
    if cloud_text.count("replace('%2E', '.')") < 2:
        raise SystemExit("GitLab CLI URLs must normalize encoded dots before checksum verification")
    if "include: [bin/glab]" not in cloud_text:
        raise SystemExit("GitLab CLI extraction must select the binary's current archive member")

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
