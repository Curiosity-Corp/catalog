# Plan: capability-focused public units

Status: proposal and measurements for maintainer approval (catalog #26).
Nothing is split by this change, and the current role keeps working unchanged.

## Why a plan and not a split

`developer_workstation` is one role of 117 task files and 1,550 tasks, run from
`tasks/main.yml` (732 lines, 55 includes, 68 top-level tasks). Splitting it also
changes variable names and the public API, which is a breaking change (see
`docs/variable-naming-plan.md`). Doing both in one release would double the
migration cost for every lab, so this plan sequences them: first the compatibility
layer and measurements, then one capability at a time.

## Proposed capability boundaries

Derived from the current include order and tags in `tasks/main.yml`.

| Capability (proposed role) | Current task files (examples) | Existing tags |
|---|---|---|
| `core` | local vars, pre-flight, user and linger, timezone, logrotate, ansible-pull timer | `user`, `timezone`, `logrotate` |
| `updates` | `update-foundation`, `update-ring`, `update-transaction`, `update-rollback`, `latest-releases`, catalog integrity | the transaction tags |
| `security` | `security`, `firewall`, `pro-mimic*`, `internal-cas`, `keycloak-sso`, `ziti*` | `security`, `firewall`, `certificates`, `keycloak`, `ziti` |
| `devtools` | `repos`, `packages`, `node-packages`, `languages`, `devops`, `kubernetes`, `cloud-clis`, `cli-tools`, `ai-assistants`, `git-config`, `user-env` | `repos`, `packages`, `git`, `shell` |
| `science` | the science virtual environment tasks and their repair | `packages` |
| `workspace` | `coder-workspace-helpers`, `hyper-workspace`, `workspace-launchers`, `coder-cli` | `workspace`, `coder` |
| `desktop` | `minimal-desktop*`, `display-config`, `kiosk-*`, `console-*`, `grub-*`, `fonts`, `keyboard` | `display`, `minimal-desktop`, `kiosk`, `console`, `grub`, `boot` |
| `hardware` | `hardware-drivers`, `t460-console`, `logitech-g604`, `samsung-tv-cli`, `cpu-thermal-guard`, `i915-pcon-frl-444` | `hardware`, `t460` |
| `media` | `streamstack*`, `streaming`, `sunshine` | `streamstack`, `streaming`, `sunshine` |
| `networking` | `wifi-netplan`, PIA, `ziti-split-dns`, DNS | `networking` |

`workstation_storage` is already a separate role and stays one.

Dependency order (an arrow means "needs"): `updates` -> `core`;
`security`, `devtools`, `workspace`, `desktop`, `hardware`, `media`, `networking`
-> `core`; `science` -> `devtools`; `media` -> `desktop` and `hardware` (display
and audio devices). `updates` wraps the others (its transaction snapshots what
they install), so it is applied last and rolls back all of them. A hardware
profile selects which `hardware` and `desktop` tasks apply, so those two stay
data-driven rather than per-machine roles.

## Compatibility wrapper

`developer_workstation` stays as a thin role that imports the new units in the
current order, so `roles: [developer_workstation]` and every existing tag keep
working. Existing variable names keep working through the plan in
`docs/variable-naming-plan.md`. The wrapper is removed no earlier than the
major version that removes the old variable names.

## Selecting one capability (target usage, after the first unit exists)

```yaml
- hosts: localhost
  become: true
  roles:
    - role: curiosity.workstation.core
    - role: curiosity.workstation.devtools
```

Until a unit is extracted, the supported way to apply a capability alone is the
tag already attached to its include (`--tags security`, `--tags workspace`); the
update transaction (docs/rollback-drill.md) keeps focused runs from entering a
partial update.

## Measurements: before

| Measure | Today | How measured |
|---|---|---|
| Task files | 117 | `ls roles/developer_workstation/tasks` |
| Tasks (nested counted) | 1,550 | `scripts/audit_tasks.py`'s task walker |
| Includes in `main.yml` | 55 | parsed `main.yml` |
| Largest files | `main.yml` 75, `languages.yml` 70, `streamstack.yml` 67, `security.yml` 53, `repos.yml` 44, `kubernetes.yml` 42, `devops.yml` 40 | same |
| Converge time | not measured | needs a real host; record first-run and no-change run per profile |
| Failure isolation | one `force_handlers` play; a failed task stops the rest of the role | by design |

Converge time and failure isolation are the two measures that need hardware or a
runner with a container engine (see `docs/molecule-scenarios.md`), so they are
not recorded here. Re-measure the same table after each unit is extracted, and
record failure isolation by injecting a failure into one unit and checking that
the others still apply when run on their own.

## Work left, in order

1. Maintainer approves the boundary table and the wrapper approach.
2. Record converge time and a no-change second run for the `desktop` and `workspace` profiles.
3. Extract `updates` first (smallest blast radius, already isolated by the transaction), behind the wrapper, with the same tests.
4. Extract one unit per merge request; each keeps the wrapper green.
5. Publish selection examples with the first extracted unit.
