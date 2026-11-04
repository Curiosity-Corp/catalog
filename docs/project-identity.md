# Project identity and public links

This page is the one place that states what the project is and where its
public pieces live. `tests/test_project_identity.py` checks that the collection
metadata, README, security policy and issue-template config agree with it, so
update this page and those files together.

## What it is

Curiosity Workstation is a free, open-source Ansible collection
(`curiosity.workstation`) for maintaining Linux computers and research VMs, so
students and researchers can spend their time on their work instead of system
maintenance.

- Intended users: students, researchers, university labs, research groups and
  ephemeral development workspaces.
- Maintainer: Curiosity Research Corporation, a non-profit. The project is
  independent and implies no endorsement by or affiliation with any vendor
  whose product the role can configure.
- License: Apache-2.0 ([LICENSE](../LICENSE), [NOTICE](../NOTICE)).
- No Curiosity account or access to Curiosity infrastructure is required.

## Public links

| What | Link |
| --- | --- |
| Source repository | https://github.com/Curiosity-Corp/catalog |
| Issue tracker | https://github.com/Curiosity-Corp/catalog/issues |
| Documentation | https://github.com/Curiosity-Corp/catalog#readme |
| Private security report | https://github.com/Curiosity-Corp/catalog/security/advisories/new |
| Support route | [SUPPORT.md](../SUPPORT.md) |
| Contribution route | [CONTRIBUTING.md](../CONTRIBUTING.md) and [GOVERNANCE.md](../GOVERNANCE.md) |
| Release notes | [CHANGELOG.md](../CHANGELOG.md), and the release page on the source repository once a release exists |

## Current support status

Public-hardening phase; collection version `0.1.0` is in `galaxy.yml`, and no
release has been published to a registry yet. Supported platforms are in the
[compatibility matrix](compatibility.md). Only the latest release and the
default branch get routine security fixes ([SECURITY.md](../SECURITY.md)).

## Public and private are separate

This repository is the public base. It carries no internal hostnames, trust
anchors, inventories, credentials or secret-store paths, and it works without
Curiosity network access. Organization-specific operations live in a separate
private downstream repository (see the README note and
[decision 0001](decisions/0001-public-and-internal-repositories.md)).
`tests/test_public_contract.py` fails the build if a known private marker
appears in the public surface.

## Where the links are checked

- `tests/test_project_identity.py` runs in CI on every merge request: metadata
  agreement, the non-profit statement, relative links in the top-level documents
  and `docs/`.
- The [release checklist](release-checklist.md) has a manual step to compare the
  repository description and the release page with this page, because those live
  on the hosting platform and CI cannot read them.
