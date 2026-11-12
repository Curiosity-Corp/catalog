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


def test_only_deskmeet_routes_brave_and_qbittorrent_through_pia() -> None:
    routed = {name: p["pia_tunnel_apps"] for name, p in PROFILES.items() if p.get("pia_tunnel_apps")}
    assert routed == {"deskmeet-b760": ["brave-browser", "qbittorrent"]}


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
    assert yaml.safe_load(rendered) == ["brave-browser", "qbittorrent"]
    assert yaml.safe_load(template.render(hardware_profiles=PROFILES, hardware_profile="generic")) == []
    assert yaml.safe_load(template.render(hardware_profiles=PROFILES, hardware_profile="unknown")) == []


def test_brave_definition_is_complete_and_cannot_recurse() -> None:
    brave = DEFINITIONS["brave-browser"]
    # The command is the package's own script; /usr/bin/brave-browser-stable holds the shim.
    assert brave["command"] == ["/opt/brave.com/brave/brave-browser"]
    assert brave["guard_exe"] == "/opt/brave.com/brave/brave"
    assert brave["diverted"] == ["/usr/bin/brave-browser-stable"]
    assert brave["command"][0] not in brave["diverted"]
    assert set(brave["desktop_ids"]) == {"brave-browser.desktop", "com.brave.Browser.desktop"}
    # Every Exec= the package ships starts with one of these, so every entry is wrapped.
    assert "/usr/bin/brave-browser-stable" in brave["desktop_commands"]
    assert brave["legacy_wrappers"] == [".local/bin/brave-capped"]
    assert brave["shims"] == []
    # The old brave-capped wrapper's limits and overrides are kept.
    assert brave["scope"] == {"slice": "app-browser.slice", "MemoryHigh": "16G", "MemoryMax": "24G"}
    assert brave["scope_env"] == {"MemoryHigh": "BRAVE_MEMORY_HIGH", "MemoryMax": "BRAVE_MEMORY_MAX"}
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
    env.filters["bool"] = lambda value: value if isinstance(value, bool) else str(value).lower() in ("true", "yes", "1")
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
        "pia_tunnel_port_forward": False,
        "pia_tunnel_bind_interval": "10min",
        "dev_users": [{"name": "mrh", "home": "/home/mrh"}, {"name": "second", "home": "/home/second"}],
    }
    values.update(overrides)
    return values


def test_config_template_carries_only_the_listed_apps() -> None:
    rendered = json.loads(_env().get_template("pia-tunnel-config.json.j2").render(**_vars()))
    assert list(rendered["apps"]) == ["brave-browser"]
    brave = rendered["apps"]["brave-browser"]
    assert brave["command"] == ["/opt/brave.com/brave/brave-browser"]
    assert brave["diverted"] == ["/usr/bin/brave-browser-stable"]
    assert brave["legacy_wrappers"] == [".local/bin/brave-capped"]
    assert brave["scope_env"]["MemoryMax"] == "BRAVE_MEMORY_MAX"
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
    assert rendered["apps"]["thunderbird"]["diverted"] == []


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
import base64, json, os, sys
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
if any(f"//{cn}:19999/" in url for cn in os.environ.get("FAKE_PF_DEAD", "").split(",") if cn):
    sys.stderr.write("curl: (7) Failed to connect to the server: Connection refused\n")
    sys.exit(7)
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
elif url.endswith("/getSignature"):
    count_file = state + "/getsig.count"
    count = int(open(count_file).read()) + 1 if os.path.exists(count_file) else 1
    open(count_file, "w").write(str(count))
    payload = base64.b64encode(json.dumps({
        "token": os.environ["FAKE_TOKEN"], "port": int(os.environ.get("FAKE_PF_PORT", "47047")),
        "expires_at": os.environ.get("FAKE_PF_EXPIRES", "2099-01-01T00:00:00.123456789Z"),
    }).encode()).decode()
    print(json.dumps({"status": "OK", "payload": payload, "signature": "c2lnbmF0dXJlLW9mLXRoZS1wYXlsb2Fk"}))
elif url.endswith("/bindPort"):
    count_file = state + "/bind.count"
    count = int(open(count_file).read()) + 1 if os.path.exists(count_file) else 1
    open(count_file, "w").write(str(count))
    refused = os.environ.get("FAKE_BIND_FAIL") or (os.environ.get("FAKE_BIND_FAIL_FIRST") and count == 1)
    print(json.dumps({"status": "ERROR" if refused else "OK", "message": "port scheduled for add"}))
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

def _with_this_python(body: str) -> str:
    """Point a fake's shebang at the interpreter running the tests.

    The fakes are written as `#!/usr/bin/python3`, which does not exist in the
    `python:*-slim` CI image (Python lives in /usr/local/bin). There the kernel
    reports ENOENT for the missing interpreter and the tunnel script says
    "ip is not installed".
    """
    return body.replace("#!/usr/bin/python3\n", f"#!{sys.executable}\n", 1)


FAKE_NOOP = '#!/bin/sh\ncat >> "$FAKE_DIR/$(basename "$0").stdin" 2>/dev/null\nexit 0\n'

SERVERS = {
    "regions": [
        {"id": "us_chicago", "name": "US Chicago", "geo": False, "offline": False,
         "servers": {"wg": [{"ip": "203.0.113.9", "cn": "chicago401"}]}},
        {"id": "nl_amsterdam", "name": "Netherlands", "geo": False, "offline": False, "port_forward": True,
         "servers": {"wg": [{"ip": "203.0.113.20", "cn": "amsterdam441"}]}},
        {"id": "dead", "name": "Offline", "geo": False, "offline": True,
         "servers": {"wg": [{"ip": "203.0.113.30", "cn": "dead1"}]}},
        {"id": "no_wg", "name": "OpenVPN only", "geo": False, "offline": False, "servers": {"ovpnudp": []}},
    ]
}


class Rig:
    """A fake root filesystem and network toolbox for one tunnel run."""

    def __init__(
        self, tmp: Path, region: str = "us_chicago", session_max_hours: int = 0, port_forward: bool = False
    ) -> None:
        self.tmp = tmp
        self.persist = tmp / "persist"
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
                    "port_forward": port_forward, "run_path": "/usr/local/bin/curiosity-pia-run", "apps": {},
                }
            )
        )
        for name, body in {"curl": FAKE_CURL, "ip": FAKE_IP, "wg": FAKE_WG}.items():
            (self.bin / name).write_text(_with_this_python(body))
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
            "CURIOSITY_PIA_PERSIST_DIR": str(self.persist),
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
    assert set(state) == {"established_at", "region", "region_name", "server_ip", "server_cn", "server_vip",
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
    assert ok.returncode == 0 and re.search(r"connected:\s+yes", ok.stdout) and "10.5.6.7" in ok.stdout
    stale = rig.run("status", FAKE_HANDSHAKE_AGE="9999")
    assert stale.returncode == 1 and re.search(r"connected:\s+no", stale.stdout)


def test_status_values_line_up_in_one_column(rig: Rig) -> None:
    rig.run("up")
    lines = rig.run("status").stdout.splitlines()
    assert len(lines) >= 8
    columns = {len(line) - len(line.split(":", 1)[1].lstrip()) for line in lines}
    assert len(columns) == 1, lines


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
    # Never /usr/bin/brave-browser-stable: on a converged host that path is the tunnel shim.
    "command": ["/opt/brave.com/brave/brave-browser"],
    "guard_exe": "/opt/brave.com/brave/brave",
    "desktop_ids": ["brave-browser.desktop"],
    "desktop_commands": ["/usr/bin/brave-browser-stable"],
    "shims": ["brave-browser"],
    "scope": {"slice": "app-browser.slice", "MemoryHigh": "16G", "MemoryMax": "24G"},
    "env_passthrough": [],
    "display_name": "Brave",
    "quit_hint": "Quit Brave (Ctrl+Shift+Q)",
}
CONFIG = {"netns": "pia", "interface": "wgpia", "run_path": "/usr/local/bin/curiosity-pia-run", "apps": {"brave-browser": BRAVE_SPEC}}

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
    assignments = command[11:command.index("/opt/brave.com/brave/brave-browser")]
    assert "HOME=/home/u" in assignments and "USER=u" in assignments and "DISPLAY=:0" in assignments
    assert "PATH=/home/u/bin:/usr/bin" in assignments
    assert command[-3:] == ["/opt/brave.com/brave/brave-browser", "--incognito", "https://example.com"]
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
    assert argv[1:4] == ["netns", "exec", "pia"] and argv[-2:] == ["/opt/brave.com/brave/brave-browser", "--new-window"]
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
    admin = tmp_path / "dpkg-admin"
    admin.mkdir()
    (admin / "diversions").write_text("")
    monkeypatch.setattr(launcher, "DPKG_ADMINDIR", str(admin))
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


