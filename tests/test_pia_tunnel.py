"""PIA tunnel for chosen apps: fail-closed network namespace (catalog#145).

Two root-owned scripts do the work, so the tests run the real files:

* `curiosity-pia-tunnel` is driven end to end with fake `curl`, `ip`, `wg`,
  `nft`, `sysctl` and `ping` executables on PATH. The fakes log every call so
  the tests can assert what ran, in what order, and that no secret (login,
  token, private key) ever reached an argument list or the output.
* `curiosity-pia-run` is imported and its pure pieces (environment allow-list,
  command construction, stray-process guard, desktop entry wrapping and the
  sync/prune logic) are tested against temporary directory trees.

The templates and tasks are checked as text and rendered with jinja2.
"""

import base64
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import jinja2
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
FILES = ROLE / "files"
TUNNEL = FILES / "curiosity-pia-tunnel"
RUN = FILES / "curiosity-pia-run"
TASKS = ROLE / "tasks/pia-tunnel.yml"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
PROFILES = DEFAULTS["hardware_profiles"]
DEFINITIONS = DEFAULTS["pia_tunnel_app_definitions"]

USERNAME = "p0000001"
PASSWORD = "s3cret-Pa55word-do-not-leak"
TOKEN = "T0kenValue_" + "x" * 40
WG_KEY = base64.b64encode(b"k" * 32).decode()


def _load(path: Path, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module  # dataclasses resolve annotations through sys.modules
    loader.exec_module(module)
    return module


tunnel = _load(TUNNEL, "curiosity_pia_tunnel")
launcher = _load(RUN, "curiosity_pia_run")


# --------------------------------------------------------------------------
# Profiles, defaults and wiring
# --------------------------------------------------------------------------


def test_only_deskmeet_routes_brave_through_pia() -> None:
    routed = {name: p["pia_tunnel_apps"] for name, p in PROFILES.items() if p.get("pia_tunnel_apps")}
    assert routed == {"deskmeet-b760": ["brave-browser"]}


def test_every_profile_app_has_a_definition() -> None:
    for name, profile in PROFILES.items():
        for app in profile.get("pia_tunnel_apps", []):
            assert app in DEFINITIONS, f"{name} routes {app} but defines no such app"


def test_no_profile_uses_both_pia_mechanisms() -> None:
    for name, profile in PROFILES.items():
        assert not (profile.get("pia_vpn") and profile.get("pia_tunnel_apps")), name


def test_the_variable_is_derived_from_the_profile() -> None:
    env = jinja2.Environment()
    template = env.from_string(DEFAULTS["pia_tunnel_apps"])
    rendered = template.render(hardware_profiles=PROFILES, hardware_profile="deskmeet-b760")
    assert yaml.safe_load(rendered) == ["brave-browser"]
    assert yaml.safe_load(template.render(hardware_profiles=PROFILES, hardware_profile="generic")) == []
    assert yaml.safe_load(template.render(hardware_profiles=PROFILES, hardware_profile="unknown")) == []


def test_brave_definition_is_complete_and_never_the_plain_wrapper() -> None:
    brave = DEFINITIONS["brave-browser"]
    assert brave["command"] == ["/usr/bin/brave-browser-stable"]
    assert brave["guard_exe"] == "/opt/brave.com/brave/brave"
    assert set(brave["desktop_ids"]) == {"brave-browser.desktop", "com.brave.Browser.desktop"}
    # Every Exec= the package ships starts with this command, so every entry is wrapped.
    assert brave["desktop_commands"] == brave["command"]
    assert {"brave-browser", "brave-browser-stable"} <= set(brave["shims"])
    policies = {p["path"]: p["content"] for p in brave["managed_policies"]}
    assert policies["/etc/brave/policies/managed/30-pia-tunnel-webrtc.json"] == {
        "WebRtcIPHandling": "default_public_interface_only"
    }


def test_the_default_login_file_reuses_the_existing_provisioning() -> None:
    env = jinja2.Environment()
    template = env.from_string(DEFAULTS["pia_tunnel_login_file"])
    assert template.render(pia_login_file="") == "/etc/curiosity/pia-tunnel/login"
    assert template.render(pia_login_file="/root/pia.login") == "/root/pia.login"


def test_main_includes_the_task_file_for_every_desktop_profile() -> None:
    main = (ROLE / "tasks/main.yml").read_text()
    block = main[main.index("Include the PIA tunnel for routed apps"):]
    block = block[: block.index("- name:", 10)]
    assert "pia-tunnel.yml" in block
    assert "workstation_profile in ['desktop', 'thin-client', 'byod-kiosk']" in block
    assert "container" in block  # never in a container


def test_tasks_validate_gate_and_clean_up() -> None:
    text = TASKS.read_text()
    assert "pia_tunnel_apps | length > 0" in text
    assert "pia_tunnel_apps | length == 0" in text
    assert 'validate: "visudo -cf %s"' in text
    assert "Refuse the PIA desktop app split tunnel and the PIA namespace together" in text
    # The login is only checked for existence and mode, never read or templated.
    assert "slurp" not in text and "no_log" not in text
    assert "down --destroy" in text and "--remove-all" in text


def test_tasks_parse_as_yaml() -> None:
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))
    assert yaml.load(TASKS.read_text(), Loader=Loader)


def test_the_handler_exists() -> None:
    handlers = yaml.safe_load((ROLE / "handlers/main.yml").read_text())
    names = {h["name"] for h in handlers}
    assert {"Restart the PIA tunnel", "Refresh the local desktop entry cache"} <= names


def test_scripts_are_executable_python_with_a_fixed_interpreter() -> None:
    for script in (TUNNEL, RUN):
        assert script.read_text().startswith("#!/usr/bin/python3\n")
        assert script.stat().st_mode & stat.S_IXUSR
        compile(script.read_text(), str(script), "exec")


def test_the_vendored_ca_is_pias() -> None:
    pem = (FILES / "pia-ca.rsa.4096.crt").read_text()
    assert pem.startswith("-----BEGIN CERTIFICATE-----")
    if shutil.which("openssl"):
        out = subprocess.run(
            ["openssl", "x509", "-noout", "-subject", "-fingerprint", "-sha256"],
            input=pem, capture_output=True, text=True, check=True,
        ).stdout
        assert "O=Private Internet Access" in out
        assert "1F:D2:56:58:45:6E:AB:30:41:FB:A7:7C:CD:39:8A:B8:12:4E:DC:C1:B8:B2:FC:1D:55:FD:F6:B1:BB:FC:9D:70" in out


# --------------------------------------------------------------------------
# Templates
# --------------------------------------------------------------------------


