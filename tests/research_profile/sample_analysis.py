"""A small analysis that the Python research profile must be able to run.

Run with the science venv's interpreter; argv[1] is where the figure goes.
Fits a line to synthetic data with scikit-learn, checks it against the known
answer with SciPy, writes a matplotlib figure, imports the notebook packages,
and prints one JSON line with the results and the installed versions.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: sample_analysis.py FIGURE.png", file=sys.stderr)
        return 2
    figure = Path(sys.argv[1])

    import ipykernel
    import ipywidgets
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    import pandas as pd
    import scipy
    import sklearn
    from scipy import stats
    from sklearn.linear_model import LinearRegression

    rng = np.random.default_rng(0)
    x = np.linspace(0, 10, 200)
    y = 3.0 * x + 2.0 + rng.normal(0, 0.5, x.size)
    frame = pd.DataFrame({"x": x, "y": y})
    model = LinearRegression().fit(frame[["x"]], frame["y"])
    slope, intercept = float(model.coef_[0]), float(model.intercept_)
    r = float(stats.pearsonr(frame["x"], frame["y"]).statistic)

    fig, ax = plt.subplots()
    ax.plot(frame["x"], frame["y"])
    fig.savefig(figure)
    plt.close(fig)

    problems = []
    if abs(slope - 3.0) >= 0.1:
        problems.append(f"slope {slope:.3f} is not 3.0")
    if abs(intercept - 2.0) >= 0.3:
        problems.append(f"intercept {intercept:.3f} is not 2.0")
    if r <= 0.99:
        problems.append(f"pearson r {r:.4f} is not above 0.99")
    if not figure.is_file() or figure.stat().st_size == 0:
        problems.append(f"no figure written to {figure}")
    if problems:
        print("sample analysis failed: " + "; ".join(problems), file=sys.stderr)
        return 1

    print(json.dumps({
        "python": sys.version.split()[0],
        "slope": round(slope, 3),
        "intercept": round(intercept, 3),
        "pearson_r": round(r, 4),
        "numpy": np.__version__,
        "scipy": scipy.__version__,
        "pandas": pd.__version__,
        "matplotlib": matplotlib.__version__,
        "scikit-learn": sklearn.__version__,
        "ipykernel": ipykernel.__version__,
        "ipywidgets": ipywidgets.__version__,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
