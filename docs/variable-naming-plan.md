# Plan: collection-scoped variable names and deprecations

Status: proposal for maintainer approval. Nothing is renamed by this change.

## The problem

Every public variable of the `developer_workstation` role is unprefixed or uses
a mix of prefixes (`coder_cli_enabled`, `workstation_update_ring`,
`streamstack_*`, `ziti_*`). `.ansible-lint` therefore skips
`var-naming[no-role-prefix]`. That is a recorded compatibility exception: existing
labs use these names in inventory, so a rename done carelessly would silently
stop applying their settings.

Currently there are 359 public variables (`python3 scripts/variable_mapping.py`).
Largest families: `streamstack_` 76, `minimal_` 31, `workstation_` 23,
`console_` 17, `logitech_` 16, `ziti_` 15, `hhtest_` 15, `tmp_` 14.

## Target names

The target prefix is the role name, `developer_workstation_`, because that is
what `ansible-lint` enforces and what removes the exception. The rule:

| Current name | New name |
|---|---|
| already starts `developer_workstation_` | unchanged |
| starts `workstation_` | `developer_workstation_` plus the rest (`workstation_update_ring` becomes `developer_workstation_update_ring`) |
| anything else | `developer_workstation_` plus the name (`coder_cli_enabled` becomes `developer_workstation_coder_cli_enabled`) |

`python3 scripts/variable_mapping.py` prints the full old-to-new table from the
current defaults, and `--check` fails on a collision. `tests/test_variable_mapping.py`
runs that check in CI, so the table is always derived from the real defaults and
cannot go stale. The `workstation_storage` role already uses its own prefix
and is not affected.

Decision needed from a maintainer before work starts: keep the role-name prefix
(long names, lint-clean) or choose a shorter collection-wide prefix (for example
`cws_`) and change the lint rule instead. The rest of this plan works for either;
only the `PREFIX` in the script changes.

## Transition design (both names work)

1. A single early task, `tasks/legacy-variables.yml`, runs before validation. For
   each old name it checks whether the host sets it (`vars` / `hostvars`, so only
   inventory and extra vars count, not role defaults).
2. Precedence while both exist:
   1. If the new name is set by the operator, it wins.
   2. Else if the old name is set by the operator, its value is copied to the new
      name.
   3. Else the role default for the new name applies.
   If both are set to different values, the new name wins and a warning says so.
3. Role code reads only the new names. Defaults are defined once, under the new
   names, so the old names stop appearing in `defaults/main.yml` and the role
   cannot read a stale old default.
4. The helper never prints values (they can be URLs or tokens), only names.

## Warnings

- Each use of an old name gives one `ansible.builtin.debug` warning per run:
  `old name X is deprecated, use Y; removed in 2.0.0`. A collected summary line
  gives the count so noisy runs stay readable.
- The support bundle (`curiosity-diagnostics`, once it is merged) should list the
  deprecated names in use, so support can see them without the values.
- `deprecation_warnings` honours a single switch,
  `developer_workstation_suppress_deprecations`, for CI that cannot be fixed yet.

## Timeline and major-version boundary

| Release | Behaviour |
|---|---|
| 0.x (today) | Old names only. This plan and the mapping tool are published. |
| 1.0.0 | New names are canonical. Old names still work and warn. Release notes carry the table. |
| 1.x minors | Warn only. At least two minor releases and 6 months, whichever is longer, before removal. |
| 2.0.0 | Old names removed. A leftover old name fails the run with a clear message (a final hard error, not a silent ignore) for one more minor, then is simply ignored. |

The removal is a breaking change and lives only in a major release. Nothing in
0.x or 1.x removes a name. `galaxy.yml` and CHANGELOG state the boundary.

## What stays exactly as it is

- The role is not split. Renaming and splitting are separate decisions; combining
  them would double the migration for every lab.
- Names inside `hardware_profiles` mappings (profile keys such as `litra_cli`)
  are data, not role variables, and are not renamed.
- Facts the role sets for itself already start with an underscore.

## Work left, in order

1. Maintainer picks the prefix.
2. Implement `tasks/legacy-variables.yml` with tests for each precedence case.
3. Move defaults to the new names and rewrite role code, one family per merge
   request (smallest first), each keeping the old name working.
4. Remove the `var-naming[no-role-prefix]` skip once the last family is moved.
5. Announce in CHANGELOG and docs/configuration.md with the table.
