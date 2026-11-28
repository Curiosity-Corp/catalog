"""StreamStack from its signed apt repository, behind `streamstack_source` (catalog#183).

The default (`vendored`) must leave every existing host exactly as it was: the plan of the StreamStack task files
(tests/streamstack_plan.py: every task that can change a host, its rendered arguments, loops, file and template
digests) is compared with the snapshot taken from the tasks before catalog#183. The `apt` plan is checked for what
the issue asks: the pinned keyring, the signed-by source, the root-only credentials, the pinned package, the vendored
install removed, no second copy of the package's udev, modules-load or WirePlumber files, the role's own units kept
and pointing at /usr/bin, and one call-safe Console restart after a package change.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import jinja2
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from streamstack_plan import plan  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
SPECS = yaml.safe_load((ROLE / "meta/argument_specs.yml").read_text())["argument_specs"]["main"]["options"]
GOLDEN = ROOT / "tests/fixtures/streamstack_vendored_plan.json"
VENDORED_PACKAGING = ROLE / "files/streamstack/packaging"
BOOTSTRAP = ROOT / "scripts/bootstrap-streamstack-apt.sh"
NATIVE_RESTART = ROLE / "files/curiosity-streamstack-console-restart-native"
RESTART = ROLE / "files/curiosity-streamstack-console-restart"
HOST = "gitlab.example.test"


def _apt_plan() -> list[dict[str, Any]]:
    return plan("apt", {"streamstack_apt_gitlab_host": HOST})


def _entry(entries: list[dict[str, Any]], name: str) -> dict[str, Any]:
    (found,) = [e for e in entries if e["name"] == name]
    return found


def _bodies(entries: list[dict[str, Any]]) -> list[tuple[str, str, dict[str, Any]]]:
    """(task name, module, module arguments) for every item of every task."""
    found = []
    for entry in entries:
        for args in entry.get("items") or [entry["args"]]:
            body = args.get(entry["module"])
            found.append((entry["name"], entry["module"], body if isinstance(body, dict) else {"_raw": body}))
    return found


def _writes(entries: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(task name, path) for everything a task creates or rewrites (removals are not writes)."""
    found = []
    for name, module, body in _bodies(entries):
        if module in ("ansible.builtin.copy", "ansible.builtin.template"):
            found.append((name, str(body["dest"])))
        elif module == "ansible.builtin.file" and body.get("state") != "absent":
            found.append((name, str(body["path"])))
    return found


# --- the default: every existing host is unchanged ------------------------------------------------------------------


def test_the_default_is_the_vendored_release() -> None:
    assert DEFAULTS["streamstack_source"] == "vendored"
    assert SPECS["streamstack_source"]["choices"] == ["vendored", "apt"]
    for key in DEFAULTS:
        if key.startswith("streamstack_apt_") or key == "streamstack_source":
            assert key in SPECS, key


def test_the_repository_url_is_empty_until_a_host_names_the_gitlab_server() -> None:
    # validate-config.yml accepts an empty *_url or an https:// one: a default that rendered "https:///api/..." on
    # hosts that never opt in stopped every run (the quickstart preview found it).
    template = jinja2.Environment().from_string(DEFAULTS["streamstack_apt_repo_url"])
    assert template.render(streamstack_apt_gitlab_host="", streamstack_apt_project_id=2122) == ""
    assert (template.render(streamstack_apt_gitlab_host="gitlab.example.org", streamstack_apt_project_id=2122)
            == "https://gitlab.example.org/api/v4/projects/2122/packages/generic/streamstack-apt/stable")


def test_the_vendored_plan_is_byte_for_byte_the_one_before_the_switch() -> None:
    # The snapshot was taken from tasks/streamstack.yml and streamstack-framing.yml as they were before catalog#183.
    # Any change to what a vendored host receives (a path, a module argument, a file's bytes, a rendered unit) fails.
    assert json.loads(json.dumps(plan("vendored"))) == json.loads(GOLDEN.read_text())


def test_the_apt_plan_differs_from_the_vendored_one() -> None:
    assert _apt_plan() != plan("vendored")


