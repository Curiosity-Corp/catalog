"""Regression checks for the workstation inbound firewall (tasks/firewall.yml).

A workstation on a shared or untrusted LAN must not expose SSH, local
daemons, the MTA or mDNS announcements to that LAN; the overlay (ziti0) and
explicitly trusted interfaces stay reachable. The posture resolution is
exercised through ansible-playbook so precedence between host vars and the
hardware profile is tested as Ansible evaluates it.
"""

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
TASKS = ROLE / "tasks"


def _firewall_tasks() -> list[dict]:
    return yaml.safe_load((TASKS / "firewall.yml").read_text())


def _task(name: str) -> dict:
    for task in _firewall_tasks():
        if task.get("name") == name:
            return task
    raise AssertionError(f"firewall.yml has no task named {name!r}")


def test_default_deny_and_ziti_allow_are_unconditional() -> None:
    assert _task("Set UFW default deny incoming")["community.general.ufw"]["default"] == "deny"
    ziti = _task("Allow all OpenZiti traffic on ziti0")
    assert ziti["community.general.ufw"]["interface"] == "ziti0"
    assert "when" not in ziti
    assert "when" not in _task("Enable UFW")


def test_ssh_is_limited_when_trusted_interfaces_are_set() -> None:
    anywhere = _task("Allow SSH on all interfaces")
    assert anywhere["when"] == "_firewall_ssh_interfaces | length == 0"
    removal = _task("Remove any-interface SSH rule when SSH is limited to trusted interfaces")
    assert removal["community.general.ufw"]["delete"] is True
    assert removal["when"] == "_firewall_ssh_interfaces | length > 0"
    scoped = _task("Allow SSH on trusted interfaces only")["community.general.ufw"]
    assert scoped["interface"] == "{{ item }}"
    assert scoped["direction"] == "in"


def test_untrusted_lan_silences_mdns_and_mta() -> None:
    avahi = _task("Stop avahi publishing this host on an untrusted LAN")
    assert avahi["community.general.ini_file"]["option"] == "disable-publishing"
    assert avahi["notify"] == "Restart avahi-daemon"
    postfix = _task("Keep Postfix on loopback on an untrusted LAN")
    assert postfix["ansible.builtin.lineinfile"]["line"] == "inet_interfaces = loopback-only"
    assert postfix["notify"] == "Restart postfix"
    handlers = {h["name"] for h in yaml.safe_load((ROLE / "handlers/main.yml").read_text())}
    assert {"Restart avahi-daemon", "Restart postfix"} <= handlers


def test_deskmeet_profile_is_firewalled_off_the_shared_lan() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults["firewall_enabled"] is False
    assert defaults["firewall_ssh_interfaces"] == []
    profile = defaults["hardware_profiles"]["deskmeet-b760"]
    assert profile["firewall"] is True
    assert profile["untrusted_lan"] is True
    assert profile["firewall_mosh"] is True
    # Only the direct cable port is trusted; the Wi-Fi interface never is.
    assert profile["firewall_ssh_interfaces"] == ["enp0s31f6"]
    assert not any(i.startswith("wl") for i in profile["firewall_ssh_interfaces"])


def test_untrusted_lan_dns_over_tls_is_per_link_and_strict() -> None:
    dot = _task("Encrypt DNS on untrusted LAN links")
    copy = dot["ansible.builtin.copy"]
    # Per link (netplan-generated unit drop-in), never resolved.conf-global:
    # the ziti0 resolver speaks plain DNS.
    assert copy["dest"].startswith("/etc/systemd/network/10-netplan-{{ item }}.network.d/")
    assert "DNSOverTLS=yes" in copy["content"]
    assert "UseDNS=false" in copy["content"]
    assert dot["notify"] == "Reload systemd-networkd"
    text = (TASKS / "firewall.yml").read_text()
    assert "resolved.conf" not in "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert all("#" in server for server in defaults["dns_over_tls_servers"]), \
        "every DoT server needs an SNI name for certificate validation"
    profile = defaults["hardware_profiles"]["deskmeet-b760"]
    assert profile["dns_over_tls_interfaces"] == ["wlp8s0"]
    assert "ziti0" not in profile["dns_over_tls_interfaces"]


