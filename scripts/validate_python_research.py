#!/usr/bin/env python3
"""Validate the Python research profile in a throwaway directory.

This is what the `research-profile` CI job runs, and it repeats on any machine
with ansible-playbook, uv and network access to PyPI. It never touches
/opt/science-venv, /usr/local or the caller's home directory:

1. Applies the role's own tasks/science-venv.yml with the variables from
   examples/python-research.yml, into <work>/science-venv, twice: the second
   run must report no changes.
2. Runs tests/research_profile/sample_analysis.py with that venv's Python (a
   fitted line checked against the known answer, a matplotlib figure, the
   notebook packages imported).
3. Registers the venv as a Jupyter kernel the way science-tools.yml does,
   under <work>/kernels instead of /usr/local.
4. Runs the project-level environment commands from
   docs/python-research-profile.md verbatim (uv init, uv add, uv run, kernel
   install), with HOME pointed at <work>/home.

Prints a Markdown report and writes it to --report when given. Exits non-zero
with the failing step's output when any step fails.

    python3 scripts/validate_python_research.py                   # temp dir
    python3 scripts/validate_python_research.py --work DIR --report report.md
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/python-research.yml"
PLAYBOOK = ROOT / "tests/research_profile/apply-science-venv.yml"
SAMPLE = ROOT / "tests/research_profile/sample_analysis.py"

# The project-level workflow, exactly as docs/python-research-profile.md shows
# it (tests/test_python_research.py keeps the two in step).
PROJECT_NAME = "my-study"
PROJECT_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("uv", "init", "--bare"),
    ("uv", "add", "numpy", "pandas", "ipykernel"),
    ("uv", "run", "python", "-c", "import numpy, pandas; print(numpy.__version__, pandas.__version__)"),
    ("uv", "run", "python", "-m", "ipykernel", "install", "--user", "--name", PROJECT_NAME,
     "--display-name", f"Python ({PROJECT_NAME})"),
)

RECAP = re.compile(r"^localhost\s*:\s*ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", re.MULTILINE)


class StepFailed(RuntimeError):
    """A validation step failed; the message carries its output."""


def run(step: str, argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
        timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise StepFailed(f"{step}: {argv[0]} is not installed ({exc})") from exc
    except subprocess.TimeoutExpired as exc:
        raise StepFailed(f"{step}: timed out after {timeout} s: {shlex.join(argv)}") from exc
    if result.returncode != 0:
        raise StepFailed(f"{step} failed (exit {result.returncode}): {shlex.join(argv)}\n"
                         f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}")
    return result


def apply_role(venv: Path) -> tuple[int, str]:
    """Run the role's science-venv tasks; return (changed count, output)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANSIBLE_")}
    env.update(ANSIBLE_ROLES_PATH=str(ROOT / "roles"), ANSIBLE_NOCOLOR="1", ANSIBLE_FORCE_COLOR="0",
               ANSIBLE_LOCALHOST_WARNING="False", ANSIBLE_INVENTORY_UNPARSED_WARNING="False",
               ANSIBLE_CONFIG=str(ROOT / "ansible.cfg"))
    result = run("apply science-venv.yml", [
        "ansible-playbook", "-i", "localhost,", str(PLAYBOOK),
        "-e", f"@{EXAMPLE}", "-e", json.dumps({"science_python_venv": str(venv)}),
    ], cwd=ROOT, env=env)
    recap = RECAP.search(result.stdout)
    if recap is None:
        raise StepFailed(f"apply science-venv.yml: no play recap in the output\n{result.stdout}")
    return int(recap.group(2)), result.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--work", type=Path, help="build here and keep it (default: a temp dir, removed)")
    parser.add_argument("--report", type=Path, help="also write the Markdown report to this file")
    args = parser.parse_args()

    try:
        example = yaml.safe_load(EXAMPLE.read_text())
    except (OSError, yaml.YAMLError) as exc:
        print(f"cannot read {EXAMPLE}: {exc}", file=sys.stderr)
        return 1
    packages = example.get("science_python_packages") if isinstance(example, dict) else None
    if not isinstance(packages, list) or not packages:
        print(f"{EXAMPLE} has no science_python_packages list", file=sys.stderr)
        return 1
    if shutil.which("uv") is None:
        print("uv is not on PATH; install it (pipx install uv, or pip install uv) and rerun", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="py-research-") as tmp:
        work = args.work.resolve() if args.work else Path(tmp)
        work.mkdir(parents=True, exist_ok=True)
        venv = work / "science-venv"
        try:
            started = time.monotonic()
            first_changed, _ = apply_role(venv)
            install_seconds = time.monotonic() - started
            second_changed, second_output = apply_role(venv)
            if second_changed != 0:
                raise StepFailed(f"second apply was not idempotent (changed={second_changed})\n{second_output}")

            python = venv / "bin/python"
            sample = run("sample analysis", [str(python), str(SAMPLE), str(work / "sample.png")])
            results = json.loads(sample.stdout.strip().splitlines()[-1])

            kernels = work / "kernels"
            run("kernel registration", [str(python), "-m", "ipykernel", "install", "--name", "science",
                                        "--display-name", "Python 3 (Science)", "--prefix", str(kernels)])
            kernel = json.loads((kernels / "share/jupyter/kernels/science/kernel.json").read_text())
            if not kernel["argv"][0].startswith(str(venv)):
                raise StepFailed(f"kernel.json does not run the science venv: {kernel['argv']}")

            home = work / "home"
            project = home / "projects" / PROJECT_NAME
            project.mkdir(parents=True, exist_ok=True)
            uv_env = {k: v for k, v in os.environ.items() if not k.startswith(("VIRTUAL_ENV", "UV_", "PYTHON"))}
            uv_env.update(HOME=str(home), UV_CACHE_DIR=str(work / "uv-cache"), UV_PYTHON_DOWNLOADS="never")
            project_output = [run(f"project step {shlex.join(cmd)}", list(cmd), cwd=project, env=uv_env).stdout.strip()
                              for cmd in PROJECT_COMMANDS]
            for expected in ("pyproject.toml", "uv.lock", ".venv/bin/python"):
                if not (project / expected).exists():
                    raise StepFailed(f"project-level environment: {expected} was not created")
            project_kernel = home / f".local/share/jupyter/kernels/{PROJECT_NAME}/kernel.json"
            if not project_kernel.is_file():
                raise StepFailed(f"project kernel was not registered at {project_kernel}")
            uv_version = run("uv --version", ["uv", "--version"]).stdout.strip()
            pip_version = run("pip --version", [str(python), "-m", "pip", "--version"]).stdout.split()[1]
        except StepFailed as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except (OSError, ValueError, KeyError, IndexError) as exc:
            print(f"validation could not read a step's result: {exc!r}", file=sys.stderr)
            return 1

        report = "\n".join([
            "## Python research profile: disposable install",
            "",
            f"- Host: Python {platform.python_version()} on {platform.platform()}",
            f"- Role tasks: `science-venv.yml` with `examples/python-research.yml`, first apply "
            f"changed={first_changed} in {install_seconds:.0f} s, second apply changed=0",
            f"- Requested: {' '.join(packages)}",
            f"- Science venv: Python {results['python']}, pip {pip_version}",
            "- Installed: " + ", ".join(f"{name} {results[name]}" for name in (
                "numpy", "scipy", "pandas", "matplotlib", "scikit-learn", "ipykernel", "ipywidgets")),
            f"- Sample analysis: slope {results['slope']}, intercept {results['intercept']}, "
            f"pearson r {results['pearson_r']}, figure written",
            "- Kernel `science` (\"Python 3 (Science)\") registered from the venv",
            f"- Project environment ({uv_version}): {' / '.join(shlex.join(c[:3]) + ' ...' for c in PROJECT_COMMANDS)}; "
            f"imports printed `{project_output[2]}`; kernel `{PROJECT_NAME}` registered",
            "",
        ])
        print(report)
        if args.report:
            args.report.write_text(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
