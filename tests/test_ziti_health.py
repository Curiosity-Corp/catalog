"""The Ziti tunnel health check restarts a half-up tunnel, and only that one.

After a crash ziti-edge-tunnel can come back with an identity Active but not
Loaded, which kills ziti0 DNS until the unit is restarted by hand (#108). These
checks pin the role wiring and run the real script against stub `systemctl`,
`ziti-edge-tunnel` and `getent` commands, so they need no tunnel or root.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import time
from pathlib import Path

import jinja2
import pytest
import yaml

ROLE = Path(__file__).resolve().parents[1] / "roles/developer_workstation"
SCRIPT = ROLE / "files/curiosity-ziti-health"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())


class _Loader(yaml.SafeLoader):
    """Safe loader that reads Ansible's `!unsafe` scalars as plain strings."""


_Loader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))


def _render(name: str, **overrides: object) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(ROLE / "templates")),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
    )
    return env.get_template(name).render(**{**DEFAULTS, **overrides})


# ---------------------------------------------------------------------------
# Defaults and wiring
# ---------------------------------------------------------------------------


def test_defaults() -> None:
    assert DEFAULTS["ziti_health_check_enabled"] is True
    assert DEFAULTS["ziti_health_check_interval"] == "2min"
    # The public role names no private host; each host sets its own probe.
    assert DEFAULTS["ziti_health_check_probe_hosts"] == []
    assert DEFAULTS["ziti_health_check_identities"] == []
    assert DEFAULTS["ziti_health_check_grace"] == 60
    assert DEFAULTS["ziti_health_check_min_interval"] == 600


def test_ziti_tasks_install_the_check_and_retire_it_when_disabled() -> None:
    tasks = yaml.load((ROLE / "tasks/ziti-health.yml").read_text(), Loader=_Loader)
    by_name = {t["name"]: t for t in tasks}
    for name in (
        "Install Ziti health check script",
        "Install Ziti health check systemd service",
        "Install Ziti health check systemd timer",
        "Enable Ziti health check timer",
    ):
        assert by_name[name]["when"] == "ziti_health_check_enabled | bool"
    assert by_name["Enable Ziti health check timer"]["ansible.builtin.systemd"]["enabled"] is True
    retire = by_name["Remove Ziti health check files when it is not wanted"]
    assert retire["when"] == "not (ziti_health_check_enabled | bool)"
    assert len(retire["loop"]) == 3
    handlers = {h["name"] for h in yaml.safe_load((ROLE / "handlers/main.yml").read_text())}
    assert by_name["Install Ziti health check systemd timer"]["notify"] in handlers


# ---------------------------------------------------------------------------
# Who gets the guard: managed tunnel, hand-managed tunnel, or no Ziti at all
# ---------------------------------------------------------------------------


def _main_tasks() -> dict[str, dict]:
    tasks = yaml.load((ROLE / "tasks/main.yml").read_text(), Loader=_Loader)
    return {t["name"]: t for t in tasks}


def _include_runs(*, ziti_enabled: bool, unit_files: str | None, health: bool = True) -> bool:
    """Evaluate the `when` list of the health include the way Ansible would."""
    include = _main_tasks()["Include Ziti tunnel health check"]
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["bool"] = lambda v: v is True or str(v).lower() in ("1", "true", "yes", "on")
    context: dict[str, object] = {
        "workstation_profile": "desktop",
        "ziti_enabled": ziti_enabled,
        "ziti_health_check_enabled": health,
    }
    if unit_files is not None:  # None: the lookup was skipped and registered no stdout
        context["_ziti_unit_files"] = {"stdout": unit_files}
    else:
        context["_ziti_unit_files"] = {"skipped": True}
    return all(
        env.compile_expression(cond)(**context) is True for cond in include["when"]
    )


def test_health_tasks_are_not_in_ziti_yml() -> None:
    names = {t["name"] for t in yaml.load((ROLE / "tasks/ziti.yml").read_text(), Loader=_Loader)}
    assert not {n for n in names if "health check" in n}
    assert "ziti_health_check" not in (ROLE / "tasks/ziti.yml").read_text()


def test_catalog_managed_tunnel_gets_the_guard() -> None:
    assert _include_runs(ziti_enabled=True, unit_files=None)


def test_hand_managed_tunnel_with_the_unit_present_gets_the_guard() -> None:
    assert _include_runs(ziti_enabled=False, unit_files="ziti-edge-tunnel.service enabled enabled\n")


