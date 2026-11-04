# Python research baseline: sample install

Status: one manual run, recorded once. It is not part of CI, which has no
network access to PyPI and would spend minutes compiling or downloading wheels.
`scripts/validate_python_research.py` repeats it.

Recorded: 2026-11-04

## What was run

`python3 scripts/validate_python_research.py`, on a shared workstation under
heavy load (load average between 25 and 85), so the timing below is a ceiling,
not a typical figure.

The script does what `tasks/science-tools.yml` does for `/opt/science-venv`, in
a throwaway directory that it deletes afterwards: create a virtual environment
with the distribution's `python3`, `pip install` the `science_python_packages`
list from `examples/python-research.yml`, then run a short analysis with the
result. It touches no system path and needs no root.

## Result

```
python 3.14.4 (Linux-7.0.0-38-generic-x86_64-with-glibc2.43)
pip 25.1.1
packages requested: numpy scipy pandas matplotlib scikit-learn ipykernel ipywidgets
venv create + install: 460 s
sample result: {"slope": 2.99, "intercept": 2.057, "pearson_r": 0.9985, "numpy": "2.5.4", "scipy": "1.18.1", "pandas": "3.1.0", "matplotlib": "3.11.2", "scikit-learn": "1.9.1", "ipykernel": "7.4.0", "ipywidgets": "8.1.9"}
```

The sample fits a line to synthetic data with scikit-learn, checks the slope,
intercept and correlation against the known answer, writes a matplotlib figure,
and imports `ipykernel` and `ipywidgets`. The run exits non-zero if any step
fails.

These versions are what PyPI served that day. The profile does not pin them:
the next run installs whatever is newest (see "Versions and channels" in
[the profile](../python-research-profile.md)).

## Notebook kernel registration

In the same environment, `python -m ipykernel install --name catalogtest
--prefix <dir>` created `kernels/catalogtest/kernel.json`. The role registers
the "Python 3 (Science)" kernel the same way from `/opt/science-venv`.

## Offline install from a wheel folder

Separately, `pip download -d wheels six packaging` on a connected machine, then
`pip install --no-index --find-links wheels six packaging` in a fresh virtual
environment, installed both packages without any network access. Asking the
same environment for `numpy`, which was not in the folder, failed with "No
matching distribution found" instead of reaching the network. This is the
mechanism [low-bandwidth-and-offline.md](../low-bandwidth-and-offline.md)
describes with `science_python_pip_extra_args`.

## Not covered

- Applying the role itself (`ansible-playbook ... --tags science`) on a clean
  machine. See [clean-room-run-template.md](clean-room-run-template.md).
- The 26-package default list, which includes compiled packages such as
  `pybullet` and `qutip` that were not part of this run.