def test_main_validates_the_source() -> None:
    validate = yaml.safe_load((ROLE / "tasks/streamstack.yml").read_text())[0]
    assert validate["name"] == "Validate the StreamStack settings"
    assert "streamstack_source in ['vendored', 'apt']" in validate["ansible.builtin.assert"]["that"]


# --- the apt path ---------------------------------------------------------------------------------------------------


def test_the_apt_source_is_signed_by_the_pinned_keyring() -> None:
    entries = _apt_plan()
    keyring = _entry(entries, "Install the StreamStack signing key")["args"]["ansible.builtin.copy"]
    assert keyring["dest"] == "/etc/apt/keyrings/streamstack.gpg"
    assert keyring["src"] == "streamstack-apt-public.gpg" and keyring["mode"] == "0644"
    source = _entry(entries, "Add the StreamStack apt repository")["args"]["ansible.builtin.copy"]
    assert source["dest"] == "/etc/apt/sources.list.d/streamstack.list"
    lines = [line for line in source["content"].splitlines() if not line.startswith("#")]
    assert lines == [
        f"deb [signed-by=/etc/apt/keyrings/streamstack.gpg] https://{HOST}/api/v4/projects/2122"
        "/packages/generic/streamstack-apt/stable ./"
    ]


def _armored_payload(path: Path) -> bytes:
    lines = path.read_text().splitlines()
    assert lines[0] == "-----BEGIN PGP PUBLIC KEY BLOCK-----"
    body = lines[lines.index("") + 1:lines.index("-----END PGP PUBLIC KEY BLOCK-----")]
    return base64.b64decode("".join(line for line in body if not line.startswith("=")))


def _packets(data: bytes) -> list[tuple[int, bytes]]:
    """OpenPGP packets (RFC 9580 section 4.2), new and old format, definite lengths only."""
    found, i = [], 0
    while i < len(data):
        header = data[i]
        i += 1
        assert header & 0x80, "not an OpenPGP packet"
        if header & 0x40:
            tag, first = header & 0x3F, data[i]
            i += 1
            if first < 192:
                length = first
            elif first < 224:
                length, i = ((first - 192) << 8) + data[i] + 192, i + 1
            else:
                assert first == 255, "partial body lengths are not used for keys"
                length, i = int.from_bytes(data[i:i + 4], "big"), i + 4
        else:
            tag, size = (header >> 2) & 0x0F, (1, 2, 4)[header & 0x03]
            length, i = int.from_bytes(data[i:i + size], "big"), i + size
        found.append((tag, data[i:i + length]))
        i += length
    return found


def test_the_role_keyring_is_streamstacks_published_key_with_the_pinned_fingerprint() -> None:
    keyring = (ROLE / "files/streamstack-apt-public.gpg").read_bytes()
    # The binary keyring is StreamStack's packaging/apt-public.asc dearmored, byte for byte.
    assert keyring == _armored_payload(VENDORED_PACKAGING / "apt-public.asc")
    primaries = [body for tag, body in _packets(keyring) if tag == 6]
    assert len(primaries) == 1, "exactly one primary key"
    (key,) = primaries
    assert key[0] == 4, "a v4 key"
    fingerprint = hashlib.sha1(b"\x99" + len(key).to_bytes(2, "big") + key).hexdigest().upper()
    assert fingerprint == (VENDORED_PACKAGING / "apt-public.fingerprint").read_text().strip()
    assert fingerprint == "F3AA4E82E815D695089DB4D2456BDF83CAB0C1E8"


def test_the_credentials_must_be_root_only_and_come_from_the_bootstrap() -> None:
    tasks = yaml.safe_load((ROLE / "tasks/streamstack-apt.yml").read_text())
    check = next(t for t in tasks if t["name"] == "Require root-only StreamStack apt credentials")
    that = check["ansible.builtin.assert"]["that"]
    assert "_streamstack_apt_auth.stat.mode == '0600'" in that
    assert "_streamstack_apt_auth.stat.pw_name == 'root'" in that
    assert "scripts/bootstrap-streamstack-apt.sh" in check["ansible.builtin.assert"]["fail_msg"]
    assert DEFAULTS["streamstack_apt_auth_file"] == "/etc/apt/auth.conf.d/streamstack.conf"
    # The role never writes or reads the credentials file.
    assert not [p for _, p in _writes(_apt_plan()) if p == DEFAULTS["streamstack_apt_auth_file"]]
    assert "slurp" not in (ROLE / "tasks/streamstack-apt.yml").read_text()


