"""Only the galaxy-collections job reaches public Galaxy (catalog#182).

galaxy.ansible.com resets connections in bursts. When every job installed
collections on its own, one reset turned a ready merge request red. Now one
job, galaxy-collections, fetches everything with retries
(scripts/fetch-collections.sh) and every other job reads its artifact. These
tests fail when any other job, through its own script lines or a repository
script it runs, names galaxy.ansible.com or runs `ansible-galaxy collection
install` or `download` without `--offline`, or runs ansible-lint without
`--offline` (it installs requirements.yml itself at start otherwise).

What a static scan cannot see (a pytest that shells out, say) meets the
runtime guard: every job but the fetch job has ANSIBLE_GALAXY_SERVER pointed
at a closed local port, so a live fetch fails at once.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
FETCH_JOBS = frozenset({"galaxy-collections"})
FETCH_SCRIPT = "scripts/fetch-collections.sh"

# Keys at the top of .gitlab-ci.yml that are not jobs.
GLOBAL_KEYS = frozenset(
    {"include", "image", "stages", "variables", "workflow", "default", "cache", "services", "before_script", "after_script"}
)
SCRIPT_KEYS = ("before_script", "script", "after_script")
# A repository script a job line runs: scripts/x.sh, python scripts/x.py. Test
# files are left to the runtime guard: they quote these commands as data.
REPO_FILE = re.compile(r"(?<![\w/.-])(?:\./)?(scripts/[\w./-]+\.(?:sh|py))")
GALAXY_HOST = re.compile(r"galaxy\.ansible\.com")
GALAXY_FETCH = re.compile(r"ansible-galaxy\s+(?:(?:collection|role)\s+)?(?:install|download)\b(?P<rest>[^\n]*)")
# ansible-lint installs the project's requirements.yml at start unless offline.
LINT_FETCH = re.compile(r"ansible-lint\b(?P<rest>[^\n]*)")
CLOSED_PORT = "http://127.0.0.1:9/"


def _flatten(value: Any) -> list[str]:
    """GitLab flattens nested script lists (YAML aliases such as *python_tools)."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    lines: list[str] = []
    for item in value:
        lines.extend(_flatten(item))
    return lines


def _resolve(name: str, config: dict[str, Any], seen: tuple[str, ...] = ()) -> dict[str, Any]:
    """Merge a job with what it `extends`, the way GitLab does for these keys."""
    if name in seen:
        raise AssertionError(f"extends cycle: {' -> '.join(seen + (name,))}")
    job = config[name]
    parents = job.get("extends", [])
    if isinstance(parents, str):
        parents = [parents]
    merged: dict[str, Any] = {}
    for parent in parents:
        merged.update(_resolve(parent, config, seen + (name,)))
    merged.update(job)
    return merged


def jobs(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        name: _resolve(name, config)
        for name, job in config.items()
        if not name.startswith(".") and name not in GLOBAL_KEYS and isinstance(job, dict)
    }


