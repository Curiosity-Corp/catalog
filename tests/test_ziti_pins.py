"""Ziti /etc/hosts pins follow the tunnel's lifecycle instead of going stale (#1).

ziti-edge-tunnel reallocates its overlay addresses when it restarts, so a pin
written before the restart can point at nothing or at another service. These
checks pin the role wiring, run the real helper against a stub `dig` and a
temporary hosts file (the before/after procedure, with documentation-only
names), and run real `ansible-playbook` to prove that `--tags ziti` reaches the
tasks inside the included file. They need no tunnel, no root and no network.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import jinja2
import pytest
import yaml

ROLE = Path(__file__).resolve().parents[1] / "roles/developer_workstation"
SCRIPT = ROLE / "files/curiosity-ziti-pins"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())

BEGIN = "# BEGIN ANSIBLE MANAGED BLOCK: Ziti DNS pins"
END = "# END ANSIBLE MANAGED BLOCK: Ziti DNS pins"


class _Loader(yaml.SafeLoader):
    """Safe loader that reads Ansible's `!unsafe` scalars as plain strings."""


_Loader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))


def _load(path: Path) -> list[dict]:
    return yaml.load(path.read_text(), Loader=_Loader)


def _render(name: str, **overrides: object) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(ROLE / "templates")),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
    )
    return env.get_template(name).render(**{**DEFAULTS, **overrides})


# ---------------------------------------------------------------------------
# Defaults and role wiring
# ---------------------------------------------------------------------------


def test_defaults_name_no_private_host_and_keep_the_lifecycle_on() -> None:
    assert DEFAULTS["ziti_etc_hosts_pins"] == []
    assert DEFAULTS["ziti_pin_lifecycle_enabled"] is True
    assert DEFAULTS["ziti_pin_resolver"] == "100.64.0.2"
    assert DEFAULTS["ziti_service_name"] == "ziti-edge-tunnel.service"
    assert int(DEFAULTS["ziti_pin_wait"]) > 0


def test_dropin_hooks_the_configured_unit_and_cannot_delay_or_fail_it() -> None:
    text = _render("curiosity-ziti-pins.dropin.conf.j2", ziti_service_name="custom-tunnel.service")
    assert "[Service]" in text
    # "-" ignores failure; --no-block returns at once; restart replaces a waiting run.
    assert "ExecStartPost=-/usr/bin/systemctl --no-block restart curiosity-ziti-pins.service" in text
    assert "custom-tunnel.service" in text  # named in the comment only
    assert "Restart=" not in text.replace("ExecStartPost", "")


def test_service_is_ordered_after_the_configured_unit_and_bounded() -> None:
    text = _render("curiosity-ziti-pins.service.j2", ziti_service_name="custom-tunnel.service", ziti_pin_wait=45)
    assert "After=custom-tunnel.service" in text
    assert "Type=oneshot" in text
    assert "ExecStart=/usr/local/sbin/curiosity-ziti-pins refresh --wait 45" in text
    assert "TimeoutStartSec=75" in text
    assert "EnvironmentFile=/etc/curiosity/ziti-pins.env" in text
    assert "Before=" not in text  # never blocks the tunnel's own start


def test_env_file_carries_the_host_local_pins_only() -> None:
    text = _render("curiosity-ziti-pins.env.j2", ziti_etc_hosts_pins=["svc-a.example.test", "svc-b.example.test"])
    assert "ZITI_PINS_HOSTS=svc-a.example.test,svc-b.example.test" in text
    assert "ZITI_PINS_RESOLVER=100.64.0.2" in text


