"""Tests for scripts/generate_reference.py and the committed docs/reference.md (catalog#27)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("generate_reference", ROOT / "scripts/generate_reference.py")
gen = importlib.util.module_from_spec(_spec)
sys.modules["generate_reference"] = gen
_spec.loader.exec_module(gen)

DEFAULTS = """\
---
# Plain description. Second sentence.
plain: 1
# =============================================================================
# Section heading that is not a description
# =============================================================================
# Real description after the banner.
after_banner: true

not_described: []
# Templated list.
templated: "{{ plain }}"
wifi_psk: hunter2
download_sha256: abc
unsafe_value: !unsafe "{{ not_expanded }}"
mapping:
  nested: value
"""

TASKS = """\
---
- name: Validate
  ansible.builtin.assert:
    that:
      - mode in ['a', 'b']
      - size | int in [1, 2]
      - other in ['x']
      - mode == 'a'
- name: Tagged block
  tags: [outer]
  when: workstation_profile in ['desktop', 'workspace']
  block:
    - name: Inner
      ansible.builtin.debug: {msg: hi}
      tags: [inner]
    - name: Untagged inner
      ansible.builtin.debug: {msg: hi}
- name: Dynamic tags
  ansible.builtin.debug: {msg: hi}
  tags: "{{ tag_list }}"
- name: Always
  ansible.builtin.debug: {msg: hi}
  tags: always
- name: No tags
  ansible.builtin.debug: {msg: hi}
