# AGENTS.md

_Last verified: 2026-11-11 against `main`._

Instructions for coding agents working in `Curiosity-Corp/catalog`. People
should start with [README.md](README.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

## What this repository is

The public, scrubbed Ansible collection `curiosity.workstation`. It maintains
Linux workstations and research VMs. It must work for an unrelated lab with no
Curiosity network access. Organization-specific policy and inventories live in
the private downstream `Curiosity-Corp/catalog-internal`. Never move them here.

## Where things are

| Path | What |
| --- | --- |
| `playbooks/site.yml` | The one playbook: both roles, then the converge stamp |
| `roles/developer_workstation/defaults/main.yml` | Authoritative variable reference, `hardware_profiles`, `pia_tunnel_app_definitions` |
| `roles/developer_workstation/tasks/main.yml` | Orchestrates ~125 task files, one per feature (`<feature>.yml`) |
| `roles/developer_workstation/files/streamstack/` | **Vendored** StreamStack release (squashed `git subtree`). Do not edit; change it upstream in `Curiosity-Corp/streamstack` and `git subtree pull` |
| `roles/workstation_storage/` | Opt-in encrypted btrfs `/home` ([docs/storage.md](docs/storage.md)) |
| `tests/` | pytest policy tests; most render the real task expressions or run the real scripts against fakes |
| `docs/` | User docs; [architecture](docs/architecture.md), [ADR index](docs/adr/README.md), [configuration](docs/configuration.md) |
| `scripts/` | Collection normalizer and SBOM builder used by CI |
| `CHANGELOG.md` | Keep a Changelog; every user-visible change gets an `[Unreleased]` entry |

## Rules

- **Public surface.** No internal hostnames, secret-store names, inventories,
  credentials or private CAs in `roles/`, `playbooks/` or `renovate.json`.
  `tests/test_public_contract.py` enforces it. Site values go in host
  variables (`/etc/ansible/local-vars.yml`) or `catalog-internal`.
- **Safe defaults.** A new feature is opt-in (`*_enabled: false`, or derived
  from a hardware-profile key that only named profiles set). Turning it off
  must remove what it installed.
- **Tests with the change.** A task or script change comes with a test in
  `tests/` that renders the real expression or runs the real script. CI runs
  every file in `tests/`, so a new test file needs no wiring.
- **Docs with the change.** Update `docs/configuration.md` for new variables,
  the README table for headline ones, and `CHANGELOG.md`. Re-stamp
  `_Last verified:` on a doc you have checked against the code.
- **No local builds.** CI builds the collection, SBOM and provenance. Local
  `pytest`, `yamllint` and `ansible-lint` are fine.

## Checks

```bash
make validate          # yamllint, syntax check, ansible-lint, pytest tests/, collection, SBOM
python -m pytest -q tests/test_public_contract.py   # the public-surface gate alone
```

## Merge requests

Work on a branch, open the merge request as a Draft
(`glab mr create --draft`), push until the pipeline is green, then land it
with `hyperharness ship --mr <iid>` (it marks the MR ready, runs the merged
result pipeline and merges through the train). Every open issue belongs to a
milestone; close an issue only when its change is merged and verified.
