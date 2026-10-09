# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and releases use
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Multi-user desktop convergence: `dev_users` (default derived from
  `dev_user`) drives per-user home checks, user-systemd policy, Openbox,
  picom, tint2, dunst, rofi, GTK, Firefox and screen-lock settings;
  `workstation_user_linger` enables lingering.
- LightDM parity options: `minimal_desktop_greeter` (`gtk` or `slick`),
  `minimal_desktop_lightdm_minimum_vt`, an opt-in VT-switch X server wrapper,
  `minimal_desktop_lightdm_remove_dropins`, an explicit Openbox user session,
  and an enabled and running `lightdm.service`.
- Per-user pinned VT sessions (`minimal_desktop_pinned_vt_sessions`) using a
  username-prefilled getty and a login-shell `startx` hook.
- Opt-in `curiosity-display-setup` helper (`minimal_desktop_display_setup_enabled`,
  `minimal_desktop_display_outputs`) for LightDM and startx sessions.
- Public-safe defaults, collection metadata, contributor policies, and release
  documentation.
- Optional custom CA, GitLab/Coder, Ziti service, and split-DNS configuration.
- Regression checks for update policy and public-surface hygiene.
- T460 console diagnostics and a guarded handoff for the experimental
  horizontal DRM fbcon test command line.
- An opt-in declarative policy for masking obsolete user-scoped systemd units.
- Declarative user-unit convergence now clears stale failed state after masking.
- A free Ubuntu-Pro equivalents plane (`pro-mimic`, tags `pro-mimic,
  security`): explicit security origins with automatic reboots disabled,
  needrestart automation, a minimal hardened sshd subset, MOTD security
  visibility, weekly ESM-status reporting, GA kernel pinning with
  reboot-required surfacing, a pull-status record, and display-manager mode
  control. Report-only by default; no Pro attach, no livepatch DIY, no CIS
  remediation, and no automatic reboots.
- T460 hardware-profile posture-flag parity (`disable_sleep`,
  `performance_mode`) so the laptop TLP/no-sleep/netplan policies converge
  on the T460 fleet, with the wired-netplan policy extended to both laptop
  profiles.
- Opt-in console tuning (`console_tuning_enabled`, tags `console, display`) for
  large or mixed-size displays: grows the Linux text console to the whole
  framebuffer, optionally loads a PSF bitmap font and a soft VT palette,
  re-applies them from a systemd oneshot plus udev triggers, and ships a
  read-only `curiosity-console-status` diagnostic. Default off; never edits
  the bootloader or kernel command line.
- Opt-in `/tmp` hygiene (`tmp_hygiene_enabled`, tags `tmp-hygiene, system`):
  an age-based `/etc/tmpfiles.d` drop-in (default `3d`) run by the native
  `systemd-tmpfiles-clean.timer`, and an hourly `curiosity-tmp-pressure`
  oneshot timer that runs `systemd-tmpfiles --clean --prefix=/tmp` when
  tmpfs `/tmp` reaches `tmp_hygiene_pressure_threshold` (default `85`), with an
  optional shorter-age second pass that keeps package-declared exclusions.
  Never deletes by name pattern or removes live `systemd-private-*`
  directories. Default off; skipped on workspace profiles and in containers.
- PAM-aware XScreenSaver locking for standard Openbox desktops, with separate
  visible authentication prompts and `vlock` for text consoles.
- Kiosk profile state snapshots for account groups, sudoers, desktop files,
  and TTY/sleep unit state, with guarded restoration when leaving kiosk mode.
- SDKMAN runtime metadata and launcher repair that preserves dirty or
  non-Git source installations, plus a managed guard against recursive init
  sourcing.

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
- The minimal Openbox profile replaces Light Locker and the single-field
  `i3lock`/`xss-lock` path with XScreenSaver, uses a ten-minute idle timeout,
  and leaves kiosk sessions without screen locking by design. Ubuntu-only
  wallpaper assets are now distro-gated; LightDM indicators and rsyslog
  restart validation are corrected.

### Fixed

- Vendor APT signing keys (GitHub CLI, Microsoft, Google Cloud, Google Chrome,
  1Password, Brave, MongoDB, NodeSource, OpenZiti, Mozilla) were fetched once
  behind `creates:` or a non-forcing download and never refreshed, so a vendor
  key rotation left hosts failing `apt update` with `NO_PUBKEY` (GitHub CLI
  key `5612B36462313325`). `tasks/apt-keyring.yml` now re-downloads every key
  on each run with retries, dearmors only ASCII-armored sources, verifies the
  OpenPGP content (and the pinned fingerprint where one is configured) before
  replacing the keyring, and reports `changed` and refreshes the apt cache
  only when the vendor content differs. Keyring paths and `signed-by=` lines
  are unchanged; staging copies live in `apt_keyring_staging_dir`.
- Hosts bootstrapped with the upstream OpenZiti install script kept
  `/etc/apt/sources.list.d/openziti.list` with
  `signed-by=/usr/share/keyrings/openziti.gpg`; the catalog's own OpenZiti
  entry in the same file then made apt fail with "Conflicting values set for
  option Signed-By". The upstream-form line is now removed before the catalog
  entry is added, and the orphaned `openziti.gpg` keyring is deleted only when
  nothing in `/etc/apt` still references it.

## [0.1.0] - planned

The first supported public collection release. The release is complete only
when the checklist, compatibility evidence, and registry artifact are
published.