def _env() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(ROLE / "templates")),
        trim_blocks=True,
        lstrip_blocks=False,
        keep_trailing_newline=True,
        undefined=jinja2.StrictUndefined,
    )
    env.filters["to_nice_json"] = lambda value: json.dumps(value, indent=4, sort_keys=True)
    return env


def _vars(**overrides):
    values = {
        "pia_tunnel_apps": ["brave-browser"],
        "pia_tunnel_app_definitions": DEFINITIONS,
        "pia_tunnel_netns": "pia",
        "pia_tunnel_interface": "wgpia",
        "pia_tunnel_region": "auto",
        "pia_tunnel_login_file": "/etc/curiosity/pia-tunnel/login",
        "pia_tunnel_mtu": 1420,
        "pia_tunnel_keepalive": 25,
        "pia_tunnel_max_handshake_age": 240,
        "pia_tunnel_session_max_hours": 0,
        "pia_tunnel_watchdog_interval": "1min",
        "pia_tunnel_users": ["mrh", "second"],
    }
    values.update(overrides)
    return values


def test_config_template_carries_only_the_listed_apps() -> None:
    rendered = json.loads(_env().get_template("pia-tunnel-config.json.j2").render(**_vars()))
    assert list(rendered["apps"]) == ["brave-browser"]
    assert rendered["apps"]["brave-browser"]["command"] == ["/usr/bin/brave-browser-stable"]
    assert rendered["netns"] == "pia" and rendered["interface"] == "wgpia"
    assert rendered["mtu"] == 1420 and rendered["max_handshake_age"] == 240
    assert rendered["run_path"] == "/usr/local/bin/curiosity-pia-run"
    assert "password" not in json.dumps(rendered).lower()


def test_adding_an_app_is_a_list_entry_plus_a_definition() -> None:
    definitions = dict(DEFINITIONS)
    definitions["thunderbird"] = {
        "command": ["/usr/bin/thunderbird"],
        "desktop_ids": ["thunderbird.desktop"],
        "desktop_commands": ["thunderbird"],
    }
    rendered = json.loads(
        _env().get_template("pia-tunnel-config.json.j2").render(
            **_vars(pia_tunnel_apps=["brave-browser", "thunderbird"], pia_tunnel_app_definitions=definitions)
        )
    )
    assert sorted(rendered["apps"]) == ["brave-browser", "thunderbird"]
    assert rendered["apps"]["thunderbird"]["scope"] == {}
    assert rendered["apps"]["thunderbird"]["shims"] == []


def test_units_use_the_expected_commands() -> None:
    env = _env()
    service = env.get_template("curiosity-pia-tunnel.service.j2").render(**_vars())
    assert "ExecStart=/usr/local/sbin/curiosity-pia-tunnel ensure --retries 6" in service
    assert "ExecStop=/usr/local/sbin/curiosity-pia-tunnel down\n" in service  # never --destroy
    assert "Type=oneshot" in service and "RemainAfterExit=yes" in service
    assert "network-online.target" in service
    watchdog = env.get_template("curiosity-pia-tunnel-watchdog.service.j2").render(**_vars())
    assert "ExecCondition=/usr/local/sbin/curiosity-pia-tunnel needs-renewal" in watchdog
    assert "ExecStart=/usr/bin/systemctl restart curiosity-pia-tunnel.service" in watchdog
    timer = env.get_template("curiosity-pia-tunnel-watchdog.timer.j2").render(**_vars())
    assert "OnUnitActiveSec=1min" in timer and "WantedBy=timers.target" in timer


def test_sudoers_is_limited_to_the_exec_stage() -> None:
    text = _env().get_template("pia-tunnel-sudoers.j2").render(**_vars())
    rules = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert rules == [
        "mrh ALL=(root) NOPASSWD: /usr/local/bin/curiosity-pia-run --exec *",
        "second ALL=(root) NOPASSWD: /usr/local/bin/curiosity-pia-run --exec *",
    ]


