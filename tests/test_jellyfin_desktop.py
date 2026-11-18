"""Jellyfin Desktop (Flathub) and the Jellyfin (web) menu entry (catalog#157).

The role installs Flatpak, the Flathub system remote and
org.jellyfin.JellyfinDesktop only when jellyfin_desktop_enabled is true,
refuses an install whose ref or origin is not Flathub's, writes a "Jellyfin
(web)" menu entry that only runs xdg-open, adds both entries to the Openbox
menu, updates Flatpak beside `snap refresh`, and installs a healthcheck helper
that is exercised here against a fake `flatpak`.
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
INSTALL = yaml.safe_load((ROLE / "tasks/jellyfin-desktop.yml").read_text())
FLATPAK = yaml.safe_load((ROLE / "tasks/flatpak.yml").read_text())
MANAGED = yaml.safe_load((ROLE / "tasks/managed-updates.yml").read_text())
UPDATES = yaml.safe_load((ROLE / "tasks/system-updates.yml").read_text())
OPENBOX = yaml.safe_load((ROLE / "tasks/minimal-desktop-openbox.yml").read_text())
MENU = (ROLE / "templates/minimal-desktop-menu.xml.j2").read_text()

APP = DEFAULTS["jellyfin_desktop_flatpak_id"]
REF = DEFAULTS["jellyfin_desktop_flatpak_ref"]
REMOTE = DEFAULTS["workstation_flatpak_flathub_remote"]
WEB_URL = "http://media.example.test"
WEB_ENTRY = DEFAULTS["jellyfin_web_desktop_entry"]
VARS = {
    "jellyfin_desktop_flatpak_id": APP,
    "jellyfin_desktop_flatpak_ref": REF,
    "workstation_flatpak_flathub_remote": REMOTE,
    "jellyfin_web_url": WEB_URL,
    "jellyfin_web_desktop_entry": WEB_ENTRY,
}

INFO = f"""
Jellyfin Desktop - Jellyfin Desktop Client

          ID: {APP}
         Ref: {REF}
        Arch: x86_64
      Branch: stable
     Version: 2.0.0
     License: GPL-2.0-only
      Origin: {REMOTE}
