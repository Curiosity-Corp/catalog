"""Public configuration validation (tasks/validate-config.yml).

The task file is run for real with ansible-playbook against localhost, using
the role's own defaults as the baseline, so these tests catch both a broken
validator and a default that the validator would reject.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
TASKS = str(ROLE / "tasks/validate-config.yml")
DEFAULTS = str(ROLE / "defaults/main.yml")

# Sentinel values that must never be echoed back in validation output.
SECRET_URL = "https://user:hunter2-sentinel@hooks.example.org/T000/B000/xyz"
SECRET_BARE = "sentinel-secret-token-1234"

pytestmark = pytest.mark.skipif(
    shutil.which("ansible-playbook") is None
    and not (Path(sys.executable).parent / "ansible-playbook").exists(),
    reason="ansible-playbook is not installed",
)


def ansible_playbook() -> str:
    local = Path(sys.executable).parent / "ansible-playbook"
    return str(local) if local.exists() else shutil.which("ansible-playbook")


def build_play(cases: dict, outdir: Path) -> list:
    """One play per case, all in one ansible-playbook run (startup is slow).

    Each play includes the real validate-config.yml with that case's variables
    and records whether it failed and with what message.
    """
    plays = []
    for name, overrides in cases.items():
        record = str(outdir / f"{name}.json")
        # vars_files outranks play vars, so overrides go in a later vars file.
        override_file = outdir.parent / f"{name}.vars.json"
        override_file.write_text(json.dumps(overrides))
        plays.append(
            {
                "name": name,
                "hosts": "localhost",
                "connection": "local",
                "gather_facts": False,
                # CI images have no /usr/bin/python3 for ansible's discovery.
                "vars": {"ansible_python_interpreter": sys.executable},
                "vars_files": [DEFAULTS, str(override_file)],
                "tasks": [
                    {
                        "block": [
                            {"ansible.builtin.include_tasks": TASKS},
                            {
                                "ansible.builtin.copy": {
                                    "dest": record,
                                    "content": "{{ {'failed': false, 'msg': ''} | to_json }}",
                                }
                            },
                        ],
                        "rescue": [
                            {
                                "ansible.builtin.copy": {
                                    "dest": record,
                                    "content": "{{ {'failed': true, 'msg': ansible_failed_result.msg} | to_json }}",
                                }
                            }
                        ],
                    }
                ],
            }
        )
    return plays


VALID = {
    "defaults": {},
    "fully_opted_in": {
        "workstation_profile": "workspace",
        "hardware_profile": "generic",
        "workstation_update_ring": "general",
        "catalog_integrity_mode": "enforce",
        "catalog_signing_key_fingerprint": "A" * 40,
        "coder_cli_enabled": True,
        "coder_deployment_url": "https://coder.example.org",
        "workspace_gitlab_host": "git.example.org",
        "failed_sweep_webhook_url": "https://hooks.example.org/path",
        "custom_ca_certificates": [{"name": "lab", "src": "/etc/ansible/private/lab.crt"}],
    },
    "plain_ssh_workspace": {"workstation_profile": "workspace", "workspace_gitlab_host": ""},
}

INVALID = {
    "bad_profile": ({"workstation_profile": "server"}, "workstation_profile"),
    "unknown_hardware_profile": ({"hardware_profile": "no-such-box"}, "hardware_profile"),
    "bad_ring": ({"workstation_update_ring": "nightly"}, "workstation_update_ring must"),
    "negative_ring_policy": (
        {"workstation_update_ring_min_age_seconds": {"canary": -1, "early": 1, "general": 2}},
        "workstation_update_ring_min_age_seconds",
    ),
    "non_numeric_quarantine": ({"workstation_update_quarantine_seconds": "soon"}, "quarantine"),
    "zero_snapshots": ({"workstation_update_snapshot_keep": 0}, "workstation_update_snapshot_keep"),
    "bad_integrity_mode": ({"catalog_integrity_mode": "strict"}, "catalog_integrity_mode must"),
    "short_fingerprint": ({"catalog_signing_key_fingerprint": "abc"}, "40 hexadecimal"),
    "enforce_without_key": ({"catalog_integrity_mode": "enforce"}, "enforce also needs"),
    "relative_home": ({"dev_user_home": "home/x"}, "dev_user_home"),
    "bad_idle_lock": ({"minimal_desktop_idle_lock_minutes": "never"}, "idle lock"),
    "ca_without_src": ({"custom_ca_certificates": [{"name": "x"}]}, "custom_ca_certificates must"),
    "relative_ca_src": (
        {"custom_ca_certificates": [{"name": "x", "src": "relative.crt"}]},
        "absolute path on the Ansible controller",
    ),
    "http_coder_url": ({"coder_deployment_url": "http://coder.example.org"}, "coder_deployment_url"),
    "coder_cli_without_url": ({"coder_cli_enabled": True}, "requires coder_deployment_url"),
    "gitlab_host_with_scheme": ({"workspace_gitlab_host": "https://git.example.org"}, "workspace_gitlab_host"),
    "auth_binary_with_shell_chars": ({"workspace_external_auth_binary": "coder; rm -rf /"}, "workspace_external_auth_binary"),
    "kiosk_without_url": ({"workstation_profile": "byod-kiosk", "kiosk_target_url": ""}, "requires kiosk_target_url"),
    "ftp_missionctl": ({"missionctl_install_url": "ftp://x.example.org/a"}, "missionctl_install_url"),
    "string_boolean": ({"coder_cli_enabled": "maybe", "coder_deployment_url": "https://c.example.org"}, "enable switches"),
    "everything_wrong": (
        {"workstation_profile": "server", "catalog_integrity_mode": "strict", "workstation_update_ring": "nightly"},
        "workstation_profile",
    ),
    "secret_webhook": ({"failed_sweep_webhook_url": SECRET_URL}, "failed_sweep_webhook_url"),
    "secret_coder": ({"coder_deployment_url": SECRET_URL}, "coder_deployment_url"),
    "secret_kiosk": ({"kiosk_target_url": SECRET_URL}, "kiosk_target_url"),
    "secret_missionctl": ({"missionctl_install_url": SECRET_URL}, "missionctl_install_url"),
    "secret_bare": (
        {"catalog_signing_key_fingerprint": SECRET_BARE, "workspace_gitlab_host": SECRET_BARE + "/x"},
        "catalog_signing_key_fingerprint",
    ),
}


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("validate")
    outdir = tmp / "out"
    outdir.mkdir()
    cases = dict(VALID)
    cases.update({name: overrides for name, (overrides, _) in INVALID.items()})
    play = tmp / "play.yml"
    play.write_text(yaml.safe_dump(build_play(cases, outdir)))
    env = {
        "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
        "HOME": str(tmp),
        "ANSIBLE_LOCAL_TEMP": str(tmp / "tmp"),
        "ANSIBLE_HOME": str(tmp / "home"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_LOCALHOST_WARNING": "False",
        "ANSIBLE_INVENTORY_UNPARSED_WARNING": "False",
    }
    proc = subprocess.run(
        [ansible_playbook(), "-i", "localhost,", str(play)],
        capture_output=True, text=True, env=env, timeout=900, check=False,
    )
    data = {path.stem: json.loads(path.read_text()) for path in outdir.glob("*.json")}
    return {"proc": proc, "data": data, "everything": proc.stdout + proc.stderr + "".join(p.read_text() for p in outdir.glob("*.json"))}


def test_the_whole_run_completed(results) -> None:
    assert len(results["data"]) == len(VALID) + len(INVALID), results["proc"].stdout[-3000:]


@pytest.mark.parametrize("case", sorted(VALID))
def test_valid_configurations_pass(results, case: str) -> None:
    assert results["data"][case] == {"failed": False, "msg": ""}


@pytest.mark.parametrize("case", sorted(INVALID))
def test_invalid_configuration_is_rejected_with_the_variable_named(results, case: str) -> None:
    expected = INVALID[case][1]
    record = results["data"][case]
    assert record["failed"] is True
    assert "nothing on this host was changed" in record["msg"]
    assert expected in record["msg"], record["msg"]


def test_all_problems_are_reported_in_one_message(results) -> None:
    msg = results["data"]["everything_wrong"]["msg"]
    for name in ("workstation_profile", "catalog_integrity_mode", "workstation_update_ring"):
        assert name in msg


def test_secret_values_are_never_echoed_anywhere(results) -> None:
    text = results["everything"]
    for sentinel in ("hunter2-sentinel", "hooks.example.org", SECRET_BARE):
        assert sentinel not in text


def test_main_runs_validation_before_any_package_or_service_task() -> None:
    tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
    names = [t.get("name", "") for t in tasks]
    index = next(i for i, t in enumerate(tasks) if "validate-config.yml" in json.dumps(t))
    first_change = next(
        i
        for i, t in enumerate(tasks)
        if any(k in t for k in ("ansible.builtin.apt", "ansible.builtin.systemd", "ansible.builtin.package"))
    )
    assert index < first_change, names[: index + 1]
    assert "always" in tasks[index]["tags"]
