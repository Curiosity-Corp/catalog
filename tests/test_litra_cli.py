"""The Litra CLI is installed only on hardware profiles that opt in."""

import re
from pathlib import Path

import jinja2
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
TASKS = yaml.safe_load((ROLE / "tasks/litra-cli.yml").read_text())
MANAGED_UPDATES = yaml.safe_load((ROLE / "tasks/managed-updates.yml").read_text())
HEALTH = yaml.safe_load((ROLE / "tasks/update-health.yml").read_text())

ASSETS = [
    "litra_v3.3.0_darwin-aarch64",
    "litra_v3.3.0_darwin-amd64",
    "litra_v3.3.0_darwin-universal",
    "litra_v3.3.0_linux-aarch64",
    "litra_v3.3.0_linux-amd64",
    "litra_v3.3.0_windows-amd64.exe",
    "litra_v3.3.0_windows-arm64.exe",
]
DIGEST = "sha256:" + "ab" * 32


def _task(tasks: list[dict], name: str) -> dict:
    return next(t for t in tasks if t.get("name") == name)


def _environment() -> jinja2.Environment:
    from ansible.plugins.filter.core import FilterModule
    from ansible.plugins.test.core import TestModule

    env = jinja2.Environment()
    env.filters.update(FilterModule().filters())
    env.tests.update(TestModule().tests())
    return env


def _enabled(profiles: dict, hardware_profile: str) -> bool:
    """Evaluate the shipped litra_cli_enabled default for a profile set."""
    template = DEFAULTS["litra_cli_enabled"]
    rendered = _environment().from_string(template).render(
        hardware_profiles=profiles, hardware_profile=hardware_profile
    )
    return rendered.strip() == "True"


def _selected_asset(architecture: str) -> dict:
    env = _environment()
    facts = {"ansible_architecture": architecture}
    for key, template in _task(TASKS, "Resolve the latest Litra CLI release asset for this architecture")[
        "ansible.builtin.set_fact"
    ].items():
        facts[key] = env.from_string(template).render(**facts).strip()
    facts["latest_github_releases"] = {
        "litra": {"assets": [{"name": name, "digest": DIGEST} for name in ASSETS]}
    }
    template = _task(TASKS, "Select the latest Litra CLI binary")["ansible.builtin.set_fact"][
        "_latest_litra_asset"
    ]
    expression = template.strip().removeprefix("{{").removesuffix("}}")
    return env.compile_expression(expression)(**facts) or {}


def test_only_the_deskmeet_b760_profile_opts_in() -> None:
    profiles = DEFAULTS["hardware_profiles"]
    opted_in = {name for name, profile in profiles.items() if profile.get("litra_cli")}
    assert opted_in == {"deskmeet-b760"}
    for name, profile in profiles.items():
        assert profile.get("litra_cli", False) in (True, False), name
    # The opt-in rides on the profile that already grants the Litra Beam
    # hidraw access.
    joined = "\n".join(rule["content"] for rule in profiles["deskmeet-b760"]["udev_rules"])
    assert 'idProduct}=="c901"' in joined


def test_litra_cli_enabled_derives_from_the_hardware_profile() -> None:
    profiles = DEFAULTS["hardware_profiles"]
    assert _enabled(profiles, "deskmeet-b760") is True
    for name in profiles:
        if name != "deskmeet-b760":
            assert _enabled(profiles, name) is False, name
    # Hosts with a profile that is unknown or lacks the key stay off.
    assert _enabled(profiles, "no-such-profile") is False
    assert _enabled({"x": {"description": "x"}}, "x") is False
    assert _enabled({"x": {"litra_cli": False}}, "x") is False


def test_repository_contract_and_managed_path_are_declared() -> None:
    assert DEFAULTS["latest_github_repositories"]["litra"] == "timrogers/litra-rs"
    contract = DEFAULTS["latest_release_contracts"]["litra"]
    assert contract["installer"] == "github-binary"
    assert contract["verification"] == "github-asset-digest"
    assert contract["enabled_when"] == "litra_cli_enabled"
    assert contract["healthcheck"] == ["/usr/local/bin/litra", "--version"]
    assert "/usr/local/bin/litra" in DEFAULTS["workstation_update_managed_paths"]
    assert "litra" in DEFAULTS["workstation_update_transaction_tags"]
    assert set(DEFAULTS["latest_github_repositories"]) == set(DEFAULTS["latest_release_contracts"])


def test_litra_is_an_opt_in_install_that_is_never_unconditional() -> None:
    include = _task(MANAGED_UPDATES, "Include Litra CLI")
    assert include["ansible.builtin.include_tasks"]["file"] == "litra-cli.yml"
    assert "litra_cli_enabled | default(false) | bool" in include["when"]
    assert "workstation_update_install_allowed | default(true)" in include["when"]
    # No other task file pulls in the installer, so a disabled host never runs it.
    for path in (ROLE / "tasks").glob("*.yml"):
        if path.name in ("managed-updates.yml", "litra-cli.yml"):
            continue
        assert "litra-cli.yml" not in path.read_text(), path.name


def test_disabled_hosts_only_warn_about_a_stale_litra_binary() -> None:
    task = _task(HEALTH, "Build healthcheck definitions from application contracts")
    template = task["ansible.builtin.set_fact"]["workstation_update_healthcheck_definitions"]
    contract = DEFAULTS["latest_release_contracts"]["litra"]

    def definition(enabled: bool) -> dict:
        variables = {
            "workstation_update_healthcheck_definitions": [],
            "item": {"key": "litra", "value": contract},
            "dev_user_home": "/home/dev",
        }
        env = jinja2.Environment()
        env.globals["lookup"] = lambda plugin, name, default=None: enabled if name == "litra_cli_enabled" else default
        env.tests["match"] = lambda value, pattern: re.match(pattern, value) is not None
        env.filters["bool"] = lambda value: bool(value)
        (entry,) = yaml.safe_load(env.from_string(template).render(**variables))
        return entry

    assert definition(True)["enabled"] is True
    assert definition(False)["enabled"] is False
    assert definition(True)["argv"] == ["/usr/local/bin/litra", "--version"]


@pytest.mark.parametrize(
    ("architecture", "expected"),
    [
        ("x86_64", "litra_v3.3.0_linux-amd64"),
        ("aarch64", "litra_v3.3.0_linux-aarch64"),
    ],
)
def test_asset_selection_follows_the_host_architecture(architecture: str, expected: str) -> None:
    assert _selected_asset(architecture)["name"] == expected


def test_unsupported_architecture_selects_nothing_so_the_assert_fails() -> None:
    assert _selected_asset("armv7l") == {}
    assertion = _task(TASKS, "Require a verifiable Litra CLI asset")["ansible.builtin.assert"]["that"]
    assert any("_litra_arch | length > 0" in condition for condition in assertion)
    assert any("sha256:[0-9a-f]{64}" in condition for condition in assertion)


def test_install_verifies_the_digest_and_installs_atomically_as_root() -> None:
    download = _task(TASKS, "Download the latest Litra CLI")["ansible.builtin.get_url"]
    assert download["checksum"] == "{{ _latest_litra_asset.digest }}"
    install = _task(TASKS, "Atomically install the latest Litra CLI")["ansible.builtin.copy"]
    assert install["dest"] == "/usr/local/bin/litra"
    assert install["remote_src"] is True
    assert install["mode"] == "0755"
    assert install["owner"] == "root"
