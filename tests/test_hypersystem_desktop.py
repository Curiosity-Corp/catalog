"""hypersystem-desktop installs a suite's web apps on a laptop (catalog#142).

These checks pin the catalog parsing and role selection (the filters), the role
wiring (off by default, after the ziti task group, never configuring Ziti), the
launcher template, and the `hypersystem-desktop` helper against stub
`systemctl` and `getent` commands. A last group runs the task file through
`ansible-playbook --check` as an unprivileged user, so the whole flow from a
catalog file to the policy, configuration and launchers is exercised without a
browser, a tunnel or root.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import jinja2
import pytest
import yaml
from ansible.errors import AnsibleFilterError

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
HELPER = ROLE / "files/hypersystem-desktop"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())


def _load_filters():
    spec = importlib.util.spec_from_file_location("hypersystem_desktop_filters", ROLE / "filter_plugins/hypersystem_desktop.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


F = _load_filters()


class _Loader(yaml.SafeLoader):
    """Safe loader that reads Ansible's `!unsafe` scalars as plain strings."""


_Loader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))


def _entry(app_id: str, roles: list[str], status: str = "ga", **extra: object) -> dict:
    entry = {
        "id": app_id,
        "name": app_id.title(),
        "purpose": f"{app_id} purpose",
        "icon": "home",
        "owner_group": "group",
        "repo": f"group/{app_id}",
        "roles": roles,
        "urls": {"web": f"https://{app_id}.suite.example/"},
        "ssh": None,
        "status": status,
    }
    entry.update(extra)
    return entry


CATALOG = {
    "schema": "hypersystem-catalog/v1",
    "apps": [
        _entry("home", [], "planned"),
        _entry("ledger", ["suite-finance", "suite-exec"], "beta"),
        _entry("sales", []),
        _entry("comms", ["suite-comms"]),
        _entry("old", [], "retired"),
    ],
}


# ---------------------------------------------------------------------------
# Catalog parsing and selection
# ---------------------------------------------------------------------------


def test_compiled_document_parses_from_json_text() -> None:
    parsed = F.hypersystem_desktop_parse_catalog(json.dumps(CATALOG), "catalog.json")
    assert parsed["problems"] == []
    assert [app["id"] for app in parsed["apps"]] == ["home", "ledger", "sales", "comms", "old"]
    ledger = parsed["apps"][1]
    assert ledger["host"] == "ledger.suite.example"
    assert ledger["urls"] == {"web": "https://ledger.suite.example/"}
    assert ledger["roles"] == ["suite-finance", "suite-exec"]


def test_bare_list_is_accepted_like_the_bar() -> None:
    parsed = F.hypersystem_desktop_parse_catalog(CATALOG["apps"])
    assert len(parsed["apps"]) == 5


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("{not json", "is not valid JSON"),
        ({"schema": "suite-catalog/v1", "apps": []}, "expected 'hypersystem-catalog/v1'"),
        ({"schema": "hypersystem-catalog/v1"}, "has no `apps` list"),
        ("42", "must be a list of apps"),
    ],
)
def test_a_document_that_is_not_a_catalog_raises(document: object, message: str) -> None:
    with pytest.raises(AnsibleFilterError, match=message):
        F.hypersystem_desktop_parse_catalog(document, "src")


def test_malformed_and_duplicate_entries_are_dropped_and_reported() -> None:
    apps = [
        _entry("good", []),
        _entry("Bad_Id", []),
        _entry("plain", [], urls={"web": "http://plain.suite.example/"}),
        _entry("script", [], urls={"web": "javascript:alert(1)"}),
        _entry("badrole", [""]),
        _entry("badstatus", [], "live"),
        _entry("badlocal", [], urls={"web": "https://x.suite.example/", "local": "ftp://x"}),
        _entry("newline", [], name="two\nlines"),
        "not a mapping",
        _entry("good", []),
    ]
    parsed = F.hypersystem_desktop_parse_catalog(apps)
    assert [app["id"] for app in parsed["apps"]] == ["good"]
    problems = "\n".join(parsed["problems"])
    for expected in (
        "Bad_Id: id 'Bad_Id' must be lowercase",
        "plain: urls.web must be an absolute https URL",
        "script: urls.web must be an absolute https URL",
        "badrole: roles must be a list of non-empty strings",
        "badstatus: status must be one of planned, beta, ga, retired",
        "badlocal: urls.local must be an absolute http(s) URL",
        "newline: name must not contain control characters",
        "entry 8: expected a mapping, got str",
        "good: duplicate id",
    ):
        assert expected in problems