"""


def make_role(root: Path, defaults: str = DEFAULTS, tasks: str = TASKS, extra_defaults: str = "") -> Path:
    role = root / "roles/demo"
    (role / "defaults").mkdir(parents=True)
    (role / "tasks").mkdir()
    (role / "defaults/main.yml").write_text(defaults + extra_defaults)
    (role / "tasks/main.yml").write_text(tasks)
    return root / "roles"


def demo_defaults(extra: str = "") -> str:
    return DEFAULTS.replace("# Templated list.\n", "mode: a\nsize: 1\nother: x\ntag_list: [dyn1, dyn2]\n# Templated list.\n") + extra


def write_annotations(path: Path, body: str = "") -> Path:
    path.write_text("---\n" + body)
    return path


# ---- the committed reference ------------------------------------------------


def test_committed_reference_is_current() -> None:
    assert gen.generate() == (ROOT / "docs/reference.md").read_text(), "run `make reference` and commit docs/reference.md"


def test_reference_covers_every_default_and_tag() -> None:
    text = (ROOT / "docs/reference.md").read_text()
    for role in sorted((ROOT / "roles").iterdir()):
        defaults = yaml.load((role / "defaults/main.yml").read_text(), gen.Loader)
        assert all(f"| `{name}` |" in text for name in defaults)
    roles = gen.load_roles()
    tags = {tag for role in roles.values() for tag in role["tags"]}
    assert tags and all(f"| `{tag}` |" in text for tag in tags)
    assert {"always", "updates", "security"} <= tags


def test_reference_lists_profiles_and_marks_unsupported_variables() -> None:
    text = (ROOT / "docs/reference.md").read_text()
    for profile in ("desktop", "workspace", "thin-client", "byod-kiosk", "deskmeet-b760", "generic"):
        assert f"`{profile}`" in text
    stability = {gen.classify(name, gen.load_annotations())[0] for role in gen.load_roles().values() for name in role["defaults"]}
    assert stability == set(gen.STABILITY)


def test_reference_never_copies_dictionary_defaults_or_secrets() -> None:
    text = (ROOT / "docs/reference.md").read_text()
    assert "ssh-ed25519" not in text
    assert "AAAAC3" not in text
    assert gen.show_default("wifi_psk", "hunter2") == "(redacted)"
    assert gen.show_default("wifi_psk", "") == '`""`'


def test_annotations_use_only_known_names_and_vocabulary() -> None:
    roles = gen.load_roles()
    gen.validate_annotations(
        gen.load_annotations(),
        {name: set(role["defaults"]) for name, role in roles.items()},
        {tag for role in roles.values() for tag in role["tags"]},
    )


def test_destructive_and_secret_variables_carry_safety_flags() -> None:
    annotations = gen.load_annotations()
    assert "destructive" in gen.classify("workstation_storage_provision", annotations)[1]
    assert "secret" in gen.classify("sunshine_default_pass", annotations)[1]
    assert "supply-chain" in gen.classify("missionctl_install_sha256", annotations)[1]
    assert gen.classify("ziti_etc_hosts_pins", annotations)[0] == "private-hook"
    assert gen.classify("workstation_update_state_dir", annotations)[0] == "internal"


# ---- the generator on small fixtures ----------------------------------------


def test_parse_defaults_reads_comments_banners_and_unsafe_tags() -> None:
    parsed = gen.parse_defaults(DEFAULTS)
    assert parsed["plain"]["description"] == "Plain description. Second sentence."
    assert parsed["after_banner"]["description"] == "Real description after the banner."
    assert parsed["not_described"]["description"] == ""
    assert parsed["unsafe_value"]["value"] == "{{ not_expanded }}"
    assert list(parsed)[0] == "plain"


def test_type_and_default_rendering() -> None:
    assert [gen.type_name(v) for v in (None, True, 1, 1.5, "x", "{{ x }}", [], {})] == [
        "null", "bool", "int", "float", "string", "string (template)", "list", "dict",
    ]
    assert gen.show_default("a", {"k": 1}) == "mapping, 1 key"
    assert gen.show_default("a", list(range(100))) == "list, 100 items"
    assert gen.show_default("a", "x" * 100) == "string, 100 characters"
    assert gen.show_default("a", "a|b") == "`\"a\\|b\"`"
    assert gen.show_default("a", True) == "`true`"


def test_tags_include_blocks_templates_and_profiles(tmp_path: Path) -> None:
    roles = make_role(tmp_path, demo_defaults())
    defaults = gen.parse_defaults((roles / "demo/defaults/main.yml").read_text())
    tags = gen.collect_tags(roles / "demo", defaults)
    assert set(tags) == {"outer", "inner", "dyn1", "dyn2", "always"}
    assert tags["outer"]["tasks"] == 3  # the block and both tasks inside it
    assert tags["inner"]["tasks"] == 1
    assert tags["outer"]["profiles"] == {"desktop", "workspace"}
    assert tags["dyn1"]["tasks"] == 1


def test_unresolvable_tag_variable_is_an_error(tmp_path: Path) -> None:
    roles = make_role(tmp_path, DEFAULTS)
    defaults = gen.parse_defaults((roles / "demo/defaults/main.yml").read_text())
    with pytest.raises(gen.ReferenceBuildError, match="tag_list"):
        gen.collect_tags(roles / "demo", defaults)


def test_valid_values_come_from_assert_statements(tmp_path: Path) -> None:
    roles = make_role(tmp_path, demo_defaults())
    found = gen.collect_asserted_values(roles / "demo", {"mode", "size", "other"})
    assert found == {"mode": ["a", "b"], "size": [1, 2], "other": ["x"]}
    assert "mode" not in gen.collect_asserted_values(roles / "demo", {"size"})


def test_generate_renders_a_fixture_role(tmp_path: Path) -> None:
    roles = make_role(tmp_path, demo_defaults())
    annotations = write_annotations(
        tmp_path / "ann.yml",
        "variables:\n  plain: {stability: experimental, safety: network}\n  mode: {note: Extra.}\n"
        "tags:\n  outer: {description: The outer tag.}\n",
    )
    text = gen.generate(roles, annotations)
    assert "| `plain` | int | `1` |  | experimental | network | Plain description. Second sentence. |" in text
    assert "| `mode` | string | `\"a\"` | `\"a\"`, `\"b\"` | supported |  | Extra. |" in text
    assert "| `templated` | string (template) | `\"{{ plain }}\"` |  | supported |  | Templated list. |" in text
    assert "| `wifi_psk` | string | (redacted) |  | supported | secret |" in text
    assert "| `download_sha256` | string | `\"abc\"` |  | supported | supply-chain |" in text
    assert "| `outer` | supported | 3 |" in text and "The outer tag." in text
    assert "| `mapping` | dict | mapping, 1 key |" in text
    assert text.endswith("\n") and not text.endswith("\n\n")


def test_stale_or_misspelled_annotations_fail(tmp_path: Path) -> None:
    roles = make_role(tmp_path, demo_defaults())
    bad = {
        "variables:\n  renamed_away: {stability: internal}\n": "renamed_away",
        "variables:\n  plain: {stability: stable}\n": "unknown stability",
        "variables:\n  plain: {safety: dangerous}\n": "unknown safety",
        "tags:\n  gone: {description: x}\n": "gone",
        "patterns:\n  - match: '('\n    stability: internal\n": "bad pattern",
    }
    for body, message in bad.items():
        path = write_annotations(tmp_path / "ann.yml", body)
        with pytest.raises(gen.ReferenceBuildError, match=message):
            gen.generate(roles, path)


# ---- drift detection --------------------------------------------------------


def test_check_fails_when_a_default_changes_and_passes_after_regeneration(tmp_path: Path, monkeypatch, capsys) -> None:
    roles = make_role(tmp_path, demo_defaults())
    annotations = write_annotations(tmp_path / "ann.yml")
    output = tmp_path / "reference.md"
    monkeypatch.setattr(gen, "ROLES_DIR", roles)
    monkeypatch.setattr(gen, "ANNOTATIONS", annotations)

    assert gen.main(["--check", "--output", str(output)]) == 1  # nothing committed yet
    assert gen.main(["--output", str(output)]) == 0
    assert gen.main(["--check", "--output", str(output)]) == 0

    (roles / "demo/defaults/main.yml").write_text(demo_defaults().replace("plain: 1", "plain: 2"))
    capsys.readouterr()
    assert gen.main(["--check", "--output", str(output)]) == 1
    err = capsys.readouterr().err
    assert "-| `plain` | int | `1`" in err and "+| `plain` | int | `2`" in err
    assert "make reference" in err

    (roles / "demo/tasks/main.yml").write_text(TASKS.replace("[inner]", "[inner, brand-new]"))
    assert gen.main(["--output", str(output)]) == 0
    assert "`brand-new`" in output.read_text()


def test_main_reports_annotation_errors_without_writing(tmp_path: Path, monkeypatch, capsys) -> None:
    def broken() -> str:
        raise gen.ReferenceBuildError("annotations: variable 'x' is not defined")

    monkeypatch.setattr(gen, "generate", broken)
    output = tmp_path / "reference.md"
    assert gen.main(["--output", str(output)]) == 2
    assert not output.exists()
    assert "annotations: variable 'x'" in capsys.readouterr().err