def test_host_without_a_tunnel_unit_is_left_alone() -> None:
    assert not _include_runs(ziti_enabled=False, unit_files="")
    assert not _include_runs(ziti_enabled=False, unit_files="\n")


def test_disabled_guard_is_not_installed_on_an_unmanaged_tunnel() -> None:
    assert not _include_runs(ziti_enabled=False, unit_files="ziti-edge-tunnel.service enabled enabled", health=False)


def test_managed_tunnel_still_retires_a_disabled_guard() -> None:
    # The retire tasks live in ziti-health.yml, so a managed host must still reach it.
    assert _include_runs(ziti_enabled=True, unit_files=None, health=False)


def test_unit_lookup_is_read_only_and_runs_in_check_mode() -> None:
    lookup = _main_tasks()["Look for the configured Ziti tunnel unit"]
    assert lookup["register"] == "_ziti_unit_files"
    assert lookup["changed_when"] is False and lookup["failed_when"] is False
    assert lookup["check_mode"] is False
    assert "{{ ziti_service_name }}" in lookup["ansible.builtin.command"]["cmd"]
    # a managed tunnel needs no lookup
    assert any("ziti_enabled" in c for c in lookup["when"])


def test_guard_restarts_only_the_default_tunnel_unit() -> None:
    assert DEFAULTS["ziti_service_name"] == "ziti-edge-tunnel.service"
    text = _render("curiosity-ziti-health.service.j2")
    assert "Environment=ZITI_HEALTH_UNIT=ziti-edge-tunnel.service" in text


def test_service_runs_as_root_oneshot_against_the_configured_unit() -> None:
    text = _render(
        "curiosity-ziti-health.service.j2",
        ziti_service_name="ziti-edge-tunnel.service",
        ziti_health_check_identities=["a", "b"],
        ziti_health_check_probe_hosts=["git.example.org", "x.example.org"],
    )
    assert "Type=oneshot" in text
    assert "User=" not in text
    assert "Environment=ZITI_HEALTH_UNIT=ziti-edge-tunnel.service" in text
    assert "Environment=ZITI_HEALTH_IDENTITIES=a,b" in text
    assert "Environment=ZITI_HEALTH_PROBE_HOSTS=git.example.org,x.example.org" in text
    assert "Environment=ZITI_HEALTH_MIN_INTERVAL=600" in text
    assert "ExecStart=/usr/local/sbin/curiosity-ziti-health" in text
    assert "StateDirectory=curiosity-ziti-health" in text
    assert "PrivateNetwork" not in text  # the probe needs the network


def test_timer_fires_every_two_minutes() -> None:
    text = _render("curiosity-ziti-health.timer.j2")
    assert "OnUnitActiveSec=2min" in text
    assert "WantedBy=timers.target" in text


def test_script_touches_only_the_system_tunnel() -> None:
    text = SCRIPT.read_text()
    assert stat.S_IMODE(SCRIPT.stat().st_mode) & 0o111
    assert '"tunnel_status"' in text
    assert 'run([binary, "tunnel_status"], timeout)' in text  # no -p IPC prefix
    assert "pkill" not in text and "kill" not in text.replace("killed", "")
    assert '["systemctl", "restart", unit]' in text


# ---------------------------------------------------------------------------
# Behaviour against stub commands
# ---------------------------------------------------------------------------

SYSTEMCTL = """#!/bin/bash
echo "systemctl $*" >> "$STUB_DIR/calls"
case "$1" in
  show)
    echo "ActiveState=${STUB_ACTIVE:-active}"
    echo "ActiveEnterTimestampMonotonic=${STUB_ENTER_US:-1000000}"
    ;;
  restart) exit "${STUB_RESTART_RC:-0}" ;;
esac
"""
TUNNEL = """#!/bin/bash
echo "tunnel $*" >> "$STUB_DIR/calls"
[ -n "$STUB_TUNNEL_SLEEP" ] && sleep "$STUB_TUNNEL_SLEEP"
cat "$STUB_DIR/status.json"
exit "${STUB_TUNNEL_RC:-0}"
"""
GETENT = """#!/bin/bash
echo "getent $*" >> "$STUB_DIR/calls"
case " $STUB_UNRESOLVABLE " in *" $2 "*) exit 2 ;; esac
echo "100.64.0.9 STREAM $2"
"""


def _status(*identities: tuple[str, bool, bool]) -> str:
    return json.dumps(
        {
            "Success": True,
            "Data": {
                "Active": False,
                "Identities": [
                    {"Name": n, "Active": a, "Loaded": loaded} for n, a, loaded in identities
                ],
            },
        }
    )