# --- scope limits and the recursion guard ------------------------------------

SCOPED = {**BRAVE_SPEC, "scope_env": {"MemoryHigh": "BRAVE_MEMORY_HIGH", "MemoryMax": "BRAVE_MEMORY_MAX"}}


def test_scope_limits_default_to_the_old_wrapper_and_honour_its_overrides() -> None:
    base = launcher.scope_properties(SCOPED, {})
    assert "MemoryHigh=16G" in base and "MemoryMax=24G" in base and "--slice=app-browser.slice" in base
    changed = launcher.scope_properties(SCOPED, {"BRAVE_MEMORY_HIGH": "8G", "BRAVE_MEMORY_MAX": "50%"})
    assert "MemoryHigh=8G" in changed and "MemoryMax=50%" in changed
    for bad in ("lots", "8G; rm", "-1", "8 G"):
        with pytest.raises(launcher.LaunchError, match="not a valid MemoryHigh"):
            launcher.scope_properties(SCOPED, {"BRAVE_MEMORY_HIGH": bad})
    # An app without scope_env ignores the variables.
    assert "MemoryHigh=16G" in launcher.scope_properties(BRAVE_SPEC, {"BRAVE_MEMORY_HIGH": "1G"})


def test_the_user_stage_applies_a_scope_override() -> None:
    config = {**CONFIG, "apps": {"brave-browser": SCOPED}}
    command = launcher.user_stage_command(config, "brave-browser", [], {"BRAVE_MEMORY_MAX": "12G"}, True)
    assert "MemoryMax=12G" in command and "MemoryHigh=16G" in command


def test_root_stage_refuses_a_command_that_is_its_own_shim(root_stage, tmp_path: Path) -> None:
    (root_stage.run / "pia").write_text("")
    shim = tmp_path / "brave-shim"
    shim.write_text(launcher.shim_text("brave-browser", "/usr/local/bin/curiosity-pia-run"))
    config = json.loads(root_stage.config.read_text())
    config["apps"]["brave-browser"]["command"] = [str(shim)]
    root_stage.config.write_text(json.dumps(config))
    with pytest.raises(launcher.LaunchError, match="is a PIA shim"):
        launcher.run_root("brave-browser", [], [])
    assert root_stage.executed == []


# --- every launch by name or path goes through the tunnel -----------------------

needs_dpkg = pytest.mark.skipif(shutil.which("dpkg-divert") is None, reason="dpkg-divert not installed")
needs_alternatives = pytest.mark.skipif(shutil.which("update-alternatives") is None, reason="update-alternatives not installed")


@pytest.fixture
def pkg(tree, tmp_path: Path):
    """The package's launcher symlink at an absolute path, with a diverted entry configured."""
    real = tmp_path / "opt/brave-browser"
    real.parent.mkdir(parents=True)
    real.write_text("#!/bin/sh\necho real brave \"$@\"\n")
    real.chmod(0o755)
    package_link = tmp_path / "usr-bin/brave-browser-stable"
    package_link.parent.mkdir()
    package_link.symlink_to(real)
    spec = tree.config["apps"]["brave-browser"]
    spec["command"] = [str(real)]
    spec["diverted"] = [str(package_link)]
    spec["legacy_wrappers"] = [".local/bin/brave-capped"]
    tree.real, tree.link = real, package_link
    return tree


def _divert_list() -> str:
    return subprocess.run(
        ["dpkg-divert", "--admindir", launcher.DPKG_ADMINDIR, "--list"], capture_output=True, text=True, check=True
    ).stdout


@needs_dpkg
def test_sync_diverts_the_package_symlink_and_puts_the_shim_in_its_place(pkg) -> None:
    changes = launcher.sync(pkg.config, [pkg.home], remove_all=False)
    assert any(f"diverted {pkg.link}" in c for c in changes)
    assert pkg.link.read_text().splitlines()[-1] == 'exec /usr/local/bin/curiosity-pia-run brave-browser "$@"'
    assert not pkg.link.is_symlink() and pkg.link.stat().st_mode & 0o777 == 0o755
    distrib = pkg.link.with_name(pkg.link.name + ".distrib")
    assert distrib.is_symlink() and distrib.resolve() == pkg.real  # the package's file, kept
    assert f"diversion of {pkg.link} to {distrib} by curiosity-pia-tunnel" in _divert_list()


@needs_dpkg
def test_sync_diversion_is_idempotent_and_survives_a_package_upgrade(pkg) -> None:
    launcher.sync(pkg.config, [pkg.home], remove_all=False)
    assert launcher.sync(pkg.config, [pkg.home], remove_all=False) == []
    # dpkg unpacks an upgrade to the diverted name, never over the shim.
    distrib = pkg.link.with_name(pkg.link.name + ".distrib")
    distrib.unlink()
    distrib.symlink_to(pkg.real)
    assert launcher.sync(pkg.config, [pkg.home], remove_all=False) == []
    assert "exec /usr/local/bin/curiosity-pia-run" in pkg.link.read_text()


@needs_dpkg
def test_leaving_the_app_list_gives_the_path_back_to_the_package(pkg) -> None:
    launcher.sync(pkg.config, [pkg.home], remove_all=False)
    changes = launcher.sync({"apps": {}}, [pkg.home], remove_all=False)
    assert any("restored" in c for c in changes)
    assert pkg.link.is_symlink() and pkg.link.resolve() == pkg.real
    assert not pkg.link.with_name(pkg.link.name + ".distrib").exists()
    assert "curiosity-pia-tunnel" not in _divert_list()
    assert launcher.sync({"apps": {}}, [pkg.home], remove_all=False) == []


@needs_dpkg
def test_remove_all_restores_the_diverted_path_too(pkg) -> None:
    launcher.sync(pkg.config, [pkg.home], remove_all=False)
    launcher.sync(pkg.config, [pkg.home], remove_all=True)
    assert pkg.link.is_symlink() and pkg.link.resolve() == pkg.real


@needs_dpkg
def test_diversions_belonging_to_other_packages_are_never_touched(pkg) -> None:
    other = pkg.link.parent / "other-file"
    other.write_text("x")
    subprocess.run(
        ["dpkg-divert", "--admindir", launcher.DPKG_ADMINDIR, "--package", "somepkg", "--add", "--rename",
         "--divert", str(other) + ".distrib", str(other)], check=True, capture_output=True,
    )
    launcher.sync(pkg.config, [pkg.home], remove_all=False)
    launcher.sync({"apps": {}}, [pkg.home], remove_all=True)
    assert "by somepkg" in _divert_list() and not other.exists()


@needs_dpkg
def test_a_missing_package_file_is_reported_not_diverted(pkg) -> None:
    pkg.link.unlink()
    changes = launcher.sync(pkg.config, [pkg.home], remove_all=False)
    assert any(c.startswith("warning:") and "nothing to divert" in c for c in changes)
    assert "curiosity-pia-tunnel" not in _divert_list()


