"""Keyboard, locale and display-scale settings are explicit, validated and opt-in.

The tests execute the role's own task text with synthetic values, writing into a
temporary directory instead of /etc, so they need neither root nor a display.
"""

from __future__ import annotations

import copy
import grp
import json
import os
import pwd
import subprocess
from pathlib import Path

import jinja2
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
TASKS = ROLE / "tasks"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())


def load(name: str) -> list[dict]:
    return yaml.safe_load((TASKS / name).read_text())


def by_name(tasks: list[dict], name: str) -> dict:
    return next(t for t in tasks if t["name"] == name)


def run_playbook(path: Path, plays: list[dict]) -> subprocess.CompletedProcess:
    path.write_text(yaml.safe_dump(plays))
    env = {**os.environ, "ANSIBLE_LOCALHOST_WARNING": "False", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "False",
           "ANSIBLE_ROLES_PATH": str(ROOT / "roles")}
    return subprocess.run(["ansible-playbook", "-i", "localhost,", str(path)], capture_output=True, text=True, env=env, cwd=ROOT)


def render_expr(expression: str, **variables) -> str:
    return jinja2.Environment().from_string(expression).render(**variables)


# --- explicit variables and opt-in gates -------------------------------------


def test_keyboard_and_locale_variables_are_explicit_and_not_forced() -> None:
    for name in ("keyboard_manage", "keyboard_layout", "keyboard_model", "keyboard_variant", "keyboard_options",
                 "locale_manage", "locale_lang", "locale_extra"):
        assert name in DEFAULTS, name
    assert DEFAULTS["keyboard_layout"] == "us"
    assert DEFAULTS["locale_manage"] is False
    assert DEFAULTS["locale_lang"] == ""
    assert DEFAULTS["locale_extra"] == []


@pytest.mark.parametrize(
    ("profile", "expected"),
    [("desktop", "False"), ("workspace", "False"), ("thin-client", "True"), ("byod-kiosk", "True")],
)
def test_keyboard_is_forced_only_where_a_profile_requests_it(profile: str, expected: str) -> None:
    assert render_expr(DEFAULTS["keyboard_manage"], workstation_profile=profile) == expected


def test_main_gates_keyboard_and_locale_on_their_manage_flags() -> None:
    main = load("main.yml")
    keyboard = next(t for t in main if t["name"].startswith("Include keyboard layout"))
    locale = next(t for t in main if t["name"].startswith("Include system locale"))
    assert keyboard["when"] == "keyboard_manage | bool"
    assert locale["when"] == "locale_manage | bool"
    assert keyboard["tags"] == ["keyboard", "system"] and locale["tags"] == ["locale", "system"]
    assert locale["ansible.builtin.include_tasks"]["apply"]["tags"] == ["locale", "system"]
    assert keyboard["name"].startswith("Include keyboard layout enforcement")  # name other tests and docs rely on


def test_keyboard_file_is_no_longer_hardcoded() -> None:
    keyboard = (TASKS / "keyboard.yml").read_text()
    assert 'XKBLAYOUT="us"' not in keyboard
    template = (ROLE / "templates/default-keyboard.j2").read_text()
    for var in ("keyboard_model", "keyboard_layout", "keyboard_variant", "keyboard_options"):
        assert "{{ " + var + " }}" in template
    assert "ansible.builtin.template" in keyboard


# --- keyboard and locale rendering and validation, executed -------------------


def scenario_play(name: str, variables: dict, out: Path, kind: str) -> dict:
    validate = load("keyboard-locale-validate.yml")
    if kind == "keyboard":
        write = copy.deepcopy(by_name(load("keyboard.yml"), "Write /etc/default/keyboard"))
        write["ansible.builtin.template"]["dest"] = str(out / f"{name}.keyboard")
        write["ansible.builtin.template"]["src"] = str(ROLE / "templates/default-keyboard.j2")
        write["ansible.builtin.template"].pop("owner")
        write["ansible.builtin.template"].pop("group")
        steps = [validate[0], write]
    else:
        line = copy.deepcopy(by_name(load("locale.yml"), "Set the system default locale"))
        line["ansible.builtin.lineinfile"]["path"] = str(out / f"{name}.locale")
        line["ansible.builtin.lineinfile"].pop("owner")
        line["ansible.builtin.lineinfile"].pop("group")
        steps = [validate[1], line]
    rejected = {"ansible.builtin.copy": {"dest": str(out / f"{name}.rejected"), "content": "{{ ansible_failed_result.msg }}"}}
    return {
        "hosts": "localhost",
        "connection": "local",
        "gather_facts": False,
        "vars": variables,
        "tasks": [{"name": "scenario", "block": steps, "rescue": [rejected]}],
    }


