"""Post-update healthchecks of disabled applications warn instead of rolling back."""

import re
from pathlib import Path

import jinja2
import yaml


ROLE = Path(__file__).resolve().parents[1] / "roles/developer_workstation"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
HEALTH = yaml.safe_load((ROLE / "tasks/update-health.yml").read_text())
FOUNDATION = yaml.safe_load((ROLE / "tasks/update-foundation.yml").read_text())
MANIFEST = (ROLE / "tasks/update-manifest.yml").read_text()

CONTRACTS = {
    **DEFAULTS["latest_release_contracts"],
    **DEFAULTS["workstation_application_contracts"],
}
# Contract name -> the feature flag that controls whether the app is installed.
EXPECTED_CONDITIONS = {
    "sunshine": "sunshine_enabled",
    "coder": "coder_cli_enabled",
    "aws": "cloud_clis_enabled",
    "glab": "cloud_clis_enabled",
    "codex": "codex_standalone_enabled",
    "litra": "litra_cli_enabled",
}
# Flags derived from other configuration (a template string) rather than a
# literal boolean; tests/test_litra_cli.py evaluates the litra expression.
DERIVED_FLAGS = {"litra_cli_enabled"}


def _task(tasks: list[dict], name: str) -> dict:
    return next(t for t in tasks if t.get("name") == name)


def _env(variables: dict) -> jinja2.Environment:
    """Bare Jinja plus the Ansible pieces the templates use.

    `lookup('vars', name, default=...)` is emulated, `match` is Ansible's
    regex-at-start test, and `bool` follows Ansible's string coercion.
    """

    def lookup(plugin: str, name: str, default=None):
        assert plugin == "vars"
        return variables.get(name, default)

    env = jinja2.Environment()
    env.globals["lookup"] = lookup
    env.tests["match"] = lambda value, pattern: re.match(pattern, value) is not None
    env.filters["bool"] = lambda value: str(value).lower() in ("true", "1", "yes")
    return env


def _render(template: str, variables: dict):
    rendered = _env(variables).from_string(template).render(**variables)
    return yaml.safe_load(rendered)


def _definitions(contracts: dict, variables: dict) -> list[dict]:
    task = _task(HEALTH, "Build healthcheck definitions from application contracts")
    template = task["ansible.builtin.set_fact"]["workstation_update_healthcheck_definitions"]
    out: list = []
    for key, value in contracts.items():
        out = _render(
            template,
            {
                **variables,
                "workstation_update_healthcheck_definitions": out,
                "item": {"key": key, "value": value},
                "dev_user_home": "/home/dev",
            },
        )
    return out


def _validation_conditions() -> list[str]:
    task = _task(FOUNDATION, "Validate every workstation application contract")
    return [" ".join(str(c).split()) for c in task["ansible.builtin.assert"]["that"]]


def _contract_is_valid(contract: dict) -> bool:
    env = _env({})
    return all(
        env.from_string("{{ " + cond + " }}").render(item={"value": contract}) == "True"
        for cond in _validation_conditions()
    )


def test_opt_in_application_contracts_name_their_feature_flag() -> None:
    for name, flag in EXPECTED_CONDITIONS.items():
        assert CONTRACTS[name]["enabled_when"] == flag, name
        if flag in DERIVED_FLAGS:
            assert isinstance(DEFAULTS[flag], str), f"{flag} must be a derived default"
        else:
            assert isinstance(DEFAULTS[flag], bool), f"{flag} must be a boolean default"
    conditioned = {n for n, c in CONTRACTS.items() if "enabled_when" in c}
    assert conditioned == set(EXPECTED_CONDITIONS)


def test_sunshine_condition_matches_its_install_gate() -> None:
    gates = (ROLE / "tasks/managed-updates.yml").read_text() + (ROLE / "tasks/main.yml").read_text()
    assert "streaming.yml" in gates
    assert "sunshine_enabled | default(false)" in gates
    assert DEFAULTS["sunshine_enabled"] is False


