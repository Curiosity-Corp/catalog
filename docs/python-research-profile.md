# Python research profile

A small Python baseline for a researcher who wants Python, `pip`, `uv`,
notebooks, compilers and the common data libraries, without the 26-package
default list or the desktop stack.

The profile is a variable file, [`examples/python-research.yml`](../examples/python-research.yml),
not a separate code path. It selects the `workspace` profile (no desktop) and
sets `science_python_packages` to a short list; everything else stays at the
role default.

## What you get

| Piece | Where it comes from | Tags |
| --- | --- | --- |
| `python3`, `python3-venv`, `python3-dev`, `python3-pip`, `pipx` | the distribution's APT packages | `packages` |
| Compilers and build tools: `build-essential`, `gcc`, `g++`, `clang`, `cmake`, `meson`, `ninja-build`, `pkg-config` | the distribution's APT packages | `packages` |
| `uv`, `ruff`, `poetry`, `pre-commit`, JupyterLab, IPython | `pipx`, per user, latest from PyPI | `python` |
| NumPy, SciPy, pandas, matplotlib, scikit-learn, ipykernel, ipywidgets | `pip`, into `science_python_venv` (`/opt/science-venv`) | `science`, `data-science` |
| The "Python 3 (Science)" notebook kernel, and the `jupyter` and `ipython` links | registered from `science_python_venv` | `science`, `data-science` |

The `packages` tag installs the whole `workspace` APT set (it also carries
Node.js, Java and Ruby); there is no separate APT list for this profile.

## Use it

1. Copy the file's contents into `/etc/ansible/local-vars.yml` on the machine,
   or pass it with `-e @examples/python-research.yml`.
2. Preview, then apply only these pieces:

   ```bash
   ansible-playbook playbooks/site.yml -e @examples/python-research.yml --tags packages,python,science --check --diff
   ansible-playbook playbooks/site.yml -e @examples/python-research.yml --tags packages,python,science
   ```

3. Check it:

   ```bash
   /opt/science-venv/bin/python -c "import numpy, pandas, scipy; print(numpy.__version__)"
   jupyter kernelspec list        # "science" (Python 3 (Science)) is listed
   ```

## Variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `science_python_packages` | 26 packages (see `roles/developer_workstation/defaults/main.yml`) | pip requirements installed into the venv with `state: latest`. Setting it replaces the default list; it does not add to it. Must include `ipykernel`. |
| `science_python_upgrade_packages` | 17 of the defaults | The packages every later run upgrades again, so their dependencies stay current. An empty list skips that step. |
| `science_python_pip_extra_args` | `""` | Extra `pip` arguments: a lab index (`--index-url ...`), a constraints file (`--constraint ...`), or a wheel folder (`--no-index --find-links ...`). |
| `science_python_venv` | `/opt/science-venv` | Where the shared environment lives. CI points it at a throwaway directory; a machine normally keeps the default. |

The role checks these before it touches the venv and stops with a message if
the package list is empty, is not a list, or has no `ipykernel` (the notebook
kernel and the `jupyter` and `ipython` links depend on it). Requirement
specifiers such as `numpy>=2` and extras such as `ipykernel[all]` are allowed.

## Versions and channels

Nothing in this profile is a release pin, on purpose: the catalog resolves
moving software to its latest release at run time.

- **Python** is the distribution's `python3`, so its version follows the
  operating system (3.14 on Ubuntu 26.04). When a distribution upgrade replaces
  the minor version, the role rebuilds the venv and reinstalls the list.
- **Science packages** come from PyPI at their latest version on every run
  (`state: latest`). The channel is PyPI unless `science_python_pip_extra_args`
  names another index.
- **`uv` and the other command-line tools** come from PyPI through `pipx`, at
  their latest version on every run.
- **Compilers and system libraries** come from the distribution's APT archive
  and move with its security and point updates.

To hold versions for a whole lab, do not pin them in the package list: keep a
constraints file and name it. Start from
[`examples/python-research-constraints.txt`](../examples/python-research-constraints.txt),
a commented example with synthetic ranges, copy it to the machine as
`/etc/lab/constraints.txt` with the ranges your lab agreed on, and set:

```yaml
science_python_pip_extra_args: "--constraint /etc/lab/constraints.txt"
```

A constraints file only limits versions of packages that are already being
installed; it never adds one to the environment.

To hold versions for one study, give the study its own environment (next
section).

## Project-level environments

Use `/opt/science-venv` for the shared, always-current tools. Give each project
its own environment, so its versions stay put when the shared one moves. With
`uv` (installed per user by the `python` tag):

```bash
mkdir -p ~/projects/my-study && cd ~/projects/my-study
uv init --bare
uv add numpy pandas ipykernel
uv run python -c 'import numpy, pandas; print(numpy.__version__, pandas.__version__)'
uv run python -m ipykernel install --user --name my-study --display-name 'Python (my-study)'
```

- `uv init --bare` writes only a `pyproject.toml`.
- `uv add` resolves the packages, records exact versions in `uv.lock`, and
  creates `.venv` with the system Python. Commit `pyproject.toml` and `uv.lock`
  so a colleague gets the same versions with `uv sync`.
- `uv run` runs a command in that environment without activating it.
- The last line adds the project to JupyterLab as "Python (my-study)".

Without `uv`, the standard library does the same with less help:

```bash
python3 -m venv .venv
.venv/bin/pip install numpy pandas ipykernel
.venv/bin/pip freeze > requirements.txt
```

The role keeps `uv` itself current; it never touches a project's `.venv`.

## How it is validated

The `research-profile` CI job runs
[`scripts/validate_python_research.py`](../scripts/validate_python_research.py)
on Python 3.14 (the Ubuntu 26.04 interpreter the role uses) and on the newest
CPython release, in a container that is thrown away afterwards. The script:

1. applies the role's own `tasks/science-venv.yml` with
   `examples/python-research.yml` into a temporary `science_python_venv`, twice,
   and fails if the second run changes anything;
2. runs a sample analysis with that venv (a scikit-learn fit checked against
   the known answer with SciPy, a matplotlib figure, the notebook packages
   imported);
3. registers the venv as a notebook kernel the way the role does;
4. runs the `uv` commands above, verbatim, in a temporary home directory.

The job's report (`research-profile-report.md`) lists the versions PyPI served
that day. Run it yourself with network access to PyPI, `ansible-playbook` and
`uv` on `PATH`:

```bash
python3 scripts/validate_python_research.py
```

`tests/test_python_research.py` checks the rest without a network: the example
uses only real role variables and keeps `ipykernel`, both pip steps read the
variables, the validation step accepts and refuses the right lists, the
documented tag set previews, and this page shows the same `uv` commands the
script runs.

## Not covered

- Applying the whole role on a clean machine (APT packages, `pipx`, the system
  kernel registration under `/usr/local`): CI has no root and runs nothing
  against real hosts.
- The 26-package default list, which includes compiled packages such as
  `pybullet` and `qutip`; the job validates the small profile.
