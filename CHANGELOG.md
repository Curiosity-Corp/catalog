# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and releases use
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Public-safe defaults, collection metadata, contributor policies, and release
  documentation.
- Optional custom CA, GitLab/Coder, Ziti service, and split-DNS configuration.
- Regression checks for update policy and public-surface hygiene.

### Changed

- Organization-specific hostnames, CA bundles, and deployment assumptions were
  removed from the tracked baseline.
- Coder, cloud CLIs, Keycloak, Ziti, Sunshine, chat, and profile cleanup are
  opt-in.
- Microsoft 365 CLI (`@pnp/cli-microsoft365`) moved out of the general
  `npm_global_packages` baseline into its own `m365_cli_packages` list and
  `tasks/m365-cli.yml`, gated behind a new `m365` Ansible tag, so consumers
  can select it independently of the rest of the Node.js toolchain.

## [0.1.0] - planned

The first supported public collection release. The release is complete only
when the checklist, compatibility evidence, and registry artifact are
published.
