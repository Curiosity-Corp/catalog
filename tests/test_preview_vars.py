"""Checks for scripts/preview_vars.py, the secret-redacting variable dump.

The helper is what the dry-run documentation tells a cautious user to paste
into an issue, so a leak here would defeat its purpose.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/preview_vars.py"
REDACTED = "***REDACTED***"

SECRET_PASSWORD = "correct-horse-battery-staple-4821"
SECRET_PSK = "wifi-psk-7f3a91c2d4"
SECRET_GH = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
SECRET_URL_PW = "s3cr3t-url-pw-99"
SECRET_PEM = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk=\n-----END OPENSSH PRIVATE KEY-----\n"
SECRETS = (SECRET_PASSWORD, SECRET_PSK, SECRET_GH, SECRET_URL_PW, "b3BlbnNzaC1rZXk=")


def _run(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    # -I: ignore the environment and the current directory when importing.
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def _load_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("preview_vars", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_secret_named_variables_are_redacted_but_plain_ones_are_not() -> None:
    mod = _load_module()
    data = {
        "wifi_psk": SECRET_PSK,
        "ubuntu_pro_token": "abc123",
        "sunshine_default_pass": SECRET_PASSWORD,
        "my_service_password": SECRET_PASSWORD,
        "qwen_provider_secret_command": "pass show qwen",
        "workstation_storage_luks_key_file": "/root/luks.key",
        "dev_user": "researcher",
        "workstation_profile": "desktop",
        "authd_archive_key_fingerprint": "155D766F2E771AED",
        "authd_archive_key_url": "https://keyserver.example/pks",
        "pro_mimic_ssh_password_auth": True,
    }
    out = {key: mod.redact(value, key) for key, value in data.items()}
    for key in (
        "wifi_psk",
        "ubuntu_pro_token",
        "sunshine_default_pass",
        "my_service_password",
        "qwen_provider_secret_command",
        "workstation_storage_luks_key_file",
    ):
        assert out[key] == REDACTED, key
    for key in (
        "dev_user",
        "workstation_profile",
        "authd_archive_key_fingerprint",
        "authd_archive_key_url",
        "pro_mimic_ssh_password_auth",
    ):
        assert out[key] == data[key], key


def test_unset_secrets_stay_visible_so_the_dump_shows_what_is_missing() -> None:
    mod = _load_module()
    assert mod.redact("", "wifi_psk") == ""
    assert mod.redact([], "ubuntu_pro_token") == []
    assert mod.redact(None, "github_api_token") is None


def test_nested_structures_are_redacted_by_key_and_by_parent_key() -> None:
    mod = _load_module()
    data = {
        "wifi_profiles": [{"ssid": "lab", "password": SECRET_PASSWORD, "hidden": False}],
        "api_credentials": {"user": "svc", "value": SECRET_PASSWORD},
        "tokens": [SECRET_GH, "second"],
    }
    out = mod.redact(data)
    assert out["wifi_profiles"] == [{"ssid": "lab", "password": REDACTED, "hidden": False}]
    assert out["api_credentials"] == REDACTED
    assert out["tokens"] == REDACTED  # a sensitive parent name redacts the whole subtree


def test_credentials_are_redacted_by_value_under_innocent_names() -> None:
    mod = _load_module()
    assert mod.redact(SECRET_GH, "notes") == REDACTED
    assert mod.redact(SECRET_PEM, "dotfiles_blob") == REDACTED
    url = f"https://deploy:{SECRET_URL_PW}@git.example.edu/lab/dotfiles.git"
    redacted = mod.redact(url, "user_dotfiles_repo")
    assert SECRET_URL_PW not in redacted
    assert redacted == f"https://deploy:{REDACTED}@git.example.edu/lab/dotfiles.git"
    # A URL without credentials is left alone.
    plain = "https://git.example.edu/lab/dotfiles.git"
    assert mod.redact(plain, "user_dotfiles_repo") == plain


def test_cli_output_never_contains_secrets(tmp_path: Path) -> None:
    vars_file = tmp_path / "local-vars.yml"
    vars_file.write_text(
        yaml.safe_dump(
            {
                "dev_user": "researcher",
                "wifi_psk": SECRET_PSK,
                "user_dotfiles_repo": f"https://deploy:{SECRET_URL_PW}@git.example.edu/x.git",
                "private_note": SECRET_PEM,
            }
        )
    )
    stdin = json.dumps({"inventory_only": {"github_api_token": SECRET_GH}})
    for fmt in ("yaml", "json"):
        result = _run(
            "--vars-file", str(vars_file),
            "-e", f"sunshine_default_pass={SECRET_PASSWORD}",
            "--stdin",
            "--format", fmt,
            stdin=stdin,
        )
        assert result.returncode == 0, result.stderr
        for secret in SECRETS:
            assert secret not in result.stdout, (fmt, secret)
        assert REDACTED in result.stdout
        assert "researcher" in result.stdout


def test_defaults_dump_redacts_every_secret_override_and_keeps_structure() -> None:
    result = _run("-e", f"wifi_psk={SECRET_PSK}", "-e", f"github_api_token={SECRET_GH}", "--format", "json")
    assert result.returncode == 0, result.stderr
    assert SECRET_PSK not in result.stdout and SECRET_GH not in result.stdout
    data = json.loads(result.stdout)
    assert data["wifi_psk"] == REDACTED
    assert data["github_api_token"] == REDACTED
    assert data["workstation_profile"] == "desktop"
    # Every shipped default whose name looks sensitive must be empty or redacted.
    mod = _load_module()
    shipped = yaml.safe_load((ROOT / "roles/developer_workstation/defaults/main.yml").read_text())
    for key, value in shipped.items():
        if mod.is_sensitive_name(key) and value not in ("", None, [], {}) and not isinstance(value, bool):
            assert mod.redact(value, key) == REDACTED, key


def test_match_filter_and_bad_input() -> None:
    result = _run("--match", "^workstation_profile$", "--format", "json")
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"workstation_profile": "desktop"}
    bad = _run("-e", "no-equals-sign")
    assert bad.returncode == 2
    assert re.search(r"KEY=VALUE", bad.stderr)
    assert _run("--match", "(").returncode == 2