def test_variables_mode_defaults_roles_and_status() -> None:
    parsed = F.hypersystem_desktop_parse_catalog(
        [{"id": "notebooks", "name": "Notebooks", "urls": {"web": "https://notebooks.lab.example.edu/"}}],
        "hypersystem_desktop_apps",
        "variables",
    )
    assert parsed["problems"] == []
    assert parsed["apps"][0]["roles"] == [] and parsed["apps"][0]["status"] == "ga"
    # The compiled document carries every field; there a missing status is a problem.
    strict = F.hypersystem_desktop_parse_catalog([{"id": "x", "name": "X", "roles": [], "urls": {"web": "https://x.example/"}}])
    assert "x: status must be one of" in strict["problems"][0]


def test_local_url_is_kept_for_per_machine_apps() -> None:
    parsed = F.hypersystem_desktop_parse_catalog([_entry("harness", [], urls={"web": "https://harness.suite.example/", "local": "http://127.0.0.1:7681/"})])
    assert parsed["apps"][0]["urls"]["local"] == "http://127.0.0.1:7681/"


@pytest.mark.parametrize(
    ("roles", "expected"),
    [
        ([], ["sales"]),
        (["suite-finance"], ["ledger", "sales"]),
        (["suite-comms", "suite-exec"], ["ledger", "sales", "comms"]),
    ],
)
def test_selection_keeps_usable_apps_the_roles_allow(roles: list[str], expected: list[str]) -> None:
    apps = F.hypersystem_desktop_parse_catalog(CATALOG)["apps"]
    selected = F.hypersystem_desktop_select(apps, roles, ["beta", "ga"])
    assert [app["id"] for app in selected] == expected


def test_selection_by_status_only() -> None:
    apps = F.hypersystem_desktop_parse_catalog(CATALOG)["apps"]
    assert [a["id"] for a in F.hypersystem_desktop_select(apps, ["suite-finance"], ["ga"])] == ["sales"]


def test_selection_rejects_non_lists() -> None:
    with pytest.raises(AnsibleFilterError):
        F.hypersystem_desktop_select([], "suite-finance", ["ga"])


def test_uncovered_hosts() -> None:
    apps = F.hypersystem_desktop_parse_catalog(CATALOG)["apps"]
    assert F.hypersystem_desktop_uncovered_hosts(apps, ["suite.example"]) == []
    assert F.hypersystem_desktop_uncovered_hosts(apps, ["*.suite.example"]) == []
    assert F.hypersystem_desktop_uncovered_hosts(apps, [".suite.example"]) == []
    assert F.hypersystem_desktop_uncovered_hosts(apps, ["ledger.suite.example"]) == [
        "home.suite.example",
        "sales.suite.example",
        "comms.suite.example",
        "old.suite.example",
    ]
    # A suffix must match on a label boundary.
    assert F.hypersystem_desktop_uncovered_hosts(apps[:1], ["e.example"]) == ["home.suite.example"]
    assert F.hypersystem_desktop_uncovered_hosts(apps[:1], []) == ["home.suite.example"]


def test_policy_force_installs_each_app_in_a_window() -> None:
    apps = F.hypersystem_desktop_parse_catalog(CATALOG)["apps"][1:3]
    assert F.hypersystem_desktop_policy(apps) == {
        "WebAppInstallForceList": [
            {"url": "https://ledger.suite.example/", "default_launch_container": "window", "create_desktop_shortcut": False, "fallback_app_name": "Ledger"},
            {"url": "https://sales.suite.example/", "default_launch_container": "window", "create_desktop_shortcut": False, "fallback_app_name": "Sales"},
        ]
    }


