"""The release tag pipeline blocks on gitleaks; keep its configuration valid on the runner's version.

v0.0.8 failed on fake fixture tokens and v0.0.9 failed because the waiver used the `[[allowlists]]` syntax that the
runner's older gitleaks silently ignores. These checks catch both before a tag is cut.
"""

import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".gitleaks.toml"


def test_the_waiver_uses_the_syntax_every_gitleaks_reads() -> None:
    config = tomllib.loads(CONFIG.read_text())
    assert "allowlists" not in config, "[[allowlists]] is ignored by older gitleaks; use the [allowlist] table"
    assert config["extend"]["useDefault"] is True
    assert config["allowlist"]["paths"] == ["^tests/"]


def test_the_working_tree_has_no_secret_outside_the_waived_fixtures() -> None:
    gitleaks = shutil.which("gitleaks")
    if gitleaks is None:
        pytest.skip("gitleaks is not installed; the tag pipeline runs it")
    result = subprocess.run(
        [gitleaks, "detect", "--source", ".", "--no-git", "--redact", "--config", CONFIG.name],
        cwd=ROOT,  # the waiver matches repo-relative paths, as the CI job runs it
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
