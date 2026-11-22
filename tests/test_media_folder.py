"""The Media folder: the media server launchers in one folder (catalog#158).

With media_folder_enabled the role writes one xdg-open launcher per
media_folder_web_entries entry, shows them (and Jellyfin Desktop, when it is
enabled) in a "Media" submenu of the Openbox Applications menu and, where
GNOME Shell is installed, in a GNOME app-grid folder declared as a system dconf
default. The read-back helper is exercised here against real rendered files and
a fake curl.
"""

import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import jinja2
import pytest
import yaml


REPO = Path(__file__).resolve().parents[1]
ROLE = REPO / "roles/developer_workstation"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
TASKS = yaml.safe_load((ROLE / "tasks/media-folder.yml").read_text())
MAIN = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
MENU = (ROLE / "templates/minimal-desktop-menu.xml.j2").read_text()
NS = {"o": "http://openbox.org/3.4/menu"}
DOMAIN = "media.example.test"
APP = DEFAULTS["jellyfin_desktop_flatpak_id"]


def _task(name: str) -> dict:
    return next(t for t in TASKS if t.get("name") == name)


def _environment() -> jinja2.Environment:
    from ansible.plugins.filter.core import FilterModule
    from ansible.plugins.test.core import TestModule

    env = jinja2.Environment()
    env.filters.update(FilterModule().filters())
    env.tests.update(TestModule().tests())
    env.filters["default"] = jinja2.filters.do_default
    return env


def _entries(domain: str = DOMAIN) -> list[dict]:
    env = _environment()
    return [
        {**entry, "url": env.from_string(entry["url"]).render(media_folder_web_domain=domain)}
        for entry in DEFAULTS["media_folder_web_entries"]
    ]


def _vars(**overrides) -> dict:
    base = {
        "media_folder_enabled": True,
        "media_folder_name": DEFAULTS["media_folder_name"],
        "media_folder_web_entries": _entries(),
        "jellyfin_desktop_enabled": True,
        "jellyfin_desktop_flatpak_id": APP,
        "jellyfin_web_url": "http://jellyfin.example.test",
        "ansible_architecture": "x86_64",
    }
    return {**base, **overrides}


def _render(text: str, **variables) -> str:
    return _environment().from_string(text).render(**variables)


def test_defaults_are_off_and_carry_no_site_domain() -> None:
    assert DEFAULTS["media_folder_enabled"] is False
    assert DEFAULTS["media_folder_name"] == "Media"
    assert DEFAULTS["media_folder_web_domain"] == ""


def test_default_entries_use_the_viewer_guide_names() -> None:
    entries = _entries()
    assert [e["name"] for e in entries] == ["Watch", "Request", "Books", "Music"]
    assert [e["url"] for e in entries] == [
        f"https://watch.{DOMAIN}",
        f"https://request.{DOMAIN}",
        f"https://books.{DOMAIN}",
        f"https://music.{DOMAIN}",
    ]
    assert len({e["id"] for e in entries}) == len(entries)


def _entry_accepted(entry: dict) -> bool:
    task = _task("Require a valid id, name and address for each Media entry")
    env = _environment()
    return all(
        env.from_string("{{ " + cond + " }}").render(item=entry) == "True"
        for cond in task["ansible.builtin.assert"]["that"]
    )


def test_every_default_entry_passes_once_the_domain_is_set() -> None:
    assert all(_entry_accepted(e) for e in _entries())


def test_an_empty_domain_fails_the_run() -> None:
    assert not any(_entry_accepted(e) for e in _entries(domain=""))


@pytest.mark.parametrize(
    "url",
    ["javascript:alert(1)", "https://a b.example", "https://x.example/'q", "https://x.example/%41", "ftp://x.example", "https://x"],
)
def test_unsafe_or_partial_addresses_are_refused(url: str) -> None:
    assert not _entry_accepted({"id": "watch", "name": "Watch", "url": url})


@pytest.mark.parametrize("name", ["", "Watch|x", "<b>", "Watch\nExec=sh"])
def test_names_that_would_break_a_launcher_are_refused(name: str) -> None:
    assert not _entry_accepted({"id": "watch", "name": name, "url": f"https://watch.{DOMAIN}"})


def test_ids_must_be_plain() -> None:
    assert not _entry_accepted({"id": "../x", "name": "Watch", "url": f"https://watch.{DOMAIN}"})