def test_launchers_map_icons_and_lead_with_the_suite_profile() -> None:
    apps = F.hypersystem_desktop_parse_catalog(CATALOG)["apps"][1:3]
    apps[1]["icon"] = "sales"
    launchers = F.hypersystem_desktop_launchers(apps, "HyperSystem", {"sales": "/usr/share/icons/sales.svg"}, "web-browser")
    assert [(item["file"], item["args"], item["icon"]) for item in launchers] == [
        ("hypersystem-desktop.desktop", "browser", "web-browser"),
        ("hypersystem-desktop-ledger.desktop", "open ledger", "web-browser"),
        ("hypersystem-desktop-sales.desktop", "open sales", "/usr/share/icons/sales.svg"),
    ]


def test_desktop_entry_values_are_escaped() -> None:
    assert F.hypersystem_desktop_entry_value("a\\b\nc") == "a\\\\b c"


def _render_launcher(entry: dict) -> str:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROLE / "templates")), undefined=jinja2.StrictUndefined, trim_blocks=True)
    env.filters["hypersystem_desktop_entry_value"] = F.hypersystem_desktop_entry_value
    return env.get_template("hypersystem-desktop-app.desktop.j2").render(_hsd_entry=entry, **DEFAULTS)


def test_launcher_template() -> None:
    text = _render_launcher({"file": "hypersystem-desktop-ledger.desktop", "name": "Ledger", "comment": "Money", "args": "open ledger", "icon": "web-browser"})
    lines = text.splitlines()
    assert lines[0] == "[Desktop Entry]"
    assert "Type=Application" in lines
    assert "Name=Ledger" in lines and "Comment=Money" in lines
    assert "Exec=/usr/local/bin/hypersystem-desktop open ledger" in lines
    assert "Terminal=false" in lines
    no_comment = _render_launcher({"file": "x.desktop", "name": "X", "comment": "", "args": "browser", "icon": "web-browser"})
    assert "Comment=" not in no_comment


# ---------------------------------------------------------------------------
# Role wiring
# ---------------------------------------------------------------------------


def test_defaults_are_off_and_name_nothing_private() -> None:
    assert DEFAULTS["hypersystem_desktop_enabled"] is False
    assert DEFAULTS["hypersystem_desktop_catalog_src"] == ""
    assert DEFAULTS["hypersystem_desktop_apps"] == []
    assert DEFAULTS["hypersystem_desktop_roles"] == []
    assert DEFAULTS["hypersystem_desktop_statuses"] == ["beta", "ga"]
    assert DEFAULTS["hypersystem_desktop_require_ziti"] is True
    assert DEFAULTS["hypersystem_desktop_catalog_strict"] is True
    assert DEFAULTS["hypersystem_desktop_hyperharness_enabled"] is False
    assert DEFAULTS["hypersystem_desktop_hyperharness_install_argv"] == []


def test_browser_targets_use_each_package_s_policy_directory() -> None:
    targets = DEFAULTS["hypersystem_desktop_browser_targets"]
    assert targets["chromium"]["policy_dir"] == "/etc/chromium/policies/managed"
    assert targets["chromium"]["install"] == "apt"
    # The Ubuntu Chromium snap reads the Ubuntu policy path, not /etc/chromium.
    assert targets["chromium-snap"]["policy_dir"] == "/etc/chromium-browser/policies/managed"
    assert targets["chromium-snap"]["install"] == "snap"
    # The snap can write only below ~/snap/chromium.
    assert targets["chromium-snap"]["user_data_dir"].startswith("snap/chromium/")
    assert targets["google-chrome"]["policy_dir"] == "/etc/opt/chrome/policies/managed"
    assert targets["google-chrome"]["install"] == "none"
    # Distinct from the Bitwarden extension policy file, so neither replaces the other.
    assert DEFAULTS["hypersystem_desktop_policy_file_name"] != "20-bitwarden-extension.json"