"""


def _task(tasks: list[dict], name: str) -> dict:
    return next(t for t in tasks if t.get("name") == name)


def _environment() -> jinja2.Environment:
    from ansible.plugins.filter.core import FilterModule
    from ansible.plugins.test.core import TestModule

    env = jinja2.Environment()
    env.filters.update(FilterModule().filters())
    env.tests.update(TestModule().tests())
    env.filters["default"] = jinja2.filters.do_default
    return env


def _render(text: str, **variables) -> str:
    return _environment().from_string(text).render(**{**VARS, **variables})


def test_defaults_are_off_and_pin_the_flathub_app() -> None:
    assert DEFAULTS["jellyfin_desktop_enabled"] is False
    assert APP == "org.jellyfin.JellyfinDesktop"
    assert REF == "app/org.jellyfin.JellyfinDesktop/x86_64/stable"
    assert REMOTE == "flathub"
    assert DEFAULTS["workstation_flatpak_flathub_url"] == "https://dl.flathub.org/repo/flathub.flatpakrepo"
    assert DEFAULTS["jellyfin_web_url"] == ""


def _url_accepted(url: object) -> bool:
    task = _task(INSTALL, "Require a Jellyfin web address")
    env = _environment()
    return all(
        env.from_string("{{ " + cond + " }}").render(jellyfin_web_url=url) == "True"
        for cond in task["ansible.builtin.assert"]["that"]
    )


def test_web_address_is_required_when_enabled() -> None:
    assert _url_accepted("http://media.example.test")
    assert _url_accepted("https://media.example.test/web/")
    for bad in ("", "media.example.test", "http://", "http://a b", "javascript:alert(1)"):
        assert not _url_accepted(bad), bad


def test_application_contract_names_its_flag_and_helper() -> None:
    contract = DEFAULTS["workstation_application_contracts"]["jellyfin_desktop"]
    assert contract["enabled_when"] == "jellyfin_desktop_enabled"
    assert contract["healthcheck"] == ["/usr/local/sbin/curiosity-jellyfin-desktop-check"]
    assert contract["installer"] == "flathub"


def test_include_is_gated_like_the_other_desktop_apps() -> None:
    include = _task(MANAGED, "Include Jellyfin desktop app")
    assert include["ansible.builtin.include_tasks"]["file"] == "jellyfin-desktop.yml"
    assert include["when"] == [
        "workstation_profile == 'desktop'",
        "jellyfin_desktop_enabled | default(false) | bool",
        "ansible_architecture == 'x86_64'",
        "workstation_update_install_allowed | default(true)",
    ]


def test_flatpak_enters_only_through_an_opt_in_app() -> None:
    """flatpak.yml is included from the app task file, never from the baseline."""
    includes = [
        path.name
        for path in (ROLE / "tasks").glob("*.yml")
        if "file: flatpak.yml" in path.read_text()
    ]
    assert includes == ["jellyfin-desktop.yml"]
    assert _task(INSTALL, "Install Flatpak and the Flathub remote for Jellyfin Desktop")


def test_flathub_remote_and_app_are_system_wide() -> None:
    remote = _task(FLATPAK, "Add the Flathub remote at system level")["community.general.flatpak_remote"]
    assert remote["method"] == "system"
    assert remote["flatpakrepo_url"] == "{{ workstation_flatpak_flathub_url }}"
    assert _task(FLATPAK, "Install Flatpak")["ansible.builtin.apt"]["name"] == "flatpak"
    app = _task(INSTALL, "Install Jellyfin Desktop from Flathub")["community.general.flatpak"]
    assert app == {
        "name": "{{ jellyfin_desktop_flatpak_id }}",
        "remote": "{{ workstation_flatpak_flathub_remote }}",
        "method": "system",
        "state": "present",
    }


def test_community_general_is_a_declared_collection() -> None:
    requirements = yaml.safe_load((REPO / "requirements.yml").read_text())
    assert "community.general" in {c["name"] for c in requirements["collections"]}
    galaxy = yaml.safe_load((REPO / "galaxy.yml").read_text())
    assert "community.general" in galaxy["dependencies"]


def _assert_passes(ref: str, origin: str, rcs: tuple[int, int] = (0, 0)) -> bool:
    task = _task(INSTALL, "Require the installed Jellyfin Desktop to be the Flathub app")
    info = {"results": [{"rc": rcs[0], "stdout": ref + "\n"}, {"rc": rcs[1], "stdout": origin + "\n"}]}
    env = _environment()
    return all(
        env.from_string("{{ " + cond + " }}").render(_jellyfin_desktop_info=info, **VARS) == "True"
        for cond in task["ansible.builtin.assert"]["that"]
    )


def test_identity_assert_accepts_the_flathub_app() -> None:
    assert _assert_passes(REF, REMOTE)


@pytest.mark.parametrize(
    "ref, origin, rcs",
    [
        (REF, "fedora", (0, 0)),
        (REF.replace("stable", "beta"), REMOTE, (0, 0)),
        (REF.replace("x86_64", "aarch64"), REMOTE, (0, 0)),
        ("", "", (1, 1)),
    ],
    ids=["other-remote", "beta-branch", "other-arch", "not-installed"],
)
def test_identity_assert_refuses_anything_else(ref: str, origin: str, rcs: tuple[int, int]) -> None:
    assert not _assert_passes(ref, origin, rcs)


def test_web_entry_only_opens_the_ziti_url() -> None:
    content = _task(INSTALL, "Install the Jellyfin (web) menu entry")["ansible.builtin.copy"]["content"]
    lines = _render(content).splitlines()
    assert "Name=Jellyfin (web)" in lines
    assert f"Exec=xdg-open {WEB_URL}" in lines
    assert "Type=Application" in lines


def test_web_entry_is_removed_when_jellyfin_is_off() -> None:
    task = _task(MANAGED, "Remove the Jellyfin (web) menu entry when Jellyfin is not enabled")
    assert task["ansible.builtin.file"] == {"path": "{{ jellyfin_web_desktop_entry }}", "state": "absent"}
    env = _environment()

    def removes(enabled: bool, arch: str = "x86_64", profile: str = "desktop") -> bool:
        cond = "{{ " + " ".join(task["when"].split()) + " }}"
        rendered = env.from_string(cond).render(
            jellyfin_desktop_enabled=enabled, ansible_architecture=arch, workstation_profile=profile
        )
        return rendered == "True"

    assert removes(False)
    assert not removes(True)
    assert removes(True, arch="aarch64")
    assert removes(True, profile="server")


def test_no_browser_policy_is_written() -> None:
    text = (ROLE / "tasks/jellyfin-desktop.yml").read_text() + (ROLE / "tasks/flatpak.yml").read_text()
    for forbidden in ("/etc/opt/chrome", "policies/managed", "ManagedBookmarks", "HomepageLocation"):
        assert forbidden not in text


def _menu_items(enabled: bool, arch: str = "x86_64") -> dict[str, str]:
    root = ET.fromstring(_render(MENU, jellyfin_desktop_enabled=enabled, ansible_architecture=arch))
    ns = {"o": "http://openbox.org/3.4/menu"}
    apps = root.find("o:menu[@id='apps-menu']", ns)
    return {item.get("label"): item.find(".//o:command", ns).text for item in apps.findall("o:item", ns)}


def test_openbox_menu_lists_both_entries_only_when_enabled() -> None:
    on = _menu_items(True)
    assert on["Jellyfin Desktop"] == f"flatpak run {APP}"
    assert on["Jellyfin (web)"] == f"xdg-open {WEB_URL}"
    assert "Jellyfin Desktop" not in _menu_items(False)
    assert "Jellyfin (web)" not in _menu_items(True, arch="aarch64")
    assert {"Firefox", "Terminal", "File Manager", "App Launcher"} <= set(_menu_items(False))


def test_openbox_menu_is_deployed_as_a_template() -> None:
    task = _task(OPENBOX, "Deploy Openbox menu.xml")
    assert task["ansible.builtin.template"]["src"] == "minimal-desktop-menu.xml.j2"


def test_flatpak_update_runs_beside_snap_refresh_only_where_installed() -> None:
    names = [t["name"] for t in UPDATES]
    snap = names.index("Refresh installed Snap packages")
    assert names[snap + 1 : snap + 3] == ["Check whether Flatpak is installed", "Update system Flatpak apps and runtimes"]
    update = _task(UPDATES, "Update system Flatpak apps and runtimes")
    assert update["ansible.builtin.command"]["argv"] == ["flatpak", "update", "-y", "--noninteractive", "--system"]
    assert "_flatpak_installed.stat.exists | default(false)" in update["when"]
    stat = _task(UPDATES, "Check whether Flatpak is installed")
    assert stat["ansible.builtin.stat"]["path"] == "/usr/bin/flatpak"
    warn = _task(UPDATES, "Warn when the Flatpak update failed")
    assert "_flatpak_update.rc != 0" in warn["when"]


def _run_helper(
    tmp_path: Path, *, installed: bool = True, ref: str = REF, origin: str = REMOTE, missing: str | None = None
) -> subprocess.CompletedProcess:
    """Run the rendered helper with a fake `flatpak` and its paths rewritten into tmp_path."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (tmp_path / "info.txt").write_text(INFO)
    fake = bindir / "flatpak"
    fake.write_text(
        "#!/bin/sh\n"
        f"[ {'0' if installed else '1'} = 0 ] || exit 1\n"
        'case "$*" in\n'
        f'  *--show-ref*) echo "{ref}" ;;\n'
        f'  *--show-origin*) echo "{origin}" ;;\n'
        f'  "info --system {APP}") cat "{tmp_path}/info.txt" ;;\n'
        "  *) exit 2 ;;\n"
        "esac\n"
    )
    fake.chmod(0o755)
    paths = {
        f"/var/lib/flatpak/exports/share/applications/$app.desktop": tmp_path / f"{APP}.desktop",
        WEB_ENTRY: tmp_path / "curiosity-jellyfin-web.desktop",
        "/etc/profile.d/flatpak.sh": tmp_path / "flatpak.sh",
        "/etc/X11/Xsession.d/20flatpak": tmp_path / "20flatpak",
    }
    content = _task(INSTALL, "Install the Jellyfin Desktop healthcheck helper")["ansible.builtin.copy"]["content"]
    script = _render(content)
    for original, local in paths.items():
        assert original in script, original
        script = script.replace(original, str(local))
        if original != missing:
            local.write_text("")
    helper = tmp_path / "check.sh"
    helper.write_text(script)
    helper.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:/usr/bin:/bin"}
    return subprocess.run([str(helper)], capture_output=True, text=True, env=env, check=False)


def test_helper_reports_the_installed_version(tmp_path: Path) -> None:
    result = _run_helper(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"jellyfin desktop 2.0.0 ({REF} from {REMOTE})"


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"installed": False}, "flatpak info --system"),
        ({"ref": REF.replace("stable", "beta")}, "unexpected ref"),
        ({"origin": "fedora"}, "unexpected origin"),
        ({"missing": "/var/lib/flatpak/exports/share/applications/$app.desktop"}, "missing"),
        ({"missing": WEB_ENTRY}, "curiosity-jellyfin-web.desktop missing"),
        ({"missing": "/etc/profile.d/flatpak.sh"}, "flatpak.sh missing"),
    ],
    ids=["not-installed", "wrong-ref", "wrong-origin", "app-entry-missing", "web-entry-missing", "no-xdg-path"],
)
def test_helper_fails_with_one_clear_line(tmp_path: Path, kwargs: dict, message: str) -> None:
    result = _run_helper(tmp_path, **kwargs)
    assert result.returncode == 1
    assert message in result.stderr
    assert len(result.stderr.strip().splitlines()) == 1
