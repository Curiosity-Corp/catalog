"""Bitwarden extension force-install for Chrome, Edge and Brave.

The Firefox policy in collaboration.yml already force-installs the extension for
Firefox. The Chromium-family browsers get one managed-policy file each, written
only when that browser is installed. These tests render the real task
expressions with Ansible's own filters and tests.
"""

import json
from pathlib import Path

import jinja2
import pytest
import yaml


ROLE = Path(__file__).resolve().parents[1] / "roles/developer_workstation"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
COLLABORATION = yaml.safe_load((ROLE / "tasks/collaboration.yml").read_text())

DETECT = "Detect installed Chromium-family browsers for the Bitwarden extension"
CREATE_DIR = "Ensure Chromium-family managed policy directories exist"
DEPLOY = "Force-install the Bitwarden extension in Chromium-family browsers"

TARGETS = DEFAULTS["bitwarden_chromium_extension_targets"]


def _task(name: str) -> dict:
    return next(t for t in COLLABORATION if t.get("name") == name)


def _environment() -> jinja2.Environment:
    from ansible.plugins.filter.core import FilterModule
    from ansible.plugins.test.core import TestModule

    # Ansible's templar keeps a template's trailing newline; so must the test.
    env = jinja2.Environment(keep_trailing_newline=True)
    env.filters.update(FilterModule().filters())
    env.tests.update(TestModule().tests())
    # Outside Ansible's templar its `default` filter returns Undefined for an
    # undefined variable; use Jinja's so `| default(true)` behaves as in a play.
    env.filters["default"] = jinja2.filters.do_default
    return env


def _render(template: str, **variables: object) -> str:
    assert isinstance(template, str), f"expected a string expression, got {template!r}"
    return _environment().from_string(template).render(**variables)


def _evaluate(template: str, **variables: object) -> object:
    """Evaluate a `{{ expression }}` YAML scalar to a Python object, as a loop does."""
    assert isinstance(template, str), f"expected a string expression, got {template!r}"
    body = template.strip()
    assert body.startswith("{{") and body.endswith("}}"), body
    return _environment().compile_expression(body[2:-2].strip())(**variables)


def _stat_results(*existing: str) -> dict:
    """What `stat` registers when looped over the default targets."""
    return {
        "results": [
            {"item": target, "stat": {"exists": target["name"] in existing}}
            for target in TARGETS
        ]
    }


def _installed(*existing: str) -> list[dict]:
    return _evaluate(
        _task(DEPLOY)["loop"],
        _bitwarden_browser_binaries=_stat_results(*existing),
    )


def test_the_switch_defaults_on():
    assert DEFAULTS["bitwarden_browser_extensions_enabled"] is True


def test_targets_cover_chrome_edge_and_brave_with_distinct_policy_dirs():
    by_name = {t["name"]: t for t in TARGETS}
    assert set(by_name) == {"chrome", "edge", "brave"}
    assert by_name["chrome"]["policy_dir"] == "/etc/opt/chrome/policies/managed"
    assert by_name["edge"]["policy_dir"] == "/etc/opt/edge/policies/managed"
    assert by_name["brave"]["policy_dir"] == "/etc/brave/policies/managed"
    assert len({t["policy_dir"] for t in TARGETS}) == 3


@pytest.mark.parametrize("target", TARGETS, ids=lambda t: t["name"])
def test_each_target_names_a_real_store_entry_over_https(target):
    assert len(target["extension_id"]) == 32
    assert set(target["extension_id"]) <= set("abcdefghijklmnop")
    assert target["update_url"].startswith("https://")
    assert target["binary"].startswith("/")


def test_chrome_and_brave_use_the_chrome_web_store_and_edge_its_own_store():
    by_name = {t["name"]: t for t in TARGETS}
    assert by_name["chrome"]["extension_id"] == "nngceckbapebfimnlniiiahkandclblb"
    assert by_name["brave"]["extension_id"] == by_name["chrome"]["extension_id"]
    assert by_name["chrome"]["update_url"] == by_name["brave"]["update_url"]
    assert by_name["edge"]["extension_id"] == "jbkfoedolllekgbhcbcoahefnbanhhlh"
    assert "edge.microsoft.com" in by_name["edge"]["update_url"]


def test_tasks_run_in_detect_directory_deploy_order_right_after_firefox():
    names = [t.get("name") for t in COLLABORATION]
    assert names.index("Deploy Firefox enterprise policies") < names.index(DETECT)
    assert names.index(DETECT) < names.index(CREATE_DIR) < names.index(DEPLOY)


def test_policy_file_is_root_owned_world_readable_and_never_replaces_another_file():
    copy = _task(DEPLOY)["ansible.builtin.copy"]
    assert (copy["owner"], copy["group"], copy["mode"]) == ("root", "root", "0644")
    assert copy["dest"].endswith("/20-bitwarden-extension.json")
    directory = _task(CREATE_DIR)["ansible.builtin.file"]
    assert (directory["owner"], directory["group"], directory["mode"]) == ("root", "root", "0755")
    assert directory["state"] == "directory"


@pytest.mark.parametrize("name", [DETECT, CREATE_DIR, DEPLOY])
def test_every_task_is_gated_by_the_switch(name):
    when = _task(name)["when"]
    assert isinstance(when, str)
    env = _environment()
    assert env.from_string("{{ " + when + " }}").render() == "True"
    off = env.from_string("{{ " + when + " }}").render(bitwarden_browser_extensions_enabled=False)
    assert off == "False"


def test_only_installed_browsers_get_a_policy():
    assert [t["name"] for t in _installed("chrome", "brave")] == ["chrome", "brave"]
    assert [t["name"] for t in _installed("edge")] == ["edge"]
    assert _installed() == []


def test_directory_and_deploy_loops_select_the_same_browsers():
    stat = _stat_results("chrome", "edge")
    deploy = _evaluate(_task(DEPLOY)["loop"], _bitwarden_browser_binaries=stat)
    directory = _evaluate(_task(CREATE_DIR)["loop"], _bitwarden_browser_binaries=stat)
    assert deploy == directory
    assert [t["name"] for t in deploy] == ["chrome", "edge"]


def test_loops_tolerate_the_detect_task_being_skipped():
    # With the switch off the detect task is skipped, so it registers results
    # whose items carry no `stat` (or nothing at all).
    skipped = {"results": [{"item": t, "skipped": True} for t in TARGETS]}
    for name in (DEPLOY, CREATE_DIR):
        assert _evaluate(_task(name)["loop"], _bitwarden_browser_binaries=skipped) == []
        assert _evaluate(_task(name)["loop"]) == []


@pytest.mark.parametrize("target", TARGETS, ids=lambda t: t["name"])
def test_rendered_policy_is_valid_json_with_one_forcelist_entry(target):
    content = _render(_task(DEPLOY)["ansible.builtin.copy"]["content"], item=target)
    policy = json.loads(content)
    assert policy == {"ExtensionInstallForcelist": [f"{target['extension_id']};{target['update_url']}"]}
    assert content.endswith("\n")