@pytest.mark.skipif(shutil.which("visudo") is None, reason="visudo not installed")
def test_sudoers_passes_visudo(tmp_path: Path) -> None:
    path = tmp_path / "sudoers"
    path.write_text(_env().get_template("pia-tunnel-sudoers.j2").render(**_vars()))
    path.chmod(0o440)
    result = subprocess.run(["visudo", "-cf", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


# --------------------------------------------------------------------------
# curiosity-pia-tunnel against fake network tools
# --------------------------------------------------------------------------

FAKE_CURL = r'''#!/usr/bin/python3
import json, os, sys
state = os.environ["FAKE_DIR"]
argv = sys.argv[1:]
stdin = sys.stdin.read()
with open(state + "/curl.argv", "a") as f:
    f.write(json.dumps(argv) + "\n")
with open(state + "/curl.stdin", "a") as f:
    f.write(stdin + "\n--\n")
url = argv[-1]
if os.environ.get("FAKE_CURL_FAIL") and os.environ["FAKE_CURL_FAIL"] in url:
    sys.stderr.write("curl: (22) The requested URL returned error: 401\n")
    sys.exit(22)
if "gtoken/generateToken" in url:
    print(json.dumps({"status": "OK", "token": os.environ["FAKE_TOKEN"]}))
elif "api/client/v2/token" in url:
    print(json.dumps({"token": os.environ["FAKE_TOKEN"]}))
elif "serverlist.piaservers.net" in url:
    print(open(state + "/servers.json").read().strip())
    print("c2lnbmF0dXJlLWJ5dGVz")  # the real list is followed by a signature
elif url.endswith("/addKey"):
    print(json.dumps({
        "status": "OK", "server_key": os.environ["FAKE_WG_KEY"], "server_port": 1337,
        "server_ip": "203.0.113.9", "server_vip": "10.0.0.1", "peer_ip": "10.5.6.7",
        "peer_pubkey": os.environ["FAKE_WG_KEY"], "dns_servers": ["10.0.0.243", "10.0.0.242"],
    }))
else:
    sys.stderr.write("fake curl: unexpected url " + url + "\n")
    sys.exit(7)
'''

FAKE_IP = r'''#!/usr/bin/python3
import json, os, subprocess, sys
state = os.environ["FAKE_DIR"]
netns_run = os.environ["CURIOSITY_PIA_NETNS_RUN"]
argv = sys.argv[1:]
with open(state + "/ip.calls", "a") as f:
    f.write(" ".join(argv) + "\n")
if argv[:2] == ["netns", "add"]:
    open(os.path.join(netns_run, argv[2]), "w").close()
elif argv[:2] == ["netns", "del"]:
    try:
        os.unlink(os.path.join(netns_run, argv[2]))
    except FileNotFoundError:
        pass
elif argv[:2] == ["netns", "exec"]:
    # `ip netns exec NS cmd...` runs cmd (a fake on PATH) and logs it.
    with open(state + "/exec.calls", "a") as f:
        f.write(" ".join(argv[3:]) + "\n")
    sys.exit(subprocess.run(argv[3:], stdin=sys.stdin, check=False).returncode)
elif argv[:2] == ["link", "add"]:
    open(state + "/host-link", "w").write(argv[2])
elif argv[:1] == ["link"] and argv[1] == "del":
    # host-namespace delete: succeeds only when the interface is there
    if os.path.exists(state + "/host-link"):
        os.unlink(state + "/host-link")
    else:
        sys.exit(1)
elif argv[:1] == ["link"] and "netns" in argv:
    os.unlink(state + "/host-link")
    open(state + "/ns-link", "w").write(argv[1])
elif argv[:1] == ["-n"] and argv[2:4] == ["link", "del"]:
    if os.path.exists(state + "/ns-link"):
        os.unlink(state + "/ns-link")
    else:
        sys.exit(1)
'''

FAKE_WG = r'''#!/usr/bin/python3
import base64, os, sys, time
state = os.environ["FAKE_DIR"]
argv = sys.argv[1:]
stdin = "" if sys.stdin.isatty() else sys.stdin.read()
with open(state + "/wg.argv", "a") as f:
    f.write(" ".join(argv) + "\n")
if argv[0] == "genkey":
    print(base64.b64encode(b"p" * 32).decode())
elif argv[0] == "pubkey":
    print(base64.b64encode(b"q" * 32).decode())
elif argv[0] == "set":
    with open(state + "/wg.stdin", "a") as f:
        f.write(stdin)
elif argv[0] == "show":
    if not os.path.exists(state + "/ns-link") or os.environ.get("FAKE_NO_HANDSHAKE"):
        sys.exit(0 if os.path.exists(state + "/ns-link") else 1)
    age = int(os.environ.get("FAKE_HANDSHAKE_AGE", "5"))
    print("PEERKEY\t%d" % (time.time() - age))
'''

FAKE_NOOP = '#!/bin/sh\ncat >> "$FAKE_DIR/$(basename "$0").stdin" 2>/dev/null\nexit 0\n'

SERVERS = {
    "regions": [
        {"id": "us_chicago", "name": "US Chicago", "geo": False, "offline": False,
         "servers": {"wg": [{"ip": "203.0.113.9", "cn": "chicago401"}]}},
        {"id": "nl_amsterdam", "name": "Netherlands", "geo": False, "offline": False,
         "servers": {"wg": [{"ip": "203.0.113.20", "cn": "amsterdam441"}]}},
        {"id": "dead", "name": "Offline", "geo": False, "offline": True,
         "servers": {"wg": [{"ip": "203.0.113.30", "cn": "dead1"}]}},
        {"id": "no_wg", "name": "OpenVPN only", "geo": False, "offline": False, "servers": {"ovpnudp": []}},
    ]
}


class Rig:
    """A fake root filesystem and network toolbox for one tunnel run."""

    def __init__(self, tmp: Path, region: str = "us_chicago", session_max_hours: int = 0) -> None:
        self.tmp = tmp
        self.fake = tmp / "fake"
        self.bin = tmp / "bin"
        self.netns_run = tmp / "netns-run"
        self.netns_etc = tmp / "netns-etc"
        self.state = tmp / "state"
        for d in (self.fake, self.bin, self.netns_run, self.netns_etc):
            d.mkdir()
        (self.fake / "servers.json").write_text(json.dumps(SERVERS))
        self.login = tmp / "login"
        self.login.write_text(f"{USERNAME}\n{PASSWORD}\n")
        self.login.chmod(0o600)
        self.nsswitch = tmp / "nsswitch.conf"
        self.nsswitch.write_text("passwd: files systemd\nhosts: files resolve [!UNAVAIL=return] dns\nnetworks: files\n")
        self.config = tmp / "config.json"
        self.config.write_text(
            json.dumps(
                {
                    "netns": "pia", "interface": "wgpia", "region": region,
                    "login_file": str(self.login), "ca_file": "/ca.crt", "mtu": 1420,
                    "keepalive": 25, "max_handshake_age": 240, "session_max_hours": session_max_hours,
                    "run_path": "/usr/local/bin/curiosity-pia-run", "apps": {},
                }
            )
        )
        for name, body in {"curl": FAKE_CURL, "ip": FAKE_IP, "wg": FAKE_WG}.items():
            (self.bin / name).write_text(body)
        for name in ("nft", "sysctl", "ping"):
            (self.bin / name).write_text(FAKE_NOOP)
        for path in self.bin.iterdir():
            path.chmod(0o755)

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        environment = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "FAKE_DIR": str(self.fake),
            "FAKE_TOKEN": TOKEN,
            "FAKE_WG_KEY": WG_KEY,
            "CURIOSITY_PIA_CONFIG": str(self.config),
            "CURIOSITY_PIA_STATE_DIR": str(self.state),
            "CURIOSITY_PIA_NETNS_RUN": str(self.netns_run),
            "CURIOSITY_PIA_NETNS_ETC": str(self.netns_etc),
            "CURIOSITY_PIA_NSSWITCH": str(self.nsswitch),
            "CURIOSITY_PIA_ALLOW_NON_ROOT": "1",
            **env,
        }
        return subprocess.run(
            [sys.executable, "-I", str(TUNNEL), *args],
            capture_output=True, text=True, env=environment, timeout=60,
        )

    def read(self, name: str) -> str:
        path = self.fake / name
        return path.read_text() if path.exists() else ""


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def _calls(rig: Rig) -> list[str]:
    return rig.read("ip.calls").splitlines()


def _assert_no_secret(rig: Rig, result: subprocess.CompletedProcess) -> None:
    visible = "\n".join(
        [result.stdout, result.stderr, rig.read("curl.argv"), rig.read("ip.calls"),
         rig.read("wg.argv"), rig.read("exec.calls")]
    )
    for secret in (PASSWORD, TOKEN, base64.b64encode(b"p" * 32).decode()):
        assert secret not in visible, "a secret reached an argument list or the output"
    assert PASSWORD not in (rig.tmp / "state" / "state.json").read_text()


def test_up_builds_a_closed_namespace_then_the_tunnel(rig: Rig) -> None:
    result = rig.run("up")
    assert result.returncode == 0, result.stderr
    calls = _calls(rig)
    # The namespace is created and closed before any tunnel exists.
    created = calls.index("netns add pia")
    assert "-n pia link set lo up" in calls
    firewall = rig.read("exec.calls").splitlines()
    assert any(line.startswith("sysctl -qw net.ipv6.conf.all.disable_ipv6=1") for line in firewall)
    assert "nft -f -" in firewall
    # The interface is created in the host namespace, keyed, then moved in.
    assert calls.index("link add wgpia type wireguard") > created
    assert "link set wgpia netns pia" in calls
    assert "-n pia addr add 10.5.6.7/32 dev wgpia" in calls
    assert "-n pia link set wgpia mtu 1420 up" in calls
    assert "-n pia route replace default dev wgpia" in calls
    assert calls.index("link set wgpia netns pia") < calls.index("-n pia route replace default dev wgpia")
    wg = rig.read("wg.argv")
    assert "set wgpia private-key /dev/stdin peer " + WG_KEY + " endpoint 203.0.113.9:1337 allowed-ips 0.0.0.0/0 persistent-keepalive 25" in wg
    assert rig.read("wg.stdin").strip() == base64.b64encode(b"p" * 32).decode()  # key only via stdin
    _assert_no_secret(rig, result)


def test_up_uses_pias_ca_and_connect_to_for_addkey(rig: Rig) -> None:
    rig.run("up")
    addkey = [json.loads(line) for line in rig.read("curl.argv").splitlines() if "/addKey" in line][0]
    assert addkey[-1] == "https://chicago401:1337/addKey"
    assert addkey[addkey.index("--cacert") + 1] == "/ca.crt"
    assert addkey[addkey.index("--connect-to") + 1] == "chicago401::203.0.113.9:"
    assert "--proto" in addkey and "=https" in addkey
    stdin = rig.read("curl.stdin")
    assert f'data-urlencode = "pt={TOKEN}"' in stdin  # secrets travel on stdin
    assert 'get' in stdin


def test_the_login_goes_to_curl_on_stdin_only(rig: Rig) -> None:
    rig.run("up")
    assert f'user = "{USERNAME}:{PASSWORD}"' in rig.read("curl.stdin")
    token_calls = [line for line in rig.read("curl.argv").splitlines() if "generateToken" in line]
    assert token_calls and USERNAME not in token_calls[0] and PASSWORD not in token_calls[0]


def test_the_netns_resolver_files_never_use_the_host_resolver(rig: Rig) -> None:
    rig.run("up")
    resolv = (rig.netns_etc / "pia/resolv.conf").read_text()
    assert [l for l in resolv.splitlines() if l.startswith("nameserver")] == [
        "nameserver 10.0.0.243", "nameserver 10.0.0.242",
    ]
    nsswitch = (rig.netns_etc / "pia/nsswitch.conf").read_text().splitlines()
    assert "hosts:          files dns" in nsswitch
    assert "passwd: files systemd" in nsswitch  # everything else is kept
    assert not any("resolve" in line for line in nsswitch)


def test_state_has_no_secrets_and_is_world_readable(rig: Rig) -> None:
    result = rig.run("up")
    state = json.loads((rig.state / "state.json").read_text())
    assert state["peer_ip"] == "10.5.6.7" and state["region"] == "us_chicago"
    assert set(state) == {"established_at", "region", "region_name", "server_ip", "server_cn",
                          "peer_ip", "dns", "interface", "netns"}
    assert (rig.state / "state.json").stat().st_mode & 0o777 == 0o644
    _assert_no_secret(rig, result)


def test_a_rejected_login_leaves_the_namespace_closed_and_no_tunnel(rig: Rig) -> None:
    result = rig.run("up", FAKE_CURL_FAIL="privateinternetaccess.com")
    assert result.returncode == 1
    assert "PIA refused the login" in result.stderr
    calls = _calls(rig)
    assert "netns add pia" in calls  # closed namespace exists
    assert not any(c.startswith("link add") for c in calls)  # but no tunnel
    assert PASSWORD not in result.stdout + result.stderr


def test_a_failed_registration_keeps_a_working_tunnel(rig: Rig) -> None:
    assert rig.run("up").returncode == 0
    before = len(_calls(rig))
    result = rig.run("up", FAKE_CURL_FAIL="/addKey")
    assert result.returncode == 1
    new = _calls(rig)[before:]
    assert not any("link del" in c for c in new), "the live tunnel was torn down before the new one was ready"
    assert (rig.fake / "ns-link").exists()


def test_a_missing_handshake_removes_the_half_built_tunnel(rig: Rig) -> None:
    result = rig.run("up", FAKE_NO_HANDSHAKE="1", CURIOSITY_PIA_HANDSHAKE_WAIT="2")
    assert result.returncode == 1
    assert "no WireGuard handshake within 2s" in result.stderr
    assert not (rig.fake / "ns-link").exists() and not (rig.fake / "host-link").exists()


def test_login_file_must_be_root_only(rig: Rig) -> None:
    rig.login.chmod(0o644)
    result = rig.run("up")
    assert result.returncode == 1 and "must not be readable by group or others" in result.stderr


def test_login_file_needs_two_lines(rig: Rig) -> None:
    rig.login.write_text(f"{USERNAME}\n")
    rig.login.chmod(0o600)
    result = rig.run("up")
    assert result.returncode == 1 and "username on line 1 and the password on line 2" in result.stderr


def test_missing_login_names_the_file_not_the_secret(rig: Rig) -> None:
    rig.login.unlink()
    result = rig.run("up")
    assert result.returncode == 1 and str(rig.login) in result.stderr and "Bitwarden" in result.stderr


def test_an_unknown_region_is_an_error_listing_real_ones(rig: Rig) -> None:
    rig.config.write_text(rig.config.read_text().replace('"us_chicago"', '"atlantis"'))
    result = rig.run("up")
    assert result.returncode == 1 and "atlantis" in result.stderr and "us_chicago" in result.stderr


def test_auto_prefers_the_nearest_server_and_skips_offline_and_geo(monkeypatch: pytest.MonkeyPatch) -> None:
    servers = [
        tunnel.Server("us_chicago", "Chicago", "203.0.113.9", "chicago401"),
        tunnel.Server("nl_amsterdam", "Amsterdam", "203.0.113.20", "amsterdam441"),
        tunnel.Server("virtual", "Virtual", "203.0.113.40", "virtual1", geo=True),
    ]
    latency = {"203.0.113.9": 0.09, "203.0.113.20": 0.02, "203.0.113.40": 0.001}
    chosen = tunnel.choose_server(servers, "auto", lambda ip, port: latency[ip])
    assert chosen.region_id == "nl_amsterdam"  # the geo server is faster but virtual
    explicit = tunnel.choose_server(servers, "virtual", lambda ip, port: latency[ip])
    assert explicit.region_id == "virtual"  # unless asked for by name


def test_auto_with_no_reachable_server_is_an_error() -> None:
    servers = [tunnel.Server("us_chicago", "Chicago", "203.0.113.9", "chicago401")]
    with pytest.raises(tunnel.TunnelError, match="no PIA server answered"):
        tunnel.choose_server(servers, "auto", lambda ip, port: None)


def test_fetch_servers_filters_offline_and_non_wireguard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tunnel, "curl", lambda url, *a, **k: json.dumps(SERVERS) + "\nc2ln\n")
    assert [s.region_id for s in tunnel.fetch_servers()] == ["us_chicago", "nl_amsterdam"]


