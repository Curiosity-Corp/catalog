"""The opt-in fleetd task installs a verified package and never leaks the secret.

These checks pin the role wiring (off by default, order of the secret and the
install, signature before checksum), evaluate the role's own Jinja expressions
for the package name and the checksum lookup, and run the role's enroll-secret
check against real files. They need no root, network or Fleet server.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
from pathlib import Path

import jinja2
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
TASKS = yaml.safe_load((ROLE / "tasks/fleetd.yml").read_text())
NOTATION_TASKS = yaml.safe_load((ROLE / "tasks/fleetd-notation.yml").read_text())
BY_NAME = {t["name"]: t for t in TASKS}
NAMES = [t["name"] for t in TASKS]

VERSION = "1.62.1-osquery5.24.0-a8943ae13524"
SUMS = """\
05849856698aea77577c744b2c8a58d40d4d7b3f0676969fcdd373ae07a02b72  fleetd-workstation-1.62.1-amd64.msi
880fb1586c2f50c8d983a3d92790cf25b98a434c724f5b6c16f83029a56c34b2  fleetd-workstation-1.62.1-amd64.deb
5da60e9ae642ec9cd62ab1bd7fb76cdd3e59cda8d667f55b80c3ec116a9c7526  fleetd-workstation-1.62.1-amd64.rpm
76a9b6f5627770660db8ceb18e7d93becefca75bc36d949959fb119649741a73  fleetd-server-1.62.1-amd64.deb
"""


def _env() -> jinja2.Environment:
    """A Jinja environment with the few Ansible filters these expressions use."""
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["b64decode"] = lambda value: base64.b64decode(value).decode()
    env.filters["regex_escape"] = re.escape
    env.filters["split"] = lambda value, sep=None: value.split(sep)
    env.tests["match"] = lambda value, pattern: re.match(pattern, value) is not None
    return env


def _render(expression: str, **context: object) -> str:
    return _env().from_string(expression).render(**context).strip()


# ---------------------------------------------------------------------------
# Defaults: off, and nothing private
# ---------------------------------------------------------------------------


def test_fleetd_is_off_by_default_and_carries_no_site_values() -> None:
    assert DEFAULTS["fleetd_enabled"] is False
    for key in (
        "fleetd_registry_id",
        "fleetd_package_base_url",
        "fleetd_package_release",
        "fleetd_package_netrc_file",
        "fleetd_enroll_secret",
        "fleetd_signing_certificate",
        "fleetd_sha256sums_sha256",
    ):
        assert DEFAULTS[key] == "", key
    assert DEFAULTS["fleetd_signature_verify"] is True
    assert DEFAULTS["fleetd_enroll_secret_env_file"] == "/etc/fleetd/enroll-secret.env"
    assert DEFAULTS["fleetd_orbit_dropin_path"].startswith("/etc/systemd/system/orbit.service.d/")
    assert DEFAULTS["workstation_converge_stamp_enabled"] == "{{ fleetd_enabled }}"
    for arch in ("amd64", "arm64"):
        assert re.fullmatch(r"[0-9a-f]{64}", DEFAULTS["fleetd_notation_sha256"][arch])


def test_main_includes_fleetd_only_when_enabled() -> None:
    main = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
    include = next(t for t in main if t["name"] == "Include the Fleet agent (fleetd)")
    assert include["ansible.builtin.include_tasks"]["file"] == "fleetd.yml"
    assert include["when"] == "fleetd_enabled | bool"
    assert include["tags"] == ["fleetd"]


# ---------------------------------------------------------------------------
# Order: verify, then trust the checksum; secret and drop-in, then install
# ---------------------------------------------------------------------------


def test_signature_is_verified_before_any_checksum_is_trusted() -> None:
    verify = NAMES.index("Verify the fleetd checksum list signature")
    assert NAMES.index("Download the fleetd checksum list signature") < verify
    assert verify < NAMES.index("Take the package checksum from the verified list")
    assert verify < NAMES.index("Download the fleetd package")
    argv = BY_NAME["Verify the fleetd checksum list signature"]["ansible.builtin.command"]["argv"]
    assert argv[1:3] == ["blob", "verify"]
    assert BY_NAME["Verify the fleetd checksum list signature"]["when"] == "fleetd_signature_verify | bool"
    package = BY_NAME["Download the fleetd package"]["ansible.builtin.get_url"]
    assert package["checksum"] == "sha256:{{ _fleetd_package_sha256 }}"


def test_secret_and_dropin_are_in_place_before_the_install() -> None:
    install = NAMES.index("Install the fleetd package")
    for name in (
        "Write the device class and registry id",
        "Write the fleetd enroll secret file",
        "Require a root-only fleetd enroll secret before installing",
        "Load the enroll secret file into orbit",
    ):
        assert NAMES.index(name) < install, name
    assert BY_NAME["Install the fleetd package"]["when"] == "_fleetd_needs_install | bool"
    assert BY_NAME["Install the fleetd package"]["ansible.builtin.command"]["argv"][:2] == ["dpkg", "--install"]


def test_secret_is_written_root_only_and_never_logged() -> None:
    write = BY_NAME["Write the fleetd enroll secret file"]
    assert write["no_log"] is True
    copy = write["ansible.builtin.copy"]
    assert (copy["owner"], copy["group"], copy["mode"]) == ("root", "root", "0600")
    assert copy["content"] == "ORBIT_ENROLL_SECRET={{ fleetd_enroll_secret }}\n"
    require = BY_NAME["Require a root-only fleetd enroll secret before installing"]
    assert "_fleetd_secret_file.stat.mode == '0600'" in require["ansible.builtin.assert"]["that"]
    # Nothing reads the secret file into the play or prints a variable.
    for task in TASKS + NOTATION_TASKS:
        assert "ansible.builtin.debug" not in task
        slurp = task.get("ansible.builtin.slurp")
        if slurp:
            assert "enroll" not in slurp["src"]
    text = (ROLE / "tasks/fleetd.yml").read_text()
    assert text.count("fleetd_enroll_secret }}") == 1


def test_dropin_overrides_the_package_placeholder() -> None:
    copy = BY_NAME["Load the enroll secret file into orbit"]["ansible.builtin.copy"]
    assert "[Service]\nEnvironmentFile={{ fleetd_enroll_secret_env_file }}\n" in copy["content"]
    assert BY_NAME["Load the enroll secret file into orbit"]["notify"] == "Restart orbit"


def test_restart_handler_skips_a_fresh_install() -> None:
    handlers = {h["name"]: h for h in yaml.safe_load((ROLE / "handlers/main.yml").read_text())}
    restart = handlers["Restart orbit"]
    assert restart["ansible.builtin.systemd"]["name"] == "orbit.service"
    assert restart["when"] == "not (_fleetd_installed.changed | default(false))"


# ---------------------------------------------------------------------------
# The role's own expressions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("package_class", "arch", "expected"),
    [
        ("workstation", "x86_64", "fleetd-workstation-1.62.1-amd64.deb"),
        ("server", "x86_64", "fleetd-server-1.62.1-amd64.deb"),
        ("workstation", "aarch64", "fleetd-workstation-1.62.1-arm64.deb"),
    ],
)
def test_package_file_name_follows_the_published_layout(package_class: str, arch: str, expected: str) -> None:
    rendered = _render(
        DEFAULTS["fleetd_package_file"],
        fleetd_package_class=package_class,
        fleetd_package_release=VERSION,
        fleetd_architecture_map=DEFAULTS["fleetd_architecture_map"],
        ansible_facts={"architecture": arch},
    )
    assert rendered == expected


def _checksum_for(package_file: str, sums: str) -> str:
    expression = BY_NAME["Take the package checksum from the verified list"]["ansible.builtin.set_fact"][
        "_fleetd_package_sha256"
    ]
    return _render(
        expression,
        _fleetd_sums={"content": base64.b64encode(sums.encode()).decode()},
        fleetd_package_file=package_file,
    )


def test_checksum_lookup_takes_the_exact_entry() -> None:
    assert (
        _checksum_for("fleetd-workstation-1.62.1-amd64.deb", SUMS)
        == "880fb1586c2f50c8d983a3d92790cf25b98a434c724f5b6c16f83029a56c34b2"
    )
    assert (
        _checksum_for("fleetd-server-1.62.1-amd64.deb", SUMS)
        == "76a9b6f5627770660db8ceb18e7d93becefca75bc36d949959fb119649741a73"
    )
    # Binary-mode entries ("*name") are accepted too.
    starred = SUMS.replace("  fleetd-server", " *fleetd-server")
    assert _checksum_for("fleetd-server-1.62.1-amd64.deb", starred).startswith("76a9b6f5")


@pytest.mark.parametrize(
    "package_file",
    [
        "fleetd-workstation-1.62.1-arm64.deb",  # not published
        "fleetd-workstation-1.62.1-amd64",  # prefix of a real entry
        "workstation-1.62.1-amd64.deb",  # suffix of a real entry
        "fleetd-workstation-1.62.1-amd64.deb.sig",
    ],
)
def test_checksum_lookup_refuses_anything_but_an_exact_name(package_file: str) -> None:
    assert _checksum_for(package_file, SUMS) == ""


def test_checksum_lookup_ignores_a_dotted_lookalike() -> None:
    # The file name is regex-escaped: "." must not match any character.
    lookalike = "0" * 64 + "  fleetd-workstation-1x62x1-amd64xdeb\n"
    assert _checksum_for("fleetd-workstation-1.62.1-amd64.deb", lookalike) == ""


@pytest.mark.parametrize(
    ("installed", "stamp", "needs"),
    [
        ("installed", f"{VERSION} fleetd-workstation-1.62.1-amd64.deb sha256:{'a' * 64}\n", "False"),
        ("installed", f"{VERSION} fleetd-workstation-1.62.1-amd64.deb sha256:{'b' * 64}\n", "True"),
        ("installed", "", "True"),
        ("", f"{VERSION} fleetd-workstation-1.62.1-amd64.deb sha256:{'a' * 64}\n", "True"),
        ("config-files", f"{VERSION} fleetd-workstation-1.62.1-amd64.deb sha256:{'a' * 64}\n", "True"),
    ],
)
def test_install_decision_follows_the_recipe_stamp(installed: str, stamp: str, needs: str) -> None:
    expression = BY_NAME["Decide whether fleetd needs installing"]["ansible.builtin.set_fact"]["_fleetd_needs_install"]
    stamp_result = {"content": base64.b64encode(stamp.encode()).decode()} if stamp else {}
    rendered = _render(
        expression,
        _fleetd_dpkg={"stdout": installed},
        _fleetd_stamp=stamp_result,
        fleetd_package_release=VERSION,
        fleetd_package_file="fleetd-workstation-1.62.1-amd64.deb",
        _fleetd_package_sha256="a" * 64,
    )
    assert rendered == needs


def test_trust_policy_accepts_only_the_site_store() -> None:
    task = next(t for t in NOTATION_TASKS if t["name"] == "Install the fleetd blob trust policy")
    policy = json.loads(_render(json.dumps(task["vars"]["_fleetd_trust_policy"]), fleetd_notation_policy_name="fleetd"))
    (only,) = policy["trustPolicies"]
    assert only["signatureVerification"]["level"] == "strict"
    assert only["trustStores"] == ["ca:fleetd"]
    download = next(t for t in NOTATION_TASKS if t["name"] == "Download the pinned Notation release")
    assert download["ansible.builtin.get_url"]["checksum"].startswith("sha256:{{ fleetd_notation_sha256[")


# ---------------------------------------------------------------------------
# The enroll secret check, run for real
# ---------------------------------------------------------------------------


def _secret_check(path: Path) -> int:
    argv = BY_NAME["Check that the fleetd enroll secret file holds a real secret"]["ansible.builtin.command"]["argv"]
    assert argv[-1] == "{{ fleetd_enroll_secret_env_file }}"
    return subprocess.run([*argv[:-1], str(path)], check=False, capture_output=True).returncode


@pytest.mark.parametrize(
    ("content", "ok"),
    [
        ("ORBIT_ENROLL_SECRET=s3cr3t-value\n", True),
        ("ORBIT_ENROLL_SECRET=dummy\n", False),
        ("ORBIT_ENROLL_SECRET=\n", False),
        ("ORBIT_ENROLL_SECRET=two words\n", False),
        ("ORBIT_ENROLL_SECRET=a\nORBIT_ENROLL_SECRET=b\n", False),
        ("SOMETHING_ELSE=x\n", False),
        ("", False),
    ],
)
def test_enroll_secret_check(tmp_path: Path, content: str, ok: bool) -> None:
    path = tmp_path / "enroll-secret.env"
    path.write_text(content)
    assert (_secret_check(path) == 0) is ok


def test_enroll_secret_check_output_never_echoes_the_secret(tmp_path: Path) -> None:
    path = tmp_path / "enroll-secret.env"
    path.write_text("ORBIT_ENROLL_SECRET=do-not-print-me\n")
    argv = BY_NAME["Check that the fleetd enroll secret file holds a real secret"]["ansible.builtin.command"]["argv"]
    result = subprocess.run([*argv[:-1], str(path)], check=False, capture_output=True, text=True)
    assert "do-not-print-me" not in result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Converge stamp
# ---------------------------------------------------------------------------


def test_converge_stamp_is_written_after_every_role() -> None:
    (play,) = yaml.safe_load((ROOT / "playbooks/site.yml").read_text())
    (stamp,) = play["post_tasks"]
    include = stamp["ansible.builtin.include_role"]
    assert include == {"name": "developer_workstation", "tasks_from": "converge-stamp.yml"}
    assert "workstation_converge_stamp_enabled" in stamp["when"]
    tasks = yaml.safe_load((ROLE / "tasks/converge-stamp.yml").read_text())
    write = next(t for t in tasks if t["name"] == "Write the converge stamp")
    assert write["ansible.builtin.copy"]["dest"] == "{{ workstation_converge_stamp_path }}"
    assert write["changed_when"] is False
    assert DEFAULTS["workstation_converge_stamp_path"] == "/var/lib/curiosity/converged.json"
