"""Every public variable is typed in its role's argument spec (catalog#31).

tasks/validate-config.yml checks the public configuration against
meta/argument_specs.yml before the role changes anything
(docs/configuration.md#validation). A public variable missing from the spec
would skip the type check without anyone noticing, so this test ties the spec
to the generated variable reference (docs/reference.md, kept current by
`catalog-tools reference --check`):

- every supported, experimental or private-hook variable has a spec entry,
  unless NOT_IN_SPEC names it with a reason;
- every spec entry is a variable of the role's defaults;
- every non-boolean variable whose reference row carries the "secret" safety
  flag is a no_log string in the spec, so Ansible prints $REDACTED$ instead of it;
- the spec carries no defaults: defaults/main.yml stays their single source.
"""

import re
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLES = ("developer_workstation", "workstation_storage")
PUBLIC = {"supported", "experimental", "private-hook"}

NOT_IN_SPEC = {
    ("developer_workstation", "authd_archive_distribution"):
        "its default is ansible_distribution_release, a gathered fact; the check runs before facts and without them",
    ("developer_workstation", "fleetd_notation_url"):
        "its default needs _fleetd_arch, which tasks/fleetd.yml sets from the host's architecture during the run",
    ("developer_workstation", "fleetd_package_file"):
        "its default reads ansible_facts['architecture'], a gathered fact; the check runs without facts",
}


# catalog#171 retired these; the role warns that a value set for one is
# ignored. A spec entry would turn that warning into a validation error.
RETIRED = {
    ("developer_workstation", "codex_standalone_installer_url"),
    ("developer_workstation", "claude_native_installer_url"),
    ("developer_workstation", "ziti_install_script_url"),
}


class _Loader(yaml.SafeLoader):
    """SafeLoader that reads Ansible's `!unsafe` scalars as plain strings."""


_Loader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))


def _yaml(path: Path):
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_Loader)


def _spec(role: str) -> dict:
    return _yaml(ROOT / "roles" / role / "meta" / "argument_specs.yml")["argument_specs"]["main"]["options"]


def _defaults(role: str) -> dict:
    return _yaml(ROOT / "roles" / role / "defaults" / "main.yml") or {}


def _reference_rows() -> dict:
    """Variable name to (status, safety impact) from docs/reference.md's variable tables."""
    rows = {}
    header = None
    for line in (ROOT / "docs" / "reference.md").read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            header = None
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells and cells[0] == "Variable":
            header = cells
            continue
        if header is None or "Status" not in header or "Safety impact" not in header:
            continue
        match = re.fullmatch(r"`([A-Za-z0-9_]+)`", cells[0])
        if match and len(cells) >= len(header):
            rows[match.group(1)] = (cells[header.index("Status")], cells[header.index("Safety impact")])
    return rows


REFERENCE = _reference_rows()


def test_the_reference_was_read() -> None:
    assert len(REFERENCE) > 300, "docs/reference.md's variable tables could not be read"


@pytest.mark.parametrize("role", ROLES)
def test_every_public_variable_is_in_the_spec(role: str) -> None:
    spec = _spec(role)
    missing = sorted(
        name for name in _defaults(role)
        if REFERENCE.get(name, ("supported", ""))[0] in PUBLIC
        and name not in spec
        and (role, name) not in NOT_IN_SPEC
    )
    assert not missing, (
        f"add these public variables to roles/{role}/meta/argument_specs.yml with their type "
        f"(docs/configuration.md#validation): {missing}"
    )


@pytest.mark.parametrize("role", ROLES)
def test_every_spec_entry_is_a_role_variable(role: str) -> None:
    unknown = sorted(set(_spec(role)) - set(_defaults(role)))
    assert not unknown, f"roles/{role}/meta/argument_specs.yml names variables the role has no default for: {unknown}"


def test_not_in_spec_entries_are_current() -> None:
    stale = sorted(
        f"{role}/{name}" for role, name in NOT_IN_SPEC
        if name not in _defaults(role) or name in _spec(role)
    )
    assert not stale, f"remove these NOT_IN_SPEC entries: {stale}"


@pytest.mark.parametrize("role", ROLES)
def test_secrets_are_no_log_strings(role: str) -> None:
    spec = _spec(role)
    unmarked = sorted(
        name for name in spec
        if "secret" in REFERENCE.get(name, ("", ""))[1]
        # A boolean flag named like a secret (pro_mimic_ssh_password_auth) holds none.
        and spec[name].get("type") != "bool"
        and not (spec[name].get("no_log") is True and spec[name].get("type") == "str")
    )
    assert not unmarked, f"mark these secrets `type: str, no_log: true` in roles/{role}/meta/argument_specs.yml: {unmarked}"


@pytest.mark.parametrize("role", ROLES)
def test_the_spec_carries_no_defaults(role: str) -> None:
    with_default = sorted(name for name, option in _spec(role).items() if "default" in option)
    assert not with_default, f"defaults belong in roles/{role}/defaults/main.yml, not the spec: {with_default}"


def test_retired_variables_stay_out_of_the_spec() -> None:
    """A retired variable still set on a host keeps only its warning (catalog#171)."""
    in_spec = sorted(f"{role}/{name}" for role, name in RETIRED if name in _spec(role))
    assert not in_spec, f"retired variables must not be in the argument spec: {in_spec}"
    still_defaulted = sorted(f"{role}/{name}" for role, name in RETIRED if name in _defaults(role))
    assert not still_defaulted, f"no longer retired, drop them from RETIRED: {still_defaulted}"