def test_the_package_is_installed_at_the_pinned_version_and_drives_the_restart() -> None:
    tasks = yaml.safe_load((ROLE / "tasks/streamstack-apt.yml").read_text())
    install = next(t for t in tasks if "ansible.builtin.apt" in t)
    assert install["ansible.builtin.apt"]["name"] == "streamstack={{ streamstack_apt_release }}"
    assert install["ansible.builtin.apt"]["allow_downgrade"] is True
    # The Console restart and the lights/deck try-restart read this register (streamstack.yml).
    assert install["register"] == "_streamstack_package_replaced"
    rendered = _entry(_apt_plan(), "Install StreamStack from the apt repository at version {{ streamstack_apt_release }}")
    assert rendered["args"]["ansible.builtin.apt"]["name"] == f"streamstack={DEFAULTS['streamstack_apt_release']}"
    # Included, not imported: on a vendored host no apt task registers over the vendored replacement result.
    main = yaml.safe_load((ROLE / "tasks/streamstack.yml").read_text())
    (include,) = [t for t in main if t.get("ansible.builtin.include_tasks")]
    assert include["ansible.builtin.include_tasks"]["file"] == "streamstack-apt.yml"
    assert include["when"] == "streamstack_source == 'apt'"
    assert main.index(include) > next(i for i, t in enumerate(main) if t["name"] == "Install the vendored StreamStack")


def test_the_apt_host_writes_nothing_under_usr_local_but_the_role_helpers() -> None:
    writes = _writes(_apt_plan())
    local = sorted(path for _, path in writes if path.startswith("/usr/local/"))
    assert local == [
        "/usr/local/lib/curiosity",
        "/usr/local/lib/curiosity/streamstack_console_restart.py",
        "/usr/local/sbin/curiosity-streamstack-console-restart",
        "/usr/local/sbin/curiosity-streamstack-loopback",
    ]


def test_the_vendored_install_is_removed() -> None:
    entries = _apt_plan()
    tasks = yaml.safe_load((ROLE / "tasks/streamstack-apt.yml").read_text())
    remove = next(t for t in tasks if t["name"] == "Remove the vendored StreamStack install")
    assert remove["ansible.builtin.file"] == {"path": "{{ item }}", "state": "absent"}
    assert "/usr/local/lib/streamstack" in remove["loop"]
    find = next(t for t in tasks if t["name"] == "Find the vendored StreamStack launchers")["ansible.builtin.find"]
    assert find == {"paths": "/usr/local/bin", "patterns": "streamstack*", "file_type": "any"}
    assert "_streamstack_vendored_launchers.files" in remove["loop"]
    for icon in ("apps/streamstack", "status/streamstack-live", "status/streamstack-raw", "status/streamstack-warn",
                 "status/streamstack-off"):
        assert f"/usr/local/share/icons/hicolor/scalable/{icon}.svg" in remove["loop"]
    # Nothing on the apt path installs the vendored tree.
    sources = [body.get("src", "") for _, module, body in _bodies(entries) if module == "ansible.builtin.copy"]
    assert not [s for s in sources if str(s).startswith("streamstack/src/") or "packaging/bin" in str(s)]


