"""catalog#151: a user's own dotfiles review gate, and no rewrites of a chezmoi home.

user_dotfiles_apply: review hands $HOME to the dotfiles repo's own review gate
(user_dotfiles_review_argv), so catalog never runs chezmoi --force over an owner's
edits. A user who already has a chezmoi source owns their shell and tmux files,
so the shell fallback and the hyper-workspace tmux block leave them alone.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
DOTFILES = yaml.safe_load((ROLE / "tasks/dotfiles.yml").read_text())
MAIN = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
HYPER_WORKSPACE = yaml.safe_load((ROLE / "tasks/hyper-workspace.yml").read_text())
CI = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())

CHEZMOI_SOURCE = "not (_dev_user_chezmoi_source.stat.exists | default(false))"


def _task(tasks: list[dict], name: str) -> dict:
    return next(t for t in tasks if t.get("name") == name)


def _when(task: dict) -> list[str]:
    when = task.get("when", [])
    return [when] if isinstance(when, str) else list(when)


def _argv(task: dict) -> list[str]:
    return task["ansible.builtin.command"]["argv"]


def test_defaults_keep_force_for_everyone_else() -> None:
    assert DEFAULTS["user_dotfiles_apply"] == "force"
    assert DEFAULTS["user_dotfiles_review_argv"] == []


def test_apply_mode_is_validated_first() -> None:
    first = DOTFILES[0]
    assert first["name"] == "Validate the dotfiles apply mode"
    rules = " ".join(first["ansible.builtin.assert"]["that"])
    assert "['force', 'review']" in rules
    assert "user_dotfiles_review_argv[0] is match('^/')" in rules


def test_every_forced_chezmoi_call_is_force_mode_only() -> None:
    forced = [t for t in DOTFILES if "ansible.builtin.command" in t and "--force" in _argv(t)]
    assert len(forced) == 3  # init --apply, update, apply
    for task in forced:
        assert "user_dotfiles_apply == 'force'" in _when(task), task["name"]


def test_review_mode_clones_without_applying_then_runs_the_gate() -> None:
    clone = _task(DOTFILES, "Clone the user's dotfiles source for the review gate")
    argv = _argv(clone)
    assert argv[-2:] == ["init", "{{ user_dotfiles_repo }}"]
    assert "--apply" not in argv and "--force" not in argv
    assert "not _chezmoi_src.stat.exists" in _when(clone)

    gate = _task(DOTFILES, "Run the dotfiles repo's own review gate")
    assert _argv(gate) == "{{ user_dotfiles_review_argv }}"
    assert gate["become_user"] == "{{ dev_user }}"
    assert gate["environment"]["HOME"] == "{{ dev_user_home }}"
    for task in (clone, gate):
        assert "user_dotfiles_apply == 'review'" in _when(task)

    names = [t["name"] for t in DOTFILES]
    assert names.index(clone["name"]) < names.index(gate["name"])


def test_review_mode_leaves_profile_to_the_gate() -> None:
    guard = _task(DOTFILES, "Guard the optional Rust environment after chezmoi applies workspace dotfiles")
    assert "user_dotfiles_apply == 'force'" in _when(guard)


def test_chezmoi_home_skips_the_shell_fallback_and_tmux_block() -> None:
    names = [t.get("name") for t in MAIN]
    detect = _task(MAIN, "Detect an existing chezmoi source for dev_user")
    assert detect["ansible.builtin.stat"]["path"] == "{{ dev_user_home }}/.local/share/chezmoi/.git"
    assert detect["tags"] == ["always"]
    assert detect["register"] == "_dev_user_chezmoi_source"

    fallback = _task(MAIN, "Include shell RC fallback")
    assert CHEZMOI_SOURCE in _when(fallback)
    hyper = _task(MAIN, "Include hyper-workspace helpers")
    assert names.index(detect["name"]) < min(names.index(fallback["name"]), names.index(hyper["name"]))

    tmux = _task(HYPER_WORKSPACE, "Add tmux configuration for hyper-workspace")
    assert CHEZMOI_SOURCE in _when(tmux)
    helper = _task(HYPER_WORKSPACE, "Deploy hyper-workspace helper script")
    assert "when" not in helper


def test_this_file_runs_in_the_policy_job() -> None:
    script = " ".join(CI["policy"]["script"])
    assert "tests/test_user_dotfiles.py" in script
