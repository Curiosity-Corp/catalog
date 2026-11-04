# Telemetry and evidence policy

The project is meant to be useful without turning maintenance into monitoring.
This page says what the software reports (nothing), what the maintainers may
collect about adoption, who can see it, and how a person gets out of it.

## Telemetry is off, and there is none to turn on

The collection does not send usage data, crash reports, host inventories or
identifiers to the maintainers or anyone else. There is no telemetry switch
because there is no telemetry. `tests/test_telemetry_policy.py` checks that the
role's defaults contain no telemetry or analytics variable and that the only
outbound alert hook is the empty-by-default `failed_sweep_webhook_url`.

This page covers the collection's own code. Third-party software the role
installs follows its vendors' policies: for example the StreamStack tooling
pulls OpenVINO and its `openvino-telemetry` package, and browsers and
chat clients have their own settings. Check those before using them with
sensitive data; the role does not change them except where a task says it
disables telemetry (the minimal-desktop Firefox profile does).

If a future change adds any reporting, it must:

- be off by default and need an explicit variable;
- be described in a [decision record](decisions/README.md) and reviewed by two
  maintainers ([GOVERNANCE.md](../GOVERNANCE.md));
- say exactly which fields leave the machine, where they go and who can read them;
- have a no-network mode and a deletion path, as described below;
- update this page and the [threat model](threat-model.md) in the same merge request.

## Network use that is not telemetry

A run does contact services to do its job: package mirrors, and the GitHub or
GitLab release APIs when a tool is installed or checked (the full list of
sources is in the role defaults). Those requests go from the machine to the
upstream service, not to the maintainers. Use the
[restricted networks guide](quickstarts/restricted-networks.md) to control or
block that traffic.

Local records stay on the machine. The update run manifest and snapshots live under
`/var/lib/curiosity/workstation-updates/` (variable `workstation_update_state_dir`).
They describe versions and results for that host and are never uploaded by the role.

The optional `failed_sweep_webhook_url` sends a failed-unit alert to an
endpoint the operator chooses. It is empty by default, and the operator owns that
endpoint.

## Adoption evidence the maintainers may collect

Only facts that people published or chose to share, in aggregate:

- registry download counts and dependent repositories or packages, when a
  registry exposes them;
- public stars, forks, issues and merged merge requests;
- public statements from downstream maintainers, with their permission;
- anonymised descriptions of a deployment (type of lab, number of machines in a
  range) volunteered by the person running it;
- results of surveys or interviews that someone agreed to, with the consent
  stated in the record.

The maintainers do not collect, buy or infer: machine identifiers, IP
addresses, usernames, hostnames, per-person activity, research data, or anything
from a student's device. They do not count bots or manufacture contributors.

## Who can see what

| Material | Who can read it | Where it lives |
| --- | --- | --- |
| Aggregate monthly evidence ([template](evidence/monthly-template.md)) | Anyone, once published | `docs/evidence/` |
| Raw survey or interview notes, and consent records | Maintainers only | An access-controlled location named in the monthly record, never this repository |
| Permission to name a downstream project | Maintainers only; the published name is public | The same location |
| Security reports | Handling maintainers only ([runbook](vulnerability-response.md)) | Private advisory |

Personal data is never committed to this repository.

## No-network mode

There is no switch that turns the role into an offline tool, and this page does not
pretend there is. What you can do today is in the
[restricted networks guide](quickstarts/restricted-networks.md): prepare
dependencies in advance, point the machine at a proxy, and accept that tasks that need an
upstream release stop rather than fall back to an unverified one. Nothing the
role does to report back depends on the network, because it does not report
back.

## Deletion

- On a machine: remove `/var/lib/curiosity/workstation-updates/` and any
  `last-run` records you do not want to keep. The role never copies them
  elsewhere. Snapshots are for rollback, so remove them only if you accept losing rollback.
- Evidence about you: ask a maintainer through the support route in
  [SUPPORT.md](../SUPPORT.md) to delete or correct anything the project holds about you
  (a survey answer, an interview note, a named deployment). A maintainer confirms the deletion
  in the same thread within 30 days, and removes it from the access-controlled store and from
  the next monthly record.

## Review

This policy has not yet been reviewed with students and researchers. Until it is,
treat it as a draft that records current behaviour. It is covered by the annual
review in [GOVERNANCE.md](../GOVERNANCE.md).