@pytest.fixture
def stubs(tmp_path: Path) -> Path:
    for name, body in (("systemctl", SYSTEMCTL), ("ziti-edge-tunnel", TUNNEL), ("getent", GETENT)):
        path = tmp_path / name
        path.write_text(body)
        path.chmod(0o755)
    (tmp_path / "status.json").write_text(_status(("ws", True, True)))
    return tmp_path


def _run(stubs: Path, **env: str) -> subprocess.CompletedProcess[str]:
    full = {
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "STUB_DIR": str(stubs),
        "ZITI_HEALTH_TUNNEL_BIN": str(stubs / "ziti-edge-tunnel"),
        "ZITI_HEALTH_STATE_FILE": str(stubs / "state.json"),
        "ZITI_HEALTH_PROBE_HOSTS": "git.example.org",
        "ZITI_HEALTH_TIMEOUT": "2",
        "STUB_ENTER_US": "1000000",  # the unit has been up for the whole uptime
        **env,
    }
    return subprocess.run(
        ["python3", "-I", str(SCRIPT)], env=full, capture_output=True, text=True, check=False
    )


def _calls(stubs: Path) -> list[str]:
    path = stubs / "calls"
    return path.read_text().splitlines() if path.exists() else []


def _restarts(stubs: Path) -> list[str]:
    return [c for c in _calls(stubs) if c.startswith("systemctl restart")]


def _set_status(stubs: Path, *identities: tuple[str, bool, bool]) -> None:
    (stubs / "status.json").write_text(_status(*identities))


def _set_state(stubs: Path, **state: float | None) -> None:
    (stubs / "state.json").write_text(json.dumps(state))


def test_healthy_tunnel_is_left_alone(stubs: Path) -> None:
    result = _run(stubs)
    assert result.returncode == 0, result.stderr
    assert "healthy" in result.stdout
    assert _restarts(stubs) == []


def test_active_but_not_loaded_is_noted_then_restarted_after_the_grace(stubs: Path) -> None:
    _set_status(stubs, ("ws", True, False))
    first = _run(stubs)
    assert first.returncode == 0
    assert "Active but not Loaded" in first.stdout
    assert _restarts(stubs) == []
    state = json.loads((stubs / "state.json").read_text())
    _set_state(stubs, bad_since=state["bad_since"] - 90)
    second = _run(stubs)
    assert second.returncode == 0, second.stderr
    assert _restarts(stubs) == ["systemctl restart ziti-edge-tunnel.service"]
    assert "restarting ziti-edge-tunnel.service" in second.stdout


def test_backoff_prevents_a_restart_loop(stubs: Path) -> None:
    _set_status(stubs, ("ws", True, False))
    now = time.time()
    _set_state(stubs, bad_since=now - 500, last_restart=now - 120)
    result = _run(stubs)
    assert result.returncode == 0
    assert _restarts(stubs) == []
    assert "backing off" in result.stdout
    _set_state(stubs, bad_since=now - 500, last_restart=now - 700)
    _run(stubs)
    assert len(_restarts(stubs)) == 1


def test_a_future_timestamp_in_the_state_file_does_not_block_forever(stubs: Path) -> None:
    _set_status(stubs, ("ws", True, False))
    now = time.time()
    _set_state(stubs, bad_since=now - 500, last_restart=now + 100000)
    _run(stubs)
    assert len(_restarts(stubs)) == 1


def test_probe_failure_with_a_loaded_identity_restarts(stubs: Path) -> None:
    _set_state(stubs, bad_since=time.time() - 300)
    result = _run(stubs, STUB_UNRESOLVABLE="git.example.org")
    assert "git.example.org does not resolve" in result.stdout
    assert len(_restarts(stubs)) == 1


def test_probe_is_per_host_configurable_and_can_be_disabled(stubs: Path) -> None:
    _run(stubs, ZITI_HEALTH_PROBE_HOSTS="other.example.org")
    assert "getent ahosts other.example.org" in _calls(stubs)
    assert "getent ahosts git.example.org" not in _calls(stubs)
    (stubs / "calls").unlink()
    _run(stubs, ZITI_HEALTH_PROBE_HOSTS="", STUB_UNRESOLVABLE="git.example.org")
    assert not any(c.startswith("getent") for c in _calls(stubs))


def test_dns_failure_without_any_loaded_identity_does_not_restart(stubs: Path) -> None:
    _set_status(stubs, ("ws", False, False))
    _set_state(stubs, bad_since=time.time() - 300)
    result = _run(stubs, STUB_UNRESOLVABLE="git.example.org")
    assert result.returncode == 0
    assert "skipping the DNS probe" in result.stdout
    assert _restarts(stubs) == []