def test_the_v2_token_endpoint_is_the_fallback(rig: Rig) -> None:
    result = rig.run("up", FAKE_CURL_FAIL="gtoken")
    assert result.returncode == 0, result.stderr
    assert 'data-urlencode = "username=' in rig.read("curl.stdin")
    _assert_no_secret(rig, result)


def test_curl_config_values_are_escaped_and_control_characters_rejected() -> None:
    assert tunnel.curl_value("user", 'a"b\\c') == 'user = "a\\"b\\\\c"'
    for bad in ("a\nb", "a\rb", "a\0b"):
        with pytest.raises(tunnel.TunnelError):
            tunnel.curl_value("user", bad)


def test_addkey_validation_rejects_a_bad_answer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = tunnel.Config("pia", "wgpia", "auto", tmp_path / "l", tmp_path / "ca", 1420, 25, 240, 0)
    server = tunnel.Server("us_chicago", "Chicago", "203.0.113.9", "chicago401")
    good = {"status": "OK", "server_key": WG_KEY, "server_port": 1337, "server_ip": "203.0.113.9",
            "peer_ip": "10.5.6.7", "dns_servers": ["10.0.0.243"]}
    for patch, message in (
        ({"status": "ERROR"}, "refused"),
        ({"server_key": "not-a-key"}, "invalid server_key"),
        ({"server_port": 70000}, "port"),
        ({"peer_ip": "nonsense"}, "missing"),
    ):
        monkeypatch.setattr(tunnel, "curl", lambda *a, _p=patch, **k: json.dumps({**good, **_p}))
        with pytest.raises(tunnel.TunnelError, match=message):
            tunnel.add_key(cfg, TOKEN, WG_KEY, server)
    monkeypatch.setattr(tunnel, "curl", lambda *a, **k: json.dumps({**good, "dns_servers": []}))
    assert tunnel.add_key(cfg, TOKEN, WG_KEY, server).dns == tunnel.FALLBACK_DNS