def test_firewall_gate_honours_hardware_profile_flag() -> None:
    main = (TASKS / "main.yml").read_text()
    gate = main.split("- name: Include firewall (UFW)", 1)[1].split("\n- name:", 1)[0]
    assert "hardware_profiles[hardware_profile].firewall | default(false) | bool" in gate
    assert "firewall_enabled | default(false) | bool" in gate


RESOLVE_CASES = [
    # (host vars, profile, expected ssh interfaces, mosh, untrusted, dot)
    ({}, {}, [], False, False, []),
    ({}, {"firewall_ssh_interfaces": ["enp0s31f6"], "firewall_mosh": True,
          "untrusted_lan": True, "dns_over_tls_interfaces": ["wlp8s0"]},
     ["enp0s31f6"], True, True, ["wlp8s0"]),
    ({"firewall_ssh_interfaces": ["eth9"]}, {"firewall_ssh_interfaces": ["enp0s31f6"]},
     ["eth9"], False, False, []),
    ({"firewall_ssh_interfaces": [], "firewall_mosh": True}, {}, [], True, False, []),
    # A host whose Wi-Fi enumerates under another name overrides the profile.
    ({"dns_over_tls_interfaces": ["wlp6s0"]}, {"dns_over_tls_interfaces": ["wlp8s0"]},
     [], False, False, ["wlp6s0"]),
    ({"dns_over_tls_interfaces": []}, {"dns_over_tls_interfaces": ["wlp8s0"]},
     [], False, False, ["wlp8s0"]),
]


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")
@pytest.mark.parametrize("host_vars,profile,ssh,mosh,untrusted,dot", RESOLVE_CASES)
def test_posture_resolution_precedence(tmp_path, host_vars, profile, ssh, mosh, untrusted, dot) -> None:
    resolve = _task("Resolve firewall posture")
    play = [{
        "hosts": "localhost",
        "gather_facts": False,
        "vars": {
            **host_vars,
            "hardware_profile": "probe",
            "hardware_profiles": {"probe": profile},
        },
        "tasks": [
            resolve,
            {"name": "Emit", "ansible.builtin.copy": {
                "dest": str(tmp_path / "out.json"),
                "content": "{{ {'ssh': _firewall_ssh_interfaces,"
                           " 'mosh': _firewall_mosh | bool,"
                           " 'untrusted': _firewall_untrusted_lan | bool,"
                           " 'dot': _firewall_dot_interfaces} | to_json }}",
            }},
        ],
    }]
    playbook = tmp_path / "resolve.yml"
    playbook.write_text(yaml.safe_dump(play))
    env = {"ANSIBLE_LOCALHOST_WARNING": "False",
           "ANSIBLE_INVENTORY_UNPARSED_WARNING": "False", "PATH": "/usr/bin:/bin"}
    env["PATH"] = str(Path(shutil.which("ansible-playbook")).parent) + ":" + env["PATH"]
    proc = subprocess.run(
        ["ansible-playbook", "-i", "localhost,", "-c", "local", str(playbook)],
        capture_output=True, text=True, env=env, cwd=tmp_path, check=False,
    )
    assert proc.returncode == 0, textwrap.shorten(proc.stdout + proc.stderr, 2000)
    msg = json.loads((tmp_path / "out.json").read_text())
    assert msg["ssh"] == ssh
    assert msg["mosh"] is mosh
    assert msg["untrusted"] is untrusted
    assert msg["dot"] == dot


def test_site_playbook_runs_notified_handlers_even_when_a_task_fails() -> None:
    """A lost networkd reload left the DoT drop-in written but never applied."""
    play = yaml.safe_load((ROOT / "playbooks/site.yml").read_text())[0]
    assert play["force_handlers"] is True
