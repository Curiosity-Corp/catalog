"""Chromium-family extensions a host removes (chromium_removed_extensions).

collaboration.yml writes one ExtensionSettings policy per installed Chrome,
Edge or Brave that sets installation_mode "removed" for every listed store id,
and deletes it when the list is empty. These tests render the real task
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

CHECK = "Check the removed Chromium-family extension ids"
DETECT = "Detect installed Chromium-family browsers for removed extensions"
CREATE_DIR = "Ensure managed policy directories exist for removed extensions"
DEPLOY = "Remove the listed extensions in Chromium-family browsers"
DELETE = "Delete the removed-extensions policy when no extension is listed"

TARGETS = DEFAULTS["bitwarden_chromium_extension_targets"]
LASTPASS = "hdokiejnpimakedhajhdlcegeplioahd"
OTHER = "aeblfdkhhhdcdjpifhhbdiojplfjncoa"


def _task(name: str) -> dict:
    return next(t for t in COLLABORATION if t.get("name") == name)


def _environment() -> jinja2.Environment:
    from ansible.plugins.filter.core import FilterModule
    from ansible.plugins.test.core import TestModule

    env = jinja2.Environment(keep_trailing_newline=True)
    env.filters.update(FilterModule().filters())
    env.tests.update(TestModule().tests())
    env.filters["default"] = jinja2.filters.do_default
    return env


def _render(template: str, **variables: object) -> str:
    return _environment().from_string(template).render(**variables)


def _evaluate(template: str, **variables: object) -> object:
    body = template.strip()
    assert body.startswith("{{") and body.endswith("}}"), body
    return _environment().compile_expression(body[2:-2].strip())(**variables)


def _true(expression: str, **variables: object) -> bool:
    return _render("{{ " + expression + " }}", **variables) == "True"


def _stat_results(*existing: str) -> dict:
    return {"results": [{"item": t, "stat": {"exists": t["name"] in existing}} for t in TARGETS]}


def test_nothing_is_removed_by_default():
    assert DEFAULTS["chromium_removed_extensions"] == []


def test_tasks_follow_the_bitwarden_policy_in_order():
    names = [t.get("name") for t in COLLABORATION]
    bitwarden = names.index("Force-install the Bitwarden extension in Chromium-family browsers")
    order = [names.index(n) for n in (CHECK, DETECT, CREATE_DIR, DEPLOY, DELETE)]
    assert bitwarden < order[0] and order == sorted(order)


@pytest.mark.parametrize(
    "value, ok",
    [
        ([], True),
        ([LASTPASS], True),
        ([LASTPASS, OTHER], True),
        (["not-an-id"], False),
        ([LASTPASS.upper()], False),
        (LASTPASS, False),
    ],
)
def test_ids_are_checked(value, ok):
    that = _task(CHECK)["ansible.builtin.assert"]["that"]
    assert all(_true(cond, chromium_removed_extensions=value) for cond in that) is ok


def test_policy_is_written_only_for_installed_browsers_and_a_non_empty_list():
    stat = _stat_results("chrome", "edge")
    for name in (CREATE_DIR, DEPLOY):
        task = _task(name)
        assert [t["name"] for t in _evaluate(task["loop"], _chromium_removed_extension_binaries=stat)] == [
            "chrome",
            "edge",
        ]
        assert _true(task["when"], chromium_removed_extensions=[LASTPASS])
        assert not _true(task["when"], chromium_removed_extensions=[])
    assert _true(_task(DELETE)["when"], chromium_removed_extensions=[])
    assert not _true(_task(DELETE)["when"], chromium_removed_extensions=[LASTPASS])


def test_detection_is_not_tied_to_the_bitwarden_switch():
    assert "when" not in _task(DETECT)


@pytest.mark.parametrize("target", TARGETS, ids=lambda t: t["name"])
def test_rendered_policy_removes_every_listed_extension(target):
    copy = _task(DEPLOY)["ansible.builtin.copy"]
    content = _render(copy["content"], chromium_removed_extensions=[LASTPASS, OTHER], item=target)
    assert json.loads(content) == {
        "ExtensionSettings": {
            LASTPASS: {"installation_mode": "removed"},
            OTHER: {"installation_mode": "removed"},
        }
    }
    assert content.endswith("\n")
    assert _render(copy["dest"], item=target) == f"{target['policy_dir']}/25-removed-extensions.json"
    assert (copy["owner"], copy["group"], copy["mode"]) == ("root", "root", "0644")
    assert _render(_task(DELETE)["ansible.builtin.file"]["path"], item=target) == _render(copy["dest"], item=target)


def test_no_other_chromium_policy_file_sets_extension_settings():
    # A browser keeps only the last file that sets a policy; the Bitwarden file
    # must keep using ExtensionInstallForcelist, not ExtensionSettings.
    bitwarden = _task("Force-install the Bitwarden extension in Chromium-family browsers")
    assert "ExtensionSettings" not in bitwarden["ansible.builtin.copy"]["content"]