KEYBOARD_CASES = {
    "kiosk_default": ({"keyboard_manage": True, "keyboard_layout": "us", "keyboard_model": "pc105", "keyboard_variant": "", "keyboard_options": ""}, 'XKBLAYOUT="us"'),
    "german": ({"keyboard_manage": True, "keyboard_layout": "de", "keyboard_model": "pc105", "keyboard_variant": "nodeadkeys", "keyboard_options": "compose:ralt"}, 'XKBLAYOUT="de"'),
    "two_layouts": ({"keyboard_manage": True, "keyboard_layout": "us,ru", "keyboard_model": "pc105", "keyboard_variant": ",", "keyboard_options": "grp:alt_shift_toggle"}, 'XKBLAYOUT="us,ru"'),
    # Rows of the "Common layouts" table in docs/accessibility-and-localization.md;
    # test_common_layouts_table_matches_keyboard_cases keeps the two in step.
    "uk_compose": ({"keyboard_manage": True, "keyboard_layout": "gb", "keyboard_model": "pc105", "keyboard_variant": "", "keyboard_options": "compose:ralt"}, 'XKBLAYOUT="gb"'),
    "french": ({"keyboard_manage": True, "keyboard_layout": "fr", "keyboard_model": "pc105", "keyboard_variant": "", "keyboard_options": ""}, 'XKBLAYOUT="fr"'),
    "spanish": ({"keyboard_manage": True, "keyboard_layout": "es", "keyboard_model": "pc105", "keyboard_variant": "", "keyboard_options": ""}, 'XKBLAYOUT="es"'),
    "us_dvorak": ({"keyboard_manage": True, "keyboard_layout": "us", "keyboard_model": "pc105", "keyboard_variant": "dvorak", "keyboard_options": ""}, 'XKBVARIANT="dvorak"'),
    "us_international": ({"keyboard_manage": True, "keyboard_layout": "us", "keyboard_model": "pc105", "keyboard_variant": "intl", "keyboard_options": ""}, 'XKBVARIANT="intl"'),
}
ACCESSIBILITY_DOC = ROOT / "docs/accessibility-and-localization.md"
KEYBOARD_REJECTED = {
    "uppercase": {"keyboard_manage": True, "keyboard_layout": "US", "keyboard_model": "pc105", "keyboard_variant": "", "keyboard_options": ""},
    "injection": {"keyboard_manage": True, "keyboard_layout": "de\"; touch /tmp/x; \"", "keyboard_model": "pc105", "keyboard_variant": "", "keyboard_options": ""},
    "quote_in_options": {"keyboard_manage": True, "keyboard_layout": "de", "keyboard_model": "pc105", "keyboard_variant": "", "keyboard_options": 'a"b'},
}
LOCALE_CASES = {
    "gb": ({"locale_manage": True, "locale_lang": "en_GB.UTF-8", "locale_extra": ["de_DE.UTF-8"]}, "LANG=en_GB.UTF-8"),
    "c": ({"locale_manage": True, "locale_lang": "C.UTF-8", "locale_extra": []}, "LANG=C.UTF-8"),
}
LOCALE_REJECTED = {
    "no_encoding": {"locale_manage": True, "locale_lang": "en_GB", "locale_extra": []},
    "word": {"locale_manage": True, "locale_lang": "english", "locale_extra": []},
    "bad_extra": {"locale_manage": True, "locale_lang": "en_GB.UTF-8", "locale_extra": ["fr_FR"]},
}