def test_contract_validation_accepts_only_bare_variable_names() -> None:
    base = {"installer": "x", "verification": "y", "healthcheck": ["/bin/true"]}
    assert _contract_is_valid(base)
    assert _contract_is_valid({**base, "enabled_when": "sunshine_enabled"})
    assert not _contract_is_valid({**base, "enabled_when": "sunshine_enabled and other"})
    assert not _contract_is_valid({**base, "enabled_when": "{{ sunshine_enabled }}"})
    assert not _contract_is_valid({**base, "enabled_when": True})


def test_every_shipped_contract_passes_the_validation_assert() -> None:
    for name, contract in CONTRACTS.items():
        assert _contract_is_valid(contract), name


def test_definitions_follow_the_feature_flag_and_default_to_enabled() -> None:
    contracts = {
        "gated": {"healthcheck": ["/bin/false"], "enabled_when": "feature_on"},
        "plain": {"healthcheck": ["/bin/true"]},
        "ghost": {"healthcheck": ["/bin/false"], "enabled_when": "never_defined"},
        "stringy": {"healthcheck": ["/bin/false"], "enabled_when": "feature_text"},
    }
    on = {d["name"]: d["enabled"] for d in _definitions(contracts, {"feature_on": True, "feature_text": "false"})}
    off = {d["name"]: d["enabled"] for d in _definitions(contracts, {"feature_on": False, "feature_text": "true"})}
    assert on == {"gated": True, "plain": True, "ghost": True, "stringy": False}
    assert off == {"gated": False, "plain": True, "ghost": True, "stringy": True}


def _inventories(system: list[dict], user: list[dict], dpkg_rc: int = 0) -> tuple[list, list]:
    task = _task(HEALTH, "Build healthcheck failure inventory")
    facts = task["ansible.builtin.set_fact"]
    variables = {
        "_workstation_system_healthchecks": {"results": system},
        "_workstation_user_healthchecks": {"results": user},
        "_workstation_dpkg_audit": {"rc": dpkg_rc},
    }
    failures = _render(facts["workstation_update_healthcheck_failures"], variables)
    warnings = _render(facts["workstation_update_healthcheck_warnings"], variables)
    return failures, warnings


def _result(name: str, rc: int | None, enabled: bool) -> dict:
    result: dict = {"item": {"item": {"name": name, "enabled": enabled}}}
    if rc is not None:
        result["rc"] = rc
    return result


def test_failures_of_disabled_applications_become_warnings() -> None:
    failures, warnings = _inventories(
        [_result("sunshine", 127, False), _result("lazygit", 0, True)],
        [_result("codex", 1, False)],
    )
    assert failures == []
    assert warnings == ["sunshine", "codex"]


def test_failures_of_enabled_applications_still_fail_hard() -> None:
    failures, warnings = _inventories(
        [_result("sunshine", 1, True), _result("coder", 1, False)],
        [_result("pnpm", 1, True)],
        dpkg_rc=1,
    )
    assert failures == ["sunshine", "pnpm", "dpkg"]
    assert warnings == ["coder"]


def test_failures_without_an_enabled_key_still_fail_hard() -> None:
    legacy = {"item": {"item": {"name": "hostcheck"}}, "rc": 1}
    failures, warnings = _inventories([], [legacy])
    assert failures == ["hostcheck"]
    assert warnings == []


def test_skipped_checks_without_rc_are_ignored() -> None:
    failures, warnings = _inventories([_result("sunshine", None, False)], [_result("npm", None, True)])
    assert failures == []
    assert warnings == []


def test_builtin_user_healthchecks_are_explicitly_enabled() -> None:
    task = _task(HEALTH, "Add user-scoped npm healthchecks")
    entries = task["ansible.builtin.set_fact"]["workstation_update_user_healthchecks"]
    assert {e["name"] for e in entries} == {"npm", "pnpm", "playwright"}
    assert all(e["enabled"] is True for e in entries)


def test_warning_is_reported_and_only_failures_gate_the_transaction() -> None:
    warn = _task(HEALTH, "Warn about failing healthchecks of disabled applications")
    assert "workstation_update_healthcheck_warnings | default([]) | length > 0" in warn["when"]
    gate = _task(HEALTH, "Require all installed application healthchecks to pass")
    assert gate["ansible.builtin.assert"]["that"] == [
        "workstation_update_healthcheck_failures | default([]) | length == 0"
    ]
    assert "'healthcheck_warnings': workstation_update_healthcheck_warnings | default([])" in MANIFEST
