# 0001: Keep a public base and a private downstream

- Status: accepted
- Date: 2026-07-19 (recorded 2026-11-04 from the README's "Deliberate split" note and `tests/test_public_contract.py`)
- Deciders: recorded retroactively; a maintainer should confirm
- Affects: the whole repository, `custom_ca_certificates`, integration defaults

## Context

Curiosity Research Corporation runs its own workstations with organization
specific policy, hostnames, trust anchors and inventories. Researchers outside
the organization need a role that works with none of that.

## Decision

This repository is the reusable, scrubbed base. Organization-specific
operations live in a separate private repository that consumes this one. General
improvements land here first and are brought into the private repository on
purpose.

## Safety impact

Nothing private can leak through this repository if the gate holds.
`tests/test_public_contract.py::test_private_material_is_not_in_the_public_surface`
fails the build when a known private hostname, secret-store path or certificate
file appears in `roles/`, `playbooks/` or `renovate.json`.

## Alternatives considered

One repository with private values in encrypted files: rejected because
accidental disclosure by a contributor would be easy and reviewers could not
tell public from private by location.

## Consequences

Contributors must not add private values to a patch (see CONTRIBUTING.md).
Examples use synthetic names; `tests/test_quickstarts.py` checks the example
files for private markers too.