def test_the_firewall_only_passes_loopback_and_the_tunnel(tmp_path: Path) -> None:
    cfg = tunnel.Config("pia", "wgpia", "auto", tmp_path / "l", tmp_path / "ca", 1420, 25, 240, 0)
    rules = tunnel.firewall_ruleset(cfg)
    assert rules.count("policy drop") == 3
    accepts = re.findall(r'(?:iifname|oifname) "([^"]+)"', rules)
    assert set(accepts) == {"lo", "wgpia"}
    assert 'iifname "wgpia" ct state established,related accept' in rules  # nothing unsolicited inbound
    assert rules.startswith("add table inet curiosity_pia\ndelete table inet curiosity_pia\n")  # idempotent


def test_config_validation() -> None:
    base = dict(netns="pia", interface="wgpia", region="auto", login_file=Path("/l"), ca_file=Path("/c"),
                mtu=1420, keepalive=25, max_handshake_age=240, session_max_hours=0)
    tunnel.Config(**base).validate()
    for patch in ({"netns": "Bad Name"}, {"interface": "x" * 16}, {"region": "a b"}, {"mtu": 100},
                  {"keepalive": 9999}, {"max_handshake_age": 10}, {"session_max_hours": -1}):
        with pytest.raises(tunnel.TunnelError):
            tunnel.Config(**{**base, **patch}).validate()


# --- health, renewal and lifecycle ---------------------------------------


def test_a_fresh_tunnel_is_healthy_and_needs_no_renewal(rig: Rig) -> None:
    assert rig.run("up").returncode == 0
    result = rig.run("needs-renewal")
    assert result.returncode == 1 and "healthy" in result.stdout
    ensure = rig.run("ensure")
    assert ensure.returncode == 0 and "tunnel healthy" in ensure.stdout
    assert len([l for l in rig.read("curl.argv").splitlines() if "addKey" in l]) == 1  # no second registration


def test_a_stale_handshake_triggers_renewal(rig: Rig) -> None:
    rig.run("up")
    result = rig.run("needs-renewal", FAKE_HANDSHAKE_AGE="900")
    assert result.returncode == 0 and "last handshake 900s ago" in result.stdout
    ensure = rig.run("ensure", FAKE_HANDSHAKE_AGE="900")
    assert ensure.returncode == 0 and "rebuilding tunnel" in ensure.stdout
    assert len([l for l in rig.read("curl.argv").splitlines() if "addKey" in l]) == 2


def test_a_vanished_interface_triggers_renewal(rig: Rig) -> None:
    rig.run("up")
    (rig.fake / "ns-link").unlink()
    assert rig.run("needs-renewal").returncode == 0


def test_a_missing_namespace_triggers_renewal(rig: Rig) -> None:
    result = rig.run("needs-renewal")
    assert result.returncode == 0 and "does not exist" in result.stdout


def test_session_age_triggers_renewal_when_configured(tmp_path: Path) -> None:
    rig = Rig(tmp_path, session_max_hours=24)
    rig.run("up")
    assert rig.run("needs-renewal").returncode == 1
    state = json.loads((rig.state / "state.json").read_text())
    state["established_at"] = int(time.time()) - 25 * 3600
    (rig.state / "state.json").write_text(json.dumps(state))
    result = rig.run("needs-renewal")
    assert result.returncode == 0 and "older than 24h" in result.stdout


def test_a_broken_config_makes_the_watchdog_fail_loudly(rig: Rig) -> None:
    rig.config.write_text("{not json")
    assert rig.run("needs-renewal").returncode == 255  # ExecCondition: only 255 is a failure
    assert rig.run("up").returncode == 1