def test_no_second_copy_of_the_packages_udev_modules_load_or_wireplumber_files() -> None:
    entries = _apt_plan()
    writes = [path for _, path in _writes(entries)]
    assert not [p for p in writes if p.startswith("/etc/modules-load.d/")]
    assert not [p for p in writes if p.startswith("/etc/wireplumber/")]
    assert [p for p in writes if p.startswith("/etc/udev/rules.d/")] == ["/etc/udev/rules.d/60-curiosity-streamstack.rules"]
    assert not [p for p in writes if Path(p).name in {"60-streamstack-litra.rules", "61-streamstack-video.rules"}]
    # The role's own modules-load file goes: the package loads v4l2loopback.
    leave = _entry(entries, "Leave loading v4l2loopback at boot to the StreamStack package")["args"]["ansible.builtin.file"]
    assert leave == {"path": "/etc/modules-load.d/curiosity-streamstack.conf", "state": "absent"}
    # The package's modprobe options would sort after the role's and win; a same-named /etc file masks them.
    mask = _entry(entries, "Mask the loopback options the StreamStack package ships")["args"]["ansible.builtin.copy"]
    assert mask["dest"] == "/etc/modprobe.d/streamstack-v4l2loopback.conf"
    assert all(line.startswith("#") for line in mask["content"].splitlines())
    assert Path(mask["dest"]).name == (VENDORED_PACKAGING / "modprobe.d/streamstack-v4l2loopback.conf").name
    # The vendored drop-ins are removed before the package first installs, so its own conffiles are the ones in place.
    dropins = _entry(entries, "Remove the vendored WirePlumber drop-ins before the package takes them over")
    assert [item["ansible.builtin.file"]["path"] for item in dropins["items"]] == [
        f"/etc/wireplumber/wireplumber.conf.d/{p.name}"
        for p in sorted((VENDORED_PACKAGING / "wireplumber/wireplumber.conf.d").iterdir())]
    assert dropins["when"] == ["_streamstack_apt_status.stdout != 'installed'"]


def test_the_role_units_stay_and_run_the_packaged_commands() -> None:
    entries = _apt_plan()
    console = _entry(entries, "Install the StreamStack Console user service")["args"]["ansible.builtin.template"]
    assert "ExecStart=/usr/bin/streamstack console --no-window" in console["rendered"]
    publish = _entry(entries, "Install the StreamStack camera publish user service")["args"]["ansible.builtin.template"]
    assert "ExecStart=/usr/bin/streamstack camera-publish --wait 120" in publish["rendered"]
    assert "ExecCondition=/usr/bin/streamstack camera-publish --help" in publish["rendered"]
    (deck,) = _entry(entries, "Install the StreamStack voice chain and deck user services")["items"]
    assert deck["ansible.builtin.copy"]["dest"] == "/etc/systemd/user/streamstack-deck.service"
    assert "ExecStart=/usr/bin/streamstack deck run" in deck["ansible.builtin.copy"]["content"]
    assert "KillMode=process" in deck["ansible.builtin.copy"]["content"]
    links = {body["path"]: body["src"] for _, module, body in _bodies(entries)
             if module == "ansible.builtin.file" and body.get("state") == "link"}
    assert links["/etc/systemd/user/graphical-session.target.wants/streamstack-lights.service"] == \
        "/usr/lib/systemd/user/streamstack-lights.service"
    assert links["/etc/systemd/user/graphical-session.target.wants/streamstack-console.service"] == \
        "/etc/systemd/user/streamstack-console.service"
    assert links["/etc/systemd/user/graphical-session.target.wants/streamstack-camera-publish.service"] == \
        "/etc/systemd/user/streamstack-camera-publish.service"
    # Copies of the packaged units in /etc/systemd/user would mask the package's and run commands that are gone.
    removed = _entry(entries, "Remove the vendored release's copies of the packaged user services")
    assert sorted(i["ansible.builtin.file"]["path"] for i in removed["items"]) == [
        f"/etc/systemd/user/streamstack-{name}.service" for name in ("audio-chain", "auto", "canon", "lights")]
    seed = _entry(entries, "Seed StreamStack defaults for each user")["items"][0]["ansible.builtin.command"]
    assert seed["argv"] == ["/usr/bin/streamstack", "setup", "--quiet"]
    # Under /usr/local only the role's own helpers are written (no launcher, no package tree).
    assert not [p for _, p in _writes(entries) if p.startswith(("/usr/local/bin/", "/usr/local/lib/streamstack"))]


