# The 1.0 stable research workstation contract (draft)

Status: DRAFT, proposed by an automated agent from what the repository already
promises and tests. It has not been reviewed by a maintainer or by downstream
labs, so nothing here is a promise yet. Maintainers accept it by following the
"Accepting the contract" steps at the end.

Until `1.0.0`, the collection follows the `0.x` convention: any release may
change an interface, and the changelog says so. This page describes what `1.0.0`
would freeze, and, just as important, what it would not.

## What is frozen at 1.0

### Profiles

The four values of `workstation_profile` are frozen: `desktop`, `workspace`,
`thin-client` and `byod-kiosk`. The role validates the value and fails on any other
(`roles/developer_workstation/tasks/main.yml`). Adding a profile is a minor
release. Removing or renaming one is a major release.

### Platforms

The contract covers the platforms the [compatibility matrix](compatibility.md)
lists as "supported target" at the time of the release, with dated disposable-VM
evidence recorded in `docs/evidence/`. A platform that is "experimental" or
"unsupported" is outside the contract, and promoting one needs evidence first.
Dropping a supported platform is a major release, announced one minor release ahead.

### Variables

A variable is part of the contract only if it is documented in
[`docs/configuration.md`](configuration.md) or a [quickstart](quickstarts/README.md).
Other variables in `defaults/main.yml` are internal and may change in any
release. For a contracted variable, its name, type and meaning are frozen;
adding a new optional variable is a minor release; renaming, removing or
changing the meaning of one is a major release.

### Tags

A tag is contracted if it appears in a quickstart or in the README's tag
documentation. A contracted tag keeps selecting the task group it selects now;
tasks may be added to it but a tag is not repurposed. Because untagged tasks do
not run under `--tags`, adding an untagged prerequisite is a bug to fix, not a
contract change.

### Update manifest

The manifest schema `curiosity.workstation.update/v1` is frozen: fields may be added,
none removed or retyped. A breaking change ships as `v2` alongside `v1` for one
minor release.

### Default-safety guarantees

These hold on every release after `1.0.0` and are already asserted in
`tests/test_public_contract.py`. Changing one needs a decision record, a
second maintainer and, for weakening a guarantee, a major release.

- Integrations that need private services default off or empty:
  `coder_cli_enabled`, `keycloak_sso_enabled`, `ziti_enabled`,
  `sunshine_enabled`, `profile_cleanup_enabled`, `custom_ca_certificates`,
  `ziti_etc_hosts_pins`, `ziti_intercept_domains`.
- No default credential: `sunshine_default_pass` is empty.
- No private hostname, trust anchor, certificate or secret-store path in `roles/`,
  `playbooks/` or `renovate.json`.
- No telemetry ([policy](telemetry-and-evidence.md)).
- A failed upstream release lookup stops the run; it never installs an
  older or unverified build ([restricted networks](quickstarts/restricted-networks.md)).
- A managed update takes a snapshot before changing files and rolls back on a
  failed health check ([update rings](update-rings.md)).

## Deprecation windows

- A deprecated variable, tag or profile keeps working for at least two minor
  releases and at least 90 days, whichever is longer, and logs a clear warning
  during the run.
- The changelog lists the deprecation under "Deprecated" in the release that
  announces it and under "Removed" in the major release that removes it.
- A security fix may shorten a window; the advisory says so.

## Upgrade and rollback guarantees

- Upgrading within `1.x` does not require editing contracted variables.
- Every release's changelog states the migration steps, or "none".
- On a managed machine, the update control plane snapshots managed paths before
  an update and can restore them. The guarantee covers the paths named in
  `workstation_update_managed_paths`; it does not cover package databases,
  user data, or hardware state, and it is not a backup.
- The previous release stays installable: the collection never rewrites
  state in a form an older release cannot read within the same major version.

## What 1.0 does not promise

- Support for experimental or unsupported platforms.
- Any variable or tag not documented as contracted above.
- Hardware profiles other than `generic`, and vendor-specific integrations,
  beyond best effort.
- That a third-party tool the role installs will keep working with a new upstream release.
- Availability of any registry or release host.

## Evidence required to change a stable default

Before a stable default changes, the merge request must attach: a decision
record in [`docs/decisions/`](decisions/README.md); a compatibility run on each
supported platform with dated evidence; a security note stating the effect on
the [threat model](threat-model.md); migration notes; and review by a second
maintainer.

## Accepting the contract

1. At least two maintainers review this page and change it to match their
   decision, replacing "DRAFT" with the accepted date.
2. At least one downstream lab or researcher outside the maintainer team reviews
   it. Record who, when and what they changed in a decision record.
3. Complete the [1.0 readiness review](evidence/readiness-review-template.md).
4. Tag `1.0.0` only after the first three are done.