def test_recovery_clears_the_pending_fault(stubs: Path) -> None:
    _set_state(stubs, bad_since=time.time() - 30)
    result = _run(stubs)
    assert "recovered" in result.stdout
    assert json.loads((stubs / "state.json").read_text())["bad_since"] is None


def test_only_watched_identities_count(stubs: Path) -> None:
    _set_status(stubs, ("ws", True, True), ("spare", True, False))
    _set_state(stubs, bad_since=time.time() - 300)
    _run(stubs, ZITI_HEALTH_IDENTITIES="ws")
    assert _restarts(stubs) == []
    _set_state(stubs, bad_since=time.time() - 300)
    _run(stubs, ZITI_HEALTH_IDENTITIES="spare")
    assert len(_restarts(stubs)) == 1


def test_a_missing_watched_identity_is_reported(stubs: Path) -> None:
    result = _run(stubs, ZITI_HEALTH_IDENTITIES="ghost")
    assert "'ghost' is not reported" in result.stdout


def test_only_the_default_socket_and_the_one_unit_are_used(stubs: Path) -> None:
    _set_status(stubs, ("ws", True, False))
    _set_state(stubs, bad_since=time.time() - 300)
    _run(stubs)
    calls = _calls(stubs)
    assert "tunnel tunnel_status" in calls  # no -p prefix: the system tunnel's socket
    assert [c for c in calls if c.startswith("tunnel")] == ["tunnel tunnel_status"]
    assert set(c for c in calls if c.startswith("systemctl")) == {
        "systemctl show ziti-edge-tunnel.service -p ActiveState -p ActiveEnterTimestampMonotonic",
        "systemctl restart ziti-edge-tunnel.service",
    }


def test_inactive_unit_is_left_to_systemd(stubs: Path) -> None:
    result = _run(stubs, STUB_ACTIVE="inactive")
    assert result.returncode == 0
    assert "not active" in result.stdout
    assert not any(c.startswith("tunnel") for c in _calls(stubs))


def test_a_freshly_started_unit_gets_its_grace_period(stubs: Path) -> None:
    uptime_us = int(float(Path("/proc/uptime").read_text().split()[0]) * 1_000_000)
    _set_status(stubs, ("ws", True, False))
    result = _run(stubs, STUB_ENTER_US=str(uptime_us - 5_000_000))
    assert "grace period" in result.stdout
    assert not any(c.startswith("tunnel") for c in _calls(stubs))


def test_invalid_json_is_a_clear_failure_without_a_restart(stubs: Path) -> None:
    (stubs / "status.json").write_text("not json {")
    result = _run(stubs)
    assert result.returncode == 1
    assert "invalid JSON" in result.stdout
    assert _restarts(stubs) == []


@pytest.mark.parametrize(
    "body, message",
    [
        ("[]", "not an object"),
        ('{"Success": true, "Data": {}}', "no Data.Identities"),
        ('{"Success": false, "Error": "boom"}', "reported failure: boom"),
    ],
)
def test_unexpected_status_shapes_are_clear_failures(stubs: Path, body: str, message: str) -> None:
    (stubs / "status.json").write_text(body)
    result = _run(stubs)
    assert result.returncode == 1
    assert message in result.stdout


def test_a_failing_or_hung_tunnel_status_fails_the_check_not_the_tunnel(stubs: Path) -> None:
    result = _run(stubs, STUB_TUNNEL_RC="3")
    assert result.returncode == 1
    assert "tunnel_status exited 3" in result.stdout
    hung = _run(stubs, STUB_TUNNEL_SLEEP="5", ZITI_HEALTH_TIMEOUT="1")
    assert hung.returncode == 1
    assert "timed out after 1s" in hung.stdout
    assert _restarts(stubs) == []


def test_bad_numeric_settings_are_reported(stubs: Path) -> None:
    result = _run(stubs, ZITI_HEALTH_GRACE="soon")
    assert result.returncode == 1
    assert "ZITI_HEALTH_GRACE='soon' is not an integer" in result.stdout


def test_a_failed_restart_is_an_error_and_still_backs_off(stubs: Path) -> None:
    _set_status(stubs, ("ws", True, False))
    _set_state(stubs, bad_since=time.time() - 300)
    result = _run(stubs, STUB_RESTART_RC="1")
    assert result.returncode == 1
    assert "restart ziti-edge-tunnel.service failed" in result.stdout
    assert json.loads((stubs / "state.json").read_text())["last_restart"] is not None


