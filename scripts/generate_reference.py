#!/usr/bin/env python3
"""Generate docs/reference.md, the supported variable and tag reference.

The reference is derived from the roles themselves so it cannot drift quietly:

* variables, types and defaults come from ``roles/<role>/defaults/main.yml``,
  and the description is the comment block directly above each variable;
* valid values come from ``ansible.builtin.assert`` statements in the role's
  tasks (``VAR in [...]``) and from ``docs/reference-annotations.yml``;
* tags come from the ``tags:`` keys of the role's task files (including tags
  inherited from a ``block`` and tags resolved from a variable);
* stability (supported, experimental, internal, private-hook) and safety impact
  are curated in ``docs/reference-annotations.yml``, plus a few name rules for
  credentials and integrity pins that live in this script.

Usage::

    python scripts/generate_reference.py            # rewrite docs/reference.md
    python scripts/generate_reference.py --check    # exit 1 if it is out of date

Only PyYAML is needed. Default values of dictionaries and long lists are
summarised, never copied, so the reference cannot leak host data from a profile.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLES_DIR = ROOT / "roles"
ANNOTATIONS = ROOT / "docs/reference-annotations.yml"
OUTPUT = ROOT / "docs/reference.md"

STABILITY = {
    "supported": "Part of the public configuration API. Changes follow the deprecation policy.",
    "experimental": "Works today but may change or be removed without a deprecation period.",
    "internal": "Implementation detail (derived value, file path or contract table). Do not set it; it may change in any release.",
    "private-hook": "Integration point for a private service or organisation. The collection ships it empty; the value belongs in protected host variables, not in a public repository.",
}

SAFETY = {
    "secret": "Credential, token or key. Keep it in protected host variables or a vault, never in git.",
    "destructive": "Can erase, repartition or reformat data.",
    "security": "Changes the host's security posture (firewall, SSH, integrity checks, certificate trust).",
    "network": "Changes networking, DNS or VPN routing. A wrong value can cut off remote access.",
    "boot": "Changes boot, console or display configuration. A wrong value can leave the host without a usable display.",
    "supply-chain": "Downloads and installs software from a configured location. Pin it with the matching checksum or fingerprint.",
    "account": "Creates, changes or grants access to user accounts.",
}

# Name rules that need no curation: a variable named like a credential is a
# credential, and a variable named like a checksum is an integrity pin.
AUTO_SAFETY = (
    (re.compile(r"(^|_)(pass|password|psk|token|secret)(_|$)"), "secret"),
    (re.compile(r"_(sha256|fingerprint)$"), "supply-chain"),
)
SECRET_NAME = AUTO_SAFETY[0][0]

TOP_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(?:\s|$)")
RULE_LINE = re.compile(r"^[=\-#*_ ]{4,}$")
PROFILE_NAMES = ("desktop", "workspace", "thin-client", "byod-kiosk")
MAX_DEFAULT = 60
MAX_DESCRIPTION = 300


class Loader(getattr(yaml, "CSafeLoader", yaml.SafeLoader)):
    """Safe loader that also accepts the ``!unsafe`` tag used in role defaults."""


Loader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))


def load_yaml(text: str) -> Any:
    return yaml.load(text, Loader)  # noqa: S506 - Loader is a SafeLoader subclass


@lru_cache(maxsize=None)
def load_task_text(text: str) -> Any:
    """Parse a task file once per distinct content; the result must not be mutated."""
    return load_yaml(text)


class ReferenceBuildError(Exception):
    """The roles and the annotations disagree; the message says how."""


# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------


def parse_defaults(text: str) -> dict[str, dict[str, Any]]:
    """Return ``{name: {"value": ..., "description": str}}`` in file order."""
    values = load_yaml(text) or {}
    lines = text.splitlines()
    found: dict[str, list[str]] = {}
    for index, line in enumerate(lines):
        match = TOP_KEY.match(line)
        if not match:
            continue
        block: list[str] = []
        cursor = index - 1
        while cursor >= 0 and lines[cursor].lstrip().startswith("#") and not lines[cursor].startswith(" "):
            block.append(lines[cursor].lstrip("#").strip())
            cursor -= 1
        block.reverse()
        # A banner (a line of = or - characters) closes a section heading: only
        # the text after the last one describes the variable.
        for position in range(len(block) - 1, -1, -1):
            if RULE_LINE.match(block[position]):
                block = block[position + 1 :]
                break
        found[match.group(1)] = [part for part in block if part]
    if list(found) != list(values):
        raise ReferenceBuildError("defaults parser disagrees with YAML loader about the top-level variables")
    return {
        name: {"value": values[name], "lines": found[name], "description": " ".join(found[name]).strip()}
        for name in values
    }


# --------------------------------------------------------------------------
# Tasks: tags and asserted values
# --------------------------------------------------------------------------


def walk_tasks(tasks: Any, inherited: tuple[str, ...] = (), conditions: tuple[str, ...] = ()) -> Iterator[tuple[dict, tuple, tuple]]:
    """Yield ``(task, inherited tags, inherited conditions)`` for every task, including nested blocks."""
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task, inherited, conditions
        own = as_list(task.get("tags"))
        when = tuple(str(item) for item in as_list(task.get("when")))
        for key in ("block", "rescue", "always"):
            yield from walk_tasks(task.get(key), inherited + tuple(own), conditions + when)


def as_list(value: Any) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def resolve_tags(raw: Any, defaults: dict[str, dict[str, Any]]) -> list[str]:
    """Flatten a ``tags:`` value; a ``"{{ variable }}"`` entry resolves to that variable's default list."""
    tags: list[str] = []
    for item in as_list(raw):
        match = re.fullmatch(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", str(item))
        if match:
            if match.group(1) not in defaults:
                raise ReferenceBuildError(f"tag expression {item!r} names a variable that has no default")
            tags.extend(str(tag) for tag in as_list(defaults[match.group(1)]["value"]))
        else:
            tags.append(str(item))
    return tags


def task_files(role: Path) -> list[Path]:
    return sorted((role / "tasks").glob("*.yml"))


def collect_tags(role: Path, defaults: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return ``{tag: {"files": set, "tasks": int, "profiles": set}}`` for the role."""
    tags: dict[str, dict[str, Any]] = {}
    for path in task_files(role):
        for task, inherited, conditions in walk_tasks(load_task_text(path.read_text())):
            own = resolve_tags(task.get("tags"), defaults)
            effective = set(resolve_tags(list(inherited), defaults)) | set(own)
            if not own and not effective:
                continue
            when = conditions + tuple(str(item) for item in as_list(task.get("when")))
            profiles = {
                name
                for condition in when
                if "workstation_profile" in condition
                for name in PROFILE_NAMES
                if f"'{name}'" in condition or f'"{name}"' in condition
            }
            for tag in effective:
                entry = tags.setdefault(tag, {"files": set(), "tasks": 0, "profiles": set()})
                entry["files"].add(path.name)
                entry["tasks"] += 1
                entry["profiles"] |= profiles
    return tags


ASSERT_IN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)(?:\s*\|\s*[A-Za-z_]+(?:\([^)]*\))?)*\s+in\s+(\[.*\])\s*$")


def collect_asserted_values(role: Path, names: set[str]) -> dict[str, list]:
    """Valid values stated by ``assert`` tasks as ``VAR in [...]``."""
    found: dict[str, list] = {}
    for path in task_files(role):
        for task, _, _ in walk_tasks(load_task_text(path.read_text())):
            module = task.get("ansible.builtin.assert", task.get("assert"))
            if not isinstance(module, dict):
                continue
            for condition in as_list(module.get("that")):
                match = ASSERT_IN.match(str(condition))
                if not match or match.group(1) not in names:
                    continue
                try:
                    values = yaml.safe_load(match.group(2))
                except yaml.YAMLError:
                    continue
                if isinstance(values, list):
                    found.setdefault(match.group(1), values)
    return found


# --------------------------------------------------------------------------
# Annotations
# --------------------------------------------------------------------------


def load_annotations(path: Path | None = None) -> dict[str, Any]:
    data = load_yaml((path or ANNOTATIONS).read_text()) or {}
    for key in ("patterns", "variables", "tags", "profile_notes"):
        data.setdefault(key, [] if key == "patterns" else {})
    return data


def check_annotation(where: str, entry: dict[str, Any], *, allow_safety: bool) -> None:
    stability = entry.get("stability")
    if stability is not None and stability not in STABILITY:
        raise ReferenceBuildError(f"{where}: unknown stability {stability!r} (use one of {', '.join(STABILITY)})")
    for flag in as_list(entry.get("safety")):
        if not allow_safety or flag not in SAFETY:
            raise ReferenceBuildError(f"{where}: unknown safety flag {flag!r} (use one of {', '.join(SAFETY)})")


def validate_annotations(annotations: dict[str, Any], variables: dict[str, set[str]], tags: set[str]) -> None:
    """Fail when annotations name a variable or tag that no longer exists, or use unknown vocabulary."""
    known = set().union(*variables.values()) if variables else set()
    for name, entry in annotations["variables"].items():
        if name not in known:
            raise ReferenceBuildError(f"annotations: variable {name!r} is not defined in any role defaults; remove or rename it")
        check_annotation(f"annotations: variable {name}", entry, allow_safety=True)
    for tag, entry in annotations["tags"].items():
        if tag not in tags:
            raise ReferenceBuildError(f"annotations: tag {tag!r} is not used by any role task; remove or rename it")
        check_annotation(f"annotations: tag {tag}", entry, allow_safety=False)
    for rule in annotations["patterns"]:
        try:
            re.compile(rule["match"])
        except (KeyError, re.error) as error:
            raise ReferenceBuildError(f"annotations: bad pattern rule {rule!r}: {error}") from error
        check_annotation(f"annotations: pattern {rule['match']}", rule, allow_safety=True)


def classify(name: str, annotations: dict[str, Any]) -> tuple[str, list[str], Any, str]:
    """Return ``(stability, safety flags, valid values override, note)`` for a variable."""
    stability = "supported"
    safety: list[str] = []
    values: Any = None
    note = ""
    for rule in annotations["patterns"]:
        if re.search(rule["match"], name):
            stability = rule.get("stability", stability)
            safety += as_list(rule.get("safety"))
    for pattern, flag in AUTO_SAFETY:
        if pattern.search(name):
            safety.append(flag)
    explicit = annotations["variables"].get(name, {})
    stability = explicit.get("stability", stability)
    safety += as_list(explicit.get("safety"))
    values = explicit.get("values")
    note = str(explicit.get("note", "")).strip()
    return stability, sorted(set(safety), key=list(SAFETY).index), values, note


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def cell(text: str) -> str:
    """Escape text for a Markdown table cell."""
    return text.replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;").replace("\n", " ")


def code(text: str) -> str:
    """A code span that survives a table cell."""
    return "`" + text.replace("|", "\\|").replace("`", "'") + "`"


def type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string (template)" if "{{" in value else "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return type(value).__name__


def show_default(name: str, value: Any) -> str:
    """A short, safe rendering of a default. Never copies dictionaries or long values."""
    if isinstance(value, dict):
        return f"mapping, {len(value)} key{'s' if len(value) != 1 else ''}"
    if isinstance(value, str) and "{{" in value and len(value) > MAX_DEFAULT:
        return "template expression"
    if isinstance(value, str) and "\n" in value:
        return "multi-line text"
    if value not in ("", None, [], False, 0) and SECRET_NAME.search(name):
        return "(redacted)"
    if isinstance(value, bool):
        return code("true" if value else "false")
    if value is None:
        return code("null")
    text = json.dumps(value, ensure_ascii=False)
    if len(text) > MAX_DEFAULT:
        if isinstance(value, list):
            return f"list, {len(value)} item{'s' if len(value) != 1 else ''}"
        return f"string, {len(value)} characters"
    return code(text)


def summarize(text: str) -> str:
    """Keep whole sentences up to MAX_DESCRIPTION characters; the full text stays in the defaults file."""
    if len(text) <= MAX_DESCRIPTION:
        return text
    kept = ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if kept and len(kept) + len(sentence) + 1 > MAX_DESCRIPTION:
            break
        kept = f"{kept} {sentence}".strip()
    if len(kept) > MAX_DESCRIPTION:
        kept = kept[: MAX_DESCRIPTION - 1].rsplit(" ", 1)[0]
    return kept + " (...)"


def show_values(values: Any) -> str:
    if values is None:
        return ""
    if isinstance(values, str):
        return cell(values)
    return ", ".join(code(json.dumps(item, ensure_ascii=False)) for item in values)


def render(roles: dict[str, dict[str, Any]], annotations: dict[str, Any]) -> str:
    out: list[str] = []
    add = out.append
    add("# Supported variable and tag reference")
    add("")
    add("<!-- Generated by scripts/generate_reference.py. Do not edit by hand: change the role defaults,")
    add("     the role tasks or docs/reference-annotations.yml, then run `make reference`. -->")
    add("")
    add(
        "This is the public configuration surface of the `curiosity.workstation` collection: every role variable "
        "with its type, default, valid values, stability and safety impact, the tags a focused run can select, "
        "and the profile selectors. It is generated from the role defaults and tasks; CI fails when it is out of date "
        "(`make reference-check`). [configuration.md](configuration.md) explains the options in prose."
    )
    add("")
    add("## How to read this reference")
    add("")
    add("Stability:")
    add("")
    for key, text in STABILITY.items():
        add(f"- **{key}**: {text}")
    add("")
    add(
        "Variables not marked otherwise are **supported**. The marking is curated in "
        "[reference-annotations.yml](reference-annotations.yml); a variable missing from it is supported by default, "
        "so review new variables when they are added."
    )
    add("")
    add("Safety impact (a variable can carry several flags; blank means no special concern):")
    add("")
    for key, text in SAFETY.items():
        add(f"- **{key}**: {text}")
    add("")
    add("Defaults of dictionaries and long values are summarised here; read the role's `defaults/main.yml` for the full value.")
    add("")

    selectors = roles["developer_workstation"]["defaults"] if "developer_workstation" in roles else {}
    add("## Profiles")
    add("")
    add("Two independent selectors choose what a host converges. Any `workstation_profile` can be combined with any hardware profile.")
    add("")
    profile_values = roles.get("developer_workstation", {}).get("asserted", {}).get("workstation_profile")
    if profile_values:
        add(f"- `workstation_profile` (default {show_default('workstation_profile', selectors['workstation_profile']['value'])}):")
        described = dict(
            m.groups() for m in (re.match(r"^(\S+)\s*=\s*(.+)$", line) for line in selectors["workstation_profile"]["lines"]) if m
        )
        described = {**annotations.get("profile_notes", {}), **described}
        for value in profile_values:
            add(f"  - {code(str(value))}: {cell(described.get(str(value), '(not described)'))}")
    if "hardware_profiles" in selectors:
        profiles = selectors["hardware_profiles"]["value"]
        add(f"- `hardware_profile` (default {show_default('hardware_profile', selectors['hardware_profile']['value'])}): "
            + ", ".join(code(str(v)) for v in profiles) + ".")
        add("")
        add("Settings each hardware profile defines in `hardware_profiles` (names only):")
        add("")
        add("| Hardware profile | Description | Settings |")
        add("| --- | --- | --- |")
        for profile, body in profiles.items():
            keys = ", ".join(code(key) for key in body if key != "description")
            add(f"| {code(profile)} | {cell(str(body.get('description', '')))} | {keys} |")
    add("")

    for role_name, role in roles.items():
        add(f"## Tags: `{role_name}`")
        add("")
        tags = role["tags"]
        if not tags:
            add("This role defines no tags: it runs in full, or not at all through `workstation_storage_enabled`.")
            add("")
            continue
        add(
            "Select tags with `--tags` or exclude them with `--skip-tags`. A task carries a tag directly or through its "
            "`block`. Tasks tagged `always` run on every invocation. The profiles column lists the "
            "`workstation_profile` values named in the tasks' conditions; blank means the tag is not profile-gated."
        )
        add("")
        add("| Tag | Status | Tasks | Task files | Profiles | Description |")
        add("| --- | --- | --- | --- | --- | --- |")
        for tag in sorted(tags):
            info = tags[tag]
            ann = annotations["tags"].get(tag, {})
            description = ann.get("description", "")
            if tag == "always" and not description:
                description = "Runs on every invocation; `--skip-tags always` is the only way to skip it."
            files = ", ".join(code(f) for f in sorted(info["files"]))
            profiles_text = ", ".join(code(p) for p in PROFILE_NAMES if p in info["profiles"])
            add(f"| {code(tag)} | {ann.get('stability', 'supported')} | {info['tasks']} | {files} | {profiles_text} | {cell(str(description))} |")
        add("")

    add("## Variables")
    add("")
    index: list[tuple[str, str, str, str]] = []
    for role_name, role in roles.items():
        for name in role["defaults"]:
            stability, _, _, note = classify(name, annotations)
            if stability != "supported":
                index.append((stability, name, role_name, note))
    if index:
        add("### Not part of the supported surface")
        add("")
        add("Variables marked experimental, internal or private-hook. Do not depend on internal ones; set private hooks only from protected host variables.")
        add("")
        add("| Variable | Role | Status |")
        add("| --- | --- | --- |")
        order = list(STABILITY)
        for stability, name, role_name, _ in sorted(index, key=lambda row: (order.index(row[0]), row[1])):
            add(f"| {code(name)} | {code(role_name)} | {stability} |")
        add("")
    for role_name, role in roles.items():
        add(f"### Role `{role_name}`")
        add("")
        add(f"{len(role['defaults'])} variables, from `roles/{role_name}/defaults/main.yml`.")
        add("")
        add("| Variable | Type | Default | Valid values | Status | Safety impact | Description |")
        add("| --- | --- | --- | --- | --- | --- | --- |")
        for name, info in role["defaults"].items():
            stability, safety, override, note = classify(name, annotations)
            values = override if override is not None else role["asserted"].get(name)
            if name == "hardware_profile" and "hardware_profiles" in role["defaults"]:
                values = values or list(role["defaults"]["hardware_profiles"]["value"])
            description = " ".join(part for part in (summarize(info["description"]), note) if part) or "(undocumented)"
            add(
                f"| {code(name)} | {type_name(info['value'])} | {show_default(name, info['value'])} | {show_values(values)} | "
                f"{stability} | {', '.join(safety)} | {cell(description)} |"
            )
        add("")
    return "\n".join(out).rstrip("\n") + "\n"


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


def load_roles(roles_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    roles_dir = roles_dir or ROLES_DIR
    roles: dict[str, dict[str, Any]] = {}
    for role in sorted(path for path in roles_dir.iterdir() if (path / "defaults/main.yml").is_file()):
        defaults = parse_defaults((role / "defaults/main.yml").read_text())
        roles[role.name] = {
            "defaults": defaults,
            "tags": collect_tags(role, defaults),
            "asserted": collect_asserted_values(role, set(defaults)),
        }
    return roles


def generate(roles_dir: Path | None = None, annotations_path: Path | None = None) -> str:
    roles = load_roles(roles_dir or ROLES_DIR)
    annotations = load_annotations(annotations_path or ANNOTATIONS)
    validate_annotations(
        annotations,
        {name: set(role["defaults"]) for name, role in roles.items()},
        set().union(*(set(role["tags"]) for role in roles.values())) if roles else set(),
    )
    return render(roles, annotations)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the output file is out of date instead of writing it")
    parser.add_argument("--output", type=Path, default=OUTPUT, help="reference file (default: docs/reference.md)")
    args = parser.parse_args(argv)
    try:
        text = generate()
    except ReferenceBuildError as error:
        print(f"generate_reference: {error}", file=sys.stderr)
        return 2
    current = args.output.read_text() if args.output.is_file() else ""
    if args.check:
        if current == text:
            print(f"{args.output} is up to date")
            return 0
        diff = difflib.unified_diff(current.splitlines(), text.splitlines(), "committed", "generated", lineterm="", n=1)
        print("\n".join(list(diff)[:60]), file=sys.stderr)
        print(
            f"\n{args.output} is out of date with the role defaults, tasks or annotations. "
            "Run `make reference` and commit the result.",
            file=sys.stderr,
        )
        return 1
    args.output.write_text(text)
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
