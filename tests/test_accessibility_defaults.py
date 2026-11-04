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
}
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

    for name, (_, needle) in KEYBOARD_CASES.items():
        text = (out / f"k_{name}.keyboard").read_text()
        assert needle in text, name
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
