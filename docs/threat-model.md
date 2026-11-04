# Threat model

This is the public threat model for the `curiosity.workstation` collection. It
says what a role that runs as root can affect, what it trusts, which failures
matter to students and researchers, and which test, if any, holds each control
in place. `tests/test_governance_docs.py` checks that every test file named here
exists, so the map does not rot silently.

Review it at the annual review in [GOVERNANCE.md](../GOVERNANCE.md) and again
whenever an integration is added, changed or removed (see
[Revisiting the model](#revisiting-the-model)).

## What the role can affect

The role runs through `become: true`. It can install and remove packages,
write APT sources and keyrings, change services and timers, edit the firewall,
install certificate authorities into the system trust store, write under
`/etc` and `/var/lib`, create or modify user accounts and files in home
directories, and reboot-sensitive pieces such as display and network
configuration. A defect or a compromised input therefore has the reach of an
administrator on every machine the role touches.

## Who matters

- A student or researcher with one laptop and no administrator to call.
- A lab running a shared or kiosk-style machine for several people.
- A maintainer who must approve a release without a security team.

Failures that matter most, in order: loss of research data or work in progress,
a machine that no longer boots or logs in, silent installation of something
unverified, and trust in a certificate authority the user did not choose.

## Trust boundaries

| Boundary | We trust | We do not trust | How it is enforced |
| --- | --- | --- | --- |
| Upstream release APIs (GitHub, GitLab) | That a tagged release exists | An unauthenticated answer as proof of integrity; a failed answer as permission to install an older build | Assets are chosen only when the API lists a SHA-256 digest, and failed discovery stops the run |
| Package managers (APT, pip, pipx) | Signed repository metadata | Unsigned or mutable installer scripts | `signed-by` keyrings, keyrings refetched through one shared flow, no shell pipelines that dearmor keys outside it |
| Host-local secrets (tokens, passwords, keys) | The operator's inventory or vault | The repository, logs and bug reports | None committed; `no_log` on secret-bearing tasks; secret scanning in CI |
| Controller-supplied files (custom CA bundles) | The operator who supplied them | Anything inside the public repository | The role ships no certificates; the operator names each file |
| The machine's own state before a run | Nothing | A partial earlier run | Snapshot before change, health check after, automatic rollback |
| Other users on a shared machine | Nothing beyond their own account | Their files and sessions | Per-user paths, cleanup that excludes live sessions, opt-in cleanup |

## Risks, controls and evidence

Each row names the control, then the test that holds it, or states the
limitation honestly where no test does.

| Area | Risk | Control | Evidence |
| --- | --- | --- | --- |
| Supply chain | An upstream release is replaced or tampered with | Verified SHA-256 asset digests; refuse to proceed without them | `tests/test_sunshine_asset_selection.py` (an unpublished release selects nothing, so the assertion fails); `tests/test_restricted_network.py` (failed discovery installs nothing) |
| Supply chain | A vendor APT key or source is changed by a mutable step | Keyrings always refetched and converged through one flow; no shell pipelines that dearmor outside it | `tests/test_apt_keyring_policy.py` |
| Supply chain | The collection artifact itself is swapped | CI builds the artifact, an SBOM and provenance metadata; release checklist compares checksums | CI jobs `collection`, `sbom`, `provenance`; `tests/test_public_contract.py` (metadata). Limitation: release signing is not in place yet |
| Supply chain | A vulnerable dependency is pulled in by a pin | Renovate configuration and secret scanning in CI | `tests/test_public_contract.py::test_renovate_is_public_and_valid_json`; CI job `secret_detection`. Limitation: no automated license or vulnerability gate on Python packages the role installs for users |
| Privilege escalation | A mistyped or non-boolean variable silently changes which privileged tasks run | Conditionals must be real booleans; tagged includes apply their tags to the included tasks | `tests/test_conditionals_are_boolean.py`; `tests/test_include_tags_apply.py`; ansible-lint in CI. Limitation: there is no complete review of every `command` and `shell` task for injection from variable values |
| Privilege escalation | A per-user systemd or session helper acts beyond its user | User-scoped masks are opt-in and native | `tests/test_user_systemd_policy.py` |
| Private CA | A certificate authority is trusted that the user did not choose | Empty by default; only operator-named files are installed | `tests/test_public_contract.py::test_defaults_are_safe_for_unrelated_machines`; `tests/test_quickstarts.py` (the thin-client example uses a placeholder path). Limitation: the role does not check a CA's name constraints or pin its fingerprint; review the file before listing it |
| Identity | A private identity provider or overlay network is configured by default | Keycloak, OpenZiti, Coder and streaming default off; host pins and domains default empty | `tests/test_public_contract.py::test_defaults_are_safe_for_unrelated_machines` |
| Identity | Shared-machine cleanup removes another user's live session files | Cleanup is off by default and excludes live sessions | `tests/test_tmp_hygiene.py`. Limitation: no test covers one user reading another's files; that relies on ordinary file permissions |
| Network | A machine exposes services to an untrusted LAN | Default-deny firewall option with trusted-interface SSH, mDNS and MTA silenced on untrusted LANs, strict DNS-over-TLS per link | `tests/test_firewall_posture.py`. Limitation: the firewall is turned on by the operator or a hardware profile, not for every machine |
| Network | A proxy or outage silently downgrades the run | The run fails closed and writes no state | `tests/test_restricted_network.py` |
| Network | A notebook or dev server is exposed publicly by accident | Examples bind to loopback and keep the token on | `tests/test_quickstarts.py`; `docs/evidence/notebook-loopback-check.md` (one manual run) |
| Rollback | An update leaves the machine broken | Snapshot before change, health check, automatic rollback, quarantine of a bad release | `tests/test_update_policy.py`; `tests/test_update_rollback_symlinks.py`; `tests/test_update_snapshot_retention.py` |
| Rollback | Rollback follows a symlink out of the snapshot | Snapshot records symlinks as links and never follows them | `tests/test_update_rollback_symlinks.py` |
| Rollback | Rollback fails on a read-only or damaged state directory | The snapshot and retention rules above | Limitation: only the cases above are tested; a full rollback drill on a disposable machine is on the roadmap, none yet |
| Data loss | Storage migration destroys data | Never writes live mount files before a verified copy and cutover marker; refuses devices with signatures; seed sync never deletes | `tests/test_workstation_storage.py` |
| Data loss | Cleanup deletes work in progress | Off by default; excludes live sessions; ages configurable | `tests/test_tmp_hygiene.py` |
| Disclosure | Real credentials or private hostnames land in the public repository | Marker scan of roles, playbooks and renovate configuration; secret scanning | `tests/test_public_contract.py::test_private_material_is_not_in_the_public_surface`; `tests/test_quickstarts.py` (examples); CI job `secret_detection` |
| Process | A leftover merge-conflict marker or half-applied change ships | Marker scan | `tests/test_no_conflict_markers.py` |

## What is out of scope

- A malicious operator or a compromised controller: the role runs what the
  operator tells it to.
- A compromised upstream vendor whose signed packages are themselves malicious.
- Physical attacks on a machine, firmware compromise, and hardware-specific
  vulnerabilities.
- Application security of the tools the role installs.

## Revisiting the model

Change this page in the same merge request that does any of the following, and
say in the description what you reviewed:

- adds, removes or changes the default of an integration (identity, tunnels,
  streaming, remote access, a new network service);
- adds a new source of downloaded code or a new package repository;
- changes snapshot, rollback or quarantine behaviour;
- adds a new way to run as another user or to install a certificate.

The merge request template has a checkbox for this.