def test_keyboard_and_locale_scenarios(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    plays = []
    for name, (variables, _) in KEYBOARD_CASES.items():
        plays.append(scenario_play(f"k_{name}", variables, out, "keyboard"))
    for name, variables in KEYBOARD_REJECTED.items():
        plays.append(scenario_play(f"k_{name}", variables, out, "keyboard"))
    # Unmanaged: garbage values are never validated or written.
    plays.append(scenario_play("k_unmanaged", {"keyboard_manage": False, "keyboard_layout": "!!", "keyboard_model": "", "keyboard_variant": "", "keyboard_options": ""}, out, "keyboard"))
    for name, (variables, _) in LOCALE_CASES.items():
        plays.append(scenario_play(f"l_{name}", variables, out, "locale"))
    for name, variables in LOCALE_REJECTED.items():
        plays.append(scenario_play(f"l_{name}", variables, out, "locale"))
    plays.append(scenario_play("l_unmanaged", {"locale_manage": False, "locale_lang": "garbage", "locale_extra": []}, out, "locale"))
    result = run_playbook(tmp_path / "kl.yml", plays)
    assert result.returncode == 0, result.stdout + result.stderr

    for name, (variables, needle) in KEYBOARD_CASES.items():
        text = (out / f"k_{name}.keyboard").read_text()
        assert needle in text, name
        for var, key in (("keyboard_layout", "XKBLAYOUT"), ("keyboard_variant", "XKBVARIANT"), ("keyboard_options", "XKBOPTIONS")):
            assert f'{key}="{variables[var]}"' in text, (name, key)
        assert not (out / f"k_{name}.rejected").exists()
    german = (out / "k_german.keyboard").read_text()
    assert 'XKBVARIANT="nodeadkeys"' in german and 'XKBOPTIONS="compose:ralt"' in german
    assert 'XKBLAYOUT="us,ru"' in (out / "k_two_layouts.keyboard").read_text()
    for name in KEYBOARD_REJECTED:
        assert (out / f"k_{name}.rejected").exists(), name
        assert not (out / f"k_{name}.keyboard").exists(), name
    assert not (out / "k_unmanaged.rejected").exists() and (out / "k_unmanaged.keyboard").exists()

    for name, (_, needle) in LOCALE_CASES.items():
        assert needle in (out / f"l_{name}.locale").read_text(), name
        assert not (out / f"l_{name}.rejected").exists()
    for name in LOCALE_REJECTED:
        assert (out / f"l_{name}.rejected").exists(), name
        assert not (out / f"l_{name}.locale").exists(), name
    assert not (out / "l_unmanaged.rejected").exists()


def common_layout_rows() -> list[tuple[str, str, str]]:
    """The (layout, variant, options) rows of the doc's "Common layouts" table."""
    lines = ACCESSIBILITY_DOC.read_text().splitlines()
    start = lines.index("### Common layouts")
    header = next(i for i in range(start, len(lines)) if lines[i].startswith("| Keyboard |"))
    rows = []
    for line in lines[header + 2:]:
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        assert len(cells) == 4, line
        values = []
        for cell in cells[1:]:
            assert cell.startswith("`") and cell.endswith("`"), f"value not in backticks: {line}"
            value = cell.strip("`")
            values.append("" if value == '""' else value)
        rows.append(tuple(values))
    return rows


def test_common_layouts_table_matches_keyboard_cases() -> None:
    rows = common_layout_rows()
    assert len(rows) >= 6
    assert len(set(rows)) == len(rows), "duplicate row in the common layouts table"
    tested = {(v["keyboard_layout"], v["keyboard_variant"], v["keyboard_options"]) for v, _ in KEYBOARD_CASES.values()}
    untested = [row for row in rows if row not in tested]
    assert not untested, f"documented layouts missing from KEYBOARD_CASES: {untested}"


# --- display scale: high-DPI and low-resolution paths, executed ---------------


def can_chown_to_self() -> bool:
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
        grp.getgrnam(user)
        return True
    except KeyError:
        return False


@pytest.mark.skipif(not can_chown_to_self(), reason="the display-scale task writes owner and group named after the user")
def test_display_scale_paths(tmp_path: Path) -> None:
    user = pwd.getpwuid(os.getuid()).pw_name
    tasks = load("minimal-desktop-display-scale.yml")
    cases = {
        "low_res": ("1", None),
        "hidpi_150": ("1.5", (144, 36)),
        "hidpi_225": ("2.25", (216, 54)),
        "hidpi_max": ("4", (384, 96)),
        "too_small": ("0.5", "rejected"),
        "bad_step": ("1.1", "rejected"),
        "too_big": ("5", "rejected"),
        "word": ("large", "rejected"),
    }
    plays = []
    for name, (scale, _) in cases.items():
        home = tmp_path / name
        home.mkdir()
        if name == "low_res":
            (home / ".Xresources").write_text("! hand-made\nXft.dpi: 120\n")
        rejected = {"ansible.builtin.copy": {"dest": str(tmp_path / f"{name}.rejected"), "content": "{{ ansible_failed_result.msg }}"}}
        plays.append({
            "hosts": "localhost", "connection": "local", "gather_facts": False,
            "vars": {"minimal_desktop_display_scale": scale, "dw_user": {"name": user, "home": str(home)}},
            "tasks": [{"name": "scenario", "block": copy.deepcopy(tasks), "rescue": [rejected]}],
        })
    result = run_playbook(tmp_path / "scale.yml", plays)
    assert result.returncode == 0, result.stdout + result.stderr

    # Scale 1 (a low-resolution or ordinary display) must leave a hand-made file alone.
    assert (tmp_path / "low_res/.Xresources").read_text() == "! hand-made\nXft.dpi: 120\n"
    assert not (tmp_path / "low_res.rejected").exists()
    for name, (_, expected) in cases.items():
        if isinstance(expected, tuple):
            text = (tmp_path / name / ".Xresources").read_text()
            assert f"Xft.dpi: {expected[0]}\n" in text and f"Xcursor.size: {expected[1]}\n" in text, name
            assert not (tmp_path / f"{name}.rejected").exists()
        elif expected == "rejected":
            assert (tmp_path / f"{name}.rejected").exists(), name
            assert not (tmp_path / name / ".Xresources").exists(), name


def test_display_scale_override_is_a_plain_variable() -> None:
    assert "minimal_desktop_display_scale" in DEFAULTS
    assert "hardware_profiles" in DEFAULTS["minimal_desktop_display_scale"]
    assert json.dumps(DEFAULTS["keyboard_manage"])  # serialisable default expression


# --- opt-in assistive technology packages (decision record 0003, catalog#173) --

RECORD_0003 = ROOT / "docs/decisions/0003-opt-in-assistive-technology-packages.md"
SPEC = yaml.safe_load((ROLE / "meta/argument_specs.yml").read_text())["argument_specs"]["main"]["options"]
# Modules accessibility-packages.yml may use: install packages and read the
# package lists. Anything that writes a file, touches a service or removes a
# package would break the record's "packages only, never overwrite" promise.
PACKAGES_ONLY_MODULES = {"ansible.builtin.apt", "ansible.builtin.command", "ansible.builtin.assert"}
TASK_KEYWORDS = {"name", "loop", "register", "changed_when", "check_mode", "vars", "when"}


def record_package_names() -> list[str]:
    """The package names of the record's accepted-list table, in its order."""
    lines = RECORD_0003.read_text().splitlines()
    header = lines.index("| Package | What it gives | Notes |")
    names = []
    for line in lines[header + 2:]:
        if not line.startswith("|"):
            break
        names.append(line.split("|")[1].strip().strip("`"))
    return names


def test_accessibility_packages_default_to_nothing() -> None:
    assert DEFAULTS["accessibility_packages"] == []


def test_accessibility_packages_accept_only_the_records_list() -> None:
    spec = SPEC["accessibility_packages"]
    assert spec["type"] == "list" and spec["elements"] == "str"
    assert spec["choices"] == record_package_names() == ["orca", "espeak-ng", "brltty", "onboard", "xkbset"]


def test_record_stays_proposed_until_an_assistive_technology_user_reviews_it() -> None:
    assert "- Status: proposed" in RECORD_0003.read_text()


def test_main_includes_accessibility_packages_only_when_asked_on_a_desktop() -> None:
    include = next(t for t in load("main.yml") if t["name"].startswith("Include assistive technology packages"))
    assert include["ansible.builtin.include_tasks"]["file"] == "accessibility-packages.yml"
    assert include["when"] == ["accessibility_packages | length > 0",
                               "workstation_profile in ['desktop', 'thin-client', 'byod-kiosk']"]
    assert include["tags"] == ["accessibility", "packages"]
    assert include["ansible.builtin.include_tasks"]["apply"]["tags"] == ["accessibility", "packages"]


def test_accessibility_packages_install_packages_only() -> None:
    tasks = load("accessibility-packages.yml")
    modules = [next(k for k in t if k not in TASK_KEYWORDS) for t in tasks]
    assert set(modules) <= PACKAGES_ONLY_MODULES, modules
    for task in tasks:
        if "ansible.builtin.command" in task:
            assert task["ansible.builtin.command"]["argv"][:2] == ["apt-cache", "policy"]
            assert task["changed_when"] is False
        if "ansible.builtin.apt" in task:
            assert task["ansible.builtin.apt"].get("state", "present") == "present"
            assert "purge" not in task["ansible.builtin.apt"] and "autoremove" not in task["ansible.builtin.apt"]
    # Refresh the lists, look up candidates, stop on a missing one, then the one install.
    assert modules == ["ansible.builtin.apt", "ansible.builtin.command", "ansible.builtin.assert", "ansible.builtin.apt"]
    assert "name" not in tasks[0]["ansible.builtin.apt"]
    assert tasks[-1]["ansible.builtin.apt"]["name"] == "{{ accessibility_packages | unique }}"


def accessibility_include(variables: dict, marker: Path) -> dict:
    include = copy.deepcopy(next(t for t in load("main.yml") if t["name"].startswith("Include assistive technology packages")))
    include["ansible.builtin.include_tasks"]["file"] = str(TASKS / "accessibility-packages.yml")
    after = {"name": "after", "ansible.builtin.copy": {"dest": str(marker), "content": "reached\n"}}
    return {"hosts": "localhost", "connection": "local", "gather_facts": False, "vars": variables,
            "tasks": [include, after]}


@pytest.mark.parametrize("profile", ["desktop", "workspace", "thin-client", "byod-kiosk"])
def test_empty_accessibility_packages_change_nothing(tmp_path: Path, profile: str) -> None:
    """The default list skips the whole file: no package list refresh, lookup or install."""
    marker = tmp_path / "after"
    result = run_playbook(tmp_path / "empty.yml",
                          [accessibility_include({"accessibility_packages": [], "workstation_profile": profile}, marker)])
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.exists()
    included = [t["name"] for t in load("accessibility-packages.yml")]
    assert not [name for name in included if name in result.stdout], result.stdout
    assert "skipping: [localhost]" in result.stdout
    assert "changed=1 " in result.stdout  # only the test's own marker file


def validate_play(variables: dict, out: Path, name: str) -> dict:
    rejected = {"ansible.builtin.copy": {"dest": str(out / f"{name}.rejected"), "content": "{{ ansible_failed_result.msg }}"}}
    # Stands in for the first task after validation: it must never run when
    # validation refuses the configuration.
    reached = {"ansible.builtin.copy": {"dest": str(out / f"{name}.reached"), "content": "reached\n"}}
    validate = {"ansible.builtin.include_role": {"name": "developer_workstation", "tasks_from": "validate-config"}}
    return {"hosts": "localhost", "connection": "local", "gather_facts": False, "vars": variables,
            "tasks": [{"name": "scenario", "block": [validate, reached], "rescue": [rejected]}]}


ACCESSIBILITY_ACCEPTED = {
    "empty_desktop": {"workstation_profile": "desktop", "accessibility_packages": []},
    "empty_workspace": {"workstation_profile": "workspace", "accessibility_packages": []},
    "all_desktop": {"workstation_profile": "desktop", "accessibility_packages": ["orca", "espeak-ng", "brltty", "onboard", "xkbset"]},
    "screen_reader_kiosk": {"workstation_profile": "byod-kiosk", "kiosk_target_url": "https://portal.example.org/",
                            "accessibility_packages": ["orca"]},
}
ACCESSIBILITY_REFUSED = {
    "unknown_name": ({"workstation_profile": "desktop", "accessibility_packages": ["orca", "nvda"]},
                     ["accessibility_packages", "orca, espeak-ng, brltty, onboard, xkbset", "nvda"]),
    "typo": ({"workstation_profile": "desktop", "accessibility_packages": ["Orca"]}, ["accessibility_packages", "Orca"]),
    "string_not_list": ({"workstation_profile": "desktop", "accessibility_packages": "orca"},
                        ["accessibility_packages must be a list"]),
    "workspace": ({"workstation_profile": "workspace", "accessibility_packages": ["orca"]},
                  ["workstation_profile workspace has no desktop session"]),
}


def test_accessibility_packages_validation(tmp_path: Path) -> None:
    plays = [validate_play(v, tmp_path, name) for name, v in ACCESSIBILITY_ACCEPTED.items()]
    plays += [validate_play(v, tmp_path, name) for name, (v, _) in ACCESSIBILITY_REFUSED.items()]
    result = run_playbook(tmp_path / "validate.yml", plays)
    assert result.returncode == 0, result.stdout + result.stderr
    for name in ACCESSIBILITY_ACCEPTED:
        assert (tmp_path / f"{name}.reached").exists(), name
        assert not (tmp_path / f"{name}.rejected").exists(), (tmp_path / f"{name}.rejected").read_text()
    for name, (_, needles) in ACCESSIBILITY_REFUSED.items():
        assert not (tmp_path / f"{name}.reached").exists(), f"{name}: a task after validation ran"
        message = (tmp_path / f"{name}.rejected").read_text()
        for needle in needles:
            assert needle in message, (name, needle, message)


def test_missing_install_candidate_stops_before_installing(tmp_path: Path) -> None:
    """espeak-ng, onboard and xkbset need Ubuntu's universe; without it the run stops with the fix."""
    check = copy.deepcopy(by_name(load("accessibility-packages.yml"), "Require an install candidate for every assistive technology package"))
    policy = {
        "results": [
            {"item": "orca", "stdout": "orca:\n  Installed: (none)\n  Candidate: 46.1-1ubuntu1\n  Version table:\n"},
            {"item": "onboard", "stdout": "onboard:\n  Installed: (none)\n  Candidate: (none)\n  Version table:\n"},
            {"item": "xkbset", "stdout": ""},
        ]
    }
    available = {"results": policy["results"][:1]}
    plays = []
    for name, results, distribution in (("missing", policy, "Ubuntu"), ("missing_debian", policy, "Debian"),
                                        ("available", available, "Ubuntu")):
        rejected = {"ansible.builtin.copy": {"dest": str(tmp_path / f"{name}.rejected"), "content": "{{ ansible_failed_result.msg }}"}}
        installed = {"ansible.builtin.copy": {"dest": str(tmp_path / f"{name}.installed"), "content": "installed\n"}}
        plays.append({"hosts": "localhost", "connection": "local", "gather_facts": False,
                      "vars": {"_accessibility_policy": results, "ansible_facts": {"distribution": distribution}},
                      "tasks": [{"name": "scenario", "block": [copy.deepcopy(check), installed], "rescue": [rejected]}]})
    result = run_playbook(tmp_path / "candidate.yml", plays)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "available.installed").exists()
    ubuntu = (tmp_path / "missing.rejected").read_text()
    assert "onboard, xkbset" in ubuntu and "orca" not in ubuntu.split("which")[0]
    assert "universe" in ubuntu and "Nothing was installed" in ubuntu
    assert "universe" not in (tmp_path / "missing_debian.rejected").read_text()
    assert not (tmp_path / "missing.installed").exists() and not (tmp_path / "missing_debian.installed").exists()
