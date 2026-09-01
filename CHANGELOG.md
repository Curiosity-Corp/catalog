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
- T460 console diagnostics and a guarded handoff for the experimental
  horizontal DRM fbcon test command line.

### Changed

- Organization-specific hostnames, CA bundles, and deployment assumptions were
  removed from the tracked baseline.
- Workspace package-tag runs now initialize release discovery and the update
  control plane before installers execute, so a failed prerequisite cannot
  leave user-scoped CLIs such as Codex stale.
- User-scoped npm and AI refreshes run before binary installers and are kept out
  of binary rollback, so unrelated installer failures cannot undo them.
- Coder, Keycloak, Ziti, Sunshine, chat, and profile cleanup remain opt-in.
  Cloud CLIs are now part of the desktop/workspace workstation baseline; their
  credentials and sessions remain host-local.
- Desktop/workspace parity now includes Mattermost `mmctl`, `sshpass`, PDF
  extraction tools, and the managed Bun `bunx` alias.
- Microsoft 365 CLI (`@pnp/cli-microsoft365`) moved out of the general
  `npm_global_packages` baseline into its own `m365_cli_packages` list and
  `tasks/m365-cli.yml`, gated behind a new `m365` Ansible tag, so consumers
  can select it independently of the rest of the Node.js toolchain.
- Codex CLI is now managed by the official standalone installer at
  `~/.local/bin/codex` instead of the nightly `@openai/codex` npm refresh.
  The npm package and the standalone binary both provide a `codex` command,
  and PATH order decided which install ran, so a missing or broken npm copy
  broke the user's `codex` command on pulls where the refresh failed. The
  role now migrates legacy npm installs, repairs a missing standalone
  launcher, and removes dangling `/usr/local/bin/codex` symlinks shipped by
  some base images.

## [0.1.0] - planned

The first supported public collection release. The release is complete only
when the checklist, compatibility evidence, and registry artifact are
published.