def test_a_corrupt_state_file_is_ignored(stubs: Path) -> None:
    (stubs / "state.json").write_text("{garbage")
    result = _run(stubs)
    assert result.returncode == 0
    assert "ignoring unreadable state file" in result.stdout


# ---------------------------------------------------------------------------
# Finding the tunnel binary (#149): systemd's PATH lacks /opt/openziti/bin
# ---------------------------------------------------------------------------


def _load_script():
    loader = importlib.machinery.SourceFileLoader("curiosity_ziti_health", str(SCRIPT))
    spec = importlib.util.spec_from_loader("curiosity_ziti_health", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_default_tunnel_bin_is_the_package_path() -> None:
    assert DEFAULTS["ziti_health_check_tunnel_bin"] == "/opt/openziti/bin/ziti-edge-tunnel"
    assert _load_script().DEFAULT_TUNNEL_BIN == DEFAULTS["ziti_health_check_tunnel_bin"]


def test_service_passes_the_tunnel_binary_to_the_check() -> None:
    text = _render("curiosity-ziti-health.service.j2")
    assert "Environment=ZITI_HEALTH_TUNNEL_BIN=/opt/openziti/bin/ziti-edge-tunnel" in text
    custom = _render("curiosity-ziti-health.service.j2", ziti_health_check_tunnel_bin="/srv/zet")
    assert "Environment=ZITI_HEALTH_TUNNEL_BIN=/srv/zet" in custom


def test_binary_is_found_on_path_when_the_variable_is_unset(stubs: Path) -> None:
    env = {k: v for k, v in os.environ.items()}
    result = subprocess.run(
        ["python3", "-I", str(SCRIPT)],
        env={
            "PATH": f"{stubs}:{env['PATH']}",
            "STUB_DIR": str(stubs),
            "ZITI_HEALTH_STATE_FILE": str(stubs / "state.json"),
            "ZITI_HEALTH_TIMEOUT": "2",
            "STUB_ENTER_US": "1000000",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout
    assert "healthy" in result.stdout
    assert "tunnel tunnel_status" in _calls(stubs)


def test_binary_falls_back_to_the_package_path_when_not_on_path(
    stubs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_script()
    package_bin = stubs / "opt-ziti-edge-tunnel"
    package_bin.write_text("#!/bin/bash\n")
    package_bin.chmod(0o755)
    monkeypatch.delenv("ZITI_HEALTH_TUNNEL_BIN", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr(module, "DEFAULT_TUNNEL_BIN", str(package_bin))
    assert module.resolve_tunnel_bin() == str(package_bin)


def test_the_variable_wins_over_path_and_the_package_path(
    stubs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_script()
    chosen = stubs / "chosen"
    chosen.write_text("#!/bin/bash\n")
    chosen.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stubs}:{os.environ['PATH']}")  # has a ziti-edge-tunnel stub
    monkeypatch.setenv("ZITI_HEALTH_TUNNEL_BIN", str(chosen))
    assert module.resolve_tunnel_bin() == str(chosen)


def test_a_missing_binary_is_a_clear_failed_check_not_a_crash(
    stubs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # In-process so the package-path fallback can be pointed at a missing file
    # whether or not the test host has OpenZiti installed.
    module = _load_script()
    (stubs / "ziti-edge-tunnel").unlink()
    monkeypatch.delenv("ZITI_HEALTH_TUNNEL_BIN", raising=False)
    monkeypatch.setenv("PATH", str(stubs))
    monkeypatch.setenv("STUB_DIR", str(stubs))
    monkeypatch.setenv("STUB_ENTER_US", "1000000")
    monkeypatch.setenv("ZITI_HEALTH_STATE_FILE", str(stubs / "state.json"))
    monkeypatch.setattr(module, "DEFAULT_TUNNEL_BIN", str(stubs / "absent/ziti-edge-tunnel"))
    with pytest.raises(module.CheckError) as raised:  # `__main__` logs it and exits 1
        module.main()
    assert f"ziti-edge-tunnel not found on PATH or at {stubs}/absent/ziti-edge-tunnel" in str(raised.value)
    assert "Errno" not in str(raised.value)
    assert _restarts(stubs) == []


def test_a_configured_binary_that_does_not_exist_is_named_in_the_error(stubs: Path) -> None:
    result = _run(stubs, ZITI_HEALTH_TUNNEL_BIN=str(stubs / "nope"))
    assert result.returncode == 1
    assert "ZITI_HEALTH_TUNNEL_BIN=" in result.stdout and "nope" in result.stdout
    assert _restarts(stubs) == []