def _tasks(name: str) -> list[dict]:
    return yaml.load((ROLE / "tasks" / name).read_text(), Loader=_Loader)


def _walk(tasks: list[dict]):
    for task in tasks:
        yield task
        yield from _walk(task.get("block", []))


def test_include_follows_the_ziti_task_group_and_applies_its_tag() -> None:
    main = _tasks("main.yml")
    names = [task["name"] for task in main]
    include = next(task for task in main if task["name"].startswith("Include hypersystem-desktop"))
    assert include["ansible.builtin.include_tasks"] == {"file": "hypersystem-desktop.yml", "apply": {"tags": ["hypersystem-desktop"]}}
    assert include["tags"] == ["hypersystem-desktop"]
    assert "when" not in include  # always included, so turning it off retires the files
    position = names.index(include["name"])
    for ziti in ("Include Ziti edge tunnel + OIDC session hooks", "Include Ziti tunnel health check", "Include Ziti split-DNS configuration (dual-stack resolver)"):
        assert names.index(ziti) < position


def test_task_group_checks_ziti_but_never_configures_it() -> None:
    text = (ROLE / "tasks/hypersystem-desktop.yml").read_text()
    for forbidden in ("ziti-edge-tunnel tunnel", "resolvectl", "apt_repository", "openziti", "ziti.yml", "systemctl restart", "systemd:"):
        assert forbidden not in text
    by_name = {task["name"]: task for task in _walk(_tasks("hypersystem-desktop.yml"))}
    lookup = by_name["Look for the Ziti tunnel unit"]
    assert lookup["check_mode"] is False and lookup["changed_when"] is False and lookup["failed_when"] is False
    assert "ziti_enabled" in by_name["Require the Ziti tunnel"]["ansible.builtin.assert"]["that"][0]
    assert "hypersystem_desktop_require_ziti: false" in by_name["Require the Ziti tunnel"]["ansible.builtin.assert"]["fail_msg"]
    assert "ziti_intercept_domains" in by_name["Require every app host inside the Ziti intercept domains"]["ansible.builtin.assert"]["fail_msg"]


def test_catalog_fetch_is_read_only_in_check_mode_and_hides_credentials() -> None:
    by_name = {task["name"]: task for task in _walk(_tasks("hypersystem-desktop.yml"))}
    fetch = by_name["Fetch the suite catalog"]
    assert fetch["ansible.builtin.uri"]["method"] == "GET"
    assert fetch["check_mode"] is False
    assert "hypersystem_desktop_catalog_headers" in fetch["no_log"]


def test_disabled_feature_retires_only_after_an_install() -> None:
    tasks = _tasks("hypersystem-desktop.yml")
    retire = next(task for task in tasks if task["name"] == "Retire hypersystem-desktop when it is off")
    assert "_hsd_installed.stat.exists" in retire["when"]
    removed = json.dumps(retire["block"])
    for path in ("hypersystem_desktop_policy_file_name", "hypersystem-desktop*.desktop", "hypersystem_desktop_helper_path", "hypersystem_desktop_config_dir"):
        assert path in removed


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, *, required: bool = True, apps: list[dict] | None = None, policy_apps: list[dict] | None = None) -> Path:
    selected = apps if apps is not None else F.hypersystem_desktop_select(F.hypersystem_desktop_parse_catalog(CATALOG)["apps"], ["suite-finance"], ["beta", "ga"])
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(F.hypersystem_desktop_policy(policy_apps if policy_apps is not None else selected)))
    browser = tmp_path / "chromium"
    browser.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$(dirname \"$0\")/argv\"\n")
    browser.chmod(0o755)
    config = {
        "schema": "hypersystem-desktop/v1",
        "browser": {"name": "chromium", "binary": str(browser), "policy_file": str(policy), "user_data_dir": ".config/hypersystem-desktop/chromium"},
        "apps": selected,
        "ziti": {"required": required, "service": "ziti-edge-tunnel.service", "dns_ip_range": "100.64.0.0/10"},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return path


def _stubs(tmp_path: Path, *, active: str = "active", hosts: dict[str, str] | None = None) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "systemctl").write_text(f"#!/bin/sh\necho {active}\n[ {active} = active ]\n")
    lines = "\n".join(f'  {host}) echo "{ip}       STREAM {host}";;' for host, ip in (hosts or {}).items())
    (bin_dir / "getent").write_text(f'#!/bin/sh\ncase "$2" in\n{lines}\n  *) exit 2;;\nesac\n')
    for tool in ("systemctl", "getent"):
        (bin_dir / tool).chmod(0o755)
    return bin_dir