def test_launchers_only_run_xdg_open() -> None:
    content = _task("Install the Media launchers")["ansible.builtin.copy"]["content"]
    for entry in _entries():
        lines = _render(content, item=entry).splitlines()
        assert f"Name={entry['name']}" in lines
        assert f"Exec=xdg-open {entry['url']}" in lines
        assert "Categories=AudioVideo;X-Curiosity-Media;" in lines
        assert "Type=Application" in lines


def _facts(**variables) -> dict:
    """Render the set_fact expressions in order, the way the play does."""
    env = _environment()
    facts = dict(variables)
    for name in ("Decide the Media folder entries", "Name the Media folder applications"):
        for key, expr in _task(name)["ansible.builtin.set_fact"].items():
            rendered = env.from_string(expr).render(**facts)
            facts[key] = yaml.safe_load(rendered) if rendered.strip()[:1] in "[{" else rendered.strip()
    return facts


def test_folder_apps_hold_jellyfin_and_the_web_entries() -> None:
    facts = _facts(**_vars())
    assert facts["_media_folder_apps"] == [
        f"{APP}.desktop",
        "curiosity-media-watch.desktop",
        "curiosity-media-request.desktop",
        "curiosity-media-books.desktop",
        "curiosity-media-music.desktop",
    ]


def test_folder_off_declares_nothing_so_stale_launchers_go() -> None:
    facts = _facts(**_vars(media_folder_enabled=False))
    assert facts["_media_folder_launchers"] == []
    assert facts["_media_folder_apps"] == []
    removal = _task("Remove Media launchers that are no longer declared")
    stale = _render(
        removal["loop"],
        _media_folder_found={"files": [{"path": "/apps/curiosity-media-watch.desktop"}]},
        _media_folder_launchers=[],
        media_folder_desktop_dir="/apps",
    )
    assert "curiosity-media-watch.desktop" in stale


def test_declared_launchers_are_kept() -> None:
    removal = _task("Remove Media launchers that are no longer declared")
    kept = _render(
        removal["loop"],
        _media_folder_found={"files": [{"path": "/apps/curiosity-media-watch.desktop"}, {"path": "/apps/curiosity-media-old.desktop"}]},
        _media_folder_launchers=["curiosity-media-watch.desktop"],
        media_folder_desktop_dir="/apps",
    )
    assert "old" in kept and "watch" not in kept


def test_gnome_folder_is_a_dconf_default() -> None:
    content = _task("Declare the GNOME app-grid Media folder")["ansible.builtin.copy"]["content"]
    facts = _facts(**_vars())
    text = _render(content, **facts)
    assert "[org/gnome/desktop/app-folders/folders/CuriosityMedia]" in text
    assert "name='Media'" in text
    assert f"apps=['{APP}.desktop', 'curiosity-media-watch.desktop'" in text
    assert "folder-children=['Utilities', 'YaST', 'Pardus', 'CuriosityMedia']" in text


def _apps_menu(**variables) -> ET.Element:
    root = ET.fromstring(_render(MENU, **_vars(**variables)))
    return root.find("o:menu[@id='apps-menu']", NS)


def _media_items(**variables) -> list[tuple[str, str]]:
    media = _apps_menu(**variables).find("o:menu[@id='media-menu']", NS)
    assert media is not None and media.get("label") == "Media"
    return [(i.get("label"), i.find(".//o:command", NS).text) for i in media.findall("o:item", NS)]


def test_openbox_media_submenu_holds_only_the_media_entries() -> None:
    assert _media_items() == [
        ("Jellyfin Desktop", f"flatpak run {APP}"),
        ("Watch", f"xdg-open https://watch.{DOMAIN}"),
        ("Request", f"xdg-open https://request.{DOMAIN}"),
        ("Books", f"xdg-open https://books.{DOMAIN}"),
        ("Music", f"xdg-open https://music.{DOMAIN}"),
    ]
    top = {i.get("label") for i in _apps_menu().findall("o:item", NS)}
    assert not top & {"Jellyfin Desktop", "Jellyfin (web)"}


def test_openbox_media_submenu_without_jellyfin() -> None:
    assert [label for label, _ in _media_items(jellyfin_desktop_enabled=False)] == ["Watch", "Request", "Books", "Music"]


def test_openbox_menu_without_the_folder_keeps_the_jellyfin_block() -> None:
    apps = _apps_menu(media_folder_enabled=False)
    assert apps.find("o:menu[@id='media-menu']", NS) is None
    assert {"Jellyfin Desktop", "Jellyfin (web)"} <= {i.get("label") for i in apps.findall("o:item", NS)}


