"""What the StreamStack task files do to a host, rendered without a host (catalog#183).

`plan(source)` walks tasks/streamstack.yml (and the files it includes) plus
tasks/streamstack-framing.yml the way Ansible would for one value of
`streamstack_source`, with the role defaults and a fixed set of facts, and
returns every task that can change the host: its module, its arguments with
every template rendered, its expanded loop, and any condition the walk cannot
decide without a host (kept as written). Tasks that only check or report
(assert, debug) or only set facts are left out; set_fact still feeds the
rendering of the tasks after it. `copy` from a role file or directory and
`template` carry the SHA-256 of the bytes they would write.

A registered result is only known on a host, so a condition or argument that
reads one stays as written (`_streamstack_package_replaced is changed` is part
of the plan as text, not decided either way). Two runs of the same tasks give the same plan, and a change
that alters what a host receives changes the plan; tests/test_streamstack_apt.py
pins the default (`vendored`) plan to the snapshot taken before catalog#183.

    python3 tests/streamstack_plan.py vendored > tests/fixtures/streamstack_vendored_plan.json
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import jinja2
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
TASK_FILES = ("streamstack.yml", "streamstack-framing.yml")
# Modules that change nothing on the host.
QUIET = {"ansible.builtin.assert", "ansible.builtin.debug", "ansible.builtin.set_fact"}
# Task keys that only steer Ansible's reporting, not what reaches the host.
REPORTING = {"name", "when", "register", "loop", "loop_control", "tags", "changed_when", "failed_when", "until",
             "retries", "delay", "block", "rescue", "always", "check_mode", "no_log"}
FACTS = {"architecture": "x86_64", "kernel": "6.8.0-generic", "os_family": "Debian"}


def _environment() -> jinja2.Environment:
    from ansible.plugins.filter.core import FilterModule
    from ansible.plugins.test.core import TestModule

    env = jinja2.Environment(trim_blocks=True, undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
    env.filters.update(FilterModule().filters())
    env.tests.update(TestModule().tests())

    def lookup(plugin: str, *terms: str, **_: Any) -> str:
        if plugin in ("ansible.builtin.file", "file"):
            return (ROLE / "files" / terms[0]).read_text().rstrip("\n")
        raise NotImplementedError(plugin)

    def query(plugin: str, *terms: str, **_: Any) -> list[str]:
        if plugin in ("ansible.builtin.fileglob", "fileglob"):
            # role_path is relative to the checkout, so the plan is the same in every clone.
            return sorted(str(p.relative_to(ROOT)) for p in ROOT.glob(terms[0]) if p.is_file())
        return [lookup(plugin, *terms)]

    env.globals.update(lookup=lookup, query=query, q=query)
    return env


def _sha256(path: Path) -> str:
    if path.is_dir():
        digest = hashlib.sha256()
        # Bytecode a local test run leaves beside the sources is git-ignored and in no checkout a host pulls.
        for child in sorted(p for p in path.rglob("*")
                            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"):
            digest.update(str(child.relative_to(path)).encode() + b"\0" + child.read_bytes() + b"\0")
        return digest.hexdigest()
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Walk:
    def __init__(self, source: str, extra: dict[str, Any] | None = None) -> None:
        self.env = _environment()
        defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
        self.context: dict[str, Any] = dict(defaults)
        self.context.update(streamstack_source=source, ansible_facts=FACTS, ansible_check_mode=False,
                            role_path=str(ROLE.relative_to(ROOT)), dev_users=[], getent_passwd={}, inventory_hostname="host")
        self.context.update(extra or {})
        for _ in range(4):  # defaults that name other defaults
            for key, value in list(self.context.items()):
                if isinstance(value, str) and "{{" in value:
                    try:
                        self.context[key] = self._render(value)
                    except Exception:  # noqa: BLE001 - a default that needs a host stays as written
                        pass
        self.entries: list[dict[str, Any]] = []

    # -- rendering ------------------------------------------------------------------------------------------------

    def _render(self, text: str, **local: Any) -> str:
        return self.env.from_string(text).render({**self.context, **local})

    def _value(self, value: Any, **local: Any) -> Any:
        """Render every string inside `value`; a string that needs a host stays as written."""
        if isinstance(value, str):
            if "{{" not in value and "{%" not in value:
                return value
            try:
                expression = value.strip()
                if (expression.startswith("{{") and expression.endswith("}}")
                        and expression.count("{{") == 1 and "{%" not in expression):
                    return self.env.compile_expression(expression[2:-2].strip())(**{**self.context, **local})
                return self._render(value, **local)
            except Exception:  # noqa: BLE001
                return value
        if isinstance(value, list):
            return [self._value(item, **local) for item in value]
        if isinstance(value, dict):
            return {key: self._value(item, **local) for key, item in value.items()}
        return value

    def _condition(self, condition: Any, **local: Any) -> bool | str:
        if isinstance(condition, bool):
            return condition
        try:
            return self._render("{% if " + str(condition) + " %}1{% else %}0{% endif %}", **local) == "1"
        except Exception:  # noqa: BLE001
            return str(condition)

    def _conjuncts(self, task: dict[str, Any]) -> list[Any]:
        when = task.get("when", [])
        return list(when) if isinstance(when, list) else [when]

    def _decide(self, conditions: list[Any], **local: Any) -> tuple[bool, list[str]]:
        open_conditions = []
        for condition in conditions:
            result = self._condition(condition, **local)
            if result is False:
                return False, []
            if isinstance(result, str):
                open_conditions.append(result)
        return True, open_conditions

    def _loop(self, task: dict[str, Any]) -> list[Any] | None:
        if "loop" not in task:
            return None
        loop = task["loop"]
        if isinstance(loop, str):
            try:
                rendered = self.env.compile_expression(loop.strip().removeprefix("{{").removesuffix("}}").strip())(
                    **self.context)
            except Exception:  # noqa: BLE001
                return [loop]
            return list(rendered) if rendered is not None else []
        return [self._value(item) for item in loop]

    # -- the walk -------------------------------------------------------------------------------------------------

    def tasks(self, tasks: list[dict[str, Any]], inherited: list[Any] | None = None) -> None:
        for task in tasks:
            self.task(task, list(inherited or []))

    def task(self, task: dict[str, Any], inherited: list[Any]) -> None:
        conditions = inherited + self._conjuncts(task)
        if "block" in task:
            for part in ("block", "rescue", "always"):
                self.tasks(task.get(part, []), conditions)
            return
        include = task.get("ansible.builtin.include_tasks")
        if include is not None:
            runs, _ = self._decide(conditions)
            if runs:
                name = include["file"] if isinstance(include, dict) else include
                self.tasks(yaml.safe_load((ROLE / "tasks" / name).read_text()) or [], conditions)
            return
        module = next(key for key in task if key.startswith(("ansible.builtin.", "community.general.")))
        loop = self._loop(task)
        if module == "ansible.builtin.set_fact":
            runs, open_conditions = self._decide(conditions)
            if runs and not open_conditions:
                for key, value in task[module].items():
                    self.context[key] = self._value(value)
            return
        if task.get("register"):
            # Known only on a host: anything that reads it stays as written.
            self.context.pop(task["register"], None)
        if module in QUIET:
            return
        items = loop if loop is not None else [None]
        rendered_items = []
        open_union: list[str] = []
        for item in items:
            local = {} if item is None else {"item": item}
            runs, open_conditions = self._decide(conditions, **local)
            if not runs:
                continue
            open_union.extend(c for c in open_conditions if c not in open_union)
            args = {key: self._value(value, **local) for key, value in task.items() if key not in REPORTING}
            rendered_items.append(self._digests(module, args))
        if not rendered_items:
            return
        entry: dict[str, Any] = {"name": task["name"], "module": module}
        if loop is None:
            entry["args"] = rendered_items[0]
        else:
            entry["items"] = rendered_items
        if open_union:
            entry["when"] = open_union
        self.entries.append(entry)

    def _digests(self, module: str, args: dict[str, Any]) -> dict[str, Any]:
        body = args.get(module)
        if not isinstance(body, dict) or body.get("remote_src"):
            return args
        if module == "ansible.builtin.copy" and isinstance(body.get("src"), str) and "{{" not in body["src"]:
            path = ROLE / "files" / body["src"]
            body = {**body, "src_sha256": _sha256(path) if path.exists() else "missing"}
        if module == "ansible.builtin.template" and isinstance(body.get("src"), str):
            text = self._render((ROLE / "templates" / body["src"]).read_text())
            body = {**body, "rendered_sha256": hashlib.sha256(text.encode()).hexdigest(), "rendered": text}
        return {**args, module: body}


def plan(source: str, extra: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    walk = Walk(source, extra)
    for name in TASK_FILES:
        walk.tasks(yaml.safe_load((ROLE / "tasks" / name).read_text()))
    return walk.entries


if __name__ == "__main__":
    json.dump(plan(sys.argv[1] if len(sys.argv) > 1 else "vendored"), sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