def test_tasks_install_the_hooks_and_retire_them() -> None:
    by_name = {t["name"]: t for t in _load(ROLE / "tasks/ziti.yml")}
    handlers = {h["name"] for h in yaml.safe_load((ROLE / "handlers/main.yml").read_text())}

    for name in (
        "Install Ziti pin refresh systemd service",
        "Install the Ziti tunnel ExecStartPost drop-in that refreshes the pins",
    ):
        task = by_name[name]
        assert "ziti_pin_lifecycle_enabled | bool" in task["when"]
        assert "ziti_etc_hosts_pins | length > 0" in task["when"]
        assert task["notify"] == "Reload systemd" and task["notify"] in handlers

    dropin = by_name["Install the Ziti tunnel ExecStartPost drop-in that refreshes the pins"]
    assert dropin["ansible.builtin.template"]["dest"] == "/etc/systemd/system/{{ ziti_service_name }}.d/20-curiosity-ziti-pins.conf"

    # Opt-out removes only this role's own drop-in and service, and restarts nothing.
    retire = by_name["Remove the Ziti pin lifecycle hooks when they are not wanted"]
    assert "not (ziti_pin_lifecycle_enabled | bool)" in retire["when"]
    assert retire["loop"] == [
        "/etc/systemd/system/{{ ziti_service_name }}.d/20-curiosity-ziti-pins.conf",
        "/etc/systemd/system/curiosity-ziti-pins.service",
    ]
    assert retire["notify"] == "Reload systemd"

    for task in _load(ROLE / "tasks/ziti.yml"):
        if "pin" in task["name"].lower():
            notified = task.get("notify")
            assert notified in (None, "Reload systemd"), f"{task['name']} must not restart the tunnel"


def test_run_time_refresh_uses_the_helper_and_the_operator_pins() -> None:
    by_name = {t["name"]: t for t in _load(ROLE / "tasks/ziti.yml")}
    task = by_name["Refresh the Ziti pins in /etc/hosts"]
    assert task["ansible.builtin.command"]["argv"] == ["{{ ziti_pin_script_path }}", "refresh"]
    assert task["environment"]["ZITI_PINS_HOSTS"] == "{{ ziti_etc_hosts_pins | join(',') }}"
    assert task["when"] == "ziti_etc_hosts_pins | length > 0"
    # An emptied list removes the block it manages instead of leaving stale pins.
    gone = by_name["Remove the Ziti pin block from /etc/hosts when no pins are configured"]
    assert gone["ansible.builtin.blockinfile"]["state"] == "absent"
    assert gone["ansible.builtin.blockinfile"]["marker"] == "# {mark} ANSIBLE MANAGED BLOCK: Ziti DNS pins"


def test_unit_name_is_validated_before_it_reaches_a_path() -> None:
    by_name = {t["name"]: t for t in _load(ROLE / "tasks/ziti.yml")}
    that = by_name["Validate Ziti pin inputs"]["ansible.builtin.assert"]["that"]
    assert any("ziti_service_name is match" in item for item in that)
    assert any("ziti_etc_hosts_pins" in item and "match" in item for item in that)


# ---------------------------------------------------------------------------
# The helper against a stub resolver (documentation-only names and addresses)
# ---------------------------------------------------------------------------

DIG = """#!/bin/bash
# Stub dig: the answers are the lines "name address..." in $STUB_ZONE.
echo "dig $*" >> "$STUB_DIR/calls"
[ -n "$STUB_DIG_RC" ] && exit "$STUB_DIG_RC"
name=""
for arg in "$@"; do case "$arg" in +*|@*) ;; *) name="$arg" ;; esac; done
awk -v n="$name" '$1 == n { for (i = 2; i <= NF; i++) print $i }' "$STUB_ZONE"
"""

HOSTS = "127.0.0.1  localhost\n192.0.2.7  keepme.example.test\n"


@pytest.fixture()
def lab(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    dig = bin_dir / "dig"
    dig.write_text(DIG)
    dig.chmod(0o755)
    (tmp_path / "zone").write_text("")
    (tmp_path / "hosts").write_text(HOSTS)
    return tmp_path


def _zone(lab: Path, **names: str) -> None:
    (lab / "zone").write_text("".join(f"{name.replace('_', '-')}.example.test {answer}\n" for name, answer in names.items()))


def _run(lab: Path, *args: str, pins: str = "svc-a.example.test,svc-b.example.test", **extra: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "STUB_DIR": str(lab),
        "STUB_ZONE": str(lab / "zone"),
        "ZITI_PINS_DIG": str(lab / "bin/dig"),
        "ZITI_PINS_HOSTS_FILE": str(lab / "hosts"),
        "ZITI_PINS_LOCK": str(lab / "lock"),
        "ZITI_PINS_HOSTS": pins,
        "ZITI_PINS_INTERVAL": "0.1",
        **extra,
    }
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60, check=False)


