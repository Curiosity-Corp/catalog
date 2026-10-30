"""Regression checks for the display package integrity guard (catalog#122).

deskmeet1 crash-looped X with SIGBUS in ValidatePicture because the page-cache
copy of /usr/lib/xorg/Xorg had flipped bits while the disk copy was correct
(`dpkg --verify` reported ``??5??????   /usr/lib/xorg/Xorg``). The helper must
reinstall exactly the packages with a non-conffile md5 mismatch and nothing
else. It runs here against stub ``dpkg-query``, ``dpkg`` and ``apt-get``.
"""

import os
import stat
import subprocess
from pathlib import Path

import jinja2
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
SCRIPT = ROLE / "files/minimal-desktop/curiosity-display-integrity"
TASK_FILE = ROLE / "tasks/minimal-desktop-integrity.yml"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
HANDLERS = yaml.safe_load((ROLE / "handlers/main.yml").read_text())

# What deskmeet1 printed on 2026-10-29 for xserver-xorg-core.
CORRUPT_XORG = "??5??????   /usr/lib/xorg/Xorg\n"
EDITED_CONFFILE = "??5?????? c /etc/lightdm/lightdm.conf\n"


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run(tmp_path: Path, verify: dict[str, str], installed=None, apt_exit=0, args=()):
    """Run the helper with stubbed tools; return (result, apt-get argv or None)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for pkg, out in verify.items():
        (tmp_path / f"verify-{pkg}").write_text(out)
    installed = set(verify) if installed is None else set(installed)
    for pkg in installed:
        (tmp_path / f"installed-{pkg}").write_text("")
    _stub(
        bindir / "dpkg-query",
        f'for p; do :; done\n[ -e "{tmp_path}/installed-$p" ] || exit 1\nprintf "ii "\n',
    )
    _stub(bindir / "dpkg", f'[ "$1" = --verify ] || exit 9\ncat "{tmp_path}/verify-$2" 2>/dev/null\n')
    _stub(bindir / "apt-get", f'echo "$@" > "{tmp_path}/apt-argv"\nexit {apt_exit}\n')
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "CURIOSITY_INTEGRITY_PACKAGES": " ".join(verify)}
    result = subprocess.run(
        ["sh", str(SCRIPT), *args], env=env, capture_output=True, text=True, check=False
    )
    argv = tmp_path / "apt-argv"
    return result, (argv.read_text().split() if argv.exists() else None)


def test_script_is_executable_and_valid_sh():
    assert os.access(SCRIPT, os.X_OK)
    subprocess.run(["sh", "-n", str(SCRIPT)], check=True)


def test_corrupt_xorg_is_reinstalled_and_nothing_else(tmp_path):
    result, argv = _run(
        tmp_path, {"xserver-xorg-core": CORRUPT_XORG, "lightdm": "", "libpixman-1-0": ""}
    )
    assert result.returncode == 0, result.stderr
    assert "/usr/lib/xorg/Xorg" in result.stdout
    assert argv is not None
    assert argv[:3] == ["install", "--reinstall", "-y"]
    assert argv[-1] == "xserver-xorg-core"
    assert "lightdm" not in argv and "libpixman-1-0" not in argv
    assert "Dpkg::Options::=--force-confold" in argv


def test_clean_packages_do_not_touch_apt(tmp_path):
    result, argv = _run(tmp_path, {"xserver-xorg-core": "", "lightdm": ""})
    assert result.returncode == 0
    assert argv is None
    assert "verified clean" in result.stdout


def test_an_edited_conffile_is_not_a_reason_to_reinstall(tmp_path):
    result, argv = _run(tmp_path, {"lightdm": EDITED_CONFFILE})
    assert result.returncode == 0
    assert argv is None


def test_packages_that_are_not_installed_are_skipped(tmp_path):
    result, argv = _run(
        tmp_path, {"slick-greeter": CORRUPT_XORG, "xserver-xorg-core": ""}, installed=["xserver-xorg-core"]
    )
    assert result.returncode == 0
    assert argv is None


def test_failed_reinstall_fails_the_unit(tmp_path):
    result, argv = _run(tmp_path, {"xserver-xorg-core": CORRUPT_XORG}, apt_exit=100)
    assert argv is not None
    assert result.returncode == 1
    assert "reinstall failed" in result.stderr


def test_defaults_cover_the_x_server_and_enable_the_guard():
    assert DEFAULTS["minimal_desktop_integrity_enabled"] is True
    assert "xserver-xorg-core" in DEFAULTS["minimal_desktop_integrity_packages"]
    assert "libc6" not in DEFAULTS["minimal_desktop_integrity_packages"]


@pytest.mark.parametrize("name", ["service", "timer"])
def test_units_render(name):
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(ROLE / "templates")), undefined=jinja2.StrictUndefined
    )
    text = env.get_template(f"curiosity-display-integrity.{name}.j2").render(
        minimal_desktop_integrity_packages=["xserver-xorg-core", "lightdm"],
        minimal_desktop_integrity_interval="10min",
    )
    if name == "service":
        assert 'CURIOSITY_INTEGRITY_PACKAGES=xserver-xorg-core lightdm"' in text
        assert "ExecStart=/usr/local/bin/curiosity-display-integrity" in text
    else:
        assert "OnUnitActiveSec=10min" in text


def test_tasks_never_restart_lightdm_and_retire_cleanly():
    tasks = yaml.safe_load(TASK_FILE.read_text())
    text = TASK_FILE.read_text()
    assert "lightdm" not in [
        t.get("ansible.builtin.systemd", {}).get("name") for b in tasks for t in b.get("block", [])
    ]
    retire = next(t for t in tasks if t["name"].startswith("Retire"))
    removed = next(t for t in retire["block"] if t["name"] == "Remove managed display integrity files")
    assert "/usr/local/bin/curiosity-display-integrity" in removed["loop"]
    assert "Restart curiosity display-integrity timer" in text
    assert any(h["name"] == "Restart curiosity display-integrity timer" for h in HANDLERS)


def test_integrity_runs_before_lightdm_and_clears_its_failed_state():
    includes = [
        t["ansible.builtin.include_tasks"]["file"]
        for t in yaml.safe_load((ROLE / "tasks/minimal-desktop.yml").read_text())
        if "ansible.builtin.include_tasks" in t
    ]
    assert includes.index("minimal-desktop-integrity.yml") < includes.index("minimal-desktop-lightdm.yml")
    block = next(t for t in yaml.safe_load(TASK_FILE.read_text()) if t["name"].startswith("Apply"))["block"]
    names = [t["name"] for t in block]
    run = block[names.index("Verify display packages before LightDM starts")]
    assert run["ansible.builtin.command"]["argv"].startswith("{{ ['/usr/local/bin/curiosity-display-integrity']")
    assert "reinstalled:" in run["changed_when"]
    reset = block[names.index("Clear LightDM's failed state after a reinstall")]
    assert reset["ansible.builtin.command"] == "systemctl reset-failed lightdm.service"
    assert reset["when"] == "_display_integrity_run.stdout is search('reinstalled:')"