def test_ensure_retries_then_fails(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    result = rig.run("ensure", "--retries", "1", FAKE_CURL_FAIL="privateinternetaccess.com")
    assert result.returncode == 1 and "attempt 1/1 failed" in result.stderr + result.stdout


def test_down_keeps_the_namespace_closed_and_destroy_removes_it(rig: Rig) -> None:
    rig.run("up")
    assert rig.run("down").returncode == 0
    assert (rig.netns_run / "pia").exists()  # still there: a running app loses its network, not its jail
    assert not (rig.fake / "ns-link").exists() and not (rig.state / "state.json").exists()
    assert rig.run("down", "--destroy").returncode == 0
    assert not (rig.netns_run / "pia").exists()
    assert not (rig.netns_etc / "pia/resolv.conf").exists()


def test_status_reports_connected_and_exit_code(rig: Rig) -> None:
    rig.run("up")
    ok = rig.run("status")
    assert ok.returncode == 0 and "connected:  yes" in ok.stdout and "10.5.6.7" in ok.stdout
    stale = rig.run("status", FAKE_HANDSHAKE_AGE="9999")
    assert stale.returncode == 1 and "connected:  no" in stale.stdout


def test_the_tunnel_manager_refuses_to_run_as_a_normal_user(rig: Rig) -> None:
    if os.geteuid() == 0:
        pytest.skip("running as root")
    environment = {**os.environ, "CURIOSITY_PIA_CONFIG": str(rig.config)}
    environment.pop("CURIOSITY_PIA_ALLOW_NON_ROOT", None)
    result = subprocess.run([sys.executable, "-I", str(TUNNEL), "status"], capture_output=True, text=True, env=environment)
    assert result.returncode == 2 and "must run as root" in result.stderr


# --------------------------------------------------------------------------
# curiosity-pia-run
# --------------------------------------------------------------------------

BRAVE_SPEC = {
    "command": ["/usr/bin/brave-browser-stable"],
    "guard_exe": "/opt/brave.com/brave/brave",
    "desktop_ids": ["brave-browser.desktop"],
    "desktop_commands": ["/usr/bin/brave-browser-stable"],
    "shims": ["brave-browser"],
    "scope": {"slice": "app-browser.slice", "MemoryHigh": "16G", "MemoryMax": "24G"},
    "env_passthrough": [],
}
CONFIG = {"netns": "pia", "run_path": "/usr/local/bin/curiosity-pia-run", "apps": {"brave-browser": BRAVE_SPEC}}

BRAVE_DESKTOP = """[Desktop Entry]
Version=1.0
Name=Brave Web Browser
Name[de]=Brave Webbrowser
Exec=/usr/bin/brave-browser-stable %U
StartupWMClass=brave-browser
Icon=brave-browser
MimeType=text/html;
Actions=new-window;new-private-window;

[Desktop Action new-window]
Name=New Window
Exec=/usr/bin/brave-browser-stable

[Desktop Action new-private-window]
Name=New Incognito Window
Exec=/usr/bin/brave-browser-stable --incognito
"""


def test_only_session_variables_pass_the_allow_list() -> None:
    environ = {
        "DISPLAY": ":0", "XAUTHORITY": "/home/u/.Xauthority", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1/bus",
        "XDG_RUNTIME_DIR": "/run/user/1", "LANG": "en_US.UTF-8", "LC_ALL": "C", "GTK_THEME": "x", "PATH": "/usr/bin",
        "LD_PRELOAD": "/tmp/evil.so", "LD_LIBRARY_PATH": "/tmp", "BASH_ENV": "/tmp/rc", "SUDO_USER": "x",
        "AWS_SECRET_ACCESS_KEY": "k", "BW_SESSION": "s", "HTTPS_PROXY": "http://leak",
    }
    passed = launcher.collect_env(environ)
    assert "LD_PRELOAD" not in passed and "BW_SESSION" not in passed and "AWS_SECRET_ACCESS_KEY" not in passed
    assert "HTTPS_PROXY" not in passed and "BASH_ENV" not in passed
    assert {"DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "LANG", "LC_ALL", "GTK_THEME", "PATH"} <= set(passed)
    assert launcher.collect_env({"MY_VAR": "1"}, ["MY_VAR"]) == {"MY_VAR": "1"}


def test_the_privileged_stage_re_validates_the_environment() -> None:
    assert launcher.parse_env_pairs(["DISPLAY=:0", "LC_ALL=C"]) == {"DISPLAY": ":0", "LC_ALL": "C"}
    for bad in (["LD_PRELOAD=/tmp/x"], ["noequals"], ["1BAD=x"], ["DISPLAY=a\0b"], ["DISPLAY=" + "x" * 9000]):
        with pytest.raises(launcher.LaunchError):
            launcher.parse_env_pairs(bad)


def test_user_stage_wraps_in_a_scope_and_calls_the_sudo_target() -> None:
    environ = {"DISPLAY": ":0", "LD_PRELOAD": "/tmp/evil.so", "LANG": "C"}
    command = launcher.user_stage_command(CONFIG, "brave-browser", ["https://example.com"], environ, True)
    assert command[:5] == ["systemd-run", "--user", "--scope", "--quiet", "--collect"]
    assert "--slice=app-browser.slice" in command
    assert command[command.index("-p") + 1].startswith("Memory")
    sudo = command.index("sudo")
    assert command[sudo:sudo + 5] == ["sudo", "-n", "/usr/local/bin/curiosity-pia-run", "--exec", "brave-browser"]
    assert "--env" in command and "DISPLAY=:0" in command and "LANG=C" in command
    assert not any("LD_PRELOAD" in part for part in command)
    assert command[-2:] == ["--", "https://example.com"]


def test_user_stage_without_a_user_manager_still_goes_through_sudo_never_plain() -> None:
    command = launcher.user_stage_command(CONFIG, "brave-browser", [], {"DISPLAY": ":0"}, False)
    assert command[:2] == ["sudo", "-n"]
    assert "systemd-run" not in command
    assert "/usr/bin/brave-browser-stable" not in command  # the app command is never built here


def test_the_caller_cannot_choose_the_command_or_an_unknown_app() -> None:
    with pytest.raises(launcher.LaunchError, match="not routed through PIA"):
        launcher.app_spec(CONFIG, "bash")
    with pytest.raises(launcher.LaunchError):
        launcher.app_spec(CONFIG, "../../bin/sh")
    broken = {"apps": {"x": {"command": []}}}
    with pytest.raises(launcher.LaunchError, match="no command"):
        launcher.app_spec(broken, "x")


def _fake_tools(tmp_path: Path) -> Path:
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("ip", "setpriv", "env", "notify-send"):
        path = tools / name
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
    return tools


def test_root_stage_enters_the_namespace_drops_privileges_and_scrubs_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tools = _fake_tools(tmp_path)
    monkeypatch.setattr(launcher, "SAFE_PATH", str(tools))
    command = launcher.root_stage_command(
        CONFIG, "brave-browser", ["DISPLAY=:0", "PATH=/home/u/bin:/usr/bin"], ["--incognito", "https://example.com"],
        1001, 1001, "/home/u", "u", "/bin/bash",
    )
    ip, setpriv, env = (str(tools / n) for n in ("ip", "setpriv", "env"))
    assert command[:4] == [ip, "netns", "exec", "pia"]
    assert command[4:9] == [setpriv, "--reuid=1001", "--regid=1001", "--init-groups", "--"]
    assert command[9:11] == [env, "-i"]
    assignments = command[11:command.index("/usr/bin/brave-browser-stable")]
    assert "HOME=/home/u" in assignments and "USER=u" in assignments and "DISPLAY=:0" in assignments
    assert "PATH=/home/u/bin:/usr/bin" in assignments
    assert command[-3:] == ["/usr/bin/brave-browser-stable", "--incognito", "https://example.com"][-3:]
    assert not any(a.startswith(("LD_", "SUDO_")) for a in assignments)


def test_root_stage_refuses_outside_sudo_and_for_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(launcher.LaunchError, match="privileged stage"):
        launcher.run_root("brave-browser", [], [])
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.delenv("SUDO_UID", raising=False)
    with pytest.raises(launcher.LaunchError, match="through sudo"):
        launcher.run_root("brave-browser", [], [])
    monkeypatch.setenv("SUDO_UID", "0")
    monkeypatch.setenv("SUDO_GID", "0")
    with pytest.raises(launcher.LaunchError, match="as root"):
        launcher.run_root("brave-browser", [], [])


@pytest.fixture
def root_stage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Run the privileged stage as the current user pretending to be root via sudo."""
    config = tmp_path / "config.json"
    config.write_text(json.dumps(CONFIG))
    run = tmp_path / "netns-run"
    run.mkdir()
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setattr(launcher, "CONFIG_PATH", config)
    monkeypatch.setattr(launcher, "NETNS_RUN", run)
    monkeypatch.setattr(launcher, "PROC_ROOT", proc)
    monkeypatch.setattr(launcher, "SAFE_PATH", str(_fake_tools(tmp_path)))
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    uid = os.getuid() or 1000
    monkeypatch.setenv("SUDO_UID", str(uid))
    monkeypatch.setenv("SUDO_GID", str(os.getgid() or 1000))
    monkeypatch.setattr(launcher.pwd, "getpwuid", lambda _uid: type("P", (), {
        "pw_dir": "/home/test", "pw_name": "test", "pw_shell": "/bin/sh"})())
    monkeypatch.setattr(launcher, "notify_user", lambda *a, **k: None)
    executed: list[list[str]] = []
    monkeypatch.setattr(os, "execv", lambda path, argv: executed.append(list(argv)))
    return type("Stage", (), {"run": run, "proc": proc, "executed": executed, "uid": uid, "config": config})()


def _proc(stage, pid: int, exe: str, cmdline: bytes, ns_inode_file: Path) -> None:
    entry = stage.proc / str(pid)
    (entry / "ns").mkdir(parents=True)
    (entry / "exe").symlink_to(exe)
    (entry / "cmdline").write_bytes(cmdline)
    (entry / "ns" / "net").hardlink_to(ns_inode_file)


def test_root_stage_refuses_when_the_namespace_is_missing(root_stage) -> None:
    with pytest.raises(launcher.LaunchError) as raised:
        launcher.run_root("brave-browser", [], [])
    assert raised.value.code == launcher.EXIT_NO_NAMESPACE
    assert "would otherwise run without the VPN" in str(raised.value)
    assert root_stage.executed == []


def test_root_stage_refuses_while_the_app_runs_outside_the_tunnel(root_stage, tmp_path: Path) -> None:
    (root_stage.run / "pia").write_text("")
    host_ns = tmp_path / "host-ns"
    host_ns.write_text("")
    exe = tmp_path / "brave"
    exe.write_text("")
    _proc(root_stage, 100, str(exe), b"/opt/brave.com/brave/brave\0", host_ns)
    monkey_exe = {"guard_exe": str(exe)}
    config = json.loads(root_stage.config.read_text())
    config["apps"]["brave-browser"].update(monkey_exe)
    root_stage.config.write_text(json.dumps(config))
    with pytest.raises(launcher.LaunchError) as raised:
        launcher.run_root("brave-browser", [], [])
    assert raised.value.code == launcher.EXIT_STRAY and "pid 100" in str(raised.value)
    assert root_stage.executed == []


def test_stray_guard_ignores_helpers_tunnelled_copies_and_other_apps(root_stage, tmp_path: Path) -> None:
    pia_ns = root_stage.run / "pia"
    pia_ns.write_text("")
    host_ns = tmp_path / "host-ns"
    host_ns.write_text("")
    exe, other = tmp_path / "brave", tmp_path / "chrome"
    exe.write_text("")
    other.write_text("")
    _proc(root_stage, 1, str(exe), b"brave\0", pia_ns)  # already tunnelled
    # Chromium rewrites helper argv with spaces; that must still read as a helper.
    _proc(root_stage, 2, str(exe), b"/opt/brave.com/brave/brave --type=renderer --lang=en", host_ns)
    _proc(root_stage, 3, str(other), b"chrome\0", host_ns)  # a different executable
    (root_stage.proc / "self").mkdir()  # non-numeric entries are skipped
    assert launcher.find_strays(str(exe), root_stage.uid, pia_ns.stat().st_ino) == []
    _proc(root_stage, 4, str(exe), b"brave\0", host_ns)
    assert launcher.find_strays(str(exe), root_stage.uid, pia_ns.stat().st_ino) == [4]
    assert launcher.find_strays(str(exe), root_stage.uid + 1, pia_ns.stat().st_ino) == []  # other users' apps


def test_root_stage_starts_the_app_inside_the_namespace(root_stage) -> None:
    (root_stage.run / "pia").write_text("")
    launcher.run_root("brave-browser", ["DISPLAY=:0"], ["--new-window"])
    argv = root_stage.executed[0]
    assert argv[1:4] == ["netns", "exec", "pia"] and argv[-2:] == ["/usr/bin/brave-browser-stable", "--new-window"]
    assert "DISPLAY=:0" in argv and any(a.startswith("--reuid=") for a in argv)


def test_exec_arguments_are_parsed_strictly() -> None:
    assert launcher.parse_exec_args(["app", "--env", "A=1", "--", "x", "--env"]) == ("app", ["A=1"], ["x", "--env"])
    assert launcher.parse_exec_args(["app", "--"]) == ("app", [], [])
    for bad in ([], ["app", "--bogus", "--"]):
        with pytest.raises(launcher.LaunchError):
            launcher.parse_exec_args(bad)


def test_main_returns_usage_codes(capsys: pytest.CaptureFixture) -> None:
    assert launcher.main([]) == launcher.EXIT_USAGE
    assert launcher.main(["--help"]) == 0
    assert launcher.main(["--exec"]) == launcher.EXIT_USAGE


# --- desktop entries and shims ---------------------------------------------


def test_every_exec_line_is_routed_and_the_entry_is_marked() -> None:
    out = launcher.wrap_desktop(BRAVE_DESKTOP, "brave-browser", BRAVE_SPEC, "/usr/local/bin/curiosity-pia-run", "src")
    execs = [l for l in out.splitlines() if l.startswith("Exec=")]
    assert execs == [
        "Exec=/usr/local/bin/curiosity-pia-run brave-browser %U",
        "Exec=/usr/local/bin/curiosity-pia-run brave-browser",
        "Exec=/usr/local/bin/curiosity-pia-run brave-browser --incognito",
    ]
    assert out.count("X-Curiosity-Pia-Tunnel-App=brave-browser") == 1
    assert out.splitlines()[0] == "[Desktop Entry]" and "Name[de]=Brave Webbrowser" in out
    assert "/usr/bin/brave-browser-stable" not in out  # no way left to launch it plain from the entry


def test_an_unrecognised_exec_fails_loudly_instead_of_leaking() -> None:
    changed = BRAVE_DESKTOP.replace("Exec=/usr/bin/brave-browser-stable %U", "Exec=/opt/other/brave %U")
    with pytest.raises(launcher.LaunchError, match="cannot route"):
        launcher.wrap_desktop(changed, "brave-browser", BRAVE_SPEC, "/r", "src")
    with pytest.raises(launcher.LaunchError, match="not a launchable"):
        launcher.wrap_desktop("[Desktop Entry]\nName=x\n", "brave-browser", BRAVE_SPEC, "/r", "src")


@pytest.fixture
def tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    system = tmp_path / "usr-share-applications"
    override = tmp_path / "usr-local-share-applications"
    shims = tmp_path / "usr-local-bin"
    home = tmp_path / "home/u"
    binary = tmp_path / "brave-bin"
    for d in (system, shims, home / ".local/share/applications"):
        d.mkdir(parents=True)
    (system / "brave-browser.desktop").write_text(BRAVE_DESKTOP)
    binary.write_text("")
    monkeypatch.setattr(launcher, "SYSTEM_APP_DIRS", (system,))
    monkeypatch.setattr(launcher, "OVERRIDE_APP_DIR", override)
    monkeypatch.setattr(launcher, "SHIM_DIR", shims)
    config = json.loads(json.dumps(CONFIG))
    config["apps"]["brave-browser"]["command"] = [str(binary)]
    config["apps"]["brave-browser"]["desktop_commands"] = ["/usr/bin/brave-browser-stable"]
    return type("Tree", (), {"system": system, "override": override, "shims": shims, "home": home,
                             "binary": binary, "config": config})()


def test_sync_wraps_entries_replaces_user_overrides_and_adds_shims(tree) -> None:
    user_entry = tree.home / ".local/share/applications/brave-browser.desktop"
    user_entry.write_text("[Desktop Entry]\nExec=/home/u/.local/bin/brave-capped %U\n")
    changes = launcher.sync(tree.config, [tree.home], remove_all=False)
    assert any("moved aside" in c for c in changes)
    wrapped = (tree.override / "brave-browser.desktop").read_text()
    assert "Exec=/usr/local/bin/curiosity-pia-run brave-browser %U" in wrapped
    assert user_entry.read_text() == wrapped  # the user's shadowing entry no longer bypasses the tunnel
    backup = user_entry.with_name("brave-browser.desktop.pre-pia-tunnel")
    assert "brave-capped" in backup.read_text()  # kept, under a name no desktop looks at
    shim = tree.shims / "brave-browser"
    assert shim.read_text().splitlines()[-1] == 'exec /usr/local/bin/curiosity-pia-run brave-browser "$@"'
    assert shim.stat().st_mode & 0o777 == 0o755


def test_sync_is_idempotent(tree) -> None:
    launcher.sync(tree.config, [tree.home], remove_all=False)
    assert launcher.sync(tree.config, [tree.home], remove_all=False) == []


def test_sync_follows_a_package_update_of_the_desktop_entry(tree) -> None:
    launcher.sync(tree.config, [tree.home], remove_all=False)
    (tree.system / "brave-browser.desktop").write_text(BRAVE_DESKTOP.replace("Version=1.0", "Version=1.5"))
    changes = launcher.sync(tree.config, [tree.home], remove_all=False)
    assert changes and "Version=1.5" in (tree.override / "brave-browser.desktop").read_text()


def test_sync_skips_an_app_that_is_not_installed(tree) -> None:
    tree.binary.unlink()
    assert launcher.sync(tree.config, [tree.home], remove_all=False) == []
    assert not tree.override.exists() and not (tree.shims / "brave-browser").exists()


def test_removing_an_app_restores_the_original_entry_and_drops_the_shim(tree) -> None:
    user_entry = tree.home / ".local/share/applications/brave-browser.desktop"
    original = "[Desktop Entry]\nExec=/home/u/.local/bin/brave-capped %U\n"
    user_entry.write_text(original)
    launcher.sync(tree.config, [tree.home], remove_all=False)
    other = json.loads(json.dumps(tree.config))
    other["apps"] = {}  # the profile no longer routes it
    changes = launcher.sync(other, [tree.home], remove_all=False)
    assert user_entry.read_text() == original
    assert not (tree.override / "brave-browser.desktop").exists()
    assert not (tree.shims / "brave-browser").exists()
    assert any("restored" in c for c in changes)
    assert launcher.sync(other, [tree.home], remove_all=False) == []


def test_remove_all_undoes_everything_even_with_the_app_still_configured(tree) -> None:
    launcher.sync(tree.config, [tree.home], remove_all=False)
    launcher.sync(tree.config, [tree.home], remove_all=True)
    assert not (tree.override / "brave-browser.desktop").exists()
    assert not (tree.shims / "brave-browser").exists()


def test_sync_never_touches_files_it_did_not_write(tree) -> None:
    foreign_shim = tree.shims / "keep-me"
    foreign_shim.write_text("#!/bin/sh\necho mine\n")
    foreign_entry = tree.override
    foreign_entry.mkdir(parents=True)
    (foreign_entry / "other.desktop").write_text("[Desktop Entry]\nExec=other\n")
    launcher.sync(tree.config, [tree.home], remove_all=False)
    launcher.sync({"apps": {}}, [tree.home], remove_all=False)
    assert foreign_shim.read_text() == "#!/bin/sh\necho mine\n"
    assert (foreign_entry / "other.desktop").exists()


def test_a_foreign_shim_with_the_same_name_is_backed_up_not_lost(tree) -> None:
    target = tree.shims / "brave-browser"
    target.write_text("#!/bin/sh\necho hand-made\n")
    launcher.sync(tree.config, [tree.home], remove_all=False)
    assert "hand-made" in (tree.shims / "brave-browser.pre-pia-tunnel").read_text()
    launcher.sync({"apps": {}}, [tree.home], remove_all=False)
    assert "hand-made" in target.read_text()


def test_the_cli_sync_command_prints_changes(tree, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps(tree.config))
    monkeypatch.setattr(launcher, "CONFIG_PATH", config)
    monkeypatch.setenv("CURIOSITY_PIA_ALLOW_NON_ROOT", "1")
    assert launcher.main(["sync", "--homes", str(tree.home)]) == 0
    assert "changed: wrote" in capsys.readouterr().out