def _hosts(lab: Path) -> str:
    return (lab / "hosts").read_text()


def _block(lab: Path) -> list[str]:
    lines = _hosts(lab).splitlines()
    if BEGIN not in lines:
        return []
    return lines[lines.index(BEGIN) + 1 : lines.index(END)]


def test_script_is_executable_and_python_parses() -> None:
    assert stat.S_IMODE(SCRIPT.stat().st_mode) & 0o111
    compile(SCRIPT.read_text(), str(SCRIPT), "exec")


def test_before_after_a_tunnel_restart_reallocates_the_addresses(lab: Path) -> None:
    """The measurement from docs/ziti-pin-lifecycle.md, run against stubs."""
    _zone(lab, svc_a="100.64.0.10", svc_b="100.64.0.11")
    assert _run(lab, "refresh").returncode == 0
    assert _block(lab) == ["100.64.0.10  svc-a.example.test", "100.64.0.11  svc-b.example.test"]
    assert _run(lab, "check").returncode == 0

    # BEFORE the fix is applied: the tunnel restarted and swapped the two addresses.
    _zone(lab, svc_a="100.64.0.11", svc_b="100.64.0.10")
    drift = _run(lab, "check")
    assert drift.returncode == 1
    assert drift.stdout.count(" stale ") == 2
    assert _block(lab) == ["100.64.0.10  svc-a.example.test", "100.64.0.11  svc-b.example.test"]  # the hazard

    # AFTER: the lifecycle refresh converges the block onto the live answers.
    done = _run(lab, "refresh")
    assert "changed: 2 of 2 pins resolved" in done.stdout
    assert _block(lab) == ["100.64.0.11  svc-a.example.test", "100.64.0.10  svc-b.example.test"]
    assert _run(lab, "check").returncode == 0


def test_check_output_is_redacted_unless_names_are_requested(lab: Path) -> None:
    _zone(lab, svc_a="100.64.0.10", svc_b="100.64.0.11")
    _run(lab, "refresh")
    _zone(lab, svc_a="100.64.0.12", svc_b="100.64.0.11")
    redacted = _run(lab, "check")
    assert redacted.stdout.splitlines() == [
        "pin-1 stale pinned=addr-1 live=addr-2",
        "pin-2 ok pinned=addr-3 live=addr-3",
        "drift=1 pins=2",
    ]
    for private in ("example.test", "100.64"):
        assert private not in redacted.stdout
    named = _run(lab, "check", "--show-names")
    assert "svc-a.example.test stale pinned=100.64.0.10 live=100.64.0.12" in named.stdout


def test_a_pin_the_resolver_cannot_answer_is_invalidated_not_left_stale(lab: Path) -> None:
    _zone(lab, svc_a="100.64.0.10", svc_b="100.64.0.11")
    _run(lab, "refresh")
    _zone(lab, svc_b="100.64.0.11")  # the tunnel came back without svc-a
    result = _run(lab, "refresh")
    assert "1 of 2 pins resolved, 1 dropped" in result.stdout
    assert _block(lab) == ["100.64.0.11  svc-b.example.test"]
    state = _run(lab, "check").stdout
    assert "pin-1 absent" in state


def test_a_dead_resolver_drops_every_pin_and_touches_nothing_else(lab: Path) -> None:
    _zone(lab, svc_a="100.64.0.10", svc_b="100.64.0.11")
    _run(lab, "refresh")
    assert _run(lab, "refresh", STUB_DIG_RC="9").returncode == 0
    assert BEGIN not in _hosts(lab)
    assert _hosts(lab) == HOSTS  # unrelated lines are byte-identical