def test_the_packaged_bridge_and_auto_links_point_at_the_package_units() -> None:
    entries = plan("apt", {"streamstack_apt_gitlab_host": HOST, "streamstack_auto_autostart": True,
                           "streamstack_canon_bridge_autostart": True, "streamstack_canon_bridge": "gphoto2",
                           "streamstack_camera_external": False})
    links = {body["path"]: body["src"] for _, module, body in _bodies(entries)
             if module == "ansible.builtin.file" and body.get("state") == "link"}
    assert links["/etc/systemd/user/graphical-session.target.wants/streamstack-auto.service"] == \
        "/usr/lib/systemd/user/streamstack-auto.service"
    assert links["/etc/systemd/user/default.target.wants/streamstack-canon.service"] == \
        "/usr/lib/systemd/user/streamstack-canon.service"


def test_the_tray_autostart_on_apt_links_the_packaged_entry() -> None:
    entries = plan("apt", {"streamstack_apt_gitlab_host": HOST, "streamstack_tray_autostart": True})
    link = _entry(entries, "Start the packaged StreamStack panel icon at login")["args"]["ansible.builtin.file"]
    assert link["path"] == "/etc/xdg/autostart/streamstack-tray.desktop"
    assert link["src"] == "/usr/share/applications/streamstack-tray.desktop"
    assert (VENDORED_PACKAGING / "applications/streamstack-tray.desktop").is_file()


def test_the_console_restarts_once_after_a_package_change_and_only_on_the_apt_host() -> None:
    entries = _apt_plan()
    restart = _entry(entries, "Start the StreamStack Console, restarting it after a package replacement unless a call is on")
    argv = restart["items"][0]["ansible.builtin.command"]["argv"]
    assert argv[-2:] == ["/usr/local/sbin/curiosity-streamstack-console-restart",
                         "{{ 'restart' if _streamstack_package_replaced is changed else 'start' }}"]
    helper = _entry(entries, "Install the call-safe StreamStack Console restart for the native StreamStack")
    assert helper["args"]["ansible.builtin.copy"]["src"] == "curiosity-streamstack-console-restart-native"
    assert helper["args"]["ansible.builtin.copy"]["dest"] == argv[-2]
    decisions = _entry(entries, "Install the call-safe StreamStack Console restart decisions")
    assert decisions["args"]["ansible.builtin.copy"]["src"] == "curiosity-streamstack-console-restart"
    # The vendored host keeps the helper it had.
    assert not [e for e in plan("vendored") if "native StreamStack" in e["name"]]


def test_openvino_wheels_never_recreate_the_vendored_tree_on_an_apt_host() -> None:
    assert not [e for e in _apt_plan() if "OpenVINO" in e["name"]]


# --- the call-safe restart on the native build ---------------------------------------------------------------------


def _native_module():
    import importlib.machinery
    import importlib.util

    loader = importlib.machinery.SourceFileLoader("console_restart_native", str(NATIVE_RESTART))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


IDLE = {"available": True, "active": False, "state": "idle", "sources": {"audio": True, "video": True}}


@pytest.mark.parametrize(("call", "busy", "says"), [
    (IDLE, False, ""),
    ({**IDLE, "active": True, "state": "in_call", "service": "Teams", "reason": ""}, True, "Teams call"),
    ({**IDLE, "active": True, "reason": "Meet in Brave holds the camera"}, True, "Meet in Brave"),
    ({**IDLE, "available": False}, True, "no call watch"),
    ({"available": True}, True, "audio server"),
    ({**IDLE, "sources": {"audio": True, "video": False}}, True, "/proc"),
    ({**IDLE, "active": None}, True, "not reported"),
])
def test_the_native_look_waits_unless_the_console_clearly_sees_no_call(call: dict, busy: bool, says: str) -> None:
    got_busy, reason = _native_module().busy_reason(call, "")
    assert got_busy is busy
    assert says in reason


