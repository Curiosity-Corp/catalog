# Contributing

Thank you for helping make workstation maintenance less of a burden for
students and researchers.

## Before you start

Read the [code of conduct](CODE_OF_CONDUCT.md), [security policy](SECURITY.md),
and [architecture notes](docs/architecture.md). Do not include private
infrastructure details, credentials, personal data, private certificates, or
customer information in a patch.

For a new feature, open an issue first when the change affects defaults,
security, supported operating systems, or the public collection API. Small
documentation and bug fixes can go directly to a merge request or pull
request.

## Local setup

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip ansible-core ansible-lint yamllint pyyaml pytest
ansible-galaxy collection install -r requirements.yml
```

Use `make validate` before submitting. Changes to Ansible tasks should include
an idempotence or regression test when practical, and changes to defaults must
explain their safety impact in the documentation.

## Pull requests

Keep each change focused. A good pull request includes:

- a short problem statement and the user impact;
- the design and why the default is safe for an unrelated research machine;
- tests run and any hardware or distribution limitations;
- documentation and changelog updates for user-visible behavior;
- migration notes for renamed variables, roles, or collection interfaces.

Never use a real credential to test a change. Use disposable VMs, synthetic
hostnames, and temporary accounts. CI is authoritative for merge readiness,
but maintainers may request a manual test on a supported device.

## Commit and release conventions

Use clear imperative commit subjects, for example `fix(ziti): refresh local
resolver state`. Maintainers use semantic versioning for collection releases:

- patch: backwards-compatible fixes and documentation;
- minor: backwards-compatible features and new opt-in variables;
- major: removed variables, changed defaults, or incompatible role APIs.

Release work follows [`docs/release-checklist.md`](docs/release-checklist.md).