def test_include_runs_on_every_desktop_after_the_minimal_desktop() -> None:
    names = [t.get("name") for t in MAIN]
    include = next(t for t in MAIN if t.get("name") == "Include the Media folder (media server launchers)")
    assert include["ansible.builtin.include_tasks"]["file"] == "media-folder.yml"
    assert include["when"] == "workstation_profile == 'desktop'"
    assert names.index("Include the Media folder (media server launchers)") > names.index(
        "Include minimal desktop (Openbox, LightDM, Firefox)"
    )


def test_no_browser_policy_is_written() -> None:
    text = (ROLE / "tasks/media-folder.yml").read_text()
    for forbidden in ("/etc/opt/chrome", "policies/managed", "ManagedBookmarks", "HomepageLocation"):
        assert forbidden not in text


def _helper(tmp_path: Path, *, jellyfin: bool = True, gnome: bool = True, missing: str | None = None,
            menu: bool = True, curl: str = "302 https://auth.example.test/realms/r?state=secret") -> subprocess.CompletedProcess:
    apps = tmp_path / "apps"
    exports = tmp_path / "exports"
    home = tmp_path / "home"
    for d in (apps, exports, home / ".config/openbox"):
        d.mkdir(parents=True)
    variables = _vars(jellyfin_desktop_enabled=jellyfin)
    facts = _facts(**variables)
    if jellyfin:
        (exports / f"{APP}.desktop").write_text("")
    for entry in facts["_media_folder_web"]:
        if entry["id"] != missing:
            (apps / f"curiosity-media-{entry['id']}.desktop").write_text("")
    menu_xml = _render(MENU, **_vars(media_folder_enabled=menu, jellyfin_desktop_enabled=jellyfin))
    (home / ".config/openbox/menu.xml").write_text(menu_xml)
    dconf = tmp_path / "50-media"
    dconf.write_text(_render(_task("Declare the GNOME app-grid Media folder")["ansible.builtin.copy"]["content"], **facts))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "curl"
    fake.write_text(f"#!/bin/sh\nprintf '%s' '{curl}'\n")
    fake.chmod(0o755)
    content = _task("Install the Media folder read-back helper")["ansible.builtin.copy"]["content"]
    script = _render(
        content,
        **{
            **facts,
            "media_folder_flatpak_exports": str(exports),
            "media_folder_desktop_dir": str(apps),
            "media_folder_dconf_file": str(dconf),
            "_media_folder_gnome_shell": {"stat": {"exists": gnome}},
            "minimal_desktop_enabled": True,
            "dev_users": [{"name": "u", "home": str(home)}],
        },
    )
    helper = tmp_path / "check.sh"
    helper.write_text(script)
    helper.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:/usr/bin:/bin"}
    args = [str(helper)] + (["--probe"] if curl is not None else [])
    return subprocess.run(args, capture_output=True, text=True, env=env, check=False)


def test_helper_reads_back_the_whole_folder(tmp_path: Path) -> None:
    result = _helper(tmp_path)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == "media folder Media: 5 entries"
    assert lines[1].startswith("  Jellyfin Desktop: ")
    assert lines[2].endswith(f"-> https://watch.{DOMAIN} (HTTP 302 to auth.example.test)")
    assert "secret" not in result.stdout
    assert any(line.startswith("  openbox submenu: ") for line in lines)
    assert any(line.startswith("  gnome app folder: ") for line in lines)


def test_helper_without_gnome_or_jellyfin(tmp_path: Path) -> None:
    result = _helper(tmp_path, jellyfin=False, gnome=False, curl="200 ")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == "media folder Media: 4 entries"
    assert "(HTTP 200)" in result.stdout
    assert "gnome app folder" not in result.stdout


def test_helper_fails_on_a_missing_launcher(tmp_path: Path) -> None:
    result = _helper(tmp_path, missing="books")
    assert result.returncode == 1
    assert "curiosity-media-books.desktop missing" in result.stderr


def test_helper_fails_when_the_menu_has_no_submenu(tmp_path: Path) -> None:
    result = _helper(tmp_path, menu=False)
    assert result.returncode == 1
    assert "has no Media submenu" in result.stderr


def test_helper_fails_when_an_address_does_not_answer(tmp_path: Path) -> None:
    result = _helper(tmp_path, curl="000 ")
    assert result.returncode == 1
    assert "did not answer" in result.stderr
    assert "(HTTP 000)" in result.stdout