def test_unsafe_diverted_paths_are_refused(tree) -> None:
    for bad in ("relative/path", "/usr/bin/../../etc/passwd", "/usr/bin/x.distrib", "/usr/bin/a b", "/usr/bin/x;y"):
        tree.config["apps"]["brave-browser"]["diverted"] = [bad]
        with pytest.raises(launcher.LaunchError, match="invalid diverted path"):
            launcher.sync(tree.config, [tree.home], remove_all=False)


@needs_dpkg
@needs_alternatives
def test_the_browser_alternatives_resolve_to_the_tunnel_shim(pkg, tmp_path: Path) -> None:
    """x-www-browser and gnome-www-browser name the package path, so they follow the diversion."""
    altdir, admin = tmp_path / "etc-alternatives", tmp_path / "alt-admin"
    altdir.mkdir()
    admin.mkdir()
    for name in ("x-www-browser", "gnome-www-browser"):
        subprocess.run(
            ["update-alternatives", "--altdir", str(altdir), "--admindir", str(admin), "--install",
             str(tmp_path / name), name, str(pkg.link), "201"], check=True, capture_output=True,
        )
        assert (altdir / name).resolve() == pkg.real  # before: the real Brave, a bypass
    launcher.sync(pkg.config, [pkg.home], remove_all=False)
    for name in ("x-www-browser", "gnome-www-browser"):
        target = (altdir / name).resolve()
        assert launcher.marker_app(target) == "brave-browser", f"{name} still bypasses the tunnel"
    launcher.sync({"apps": {}}, [pkg.home], remove_all=False)
    for name in ("x-www-browser", "gnome-www-browser"):
        assert (altdir / name).resolve() == pkg.real  # restored when Brave is not a tunnel app


# --- the old brave-capped wrapper -------------------------------------------

HAND_MADE = "#!/usr/bin/env bash\nexec systemd-run --user --scope /usr/bin/brave-browser-stable \"$@\"\n"


def test_a_hand_made_wrapper_is_replaced_by_a_shim_and_backed_up(tree) -> None:
    tree.config["apps"]["brave-browser"]["legacy_wrappers"] = [".local/bin/brave-capped"]
    wrapper = tree.home / ".local/bin/brave-capped"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text(HAND_MADE)
    wrapper.chmod(0o755)
    changes = launcher.sync(tree.config, [tree.home], remove_all=False)
    assert any("moved aside" in c for c in changes)
    assert wrapper.read_text().splitlines()[-1] == 'exec /usr/local/bin/curiosity-pia-run brave-browser "$@"'
    assert wrapper.stat().st_mode & 0o777 == 0o755
    assert wrapper.with_name("brave-capped.pre-pia-tunnel").read_text() == HAND_MADE
    assert launcher.sync(tree.config, [tree.home], remove_all=False) == []


def test_a_wrapper_the_user_does_not_have_is_not_created(tree) -> None:
    tree.config["apps"]["brave-browser"]["legacy_wrappers"] = [".local/bin/brave-capped"]
    launcher.sync(tree.config, [tree.home], remove_all=False)
    assert not (tree.home / ".local/bin/brave-capped").exists()


def test_the_wrapper_comes_back_when_the_app_leaves_the_list(tree) -> None:
    tree.config["apps"]["brave-browser"]["legacy_wrappers"] = [".local/bin/brave-capped"]
    wrapper = tree.home / ".local/bin/brave-capped"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text(HAND_MADE)
    launcher.sync(tree.config, [tree.home], remove_all=False)
    launcher.sync({"apps": {}}, [tree.home], remove_all=False)
    assert wrapper.read_text() == HAND_MADE
    assert not wrapper.with_name("brave-capped.pre-pia-tunnel").exists()
    assert launcher.sync({"apps": {}}, [tree.home], remove_all=False) == []


def test_other_scripts_in_local_bin_are_left_alone(tree) -> None:
    mine = tree.home / ".local/bin/my-tool"
    mine.parent.mkdir(parents=True)
    mine.write_text("#!/bin/sh\necho hi\n")
    launcher.sync(tree.config, [tree.home], remove_all=False)
    launcher.sync({"apps": {}}, [tree.home], remove_all=True)
    assert mine.read_text() == "#!/bin/sh\necho hi\n"


def test_unsafe_legacy_wrapper_paths_are_refused(tree) -> None:
    for bad in ("../outside", "/etc/passwd", ".local/../../x"):
        tree.config["apps"]["brave-browser"]["legacy_wrappers"] = [bad]
        with pytest.raises(launcher.LaunchError, match="invalid legacy wrapper path"):
            launcher.sync(tree.config, [tree.home], remove_all=False)


# --------------------------------------------------------------------------
# Refusal feedback: the person is told on the desktop and in the journal
# --------------------------------------------------------------------------


@pytest.fixture
def feedback(root_stage, monkeypatch: pytest.MonkeyPatch):
    notices: list[str] = []
    journal: list[tuple[str, int]] = []
    monkeypatch.setattr(launcher, "notify_user", lambda uid, gid, env, message: notices.append(message))
    monkeypatch.setattr(launcher, "log_event", lambda message, priority=6: journal.append((message, priority)))
    root_stage.notices, root_stage.journal = notices, journal
    return root_stage


def test_a_stray_app_gets_a_plain_sentence_notice_and_a_journal_entry(feedback, tmp_path: Path) -> None:
    (feedback.run / "pia").write_text("")
    host_ns, exe = tmp_path / "host-ns", tmp_path / "brave"
    host_ns.write_text("")
    exe.write_text("")
    _proc(feedback, 100, str(exe), b"brave\0", host_ns)
    config = json.loads(feedback.config.read_text())
    config["apps"]["brave-browser"]["guard_exe"] = str(exe)
    feedback.config.write_text(json.dumps(config))
    with pytest.raises(launcher.LaunchError) as raised:
        launcher.run_root("brave-browser", [], [])
    assert raised.value.code == launcher.EXIT_STRAY
    assert feedback.notices == [
        "Brave is already open without the VPN. Quit Brave (Ctrl+Shift+Q) and open it again to use PIA."
    ]
    assert len(feedback.journal) == 1
    line, priority = feedback.journal[0]
    assert line.startswith("refused:") and "pid 100" in line and priority == launcher.syslog.LOG_WARNING


def test_a_missing_namespace_gets_a_notice_and_a_journal_entry(feedback) -> None:
    with pytest.raises(launcher.LaunchError) as raised:
        launcher.run_root("brave-browser", [], [])
    assert raised.value.code == launcher.EXIT_NO_NAMESPACE
    assert feedback.notices == ["The PIA tunnel is down, so Brave was not started. It would run without the VPN otherwise."]
    assert feedback.journal and feedback.journal[0][0].startswith("refused:") and "'pia' does not exist" in feedback.journal[0][0]


def test_a_down_tunnel_interface_is_announced_but_the_app_still_starts_offline(feedback) -> None:
    (feedback.run / "pia").write_text("")
    (Path(launcher.SAFE_PATH) / "ip").write_text("#!/bin/sh\nexit 1\n")  # `ip -n pia link show wgpia` fails
    launcher.run_root("brave-browser", [], [])
    assert feedback.notices == ["The PIA tunnel is down; Brave stays offline until it reconnects."]
    assert feedback.executed, "fail-closed: the app starts inside the closed namespace, it never runs outside"
    assert any("tunnel interface wgpia is down" in line for line, _ in feedback.journal)


def test_a_healthy_tunnel_launch_is_silent(feedback) -> None:
    (feedback.run / "pia").write_text("")
    launcher.run_root("brave-browser", [], [])
    assert feedback.notices == [] and feedback.journal == []


