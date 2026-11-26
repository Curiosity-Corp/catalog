# ADR 0001: The catalog's tools are one Rust crate

- Status: accepted
- Date: 2026-11-23
- Issues: catalog#160 (first slice), catalog#27; hypervoid/admiral#136

## Context

The collection is Ansible and stays Ansible: roles, tasks and templates,
checked with ansible-lint and molecule. Around it, the repository needs a few
tools that are not configuration: a generated variable and tag reference
(#27), a platform package probe (#14, #15), a redacted diagnostic command
(#41, #25, #18), GPU status (#33) and variable mapping (#26).

Agents first wrote each of these as its own Python script (!159, !173, !174,
!175, !176). Those MRs were closed unshipped: the fleet decided that no new
Python or Go tools are written (hypervoid/admiral#136, CEO note 484267).
Separate scripts also meant separate dependency sets, separate test styles and
an interpreter on every managed host for the commands the role installs.

## Decision

All of the catalog's tools live in one Cargo workspace, `tools/`, built as one
binary, `catalog-tools`, with one subcommand per tool. The first subcommand is
`reference` (#27); each later tool is a further subcommand in its own slice
and its own Draft MR.

- The crate is a library plus a thin binary, so every tool is unit-tested
  through the library and the command line stays small.
- CI builds and tests it in the `tools` job (rustfmt, clippy with warnings as
  errors, `cargo test`), in the existing pipeline on the Kubernetes runners.
  Nothing is built on a workstation to ship it.
- A tool whose output is committed (the reference, dated evidence) also has a
  `--check` mode that CI runs, so committed output cannot drift from its
  source. The job keeps the generated file as an artifact, so it can be
  refreshed without a local toolchain.
- A command the role installs on hosts (diagnostics, GPU status) ships as a
  release binary of this crate, installed by an Ansible task with a pinned
  checksum, not as an interpreted script.
- The crate is not part of the Ansible collection artifact (`galaxy.yml`
  `build_ignore`).

## Consequences

- One toolchain, one dependency lockfile and one test runner for all tools.
- Python stays only where it already is (the existing policy tests and the
  vendored StreamStack project); this decision does not port those.
- Contributors who change role defaults or tasks may need to refresh
  `docs/reference.md` (`make reference`, or the `tools` job artifact).
