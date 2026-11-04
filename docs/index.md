# Curiosity Workstation documentation

An Ansible collection that keeps research workstations, VMs and thin clients
current, so students and researchers spend less time on maintenance. These pages
need no private service to read or to build.

## Choose a starting point

| You have | Read |
| --- | --- |
| An Ubuntu laptop | [Researcher laptop](quickstarts/laptop.md) |
| A VM for Python and data tools | [Research VM](quickstarts/research-vm.md) and the [Python research profile](python-research-profile.md) |
| A lab that boots VMs from images | [cloud-init and libvirt](quickstarts/cloud-init-libvirt.md) |
| An access appliance or shared lab machine | [Thin client and shared lab](quickstarts/thin-client-and-shared-lab.md) |
| A proxy, a slow link or no internet | [Restricted networks](quickstarts/restricted-networks.md) and [low-bandwidth and offline maintenance](low-bandwidth-and-offline.md) |
| Jupyter notebooks | [Notebooks](quickstarts/notebooks.md) |

Try every guide on a disposable machine first. All of them preview with
`ansible-playbook --check --diff` before changing anything.

## Understand and operate it

- [Architecture](architecture.md), [configuration](configuration.md) (variables and tags) and [compatibility](compatibility.md).
- [Update rings](update-rings.md) and the [maintenance summary](maintenance-summary.md) of the last run.
- [Accessibility and localization](accessibility-and-localization.md).

## Trust and security

- [Threat model](threat-model.md), [vulnerability response](vulnerability-response.md), [telemetry and evidence policy](telemetry-and-evidence.md) and [project identity](project-identity.md).
- [Release checklist](release-checklist.md) and the [draft 1.0 contract](stable-contract-1.0-draft.md).

## Take part

- [Good-first and help-wanted backlog](community/good-first-issues.md), [review areas and maintainer onboarding](community/maintainer-onboarding.md).
- [Decision records](decisions/README.md) and the [roadmap](roadmap.md).

## Versions

This site is built from the repository at a release tag, so the pages you read
match the collection version you installed. Build it yourself with `make docs-site`.