def _code(text: str) -> str:
    """Drop whole-line comments: prose may name Galaxy, commands may not."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _reached_files(lines: list[str], root: Path) -> dict[str, str]:
    """Every repository script these lines run, and the scripts those run."""
    found: dict[str, str] = {}
    queue = [match for line in lines for match in REPO_FILE.findall(line)]
    while queue:
        rel = queue.pop()
        if rel in found or not (root / rel).is_file():
            continue
        found[rel] = _code((root / rel).read_text())
        queue.extend(REPO_FILE.findall(found[rel]))
    return found


def galaxy_reaches(text: str) -> list[str]:
    """The lines in TEXT that would reach public Galaxy."""
    hits = [m.group(0) for m in GALAXY_HOST.finditer(text)]
    for pattern in (GALAXY_FETCH, LINT_FETCH):
        hits += [m.group(0).strip() for m in pattern.finditer(text) if "--offline" not in m.group("rest")]
    return hits


def violations(config: dict[str, Any], root: Path) -> dict[str, list[str]]:
    """{job: [where it reaches Galaxy]} for every job but the fetch job(s)."""
    found: dict[str, list[str]] = {}
    for name, job in jobs(config).items():
        if name in FETCH_JOBS:
            continue
        lines = [_code(line) for key in SCRIPT_KEYS for line in _flatten(job.get(key))]
        sources = {"job script": "\n".join(lines), **_reached_files(lines, root)}
        hits = [f"{where}: {hit}" for where, text in sources.items() for hit in galaxy_reaches(text)]
        if hits:
            found[name] = hits
    return found


def _config() -> dict[str, Any]:
    return yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())


def test_no_job_but_the_fetch_job_reaches_galaxy() -> None:
    assert violations(_config(), ROOT) == {}


def test_the_fetch_job_is_the_one_that_fetches() -> None:
    config = _config()
    fetch = jobs(config)["galaxy-collections"]
    assert fetch["stage"] == config["stages"][0]
    assert FETCH_SCRIPT in _flatten(fetch["script"])
    fetch_code = _code((ROOT / FETCH_SCRIPT).read_text())
    assert galaxy_reaches(fetch_code), "the fetch script no longer fetches"
    assert "retry" in fetch_code
    for path in (".ci-collections/", ".ci-collections-floor/", ".ci-collections-user/"):
        assert path in fetch["artifacts"]["paths"]


def test_every_other_job_meets_a_closed_galaxy() -> None:
    config = _config()
    assert config["variables"]["ANSIBLE_GALAXY_SERVER"] == CLOSED_PORT
    for name, job in jobs(config).items():
        server = (job.get("variables") or {}).get("ANSIBLE_GALAXY_SERVER", CLOSED_PORT)
        if name in FETCH_JOBS:
            assert server == "https://galaxy.ansible.com/", name
        else:
            assert server == CLOSED_PORT, f"{name} reopens Galaxy"
    for key in ("ANSIBLE_GALAXY_SERVER_LIST", "ANSIBLE_CONFIG"):
        assert key not in config["variables"], f"{key} would bypass the closed server"


def test_jobs_with_needs_still_get_the_collections() -> None:
    """A job with `needs` gets only the artifacts it lists; .python_setup
    then fails instead of fetching, so the list must name the fetch job."""
    config = _config()
    for name, job in jobs(config).items():
        uses_collections = any(
            "verify --offline -r ci/requirements.yml" in line for line in _flatten(job.get("before_script"))
        )
        if not uses_collections or "needs" not in job:
            continue
        needed = {n if isinstance(n, str) else n["job"] for n in job["needs"]}
        assert needed & FETCH_JOBS, f"{name} needs {sorted(needed)} but not galaxy-collections"


def test_the_guard_catches_a_job_that_fetches(tmp_path: Path) -> None:
    """The guard itself: a direct call, a script that fetches, and the host
    are each caught; an --offline install and a comment are not."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/outer.sh").write_text("#!/bin/sh\nscripts/inner.sh\n")
    (tmp_path / "scripts/inner.sh").write_text("#!/bin/sh\nansible-galaxy collection install -r requirements.yml\n")
    (tmp_path / "scripts/offline.sh").write_text(
        "#!/bin/sh\n# galaxy.ansible.com resets connections\nansible-galaxy collection install --offline -r requirements.yml\n"
    )
    config = {
        "stages": ["prepare", "test"],
        ".base": {"before_script": [["python -m venv .v"], "ansible-galaxy install -r roles.yml"]},
        "galaxy-collections": {"stage": "prepare", "script": ["ansible-galaxy collection install -r ci/requirements.yml"]},
        "direct": {"script": ["ansible-galaxy collection download -r requirements.yml"]},
        "inherited": {"extends": ".base", "script": ["true"]},
        "nested": {"script": ["scripts/outer.sh"]},
        "host": {"script": ["curl -fsS https://galaxy.ansible.com/api/"]},
        "offline": {"script": ["scripts/offline.sh", "ansible-lint --offline roles"]},
        "lint": {"script": ["ANSIBLE_ROLES_PATH=roles ansible-lint roles"]},
    }
    found = violations(config, tmp_path)
    assert sorted(found) == ["direct", "host", "inherited", "lint", "nested"]
    assert any("scripts/inner.sh" in hit for hit in found["nested"])