def _helper(tmp_path: Path, config: Path, *args: str, bin_dir: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HYPERSYSTEM_DESKTOP_CONFIG": str(config), "HOME": str(tmp_path / "home")}
    if bin_dir is not None:
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return subprocess.run([sys.executable, str(HELPER), *args], capture_output=True, text=True, env=env, timeout=60, check=False)


def test_helper_is_executable() -> None:
    assert stat.S_IMODE(HELPER.stat().st_mode) & 0o111


def test_helper_lists_apps(tmp_path: Path) -> None:
    result = _helper(tmp_path, _write_config(tmp_path), "list", "--json")
    assert result.returncode == 0, result.stderr
    assert [app["id"] for app in json.loads(result.stdout)] == ["ledger", "sales"]


def test_helper_opens_an_app_window_in_the_suite_profile(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    printed = _helper(tmp_path, config, "open", "ledger", "--print")
    assert printed.returncode == 0, printed.stderr
    assert f"--user-data-dir={tmp_path}/home/.config/hypersystem-desktop/chromium" in printed.stdout
    assert "--app=https://ledger.suite.example/" in printed.stdout
    ran = _helper(tmp_path, config, "open", "sales")
    assert ran.returncode == 0, ran.stderr
    argv = (tmp_path / "argv").read_text().splitlines()
    assert argv[-1] == "--app=https://sales.suite.example/"
    assert (tmp_path / "home/.config/hypersystem-desktop/chromium").is_dir()


def test_helper_refuses_unknown_apps_and_bad_config(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    unknown = _helper(tmp_path, config, "open", "comms", "--print")
    assert unknown.returncode == 2 and "no app 'comms'" in unknown.stderr and "ledger, sales" in unknown.stderr
    no_local = _helper(tmp_path, config, "open", "ledger", "--local", "--print")
    assert no_local.returncode == 2 and "urls.local" in no_local.stderr
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    assert _helper(tmp_path, broken, "list").returncode == 2
    assert _helper(tmp_path, tmp_path / "missing.json", "list").returncode == 2


def test_doctor_passes_through_the_tunnel(tmp_path: Path) -> None:
    bin_dir = _stubs(tmp_path, hosts={"ledger.suite.example": "100.64.0.7", "sales.suite.example": "100.64.0.8"})
    result = _helper(tmp_path, _write_config(tmp_path), "doctor", "--json", "--offline", bin_dir=bin_dir)
    report = json.loads(result.stdout)
    assert result.returncode == 0, result.stdout
    assert {check["name"]: check["status"] for check in report["checks"]} == {"browser": "ok", "policy": "ok", "ziti": "ok"}


def test_doctor_fails_on_a_down_tunnel_and_a_missing_policy_entry(tmp_path: Path) -> None:
    selected = F.hypersystem_desktop_select(F.hypersystem_desktop_parse_catalog(CATALOG)["apps"], ["suite-finance"], ["ga", "beta"])
    config = _write_config(tmp_path, policy_apps=selected[:1])
    result = _helper(tmp_path, config, "doctor", "--offline", bin_dir=_stubs(tmp_path, active="inactive"))
    assert result.returncode == 1
    assert "FAIL  ziti: ziti-edge-tunnel.service is inactive" in result.stdout
    assert "FAIL  policy:" in result.stdout and "https://sales.suite.example/" in result.stdout


def test_doctor_dns_must_land_in_the_ziti_range(tmp_path: Path) -> None:
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader("hypersystem_desktop_helper", str(HELPER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    helper = importlib.util.module_from_spec(spec)
    # dataclasses resolve string annotations through sys.modules.
    sys.modules[loader.name] = helper
    loader.exec_module(helper)
    config = helper.load_config(str(_write_config(tmp_path)))
    bin_dir = _stubs(tmp_path, hosts={"ledger.suite.example": "100.64.0.7", "sales.suite.example": "203.0.113.5"})
    old_path = os.environ["PATH"]
    os.environ["PATH"] = f"{bin_dir}:{old_path}"
    try:
        report = helper.Report()
        helper.check_dns(config, report, 5.0)
        config["ziti"]["required"] = False
        relaxed = helper.Report()
        helper.check_dns(config, relaxed, 5.0)
    finally:
        os.environ["PATH"] = old_path
    statuses = {check.name: (check.status, check.detail) for check in report.checks}
    assert statuses["dns ledger"][0] == "ok"
    assert statuses["dns sales"][0] == "fail" and "bypass the tunnel" in statuses["dns sales"][1]
    assert all(check.status == "ok" for check in relaxed.checks)


# ---------------------------------------------------------------------------
# The task file end to end, in check mode as an unprivileged user
# ---------------------------------------------------------------------------

ANSIBLE_PLAYBOOK = shutil.which("ansible-playbook")

# A notification callback that saves every task result by task name, so the
# default stdout callback (and its failure messages) stays in place.
RECORDER = """
import json
import os

from ansible.plugins.callback import CallbackBase


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "notification"
    CALLBACK_NAME = "record_results"
    CALLBACK_NEEDS_ENABLED = True

    def __init__(self):
        super().__init__()
        self.results = {}

    def _record(self, result, *args, **kwargs):
        self.results[result._task.name] = json.loads(json.dumps(result._result, default=str))

    v2_runner_on_ok = v2_runner_on_failed = v2_runner_on_skipped = _record

    def v2_playbook_on_stats(self, stats):
        with open(os.environ["RECORD_RESULTS_TO"], "w", encoding="utf-8") as handle:
            json.dump(self.results, handle)
"""


def _play(tmp_path: Path, overrides: dict) -> subprocess.CompletedProcess[str]:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps(CATALOG))
    variables = {
        "workstation_profile": "desktop",
        "dev_user": os.environ.get("USER", "nobody"),
        "dev_user_home": str(tmp_path / "home"),
        "hypersystem_desktop_enabled": True,
        "hypersystem_desktop_catalog_src": str(catalog),
        "hypersystem_desktop_roles": ["suite-finance"],
        "hypersystem_desktop_browser": "test",
        "hypersystem_desktop_install_browser": False,
        "hypersystem_desktop_browser_targets": {
            "test": {"install": "none", "package": "none", "binary": "/bin/sh", "policy_dir": str(tmp_path / "policy"), "user_data_dir": ".suite"},
        },
        "hypersystem_desktop_config_dir": str(tmp_path / "etc"),
        "hypersystem_desktop_helper_path": str(tmp_path / "bin/hypersystem-desktop"),
        "hypersystem_desktop_applications_dir": str(tmp_path / "applications"),
        "ziti_enabled": True,
        "ziti_intercept_domains": ["suite.example"],
        **overrides,
    }
    (tmp_path / "vars.json").write_text(json.dumps(variables))
    playbook = tmp_path / "play.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "name": "hypersystem-desktop check",
                    "hosts": "localhost",
                    "connection": "local",
                    "gather_facts": True,
                    "tasks": [{"name": "Run the task group", "ansible.builtin.include_role": {"name": "developer_workstation", "tasks_from": "hypersystem-desktop"}}],
                }
            ]
        )
    )
    plugins = tmp_path / "callback_plugins"
    plugins.mkdir()
    (plugins / "record_results.py").write_text(RECORDER)
    env = {
        **os.environ,
        "ANSIBLE_ROLES_PATH": str(ROOT / "roles"),
        "ANSIBLE_CALLBACK_PLUGINS": str(plugins),
        "ANSIBLE_CALLBACKS_ENABLED": "record_results",
        "RECORD_RESULTS_TO": str(tmp_path / "results.json"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_LOCALHOST_WARNING": "false",
        "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false",
    }
    return subprocess.run(
        [ANSIBLE_PLAYBOOK, "-i", "localhost,", "--check", str(playbook), "-e", f"@{tmp_path / 'vars.json'}"],
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
        check=False,
    )


def _results(tmp_path: Path) -> dict[str, dict]:
    """Task name -> result of the last run, as the recorder callback saved them."""
    return json.loads((tmp_path / "results.json").read_text())


@pytest.mark.skipif(ANSIBLE_PLAYBOOK is None, reason="ansible-playbook is not installed")
def test_check_mode_run_selects_apps_and_previews_every_file(tmp_path: Path) -> None:
    run = _play(tmp_path, {})
    assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-2000:]
    results = _results(tmp_path)
    assert [app["id"] for app in results["Select the apps this person may use"]["ansible_facts"]["_hsd_apps"]] == ["ledger", "sales"]
    for name in ("Force-install the suite apps as web apps", "Write the hypersystem-desktop configuration", "Install the hypersystem-desktop helper", "Write the suite launchers"):
        assert results[name].get("changed") is True, name
    launchers = [item["_hsd_entry"]["file"] for item in results["Write the suite launchers"]["results"]]
    assert launchers == ["hypersystem-desktop.desktop", "hypersystem-desktop-ledger.desktop", "hypersystem-desktop-sales.desktop"]
    # Check mode wrote nothing.
    assert not (tmp_path / "policy").exists() and not (tmp_path / "etc").exists()


