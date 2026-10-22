"""Every tagged include in the workstation role passes its tags on to the included tasks.

A tag on a dynamic `include_tasks` selects only the include itself, so without
`apply: tags` a focused pull such as `--tags logitech-g604` ran the include and
none of the tasks in it, and reported success (Curiosity-Corp/catalog#104).
"""

from __future__ import annotations

from pathlib import Path

import yaml

TASKS = Path(__file__).resolve().parents[1] / "roles/developer_workstation/tasks"


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))


def _walk(tasks):
    for task in tasks or []:
        yield task
        for key in ("block", "rescue", "always"):
            yield from _walk(task.get(key))


def _tagged_includes():
    for path in sorted(TASKS.glob("*.yml")):
        for task in _walk(yaml.load(path.read_text(), _Loader)):
            include = task.get("ansible.builtin.include_tasks", task.get("include_tasks"))
            if include is not None and task.get("tags") not in (None, [], ["always"], "always"):
                yield path.name, task, include


def test_main_has_tagged_includes_to_check() -> None:
    assert sum(1 for name, _, _ in _tagged_includes() if name == "main.yml") >= 40


def test_every_tagged_include_applies_its_tags_to_the_included_tasks() -> None:
    missing = [
        f"{name}: {task['name']}"
        for name, task, include in _tagged_includes()
        if not isinstance(include, dict) or include.get("apply", {}).get("tags") != task["tags"]
    ]
    assert not missing, "tagged include_tasks without matching apply.tags:\n" + "\n".join(missing)