class _Console(http.server.BaseHTTPRequestHandler):
    token = "test-token-not-a-secret"
    call: dict[str, Any] = IDLE

    def do_GET(self) -> None:  # noqa: N802 - the http.server interface
        if self.path != "/api/state" or self.headers.get("X-StreamStack-Token") != self.token:
            self.send_response(403)
            self.end_headers()
            return
        body = json.dumps({"video": {}, "call": type(self).call}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        return


@pytest.fixture
def console(tmp_path: Path):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Console)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    runtime = tmp_path / "run"
    runtime.mkdir()
    (runtime / "streamstack-console.json").write_text(
        json.dumps({"pid": os.getpid(), "port": server.server_address[1], "token": _Console.token}))
    yield runtime
    server.shutdown()
    _Console.call = IDLE


def _run_native(tmp_path: Path, runtime: Path, mode: str) -> tuple[int, str, list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "systemctl.calls"
    (bin_dir / "systemctl").write_text(f'#!/bin/sh\n[ "$1" = --user ] || exit 64\nshift\necho "$*" >> {calls}\nexit 0\n')
    (bin_dir / "systemctl").chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", XDG_RUNTIME_DIR=str(runtime),
               CURIOSITY_STREAMSTACK_CONSOLE_RESTART_LIB=str(RESTART),
               CURIOSITY_STREAMSTACK_CONSOLE_RESTART_RECHECK_S="0", STREAMSTACK_LIB=str(tmp_path / "no-python-package"))
    env.pop("PYTHONPATH", None)
    done = subprocess.run([sys.executable, "-I", str(NATIVE_RESTART), mode], capture_output=True, text=True, env=env,
                          check=False, timeout=60)
    return done.returncode, done.stdout + done.stderr, calls.read_text().splitlines() if calls.exists() else []


def test_the_native_helper_restarts_an_idle_console_by_asking_it(tmp_path: Path, console: Path) -> None:
    code, out, calls = _run_native(tmp_path, console, "restart")
    assert code == 0, out
    assert out.startswith("restarted:"), out
    assert calls.count("restart streamstack-console.service") == 1
    assert _Console.token not in out


def test_the_native_helper_defers_during_a_call_then_restarts_once(tmp_path: Path, console: Path) -> None:
    _Console.call = {**IDLE, "active": True, "state": "in_call", "service": "Zoom", "reason": ""}
    code, out, calls = _run_native(tmp_path, console, "restart")
    assert code == 3, out
    assert out.startswith("deferred:") and "Zoom call" in out, out
    assert "restart streamstack-console.service" not in calls
    assert "start curiosity-streamstack-console-restart.timer" in calls
    assert (console / "streamstack/console-restart-pending").is_file()
    _Console.call = IDLE
    code, out, calls = _run_native(tmp_path, console, "--pending")
    assert code == 0, out
    assert out.startswith("restarted:"), out
    assert calls.count("restart streamstack-console.service") == 1
    assert not (console / "streamstack/console-restart-pending").exists()


def test_the_native_helper_waits_when_the_console_does_not_answer(tmp_path: Path) -> None:
    runtime = tmp_path / "run"
    runtime.mkdir()
    (runtime / "streamstack-console.json").write_text(json.dumps({"pid": 1, "port": 9, "token": "t"}))
    code, out, calls = _run_native(tmp_path, runtime, "restart")
    assert code == 3, out
    assert "not answering" in out, out
    assert "restart streamstack-console.service" not in calls


# --- scripts/bootstrap-streamstack-apt.sh ---------------------------------------------------------------------------

FAKE_VALUE = "deploy-user:fake-token-for-tests"


