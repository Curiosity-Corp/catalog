# Python research profile

A small Python baseline for a researcher who wants Python, `pip`, `uv`,
notebooks, a compiler and the common data libraries, without the 26-package
default list or the desktop stack.

The profile is a variable file, [`examples/python-research.yml`](../examples/python-research.yml),
not a separate code path. It sets `science_python_packages` to a short list and
leaves everything else at the role default.

## What you get

| Piece | Where it comes from | Tags |
| --- | --- | --- |
| `python3`, `python3-venv`, `python3-dev`, `pipx`, `build-essential` | the distribution's APT packages | `packages`, `python` |
| `uv`, `ruff`, `poetry`, `pre-commit`, JupyterLab, IPython | `pipx`, per user | `python` |
| NumPy, SciPy, pandas, matplotlib, scikit-learn, ipykernel, ipywidgets | `pip`, into `/opt/science-venv` | `science`, `data-science` |
| The "Python 3 (Science)" notebook kernel, and the `jupyter` and `ipython` links | registered from `/opt/science-venv` | `science`, `data-science` |

The scientific packages and the notebook kernel apply on the `desktop` and
`workspace` profiles. The example uses `workspace`.

## Use it

1. Copy the file's contents into `/etc/ansible/local-vars.yml` on the machine
   (or pass it with `-e @examples/python-research.yml`).
2. Preview, then apply only these pieces:

   ```bash
   ansible-playbook playbooks/site.yml --tags packages,python,science --check --diff
   ansible-playbook playbooks/site.yml --tags packages,python,science
   ```

3. Check it:

   ```bash
   /opt/science-venv/bin/python -c "import numpy, pandas, scipy; print(numpy.__version__)"
   jupyter kernelspec list        # "Python 3 (Science)" is listed
   ```

[The research VM quickstart](quickstarts/research-vm.md) walks through the same
steps on a fresh machine, and [the notebook guide](quickstarts/notebooks.md)
covers running JupyterLab safely.

## Versions and channels

Nothing in this profile is a release pin, on purpose. The catalog's rule is that
moving applications are resolved to their latest release at run time.

- **Python** is the distribution's `python3`. Its minor version changes with the
  Ubuntu release (3.14 on Ubuntu 26.04). If a distribution upgrade replaces the
  minor version, the role rebuilds `/opt/science-venv`.
- **Packages** come from PyPI, at the latest version, on every run:
  `science_python_packages` is installed with `state: latest`, and
  `science_python_upgrade_packages` is the subset the periodic update refreshes.
  The first working install is recorded in
  [the sample-install evidence](evidence/python-research-sample-install.md) with
  the versions PyPI served that day.
- **Channel**: PyPI, or the index you name in `science_python_pip_extra_args`.

If a project needs exact versions, do not pin them here: freeze them in the
project's own environment (next section), or point `science_python_pip_extra_args`
at a constraints file you maintain:

```yaml
science_python_pip_extra_args: "--constraint /etc/lab/constraints.txt"
```

`science_python_packages` must include `ipykernel`. The role refuses to start the
install without it, because the notebook kernel and the `jupyter` and `ipython`
links depend on it. Requirement specifiers such as `numpy>=2` are allowed.
Setting `science_python_packages` replaces the default list, it does not add to
it.

## Project-level environments

Use `/opt/science-venv` for the shared, always-current tools. Give each project
its own environment so its versions stay put when the shared one moves:

```bash
cd ~/projects/my-study
uv venv                                  # creates .venv with the system Python
uv pip install numpy pandas ipykernel    # or: python3 -m venv .venv && .venv/bin/pip install ...
uv pip freeze > requirements.txt         # exact versions, for your project to keep
.venv/bin/python -m ipykernel install --user --name my-study --display-name "Python (my-study)"
```

`uv init` and `uv add` do the same with a `pyproject.toml` and a lock file, which
is the better choice when you share the project with others. The role updates
`uv` itself; it never touches a project's `.venv`.

## Checking a change to the profile

`tests/test_python_research.py` checks that the example only uses real role
variables, keeps `ipykernel`, that the install step reads the variables, that a
list without `ipykernel` is refused, and that the documented tag set previews.
A real install is not run in CI; the sample-install evidence above and
`scripts/validate_python_research.py` cover that by hand, in a throwaway virtual
environment.

## Not covered

- Applying the role itself on a clean machine: see the
  [clean-room run template](evidence/clean-room-run-template.md).
- Hash-pinned or fully offline installs: see
  [low-bandwidth and offline maintenance](low-bandwidth-and-offline.md).