def test_the_default_wording_uses_the_app_name(feedback, tmp_path: Path) -> None:
    config = json.loads(feedback.config.read_text())
    spec = config["apps"]["brave-browser"]
    for key in ("display_name", "quit_hint"):
        spec.pop(key)
    feedback.config.write_text(json.dumps(config))
    with pytest.raises(launcher.LaunchError):
        launcher.run_root("brave-browser", [], [])
    assert feedback.notices == ["The PIA tunnel is down, so brave-browser was not started. It would run without the VPN otherwise."]


def test_notify_user_runs_notify_send_as_the_user_with_only_the_session_variables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tools = tmp_path / "recording-tools"
    tools.mkdir()
    (tools / "setpriv").write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        "open(os.environ['RECORD'], 'w').write(json.dumps({'argv': sys.argv[1:], 'env': dict(os.environ)}))\n"
    )
    (tools / "notify-send").write_text("#!/bin/sh\n")
    for tool in tools.iterdir():
        tool.chmod(0o755)
    record = tmp_path / "record.json"
    monkeypatch.setattr(launcher, "SAFE_PATH", str(tools))
    # subprocess is given only the cleaned env, so RECORD must be one of the kept names to reach the fake.
    real_run = subprocess.run  # the launcher shares this module, so capture it before patching
    monkeypatch.setattr(launcher.subprocess, "run", lambda argv, env, **kw: real_run(
        argv, env={**env, "RECORD": str(record)}, **kw))
    launcher.notify_user(
        1001, 1002, {"DISPLAY": ":0", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1001/bus", "SECRET": "x"}, "hello"
    )
    recorded = json.loads(record.read_text())
    assert recorded["argv"][:4] == ["--reuid=1001", "--regid=1002", "--init-groups", "--"]
    assert recorded["argv"][4].endswith("notify-send") and recorded["argv"][-1] == "hello"
    assert recorded["env"]["DISPLAY"] == ":0" and "SECRET" not in recorded["env"]


def test_notify_user_without_the_tools_is_silent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(launcher, "SAFE_PATH", str(tmp_path))
    launcher.notify_user(1000, 1000, {}, "nobody hears this")  # must not raise


# --------------------------------------------------------------------------
# qBittorrent: definition, config sync, retirement of the hand-made helper
# --------------------------------------------------------------------------

QBT_CONF = """[AddNewTorrentDialog]
Enabled=true

[BitTorrent]
Session\\DefaultSavePath=/home/u/Downloads
Session\\Interface=wgpia0
Session\\InterfaceAddress=
Session\\InterfaceName=wgpia0
Session\\Port=41083
Session\\SSL\\Port=49462

[Network]
PortForwardingEnabled=false

[Preferences]
Connection\\Interface=wgpia0
Connection\\InterfaceAddress=
Connection\\InterfaceName=wgpia0
Connection\\PortRangeMin=41083
Connection\\UPnP=false
WebUI\\Enabled=false
"""


def test_qbittorrent_definition_is_complete_and_cannot_recurse() -> None:
    qbt = DEFINITIONS["qbittorrent"]
    assert qbt["packages"] == ["qbittorrent"]
    # The ELF at /usr/bin/qbittorrent is diverted; the launcher runs the diverted binary.
    assert qbt["diverted"] == ["/usr/bin/qbittorrent"]
    assert qbt["command"] == ["/usr/bin/qbittorrent.distrib"] == [qbt["guard_exe"]]
    assert qbt["command"][0] not in qbt["diverted"]
    assert qbt["desktop_ids"] == ["org.qbittorrent.qBittorrent.desktop"]
    assert "qbittorrent" in qbt["desktop_commands"]  # the packaged entry says `Exec=qbittorrent %U`
    assert qbt["port_forward"] == {"config": ".config/qBittorrent/qBittorrent.conf"}
    assert qbt["retire_files"] == [".local/bin/pia-qbt-port-sync"]
    assert qbt["retire_user_units"] == ["pia-qbt-port-sync.service", "pia-qbt-port-sync.timer"]
    assert DEFINITIONS["brave-browser"].get("port_forward") is None


def test_the_packaged_qbittorrent_entry_is_routed_through_the_launcher() -> None:
    entry = "[Desktop Entry]\nName=qBittorrent\nExec=qbittorrent %U\nMimeType=application/x-bittorrent;x-scheme-handler/magnet;\n"
    out = launcher.wrap_desktop(entry, "qbittorrent", DEFINITIONS["qbittorrent"], "/usr/local/bin/curiosity-pia-run", "src")
    assert "Exec=/usr/local/bin/curiosity-pia-run qbittorrent %U" in out
    assert "Exec=qbittorrent" not in out


def test_port_forward_defaults_on_exactly_when_a_routed_app_takes_the_port() -> None:
    env = jinja2.Environment()
    env.filters["extract"] = lambda key, container: container[key]  # Ansible's `extract`
    template = env.from_string(DEFAULTS["pia_tunnel_port_forward"])
    for apps, expected in ((["brave-browser"], False), (["qbittorrent"], True), (["brave-browser", "qbittorrent"], True), ([], False)):
        rendered = template.render(pia_tunnel_apps=apps, pia_tunnel_app_definitions=DEFINITIONS)
        assert (rendered.strip() == "True") is expected, apps


def test_the_config_template_carries_the_port_forward_settings() -> None:
    rendered = json.loads(
        _env().get_template("pia-tunnel-config.json.j2").render(
            **_vars(pia_tunnel_apps=["brave-browser", "qbittorrent"], pia_tunnel_port_forward=True)
        )
    )
    assert rendered["port_forward"] is True
    assert rendered["homes"] == ["/home/mrh", "/home/second"]
    qbt = rendered["apps"]["qbittorrent"]
    assert qbt["port_forward"] == {"config": ".config/qBittorrent/qBittorrent.conf"}
    assert qbt["display_name"] == "qBittorrent" and qbt["quit_hint"] == "Quit qBittorrent (Ctrl+Q)"
    assert qbt["retire_user_units"] == ["pia-qbt-port-sync.service", "pia-qbt-port-sync.timer"]
    assert rendered["apps"]["brave-browser"]["port_forward"] == {}


def test_the_bind_units_run_bind_then_the_port_sync() -> None:
    env = _env()
    service = env.get_template("curiosity-pia-tunnel-bind.service.j2").render(**_vars())
    assert "ExecStart=/usr/local/sbin/curiosity-pia-tunnel bind\n" in service
    assert "ExecStartPost=/usr/local/bin/curiosity-pia-run port-sync\n" in service
    assert "Type=oneshot" in service and "After=curiosity-pia-tunnel.service" in service
    timer = env.get_template("curiosity-pia-tunnel-bind.timer.j2").render(**_vars())
    assert "OnUnitActiveSec=10min" in timer and "WantedBy=timers.target" in timer


def test_the_tasks_install_packages_units_and_retire_the_old_helper() -> None:
    text = TASKS.read_text()
    assert "_pia_tunnel_routed_packages" in text and "curiosity-pia-tunnel-bind.timer" in text
    assert "pia_tunnel_bind_interval is match" in text  # PIA drops a binding after 15 minutes
    assert "Stop the retired hand-made user units" in text
    # Removing the tunnel removes the bind units too.
    assert text.count("curiosity-pia-tunnel-bind.service") >= 3


def test_the_qbittorrent_config_gets_the_port_and_loses_the_interface_binding() -> None:
    out = launcher.qbittorrent_config(QBT_CONF, 52311)
    lines = out.split("\n")
    assert "Session\\Port=52311" in lines and "Connection\\PortRangeMin=52311" in lines
    for key in ("Session\\Interface", "Session\\InterfaceName", "Connection\\Interface", "Connection\\InterfaceName"):
        assert f"{key}=" in lines, key  # present and empty
    assert "wgpia0" not in out
    # Nothing else moves: other sections, the SSL port and the WebUI switch.
    assert "Session\\SSL\\Port=49462" in lines and "WebUI\\Enabled=false" in lines
    assert "[Network]" in lines and "PortForwardingEnabled=false" in lines
    assert launcher.qbittorrent_config(out, 52311) == out  # idempotent


def test_the_qbittorrent_config_edit_adds_missing_keys_and_sections() -> None:
    out = launcher.qbittorrent_config("[GUI]\nx=1\n", 40000)
    assert "[BitTorrent]\nSession\\Port=40000" in out and "[Preferences]\nConnection\\PortRangeMin=40000" in out
    assert out.startswith("[GUI]\nx=1\n")
    # A key goes into its own section, before the next one, not at the end of the file.
    partial = launcher.qbittorrent_config("[BitTorrent]\nSession\\Foo=1\n\n[Preferences]\nA=1\n", 40001).split("\n")
    assert partial.index("Session\\Port=40001") < partial.index("[Preferences]")
    assert partial.index("Connection\\PortRangeMin=40001") > partial.index("[Preferences]")
    # A random listen port would ignore the forwarded one.
    assert "Session\\UseRandomPort=false" in launcher.qbittorrent_config("[BitTorrent]\nSession\\UseRandomPort=true\n", 40002)


def test_without_a_forwarded_port_only_the_binding_is_cleared() -> None:
    out = launcher.qbittorrent_config(QBT_CONF, None)
    assert "Session\\Port=41083" in out and "wgpia0" not in out


@pytest.fixture
def qbt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    home = tmp_path / "home/u"
    conf = home / ".config/qBittorrent/qBittorrent.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(QBT_CONF)
    conf.chmod(0o600)
    proc, state, runtime = tmp_path / "proc", tmp_path / "state", tmp_path / "runtime"
    for d in (proc, state, runtime):
        d.mkdir()
    exe = tmp_path / "qbittorrent.distrib"
    exe.write_text("")
    monkeypatch.setattr(launcher, "PROC_ROOT", proc)
    monkeypatch.setattr(launcher, "STATE_DIR", state)
    monkeypatch.setattr(launcher, "RUNTIME_ROOT", runtime)
    monkeypatch.setattr(launcher, "log_event", lambda *a, **k: None)
    spec = {
        "command": [str(exe)], "guard_exe": str(exe), "display_name": "qBittorrent",
        "quit_hint": "Quit qBittorrent (Ctrl+Q)", "port_forward": {"config": ".config/qBittorrent/qBittorrent.conf"},
    }
    config = {"netns": "pia", "interface": "wgpia", "homes": [str(home)], "apps": {"qbittorrent": spec}}
    return type("Qbt", (), {"home": home, "conf": conf, "proc": proc, "state": state, "runtime": runtime,
                            "exe": exe, "spec": spec, "config": config, "tmp": tmp_path})()


def _run_qbt(q, pid: int = 4242) -> None:
    ns = q.tmp / "some-ns"
    ns.write_text("")
    _proc(type("S", (), {"proc": q.proc})(), pid, str(q.exe), b"qbittorrent\0", ns)


def test_the_sync_writes_the_port_keeps_the_mode_and_backs_up_once(qbt) -> None:
    lines = launcher.sync_app_port("qbittorrent", qbt.spec, qbt.home, os.getuid(), 52311)
    assert lines == [f"changed: qbittorrent listen port 41083 -> 52311 in {qbt.conf}"]
    assert "Session\\Port=52311" in qbt.conf.read_text()
    assert qbt.conf.stat().st_mode & 0o777 == 0o600
    backup = qbt.conf.with_name("qBittorrent.conf.pre-pia-tunnel")
    assert backup.read_text() == QBT_CONF
    launcher.sync_app_port("qbittorrent", qbt.spec, qbt.home, os.getuid(), 52312)
    assert backup.read_text() == QBT_CONF  # the first, hand-made version is what stays
    assert launcher.sync_app_port("qbittorrent", qbt.spec, qbt.home, os.getuid(), 52312) == [
        f"unchanged: qbittorrent listens on 52312 for u"
    ]


def test_the_sync_never_edits_a_running_app_and_reports_it_once(qbt) -> None:
    _run_qbt(qbt)
    told: list[int] = []
    lines = launcher.sync_app_port("qbittorrent", qbt.spec, qbt.home, os.getuid(), 52311, told.append)
    assert qbt.conf.read_text() == QBT_CONF  # qBittorrent rewrites it on exit; we must not race it
    assert told == [52311] and lines[0].startswith("deferred: qbittorrent is running")


def test_a_missing_config_is_left_for_the_first_run(qbt) -> None:
    qbt.conf.unlink()
    assert launcher.sync_app_port("qbittorrent", qbt.spec, qbt.home, os.getuid(), 52311) == []
    assert not qbt.conf.exists()


def test_apps_without_a_port_forward_entry_are_not_touched(qbt) -> None:
    assert launcher.sync_app_port("brave-browser", BRAVE_SPEC, qbt.home, os.getuid(), 52311) == []


def test_unsafe_port_forward_config_paths_are_refused(qbt) -> None:
    for bad in ("/etc/passwd", "../x", "a/../../b"):
        with pytest.raises(launcher.LaunchError):
            launcher.port_forward_config("qbittorrent", {"port_forward": {"config": bad}})


def test_the_forwarded_port_file_is_validated(qbt) -> None:
    assert launcher.read_forwarded_port() is None
    for text, expected in (("52311\n", 52311), ("80\n", None), ("junk\n", None), ("99999\n", None)):
        (qbt.state / "forwarded-port").write_text(text)
        assert launcher.read_forwarded_port() == expected, text


def test_the_launch_prepares_the_config_before_the_app_starts(qbt, monkeypatch: pytest.MonkeyPatch) -> None:
    (qbt.state / "forwarded-port").write_text("52311\n")
    monkeypatch.setattr(launcher.pwd, "getpwuid", lambda _uid: type("P", (), {"pw_dir": str(qbt.home)})())
    launcher.prepare_app_for_launch(qbt.config, "qbittorrent")
    assert "Session\\Port=52311" in qbt.conf.read_text() and "wgpia0" not in qbt.conf.read_text()


def test_a_launch_without_a_forwarded_port_still_clears_the_dead_interface_binding(qbt, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher.pwd, "getpwuid", lambda _uid: type("P", (), {"pw_dir": str(qbt.home)})())
    launcher.prepare_app_for_launch(qbt.config, "qbittorrent")
    text = qbt.conf.read_text()
    assert "Session\\Port=41083" in text and "wgpia0" not in text


def test_a_failing_prepare_never_blocks_the_launch(qbt, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_uid):
        raise KeyError("no account")
    monkeypatch.setattr(launcher.pwd, "getpwuid", broken)
    launcher.prepare_app_for_launch(qbt.config, "qbittorrent")  # logs, does not raise


def test_the_user_stage_prepares_the_app_then_goes_through_sudo(qbt, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(launcher, "load_config", lambda: qbt.config)
    monkeypatch.setattr(launcher, "prepare_app_for_launch", lambda config, app: seen.append(app))
    monkeypatch.setattr(launcher, "systemd_user_available", lambda: False)
    monkeypatch.setattr(launcher.os, "execvp", lambda cmd, argv: seen.append(" ".join(argv[:3])))
    launcher.run_user("qbittorrent", ["magnet:?xt=urn:btih:x"])
    assert seen[0] == "qbittorrent" and seen[1].startswith("sudo -n /usr/local/bin/curiosity-pia-run")


def test_port_sync_updates_every_home_and_tells_the_user_once_when_the_app_runs(qbt, monkeypatch: pytest.MonkeyPatch) -> None:
    (qbt.state / "forwarded-port").write_text("52311\n")
    uid = os.getuid()
    (qbt.runtime / str(uid)).mkdir()
    (qbt.runtime / str(uid) / "bus").write_text("")
    notices: list[tuple[int, dict, str]] = []
    monkeypatch.setattr(launcher, "notify_user", lambda u, g, env, message: notices.append((u, dict(env), message)))
    _run_qbt(qbt)
    first = launcher.port_sync(qbt.config, [qbt.home])
    second = launcher.port_sync(qbt.config, [qbt.home])
    assert first[0].startswith("deferred:") and second[0].startswith("deferred:")
    assert len(notices) == 1, "the 10-minute timer must not repeat the same notice"
    assert notices[0][1]["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={qbt.runtime / str(uid) / 'bus'}"
    assert notices[0][2] == "PIA gave qBittorrent a new port (52311). Quit qBittorrent (Ctrl+Q) and open it again to use it."
    # A new port is a new notice.
    (qbt.state / "forwarded-port").write_text("52400\n")
    launcher.port_sync(qbt.config, [qbt.home])
    assert len(notices) == 2 and "52400" in notices[1][2]


def test_port_sync_with_the_app_closed_just_writes_the_port(qbt, monkeypatch: pytest.MonkeyPatch) -> None:
    (qbt.state / "forwarded-port").write_text("52311\n")
    monkeypatch.setattr(launcher, "notify_user", lambda *a: pytest.fail("nothing to tell: the app is not running"))
    assert launcher.port_sync(qbt.config, [qbt.home])[0].startswith("changed:")
    assert "Session\\Port=52311" in qbt.conf.read_text()


def test_port_sync_without_a_bound_port_says_so_and_changes_no_port(qbt) -> None:
    lines = launcher.port_sync(qbt.config, [qbt.home])
    assert lines[0] == "no forwarded port is bound; the apps keep the port they have"
    assert "Session\\Port=41083" in qbt.conf.read_text()


def test_the_cli_port_sync_command_prints_and_uses_the_configured_homes(qbt, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    (qbt.state / "forwarded-port").write_text("52311\n")
    config = tmp_path / "config.json"
    config.write_text(json.dumps(qbt.config))
    monkeypatch.setattr(launcher, "CONFIG_PATH", config)
    monkeypatch.setenv("CURIOSITY_PIA_ALLOW_NON_ROOT", "1")
    assert launcher.main(["port-sync"]) == 0
    assert "listen port 41083 -> 52311" in capsys.readouterr().out


def test_port_sync_needs_root(qbt, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.delenv("CURIOSITY_PIA_ALLOW_NON_ROOT", raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert launcher.main(["port-sync"]) == launcher.EXIT_USAGE
    assert "must run as root" in capsys.readouterr().err


def test_an_app_that_is_diverted_counts_as_installed_before_the_diversion(tmp_path: Path) -> None:
    package_file = tmp_path / "qbittorrent"
    package_file.write_text("")
    spec = {"command": [str(package_file) + ".distrib"], "diverted": [str(package_file)]}
    assert launcher.installed(spec)  # otherwise nothing would ever divert it
    assert not launcher.installed({"command": [str(tmp_path / "absent")], "diverted": [str(tmp_path / "absent2")]})


@pytest.fixture
def qbt_pkg(tree, tmp_path: Path):
    """qBittorrent as dpkg ships it: one ELF at its final path, diverted to .distrib."""
    (tree.system / "org.qbittorrent.qBittorrent.desktop").write_text(
        "[Desktop Entry]\nName=qBittorrent\nExec=qbittorrent %U\nMimeType=x-scheme-handler/magnet;\n"
    )
    elf = tmp_path / "usr-bin/qbittorrent"
    elf.parent.mkdir()
    elf.write_text("#!/bin/sh\necho real qbittorrent\n")
    elf.chmod(0o755)
    tree.config["apps"]["qbittorrent"] = {
        **json.loads(json.dumps(DEFINITIONS["qbittorrent"])),
        "command": [str(elf) + ".distrib"], "guard_exe": str(elf) + ".distrib", "diverted": [str(elf)],
        "desktop_commands": ["qbittorrent", str(elf)],
    }
    tree.config["apps"].pop("brave-browser")
    tree.elf = elf
    return tree


@needs_dpkg
def test_sync_diverts_the_qbittorrent_binary_and_the_shim_runs_the_launcher(qbt_pkg) -> None:
    changes = launcher.sync(qbt_pkg.config, [qbt_pkg.home], remove_all=False)
    assert any("diverted" in c for c in changes)
    diverted = Path(str(qbt_pkg.elf) + ".distrib")
    assert "echo real qbittorrent" in diverted.read_text()  # the real program is kept
    assert launcher.marker_app(qbt_pkg.elf) == "qbittorrent"  # its old path is the shim
    assert "Exec=/usr/local/bin/curiosity-pia-run qbittorrent %U" in (qbt_pkg.override / "org.qbittorrent.qBittorrent.desktop").read_text()
    assert launcher.sync(qbt_pkg.config, [qbt_pkg.home], remove_all=False) == []  # idempotent
    # The recursion guard accepts the diverted binary (it is not a shim).
    assert launcher.marker_app(diverted) is None
    launcher.sync(qbt_pkg.config, [qbt_pkg.home], remove_all=True)
    assert "echo real qbittorrent" in qbt_pkg.elf.read_text()


def test_sync_retires_the_hand_made_port_sync_and_its_units(qbt_pkg) -> None:
    home = qbt_pkg.home
    helper = home / ".local/bin/pia-qbt-port-sync"
    units = home / ".config/systemd/user"
    (units / "timers.target.wants").mkdir(parents=True)
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_text("#!/usr/bin/env bash\npiactl get portforward\n")
    for unit in ("pia-qbt-port-sync.service", "pia-qbt-port-sync.timer"):
        (units / unit).write_text("[Unit]\nDescription=old\n")
    (units / "timers.target.wants/pia-qbt-port-sync.timer").symlink_to("../pia-qbt-port-sync.timer")
    (units / "other.timer").write_text("[Unit]\n")
    changes = launcher.sync(qbt_pkg.config, [home], remove_all=False)
    assert not helper.exists() and (helper.parent / "pia-qbt-port-sync.pre-pia-tunnel").read_text().endswith("portforward\n")
    assert not (units / "pia-qbt-port-sync.timer").exists() and (units / "pia-qbt-port-sync.timer.pre-pia-tunnel").exists()
    assert not (units / "timers.target.wants/pia-qbt-port-sync.timer").is_symlink()
    assert (units / "other.timer").exists()  # nothing else is touched
    assert any("retired unit" in c for c in changes)  # what the playbook keys the live stop on
    assert launcher.sync(qbt_pkg.config, [home], remove_all=False) == []  # nothing left to retire


def test_unsafe_retire_entries_are_refused(tree) -> None:
    for bad in ({"retire_files": ["../x"]}, {"retire_user_units": ["../evil.service"]}, {"retire_user_units": ["a.sh"]}):
        with pytest.raises(launcher.LaunchError):
            launcher.retire_paths("x", bad)


# --------------------------------------------------------------------------
# Port forwarding in the tunnel manager
# --------------------------------------------------------------------------


@pytest.fixture
def pf_rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path, region="nl_amsterdam", port_forward=True)


def _api_calls(rig: Rig, endpoint: str) -> list[list[str]]:
    return [json.loads(line) for line in rig.read("curl.argv").splitlines() if line.endswith(f"/{endpoint}\"]")]


def test_up_gets_and_binds_a_forwarded_port_from_inside_the_namespace(pf_rig: Rig) -> None:
    result = pf_rig.run("up")
    assert result.returncode == 0, result.stderr
    assert (pf_rig.state / "forwarded-port").read_text() == "47047\n"
    assert (pf_rig.state / "forwarded-port").stat().st_mode & 0o777 == 0o644
    assert "forwarded port 47047 bound" in result.stdout
    for endpoint in ("getSignature", "bindPort"):
        (call,) = _api_calls(pf_rig, endpoint)
        assert f"https://amsterdam441:19999/{endpoint}" == call[-1]
        assert call[call.index("--connect-to") + 1] == "amsterdam441::10.0.0.1:"  # the in-tunnel gateway
        assert "--cacert" in call
    # The calls are made through `ip netns exec`, never from the host namespace.
    execs = pf_rig.read("exec.calls").splitlines()
    assert sum(1 for line in execs if line.startswith("curl") and ":19999/" in line) == 2
    # The addKey call, in contrast, is made from the host (the tunnel does not exist yet).
    assert not any(":1337/addKey" in line for line in execs)


def test_the_port_grant_and_token_travel_only_through_stdin(pf_rig: Rig) -> None:
    result = pf_rig.run("up")
    stdin = pf_rig.read("curl.stdin")
    assert f"token={TOKEN}" in stdin and "payload=" in stdin and "signature=" in stdin
    grant = json.loads((pf_rig.persist / "portforward.json").read_text())
    visible = "\n".join([result.stdout, result.stderr, pf_rig.read("curl.argv"), pf_rig.read("ip.calls"), pf_rig.read("exec.calls")])
    for secret in (grant["payload"], grant["signature"], TOKEN):
        assert secret not in visible
    assert (pf_rig.persist / "portforward.json").stat().st_mode & 0o777 == 0o600
    assert pf_rig.persist.stat().st_mode & 0o777 == 0o700
    assert grant["port"] == 47047 and grant["server_cn"] == "amsterdam441"


def test_a_rebuilt_tunnel_to_the_same_server_keeps_its_port_without_a_new_signature(pf_rig: Rig) -> None:
    pf_rig.run("up")
    again = pf_rig.run("up")
    assert again.returncode == 0
    assert pf_rig.read("getsig.count") == "1"  # the stored grant was re-bound, not re-requested
    assert pf_rig.read("bind.count") == "2"
    assert (pf_rig.state / "forwarded-port").read_text() == "47047\n"


def test_a_grant_for_another_server_or_nearly_expired_is_replaced(pf_rig: Rig) -> None:
    pf_rig.run("up")
    grant_file = pf_rig.persist / "portforward.json"
    grant = json.loads(grant_file.read_text())
    grant["server_cn"] = "somewhere-else"
    grant_file.write_text(json.dumps(grant))
    assert pf_rig.run("up", FAKE_PF_PORT="48000").returncode == 0
    assert pf_rig.read("getsig.count") == "2" and (pf_rig.state / "forwarded-port").read_text() == "48000\n"
    grant = json.loads(grant_file.read_text())
    grant["expires_at"] = int(time.time()) + 3600  # under a day left
    grant_file.write_text(json.dumps(grant))
    assert pf_rig.run("up", FAKE_PF_PORT="48001").returncode == 0
    assert pf_rig.read("getsig.count") == "3" and (pf_rig.state / "forwarded-port").read_text() == "48001\n"


def test_bind_renews_with_the_stored_grant_and_needs_no_login(pf_rig: Rig) -> None:
    pf_rig.run("up")
    token_calls = pf_rig.read("curl.argv").count("gtoken")
    result = pf_rig.run("bind")
    assert result.returncode == 0 and "forwarded port 47047 bound" in result.stdout
    assert pf_rig.read("bind.count") == "2" and pf_rig.read("getsig.count") == "1"
    assert pf_rig.read("curl.argv").count("gtoken") == token_calls  # no new login for a plain renewal


def test_bind_falls_back_to_a_new_signature_when_pia_rejects_the_stored_one(pf_rig: Rig) -> None:
    pf_rig.run("up")
    (pf_rig.fake / "bind.count").write_text("0")  # the fake refuses the first bind of this run
    result = pf_rig.run("bind", FAKE_BIND_FAIL_FIRST="1", FAKE_PF_PORT="47999")
    assert result.returncode == 0, result.stderr
    assert "stored port grant was not accepted" in result.stdout
    assert pf_rig.read("getsig.count") == "2" and (pf_rig.state / "forwarded-port").read_text() == "47999\n"


def test_a_port_failure_never_takes_the_tunnel_down(pf_rig: Rig) -> None:
    result = pf_rig.run("up", FAKE_CURL_FAIL="getSignature")
    assert result.returncode == 0, "the tunnel works without a port; Brave must not lose its VPN over it"
    assert "port forwarding is not bound yet" in result.stdout
    assert not (pf_rig.state / "forwarded-port").exists()
    status = pf_rig.run("status")
    assert status.returncode == 0 and re.search(r"forwarded_port:\s+none", status.stdout)
    failed = pf_rig.run("bind", FAKE_CURL_FAIL="getSignature")
    assert failed.returncode == 1 and "getSignature" in failed.stderr  # the timer unit shows failed
    assert pf_rig.run("bind").returncode == 0 and (pf_rig.state / "forwarded-port").exists()  # and recovers


def _two_amsterdam_servers(rig: Rig) -> None:
    servers = json.loads(json.dumps(SERVERS))
    servers["regions"][1]["servers"]["wg"].append({"ip": "203.0.113.21", "cn": "amsterdam442"})
    (rig.fake / "servers.json").write_text(json.dumps(servers))


def _state(rig: Rig) -> dict[str, object]:
    return json.loads((rig.state / "state.json").read_text())


def test_a_server_without_a_reachable_port_api_is_dropped_for_another(pf_rig: Rig) -> None:
    _two_amsterdam_servers(pf_rig)
    result = pf_rig.run("up", FAKE_PF_DEAD="amsterdam441")
    assert result.returncode == 0, result.stderr
    assert "amsterdam441 has no reachable port forwarding API" in result.stdout
    assert "registered with Netherlands (amsterdam442)" in result.stdout
    assert _state(pf_rig)["server_cn"] == "amsterdam442"
    assert (pf_rig.state / "forwarded-port").read_text() == "47047\n"
    assert set(json.loads((pf_rig.state / "pf-unreachable.json").read_text())) == {"amsterdam441"}
    # A later rebuild stays away from the dead server even though it is listed first.
    again = pf_rig.run("up")
    assert again.returncode == 0 and _state(pf_rig)["server_cn"] == "amsterdam442"
    assert "has no reachable" not in again.stdout


def test_when_no_server_answers_the_tunnel_stays_up_and_the_attempts_are_bounded(pf_rig: Rig) -> None:
    result = pf_rig.run("up", FAKE_PF_DEAD="amsterdam441")
    assert result.returncode == 0, "Brave must keep its VPN even when no port can be had"
    assert result.stdout.count("registered with") == tunnel.PF_SERVER_ATTEMPTS
    assert "port forwarding is not bound yet" in result.stdout
    assert not (pf_rig.state / "forwarded-port").exists() and _state(pf_rig)["server_cn"] == "amsterdam441"


def test_bind_moves_the_tunnel_off_a_server_whose_port_api_went_away(pf_rig: Rig) -> None:
    _two_amsterdam_servers(pf_rig)
    assert pf_rig.run("up").returncode == 0 and _state(pf_rig)["server_cn"] == "amsterdam441"
    result = pf_rig.run("bind", FAKE_PF_DEAD="amsterdam441")
    assert result.returncode == 0, result.stderr
    assert "moving the tunnel to another server" in result.stdout
    assert _state(pf_rig)["server_cn"] == "amsterdam442"
    assert (pf_rig.state / "forwarded-port").read_text() == "47047\n"


def test_a_refusal_by_pia_is_not_mistaken_for_a_dead_server(pf_rig: Rig) -> None:
    pf_rig.run("up")
    failed = pf_rig.run("bind", FAKE_BIND_FAIL="1", FAKE_CURL_FAIL="getSignature")
    assert failed.returncode == 1 and not (pf_rig.state / "pf-unreachable.json").exists()


def test_dead_servers_are_avoided_for_a_limited_time(pf_rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tunnel, "STATE_DIR", pf_rig.state)
    pf_rig.state.mkdir(exist_ok=True)
    tunnel.mark_bad_server("amsterdam441")
    assert tunnel.bad_servers() == {"amsterdam441"}
    assert tunnel.bad_servers(time.time() + tunnel.PF_BAD_SERVER_TTL + 5) == set()
    tunnel.mark_bad_server("amsterdam442")  # prunes nothing live, keeps both
    assert tunnel.bad_servers() == {"amsterdam441", "amsterdam442"}


def test_excluded_servers_are_skipped_unless_nothing_else_is_left() -> None:
    servers = [
        tunnel.Server("nl_amsterdam", "Amsterdam", "203.0.113.20", "amsterdam441", port_forward=True),
        tunnel.Server("nl_amsterdam", "Amsterdam", "203.0.113.21", "amsterdam442", port_forward=True),
    ]
    fast = lambda ip, port: 0.01 if ip.endswith(".20") else 0.05  # noqa: E731
    assert tunnel.choose_server(servers, "nl_amsterdam", fast, port_forward=True).cn == "amsterdam441"
    assert tunnel.choose_server(servers, "nl_amsterdam", fast, port_forward=True, exclude={"amsterdam441"}).cn == "amsterdam442"
    assert tunnel.choose_server(servers, "auto", fast, port_forward=True, prefer_cn="amsterdam441", exclude={"amsterdam441"}).cn == "amsterdam442"
    both = {"amsterdam441", "amsterdam442"}
    assert tunnel.choose_server(servers, "nl_amsterdam", fast, port_forward=True, exclude=both).cn == "amsterdam441"


def test_status_shows_the_forwarded_port(pf_rig: Rig) -> None:
    pf_rig.run("up")
    status = pf_rig.run("status")
    assert re.search(r"forwarded_port:\s+47047 \(bound \d+s ago\)", status.stdout)
    columns = {len(line) - len(line.split(":", 1)[1].lstrip()) for line in status.stdout.splitlines()}
    assert len(columns) == 1, status.stdout


def test_down_unpublishes_the_port(pf_rig: Rig) -> None:
    pf_rig.run("up")
    assert pf_rig.run("down").returncode == 0
    assert not (pf_rig.state / "forwarded-port").exists()  # a stale port must not reach an app's config
    assert (pf_rig.persist / "portforward.json").exists()  # but the grant is kept for the next tunnel


def test_the_published_port_is_cleared_while_a_replacement_tunnel_is_built(pf_rig: Rig) -> None:
    pf_rig.run("up")
    assert pf_rig.run("up", FAKE_CURL_FAIL="privateinternetaccess.com").returncode == 1
    # a failed registration keeps the working tunnel (and its binding) - nothing was torn down
    assert (pf_rig.state / "forwarded-port").exists()


def test_bind_is_a_no_op_when_port_forwarding_is_off_or_the_tunnel_is_down(tmp_path: Path) -> None:
    (tmp_path / "off").mkdir()
    (tmp_path / "on").mkdir()
    off = Rig(tmp_path / "off", region="nl_amsterdam")
    off.run("up")
    result = off.run("bind")
    assert result.returncode == 0 and "port forwarding is off" in result.stdout
    assert off.read("bind.count") == ""
    on = Rig(tmp_path / "on", region="nl_amsterdam", port_forward=True)
    down = on.run("bind")  # nothing is up yet
    assert down.returncode == 0 and "not binding a port while the tunnel is down" in down.stdout
    assert on.read("bind.count") == ""


def test_without_port_forwarding_nothing_is_asked_of_pia_or_stored(tmp_path: Path) -> None:
    plain = Rig(tmp_path)
    assert plain.run("up").returncode == 0
    assert plain.read("getsig.count") == "" and not (plain.state / "forwarded-port").exists()
    assert not plain.persist.exists()
    assert "forwarded_port" not in plain.run("status").stdout


def test_a_region_without_port_forwarding_is_refused_with_the_ones_that_have_it(tmp_path: Path) -> None:
    rig = Rig(tmp_path, region="us_chicago", port_forward=True)
    result = rig.run("up")
    assert result.returncode == 1
    assert "does not offer port forwarding" in result.stderr and "nl_amsterdam" in result.stderr
    assert not (rig.fake / "ns-link").exists(), "no tunnel was built"


def test_only_port_forwarding_regions_qualify_and_the_previous_server_is_kept() -> None:
    servers = [
        tunnel.Server("us_chicago", "Chicago", "203.0.113.9", "chicago401"),
        tunnel.Server("nl_amsterdam", "Amsterdam", "203.0.113.20", "amsterdam441", port_forward=True),
        tunnel.Server("ca_ontario", "Ontario", "203.0.113.21", "ontario401", port_forward=True),
    ]
    latency = {"203.0.113.9": 0.001, "203.0.113.20": 0.09, "203.0.113.21": 0.02}
    fast = lambda ip, port: latency[ip]  # noqa: E731
    assert tunnel.choose_server(servers, "auto", fast).region_id == "us_chicago"  # without PF: nearest wins
    assert tunnel.choose_server(servers, "auto", fast, port_forward=True).region_id == "ca_ontario"
    # The port belongs to the server: stay on it while it answers, even when another is faster.
    assert tunnel.choose_server(servers, "auto", fast, port_forward=True, prefer_cn="amsterdam441").cn == "amsterdam441"
    # ... and move on when it does not.
    flaky = lambda ip, port: None if ip == "203.0.113.20" else latency[ip]  # noqa: E731
    assert tunnel.choose_server(servers, "auto", flaky, port_forward=True, prefer_cn="amsterdam441").cn == "ontario401"
    # A server that is not port-forwarding capable is never preferred.
    assert tunnel.choose_server(servers, "auto", fast, port_forward=True, prefer_cn="chicago401").cn == "ontario401"
    with pytest.raises(tunnel.TunnelError, match="does not offer port forwarding.*ca_ontario"):
        tunnel.choose_server(servers, "us_chicago", fast, port_forward=True)
    with pytest.raises(tunnel.TunnelError, match="no online region that offers port forwarding"):
        tunnel.choose_server(servers[:1], "auto", fast, port_forward=True)


def test_the_server_list_flags_port_forwarding_regions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tunnel, "curl", lambda url, *a, **k: json.dumps(SERVERS) + "\nc2ln\n")
    assert {s.region_id: s.port_forward for s in tunnel.fetch_servers()} == {"us_chicago": False, "nl_amsterdam": True}


def test_the_port_payload_is_decoded_and_bounded() -> None:
    def payload(body: dict) -> str:
        return base64.b64encode(json.dumps(body).encode()).decode()
    assert tunnel.decode_payload(payload({"port": 47047, "expires_at": "2026-12-30T12:34:56.789012345Z"})) == (
        47047, 1798634096,
    )
    for bad in (payload({"port": 80, "expires_at": "2026-12-30T12:34:56Z"}),
                payload({"port": 47047, "expires_at": "tomorrow"}),
                payload({"expires_at": "2026-12-30T12:34:56Z"}), "!!notbase64", base64.b64encode(b"not json").decode()):
        with pytest.raises(tunnel.TunnelError):
            tunnel.decode_payload(bad)


def test_the_curl_helper_can_run_inside_a_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(tunnel, "run", lambda argv, **kw: seen.append(list(argv)) or type("R", (), {"stdout": "{}"})())
    tunnel.curl("https://example.invalid/x", netns="pia")
    assert seen[0][:5] == ["ip", "netns", "exec", "pia", "curl"] and seen[0][-1] == "https://example.invalid/x"
    tunnel.curl("https://example.invalid/x")
    assert seen[1][0] == "curl"
