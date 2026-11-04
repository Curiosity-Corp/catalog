#!/usr/bin/env python3
"""Install the example Python research baseline into a throwaway venv and use it.

This is the manual validation behind docs/python-research-profile.md. It needs
network access to PyPI and a few minutes; it never touches /opt/science-venv or
any system path. It does what the role does for /opt/science-venv: create a venv
with the distribution Python, then `pip install` the declared packages.

    python3 scripts/validate_python_research.py            # temp directory
    python3 scripts/validate_python_research.py --keep DIR  # keep the venv

Prints a Markdown block (versions, timings, results) suitable for
docs/evidence/. Exit status is non-zero if the install or the sample fails.
"""

from __future__ import annotations

import argparse
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/python-research.yml"

SAMPLE = r"""
import json, sys
import numpy as np, pandas as pd, scipy, sklearn, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy import stats
from sklearn.linear_model import LinearRegression

rng = np.random.default_rng(0)
x = np.linspace(0, 10, 200)
y = 3.0 * x + 2.0 + rng.normal(0, 0.5, x.size)
frame = pd.DataFrame({"x": x, "y": y})
model = LinearRegression().fit(frame[["x"]], frame["y"])
slope, intercept = float(model.coef_[0]), float(model.intercept_)
r = stats.pearsonr(frame["x"], frame["y"]).statistic
fig, ax = plt.subplots()
ax.plot(frame["x"], frame["y"])
fig.savefig(sys.argv[1])
assert abs(slope - 3.0) < 0.1 and abs(intercept - 2.0) < 0.3 and r > 0.99
import ipykernel, ipywidgets
print(json.dumps({
    "slope": round(slope, 3), "intercept": round(intercept, 3), "pearson_r": round(float(r), 4),
    "numpy": np.__version__, "scipy": scipy.__version__, "pandas": pd.__version__,
    "matplotlib": matplotlib.__version__, "scikit-learn": sklearn.__version__,
    "ipykernel": ipykernel.__version__, "ipywidgets": ipywidgets.__version__,
}))
"""


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", metavar="DIR", help="build the venv here and keep it")
    args = parser.parse_args()
    packages = yaml.safe_load(EXAMPLE.read_text())["science_python_packages"]

    with tempfile.TemporaryDirectory(prefix="py-research-") as tmp:
        base = Path(args.keep) if args.keep else Path(tmp)
        base.mkdir(parents=True, exist_ok=True)
        venv = base / "venv"
        started = time.monotonic()
        created = run([sys.executable, "-m", "venv", str(venv)])
        if created.returncode:
            print(created.stderr, file=sys.stderr)
            return 1
        py = venv / "bin/python"
        installed = run([str(py), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", *packages])
        elapsed = time.monotonic() - started
        if installed.returncode:
            print(installed.stderr, file=sys.stderr)
            return 1
        ran = run([str(py), "-c", SAMPLE, str(base / "sample.png")])
        if ran.returncode:
            print(ran.stdout, ran.stderr, file=sys.stderr)
            return 1
        pip_version = run([str(py), "-m", "pip", "--version"]).stdout.split()[1]
        print("```")
        print(f"python {platform.python_version()} ({platform.platform()})")
        print(f"pip {pip_version}")
        print(f"packages requested: {' '.join(packages)}")
        print(f"venv create + install: {elapsed:.0f} s")
        print(f"sample result: {ran.stdout.strip()}")
        print("```")
    return 0


if __name__ == "__main__":
    sys.exit(main())
