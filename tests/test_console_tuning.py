"""Regression checks for the opt-in console tuning task group.

The feature is OPT-IN, never touches the bootloader or kernel command line, and
its helper is a no-op whenever there is nothing to do. These checks pin those
properties and unit-test the helper's pure logic with a fake fb_var_screeninfo,
so they run on any machine without a framebuffer.
"""

import configparser
import importlib.machinery
import importlib.util
import re
import struct
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
TASKS = ROLE / "tasks"
HELPER = ROLE / "files/curiosity-console-tuning"
TASK_FILE = TASKS / "console-tuning.yml"
NEW_FILES = (
    HELPER,
    TASK_FILE,
    ROLE / "templates/console-tuning.conf.j2",
    ROOT / "docs/console-tuning.md",
)


def _load_helper():
    loader = importlib.machinery.SourceFileLoader("curiosity_console_tuning", str(HELPER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve annotations through sys.modules.
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


helper = _load_helper()


def _fake_var(xres, yres, xv, yv, xoff=0, yoff=0, bpp=32, activate=0):
    words = [0] * 40
    words[0], words[1], words[2], words[3] = xres, yres, xv, yv
    words[4], words[5], words[6], words[21] = xoff, yoff, bpp, activate
    words[26] = 12345  # pixclock: must survive a PUT untouched
    return helper.parse_var(struct.pack("=40I", *words))


# ---------------------------------------------------------------------------
# Defaults and wiring
# ---------------------------------------------------------------------------


def test_console_tuning_is_off_by_default_and_changes_nothing_else() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults["console_tuning_enabled"] is False
    assert defaults["console_tuning_font"] == ""
    assert defaults["console_tuning_palette"] == {}
    assert defaults["console_tuning_manage_console_setup"] is False
    assert defaults["console_tuning_extra_packages"] == []
    # Existing T460 defaults are untouched.
    assert defaults["t460_fbcon_horizontal_enabled"] is False
    assert defaults["t460_console_diagnostics_enabled"] is True


def test_main_includes_console_tuning_behind_the_opt_in() -> None:
    main = yaml.safe_load((TASKS / "main.yml").read_text())
    entry = next(
        task
        for task in main
        if task.get("ansible.builtin.include_tasks") == "console-tuning.yml"
    )
    assert entry["tags"] == ["console", "display"]
    conditions = "\n".join(entry["when"])
    assert "console_tuning_enabled | default(false)" in conditions
    assert "workstation_profile in ['desktop', 'thin-client']" in conditions
    assert "ansible_system == 'Linux'" in conditions
    assert "ansible_virtualization_type" in conditions
    assert "container" in conditions


def test_no_bootloader_or_reboot_changes() -> None:
    forbidden = (
        "grub",
        "update-grub",
        "grub-mkconfig",
        "/boot",
        "kernelstub",
        "systemd-boot",
        "reboot",
        "poweroff",
        "shutdown",
        "kexec",
        "modprobe",
        "rmmod",
    )
    for path in (HELPER, TASK_FILE, ROLE / "templates/console-tuning.conf.j2"):
        text = path.read_text().lower()
        for word in forbidden:
            assert word not in text, f"{path.name} must not reference {word!r}"


def test_apt_packages_are_debian_scoped() -> None:
    tasks = yaml.safe_load(TASK_FILE.read_text())
    apt = next(t for t in tasks if "ansible.builtin.apt" in t)
    assert "ansible_os_family == 'Debian'" in apt["when"]
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert {"kbd", "console-setup", "python3"} <= set(defaults["console_tuning_packages"])


def test_unit_and_udev_rule_are_sane() -> None:
    tasks = yaml.safe_load(TASK_FILE.read_text())
    unit = next(
        t["ansible.builtin.copy"]["content"]
        for t in tasks
        if "ansible.builtin.copy" in t
        and t["ansible.builtin.copy"]["dest"].endswith("curiosity-console-tuning.service")
    )
    assert "Type=oneshot" in unit
    assert "After=console-setup.service" in unit
    assert "ConditionVirtualization=!container" in unit
    assert "WantedBy=multi-user.target" in unit
    # Not RemainAfterExit: a finished oneshot must be startable again by udev.
    assert "RemainAfterExit" not in unit
    assert "StartLimitBurst" in unit

    rules = next(
        t["ansible.builtin.copy"]["content"]
        for t in tasks
        if "ansible.builtin.copy" in t
        and t["ansible.builtin.copy"]["dest"].endswith(".rules")
    )
    assert 'SUBSYSTEM=="graphics", KERNEL=="fb0", ACTION=="add"' in rules
    assert 'TAG+="systemd"' in rules
    assert 'ENV{SYSTEMD_WANTS}+="curiosity-console-tuning.service"' in rules
    assert 'SUBSYSTEM=="drm"' in rules and 'ACTION=="change"' in rules
    assert "systemctl --no-block start curiosity-console-tuning.service" in rules


def test_console_setup_integration_uses_font_and_save_only() -> None:
    text = TASK_FILE.read_text()
    assert "regexp: '^FONT='" in text
    assert "when:\n    - console_tuning_manage_console_setup | bool" in text
    handlers = (ROLE / "handlers/main.yml").read_text()
    assert "setupcon --save-only" in handlers
    assert "_console_tuning_console_setup.stat.exists" in handlers


def test_every_handler_notified_exists() -> None:
    handlers = {h["name"] for h in yaml.safe_load((ROLE / "handlers/main.yml").read_text())}
    notified = set()
    for task in yaml.safe_load(TASK_FILE.read_text()):
        value = task.get("notify", [])
        notified.update([value] if isinstance(value, str) else value)
    assert {"Apply console tuning now", "Reload udev rules", "Save console-setup font cache"} <= notified
    assert notified <= handlers


def test_docs_changelog_and_readme_are_linked() -> None:
    assert "docs/console-tuning.md" in (ROOT / "README.md").read_text()
    assert "console-tuning.md" in (ROOT / "docs/configuration.md").read_text()
    assert "console tuning" in (ROOT / "CHANGELOG.md").read_text().lower()
    docs = (ROOT / "docs/console-tuning.md").read_text()
    for needle in (
        "console_tuning_enabled",
        "console_tuning_font",
        "console_tuning_palette",
        "curiosity-console-status",
        "video=",
        "echo detect",
        "Shift",
    ):
        assert needle in docs


def test_no_private_strings_in_new_files() -> None:
    markers = (
        "developerdojo",
        "focusapiary",
        "focuspass",
        "instarlab",
        "seantech",
        "/home/",
        "omlab",
        "worldenterprise",
        "tester@",
    )
    for path in NEW_FILES:
        text = path.read_text().lower()
        for marker in markers:
            assert marker not in text, f"{path.name} contains {marker!r}"
        assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text)


def test_no_empty_except_blocks_in_helper() -> None:
    text = HELPER.read_text()
    assert not re.search(r"except[^\n]*:\s*\n\s*pass\b", text)


# ---------------------------------------------------------------------------
# Helper logic
# ---------------------------------------------------------------------------


def test_plan_resize_grows_cropped_console() -> None:
    var = _fake_var(1920, 1080, 3840, 2160)
    target, reason = helper.plan_resize(var)
    assert target == (3840, 2160)
    assert "smaller" in reason


@pytest.mark.parametrize(
    "var",
    [
        _fake_var(3840, 2160, 3840, 2160),  # already full size: idempotent
        _fake_var(1920, 1080, 1920, 1080),  # single display
        _fake_var(0, 0, 0, 0),  # nothing reported
        _fake_var(1920, 1080, 3840, 2160, yoff=10),  # panned: leave alone
    ],
)
def test_plan_resize_is_a_noop_when_nothing_to_do(var) -> None:
    target, reason = helper.plan_resize(var)
    assert target is None
    assert reason


def test_put_buffer_only_changes_geometry_and_activation() -> None:
    var = _fake_var(1920, 1080, 3840, 2160)
    payload = helper.build_put_buffer(var, 3840, 2160)
    out = helper.parse_var(payload)
    assert (out.xres, out.yres) == (3840, 2160)
    assert (out.xres_virtual, out.yres_virtual) == (3840, 2160)
    assert out.raw[21] == helper.FB_ACTIVATE_NOW | helper.FB_ACTIVATE_ALL == 64
    assert out.raw[26] == 12345
    assert out.bits_per_pixel == 32
    # Applying the plan to its own output is a no-op: idempotent.
    assert helper.plan_resize(out)[0] is None


def test_parse_var_rejects_wrong_size() -> None:
    with pytest.raises(ValueError):
        helper.parse_var(b"\0" * 12)


def test_fb_name_classification() -> None:
    assert helper.is_drm_fb("i915drmfb")
    assert helper.is_drm_fb("inteldrmfb")
    assert not helper.is_drm_fb("vesafb")
    assert helper.is_early_fb("simpledrmdrmfb")
    assert helper.is_early_fb("efifb")
    assert not helper.is_early_fb("i915drmfb")


def test_palette_sequence_uses_osc_p_escapes() -> None:
    seq = helper.palette_sequence({15: "B4AFAA", 7: "857f7a"})
    assert seq == b"\x1b]P7857f7a\x1b]PFb4afaa"
    assert helper.palette_sequence({}) == b""
    for bad in ({16: "aaaaaa"}, {7: "zzzzzz"}, {7: "fff"}, {-1: "aaaaaa"}):
        with pytest.raises(ValueError):
            helper.palette_sequence(bad)


def test_config_parsing_skips_invalid_values() -> None:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(
        "[console-tuning]\n"
        "fill_framebuffer = maybe\n"
        "font = Uni3-Terminus20x10.psf.gz\n"
        "ttys = 1 2 x 99 3\n"
        "wait_seconds = soon\n"
        "settle_seconds = 500\n"
        "[palette]\n"
        "7 = 857F7A\n"
        "16 = 000000\n"
        "15 = nothex\n"
    )
    cfg, warnings = helper.parse_config(parser)
    assert cfg.fill_framebuffer is True
    assert cfg.font == "Uni3-Terminus20x10.psf.gz"
    assert cfg.ttys == [1, 2, 3]
    assert cfg.wait_seconds == 15
    assert cfg.settle_seconds == 120
    assert cfg.palette == {7: "857f7a"}
    assert len(warnings) == 4


def test_missing_config_falls_back_to_safe_defaults(tmp_path) -> None:
    cfg, warnings = helper.load_config(str(tmp_path / "absent.conf"))
    assert cfg.font == "" and cfg.palette == {}
    assert warnings


def test_wait_for_framebuffer_is_bounded_and_replaces_early_fb() -> None:
    clock = {"now": 0.0}
    names = iter(["simpledrmdrmfb", "simpledrmdrmfb", "i915drmfb"])
    current = {"name": "simpledrmdrmfb"}

    def sleep(seconds: float) -> None:
        clock["now"] += seconds
        current["name"] = next(names, "i915drmfb")

    assert helper.wait_for_framebuffer(
        lambda: True, lambda: current["name"], 15, sleep=sleep, clock=lambda: clock["now"]
    )
    assert clock["now"] <= 3

    # A framebuffer that never gets replaced is used after the budget expires.
    clock["now"] = 0.0
    assert helper.wait_for_framebuffer(
        lambda: True,
        lambda: "efifb",
        5,
        sleep=lambda s: clock.__setitem__("now", clock["now"] + s),
        clock=lambda: clock["now"],
    )
    assert clock["now"] >= 5

    # No framebuffer at all returns immediately without sleeping.
    assert not helper.wait_for_framebuffer(
        lambda: False, lambda: "", 15, sleep=lambda s: pytest.fail("must not sleep")
    )


def test_main_exits_zero_without_a_framebuffer(tmp_path, capsys) -> None:
    code = helper.main(
        ["--fb", str(tmp_path / "no-fb"), "--config", str(tmp_path / "none.conf")]
    )
    assert code == 0
    assert "nothing to do" in capsys.readouterr().out


def test_status_verdicts(tmp_path, monkeypatch, capsys) -> None:
    fb = tmp_path / "fb0"
    fb.write_text("")
    monkeypatch.setattr(helper, "drm_connectors", lambda: [])
    monkeypatch.setattr(helper, "vt_grid", lambda tty="": None)
    monkeypatch.setattr(helper, "vt_font_cell", lambda tty="": None)
    monkeypatch.setattr(helper, "fb_name", lambda path: "i915drmfb")
    args = helper.build_parser().parse_args(
        ["--status", "--fb", str(fb), "--config", str(tmp_path / "none.conf")]
    )

    cases = (
        (helper.KD_TEXT, _fake_var(1920, 1080, 3840, 2160), "only a crop of the framebuffer"),
        (helper.KD_TEXT, _fake_var(3840, 2160, 3840, 2160), "OK"),
        (helper.KD_GRAPHICS, _fake_var(1920, 1080, 3840, 2160), "graphical session"),
    )
    for mode, var, expected in cases:
        monkeypatch.setattr(helper, "vt_text_mode", lambda tty="", m=mode: m)
        monkeypatch.setattr(helper, "read_fb_var", lambda path, v=var: v)
        assert helper.run_status(args) == 0
        out = capsys.readouterr().out
        verdict = [line for line in out.splitlines() if line.startswith("verdict:")]
        assert expected in verdict[0]


def test_dry_run_never_writes(tmp_path, monkeypatch, capsys) -> None:
    fb = tmp_path / "fb0"
    fb.write_text("")
    conf = tmp_path / "c.conf"
    conf.write_text("[console-tuning]\nfont = x.psf.gz\n[palette]\n7 = 857f7a\n")
    var = _fake_var(1920, 1080, 3840, 2160)
    monkeypatch.setattr(helper, "read_fb_var", lambda path: var)
    monkeypatch.setattr(helper, "fb_name", lambda path: "i915drmfb")
    monkeypatch.setattr(helper, "vt_text_mode", lambda tty="": helper.KD_TEXT)
    monkeypatch.setattr(helper, "in_container", lambda: False)
    monkeypatch.setattr(helper, "tty_path", lambda n: str(fb))
    monkeypatch.setattr(
        helper, "put_fb_var", lambda *a: pytest.fail("dry-run must not write the fb")
    )
    monkeypatch.setattr(
        helper.subprocess, "run", lambda *a, **k: pytest.fail("dry-run must not run setfont")
    )
    monkeypatch.setattr(helper.shutil, "which", lambda name: "/usr/bin/setfont")
    assert helper.main(["--dry-run", "--fb", str(fb), "--config", str(conf)]) == 0
    out = capsys.readouterr().out
    assert "would set" in out and "3840x2160" in out
    assert "would run setfont" in out
    assert "would write 1 palette entries" in out
    assert fb.read_text() == ""
