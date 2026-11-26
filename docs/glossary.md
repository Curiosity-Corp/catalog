# Glossary

Words the guides use without stopping to explain them. Each entry says what the
word means in this repository and links to the page that covers it in depth.
CI checks that every link here resolves and that every term is still used on
another page (`tests/test_glossary.py`).

## ansible-pull

How a managed machine updates itself: a timer clones this catalog and applies
it to the machine it runs on, with no central server pushing changes.
[Update snapshots and the pull service](configuration.md#update-snapshots-and-the-pull-service)

## Hardware profile

A named entry in `hardware_profiles` that describes one kind of machine (for
example `generic`, `t460` or `deskmeet-b760`) and turns on the drivers and
device features it needs. You pick one with `hardware_profile`; it is
`generic` unless you set it. [Hardware profile opt-ins](configuration.md#hardware-profile-opt-ins)

## Health check

A command run after an update to prove an installed tool still starts, such as
`lazygit --version`. If one fails, the update is rolled back.
[Update rolled back after a failed health check](troubleshooting.md#update-rolled-back-after-a-failed-health-check)

## Manifest

The record each run writes to
`/var/lib/curiosity/workstation-updates/last-run.json`: the run's status,
installed versions, provenance and SBOM path. Read it first after a failed run.
[Troubleshooting](troubleshooting.md)

## Profile

The kind of workstation the role builds, set with `workstation_profile`:
`desktop` (the default), `workspace`, `thin-client` or `byod-kiosk`. It decides
which packages and task groups run. [Profiles](architecture.md#profiles)

## Quarantine

After an update fails its health check and is rolled back, the role installs
nothing new for `workstation_update_quarantine_seconds` (one hour by default).
`quarantine.json` names the failed run and why it failed.
[Update rolled back after a failed health check](troubleshooting.md#update-rolled-back-after-a-failed-health-check)

## Ring

The update ring, set with `workstation_update_ring`, says how old an upstream
release must be before a machine installs it: `canary` (the default) at once,
`early` after six hours, `general` after a day.
[An update is held for observation](troubleshooting.md#an-update-is-held-for-observation)

## Rollback

Putting the managed files back from the run's snapshot after a health check
fails, so the machine keeps working with the versions it had before. A
quarantine follows.
[Update rolled back after a failed health check](troubleshooting.md#update-rolled-back-after-a-failed-health-check)

## Snapshot

A copy of the files in `workstation_update_managed_paths` taken before each
managed update, under `/var/lib/curiosity/workstation-updates/snapshots/`. A
failed update is rolled back from it; only the newest few are kept.
[Update snapshots and the pull service](configuration.md#update-snapshots-and-the-pull-service)

## Tag

An Ansible label on a task group, such as `updates`, `packages` or
`streamstack`. `ansible-playbook --tags <tag>` runs only that group, which is
the safe way to try one part on a disposable VM. [Quick start](../README.md#quick-start)

## Update transaction

One managed update as a whole: take a snapshot, install the new releases, run
the health checks, then keep the result or roll it back.
[Architecture](architecture.md)