def test_only_overlay_addresses_are_pinned(lab: Path) -> None:
    (lab / "zone").write_text(
        "svc-a.example.test target.example.test. 10.1.2.3 100.64.0.20\nsvc-b.example.test 203.0.113.9 not-an-address\n"
    )
    _run(lab, "refresh")
    assert _block(lab) == ["100.64.0.20  svc-a.example.test"]  # CNAME text and non-overlay answers ignored


def test_refresh_is_idempotent_and_keeps_unrelated_lines(lab: Path) -> None:
    _zone(lab, svc_a="100.64.0.10", svc_b="100.64.0.11")
    first = _run(lab, "refresh")
    after_first = _hosts(lab)
    second = _run(lab, "refresh")
    assert "changed:" in first.stdout and "unchanged:" in second.stdout
    assert _hosts(lab) == after_first
    assert after_first.startswith(HOSTS)


def test_an_existing_ansible_blockinfile_block_is_taken_over_in_place(lab: Path) -> None:
    (lab / "hosts").write_text(f"{HOSTS}{BEGIN}\n100.64.0.99  svc-a.example.test\n{END}\n10.0.0.1  after.example.test\n")
    _zone(lab, svc_a="100.64.0.10")
    _run(lab, "refresh", pins="svc-a.example.test")
    text = _hosts(lab)
    assert text.count(BEGIN) == 1 and text.count(END) == 1
    assert "100.64.0.99" not in text
    assert text.endswith("10.0.0.1  after.example.test\n")
    assert _block(lab) == ["100.64.0.10  svc-a.example.test"]


def test_wait_adds_a_pin_as_soon_as_the_identity_loads(lab: Path) -> None:
    _zone(lab, svc_a="100.64.0.10")  # svc-b is not loaded yet
    threading.Timer(0.6, lambda: _zone(lab, svc_a="100.64.0.10", svc_b="100.64.0.11")).start()
    started = time.monotonic()
    result = _run(lab, "refresh", "--wait", "20")
    assert time.monotonic() - started < 15  # stops polling once everything resolves
    assert "2 of 2 pins resolved" in result.stdout
    assert _block(lab) == ["100.64.0.10  svc-a.example.test", "100.64.0.11  svc-b.example.test"]


def test_wait_gives_up_after_the_deadline_and_drops_the_pin(lab: Path) -> None:
    _zone(lab, svc_a="100.64.0.10")
    started = time.monotonic()
    result = _run(lab, "refresh", "--wait", "1")
    assert 1 <= time.monotonic() - started < 10
    assert "1 of 2 pins resolved, 1 dropped" in result.stdout
    assert _block(lab) == ["100.64.0.10  svc-a.example.test"]


def test_empty_pin_list_and_invalidate_remove_the_block(lab: Path) -> None:
    _zone(lab, svc_a="100.64.0.10", svc_b="100.64.0.11")
    _run(lab, "refresh")
    assert "managed block removed" in _run(lab, "refresh", pins="").stdout
    assert _hosts(lab) == HOSTS
    _run(lab, "refresh")
    assert "managed block removed" in _run(lab, "invalidate").stdout
    assert _hosts(lab) == HOSTS
    assert "unchanged" in _run(lab, "invalidate").stdout


def test_invalid_hostnames_are_refused_before_anything_is_written(lab: Path) -> None:
    _zone(lab, svc_a="100.64.0.10")
    for bad in ("bad host", "evil;rm", "-lead.example.test", "x\n127.0.0.1 y"):
        result = _run(lab, "refresh", pins=bad)
        assert result.returncode == 2 and "invalid pinned hostname" in result.stderr
    assert _hosts(lab) == HOSTS
    assert not (lab / "calls").exists()  # nothing was looked up


def test_the_helper_only_ever_edits_the_managed_block_and_signals_nothing() -> None:
    text = SCRIPT.read_text()
    assert "systemctl" not in text and "kill" not in text  # never restarts or stops the tunnel
    assert BEGIN in text and END in text


# ---------------------------------------------------------------------------
# `--tags ziti` runs the child tasks (real ansible-playbook)
# ---------------------------------------------------------------------------

STUB_CHILD = """---
- name: stub child tasks
  ansible.builtin.debug:
    msg: "CHILD-RAN-{marker}"
"""