def _bootstrap(tmp_path: Path, *args: str, value: str = FAKE_VALUE) -> tuple[subprocess.CompletedProcess, Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    az_log, value_file = tmp_path / "az.log", tmp_path / "value"
    value_file.write_text(value + "\n")
    # The stub az logs its arguments (names only) and prints the value, as `--query value -o tsv` would.
    (bin_dir / "az").write_text(f'#!/bin/sh\necho "$*" >> {az_log}\ncat {value_file}\n')
    (bin_dir / "sudo").write_text('#!/bin/sh\nwhile [ "${1#-}" != "$1" ]; do shift; done\n[ $# -eq 0 ] || exec "$@"\n')
    (bin_dir / "chown").write_text("#!/bin/sh\nexit 0\n")  # the test runs unprivileged; ownership is the host's
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    auth = tmp_path / "auth.conf.d/streamstack.conf"
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", STREAMSTACK_APT_AUTH_FILE=str(auth))
    done = subprocess.run(["bash", str(BOOTSTRAP), *args], capture_output=True, text=True, env=env, check=False,
                          timeout=60)
    return done, auth, az_log


ARGS = ("--host", HOST, "--vault", "test-vault", "--secret", "streamstack-apt-pull-deploy-token")


@pytest.mark.skipif(os.geteuid() == 0, reason="the script refuses to run as root, by design")
def test_the_bootstrap_writes_a_path_scoped_root_only_entry_without_printing_it(tmp_path: Path) -> None:
    done, auth, az_log = _bootstrap(tmp_path, *ARGS)
    assert done.returncode == 0, done.stderr
    assert stat.S_IMODE(auth.stat().st_mode) == 0o600
    entries = [line for line in auth.read_text().splitlines() if not line.startswith("#")]
    assert entries == [
        f"machine {HOST}/api/v4/projects/2122/packages/generic/streamstack-apt "
        "login deploy-user password fake-token-for-tests"]
    assert "fake-token-for-tests" not in done.stdout + done.stderr
    assert az_log.read_text().split() == ["keyvault", "secret", "show", "--vault-name", "test-vault", "--name",
                                          "streamstack-apt-pull-deploy-token", "--query", "value", "-o", "tsv"]
    # Idempotent: a second run gives the same file.
    first = auth.read_bytes()
    done, auth, _ = _bootstrap(tmp_path, *ARGS)
    assert done.returncode == 0, done.stderr
    assert auth.read_bytes() == first
    assert sorted(p.name for p in auth.parent.iterdir()) == ["streamstack.conf"]  # no temporary file left


@pytest.mark.skipif(os.geteuid() == 0, reason="the script refuses to run as root, by design")
def test_the_bootstrap_takes_another_project_id(tmp_path: Path) -> None:
    done, auth, _ = _bootstrap(tmp_path, *ARGS, "--project-id", "77")
    assert done.returncode == 0, done.stderr
    assert f"machine {HOST}/api/v4/projects/77/packages/generic/streamstack-apt " in auth.read_text()


@pytest.mark.skipif(os.geteuid() == 0, reason="the script refuses to run as root, by design")
@pytest.mark.parametrize("value", ["no-separator", "user:", ":token", "user:to ken", ""])
def test_a_malformed_secret_keeps_the_old_file_and_prints_nothing_of_it(tmp_path: Path, value: str) -> None:
    done, auth, _ = _bootstrap(tmp_path, *ARGS)
    assert done.returncode == 0, done.stderr
    before = auth.read_bytes()
    done, auth, _ = _bootstrap(tmp_path, *ARGS, value=value)
    assert done.returncode != 0
    assert auth.read_bytes() == before
    if value:
        assert value not in done.stdout + done.stderr


@pytest.mark.parametrize("args", [
    (),
    ("--host", HOST, "--vault", "test-vault"),
    ("--host", "bad host", "--vault", "test-vault", "--secret", "s"),
    ("--host", HOST, "--vault", "test-vault", "--secret", "s", "--project-id", "0"),
    ("--host", HOST, "--vault", "test-vault", "--secret", "bad/name"),
    ("--unknown",),
])
def test_the_bootstrap_refuses_incomplete_or_invalid_arguments(tmp_path: Path, args: tuple[str, ...]) -> None:
    done, auth, az_log = _bootstrap(tmp_path, *args)
    assert done.returncode != 0
    assert not auth.exists()
    assert not az_log.exists()


def test_the_bootstrap_names_no_site() -> None:
    # Public repository: the GitLab host, the Key Vault and the secret are the operator's arguments.
    text = BOOTSTRAP.read_text()
    for word in ("developerdojo", "omlab", "focuspass"):
        assert word not in text