@pytest.mark.skipif(ANSIBLE_PLAYBOOK is None, reason="ansible-playbook is not installed")
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"ziti_enabled": False, "ziti_service_name": "hypersystem-desktop-test-no-such.service"}, "this host has none: set ziti_enabled: true"),
        ({"ziti_intercept_domains": ["other.example"]}, "not inside ziti_intercept_domains"),
        ({"hypersystem_desktop_roles": ["nobody"], "hypersystem_desktop_statuses": ["beta"]}, "No app in the catalog"),
        ({"hypersystem_desktop_apps": [{"id": "x", "name": "X", "urls": {"web": "https://x.suite.example/"}}]}, "exactly one of hypersystem_desktop_catalog_src"),
        ({"workstation_profile": "byod-kiosk"}, "desktop or thin-client"),
        ({"hypersystem_desktop_catalog_sha256": "0" * 64}, "not the pinned"),
    ],
)
def test_check_mode_run_fails_with_a_clear_message(tmp_path: Path, overrides: dict, message: str) -> None:
    run = _play(tmp_path, overrides)
    assert run.returncode != 0
    assert message in run.stdout, run.stdout[-4000:]


@pytest.mark.skipif(ANSIBLE_PLAYBOOK is None, reason="ansible-playbook is not installed")
def test_check_mode_run_without_ziti_when_not_required(tmp_path: Path) -> None:
    run = _play(tmp_path, {"ziti_enabled": False, "ziti_intercept_domains": [], "hypersystem_desktop_require_ziti": False})
    assert run.returncode == 0, run.stdout[-4000:]


@pytest.mark.skipif(ANSIBLE_PLAYBOOK is None, reason="ansible-playbook is not installed")
def test_disabled_run_on_a_clean_host_touches_nothing(tmp_path: Path) -> None:
    run = _play(tmp_path, {"hypersystem_desktop_enabled": False})
    assert run.returncode == 0, run.stdout[-4000:]
    results = _results(tmp_path)
    assert results["Check for an earlier hypersystem-desktop install"]["stat"]["exists"] is False
    assert all(not result.get("changed") for result in results.values())