PLAYBOOK = """---
- hosts: localhost
  connection: local
  gather_facts: false
  roles:
    - developer_workstation
"""


def _ziti_includes() -> list[dict]:
    return [t for t in _load(ROLE / "tasks/main.yml") if "ziti" in (t.get("tags") or [])]


def test_main_includes_both_ziti_task_files_and_applies_the_tags() -> None:
    includes = {t["ansible.builtin.include_tasks"]["file"]: t for t in _ziti_includes()}
    assert set(includes) == {"ziti.yml", "ziti-split-dns.yml"}
    for task in includes.values():
        assert task["ansible.builtin.include_tasks"]["apply"]["tags"] == task["tags"]
    assert "ziti" in includes["ziti.yml"]["tags"]
    assert {"ziti", "dns"} <= set(includes["ziti-split-dns.yml"]["tags"])


def _harness(tmp_path: Path, *, apply_tags: bool) -> Path:
    """A role whose main.yml is the real ziti include stanza and whose children are stubs."""
    tasks = tmp_path / "roles/developer_workstation/tasks"
    tasks.mkdir(parents=True)
    includes = _ziti_includes()
    if not apply_tags:
        for task in includes:
            del task["ansible.builtin.include_tasks"]["apply"]
    (tasks / "main.yml").write_text(yaml.safe_dump(includes))
    (tasks / "ziti.yml").write_text(STUB_CHILD.format(marker="ZITI"))
    (tasks / "ziti-split-dns.yml").write_text(STUB_CHILD.format(marker="SPLITDNS"))
    (tmp_path / "site.yml").write_text(PLAYBOOK)
    return tmp_path


def _playbook(tmp_path: Path, *tags: str) -> str:
    assert shutil.which("ansible-playbook"), "ansible-playbook must be installed to prove tag selection"
    extra = {"workstation_profile": "thin-client", "ziti_enabled": True, "ziti_intercept_domains": ["svc.example.test"]}
    env = {
        **os.environ,
        "ANSIBLE_ROLES_PATH": str(tmp_path / "roles"),
        "ANSIBLE_HOME": str(tmp_path / "home"),
        "ANSIBLE_LOCAL_TEMP": str(tmp_path / "tmp"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_FORCE_COLOR": "0",
        "ANSIBLE_CONFIG": str(tmp_path / "none.cfg"),
    }
    done = subprocess.run(
        ["ansible-playbook", "-i", "localhost,", "--tags", ",".join(tags), "-e", json.dumps(extra), str(tmp_path / "site.yml")],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=180,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


def test_tags_ziti_runs_the_child_tasks(tmp_path: Path) -> None:
    out = _playbook(_harness(tmp_path, apply_tags=True), "ziti")
    assert "CHILD-RAN-ZITI" in out
    assert "CHILD-RAN-SPLITDNS" in out


def test_tags_dns_runs_only_the_split_dns_child(tmp_path: Path) -> None:
    out = _playbook(_harness(tmp_path, apply_tags=True), "dns")
    assert "CHILD-RAN-SPLITDNS" in out
    assert "CHILD-RAN-ZITI" not in out


def test_an_unrelated_tag_runs_neither_child(tmp_path: Path) -> None:
    out = _playbook(_harness(tmp_path, apply_tags=True), "storage")
    assert "CHILD-RAN" not in out


def test_the_harness_detects_the_old_silent_no_op(tmp_path: Path) -> None:
    """Control: without `apply: tags` the include runs and its tasks silently do not."""
    out = _playbook(_harness(tmp_path, apply_tags=False), "ziti")
    assert "CHILD-RAN" not in out


def test_every_ziti_child_task_is_reachable_through_the_ziti_tag() -> None:
    # A task-level tag that does not include `ziti` would hide it from `--tags ziti`.
    for name in ("ziti.yml", "ziti-split-dns.yml"):
        for task in _load(ROLE / "tasks" / name):
            tags = task.get("tags")
            assert tags is None or "ziti" in tags or "always" in tags, f"{name}: {task['name']}"


