"""authd brokers refuse to start when broker.conf is readable by others."""

from pathlib import Path

import yaml

TASKS = Path(__file__).resolve().parents[1] / "roles/developer_workstation/tasks"


def _broker_conf_task(name: str) -> dict:
    tasks = yaml.safe_load((TASKS / name).read_text())
    for task in tasks:
        copy = task.get("ansible.builtin.copy") or {}
        if str(copy.get("dest", "")).endswith("/broker.conf"):
            return copy
    raise AssertionError(f"no broker.conf copy task in {name}")


def test_authd_oidc_broker_conf_is_private() -> None:
    assert _broker_conf_task("keycloak-sso.yml")["mode"] == "0600"


def test_authd_msentraid_broker_conf_is_private() -> None:
    assert _broker_conf_task("entra-id.yml")["mode"] == "0600"
