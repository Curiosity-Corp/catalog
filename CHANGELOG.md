# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and releases use
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- tint2 panel layouts for the minimal desktop: `minimal_desktop_tint2_layout`
  (`bottom`, the default dark 32px bar, or `top`, a 36px bar with launcher,
  multi-desktop taskbar and a PulseAudio volume item) and a per-user
  `tint2_layout` key on `dev_users` items. An unknown layout fails the run.
  The role installs `/usr/local/bin/tint2-volume-status` for the `top` layout,
  and `pulseaudio-utils`, `zenity` and `arandr` for the panels.
- XDG autostart for Openbox sessions: `python3-xdg` is installed, and each
  `dev_users` account gets `Hidden=true` overrides in `~/.config/autostart` for
  the ids in `minimal_desktop_xdg_autostart_hidden` (nm-applet, blueman,
  pasystray, picom, light-locker, xfce4-screensaver), so XDG autostart does not
  duplicate tray applets, the compositor or competing screen lockers.
- `lxpolkit` (available on Ubuntu and Debian) provides the PolicyKit
  authentication agent for GUI privilege prompts; the XDG autostart starts it.
  The `byod-kiosk` profile keeps XDG autostart off.
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
- Desktop self-healing installs `lm-sensors` next to `smartmontools`, so CPU
  and board temperatures from the kernel hwmon drivers are visible.
- `flameshot` in the minimal Openbox desktop, started from the session
  autostart as a screenshot tray applet; the `deskmeet-b760` hardware profile
  adds `libgphoto2-dev` so camera tooling that builds gphoto2 Python bindings
  can rebuild its virtualenv.
- Minimal-desktop Openbox keybindings for screenshots (`Print`, `S-Print`,
  `A-Print` run `flameshot gui` into `~/Pictures/Screenshots`), media keys
  (`wpctl` volume, mute and microphone mute), `W-a` for `pavucontrol`, and the
  stock Openbox window and desktop navigation keys. Existing catalog bindings
  are unchanged.
- SDKMAN runtime metadata and launcher repair that preserves dirty or
  non-Git source installations, plus a managed guard against recursive init
  sourcing.
- Opt-in Logitech Litra CLI for lightbar hardware profiles: a hardware profile
  with `litra_cli: true` (enabled for `deskmeet-b760`, which already grants
  Litra Beam hidraw access) sets the derived `litra_cli_enabled` flag and
  installs the latest `timrogers/litra-rs` release binary to
  `/usr/local/bin/litra` through the managed-update contract: GitHub asset
  digest verification, rollback inventory, and a `litra --version` healthcheck
  that only warns on hosts where the flag is off. Other profiles install
  nothing.

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
- The minimal Openbox profile sets wallpapers with `feh` (restored from
  `~/.fehbg`) and uses the PipeWire/Pulse `pasystray` volume applet (with
  `pavucontrol`) because `nitrogen` and `volumeicon-alsa` are no longer
  packaged in Ubuntu 26.04.

### Fixed

- The minimal desktop's tint2 panel config set `panel_background_id = 1` before
  any background block existed and used options tint2 17 rejects
  (`taskbar_name_active_color`, `taskbar_name_color`, `task_icon_size`). tint2
  17.0.1 (Ubuntu 26.04) segfaulted at startup, so every catalog-managed Openbox
  session had no panel, taskbar or tray. The role now ships valid layouts, and
  the Openbox autostart starts tint2 with that config and replaces an existing
  panel on the same display instead of stacking a second one.
- User-systemd masking tolerates declared units that are absent on the host
  ("Could not find the requested service").
- Pinned VT sessions keep their getty drop-ins: the stale-drop-in cleanup used
  a `'\1'` backreference inside a Jinja literal (U+0001) and removed every
  drop-in it had just written.
- pipx virtualenvs whose base interpreter was removed (distro Python upgrade or
  a home copied from another host) are rebuilt with `pipx reinstall-all`
  before managed pipx installs (`pipx_repair_python`). The user-systemd policy
  test now runs under pytest in CI instead of as a no-op script.
- `/opt/science-venv` built on a Python minor the distro has since replaced
  (3.13 on hosts upgraded to Ubuntu 26.04's 3.14) is rebuilt with
  `python3 -m venv --clear` when its interpreter cannot import pip. The old
  `creates: bin/activate` guard kept the stale venv and every science pip task
  failed, rolling back the whole update transaction.
- Microsoft 365 CLI entry points (`m365_cli_command_links`) are made executable
  after each refresh. `@pnp/cli-microsoft365` publishes `dist/index.js` as
  0644 and npm 12.2 installed it unchanged, so `m365` failed with permission
  denied and the post-update healthcheck rolled the transaction back.
- Sunshine package selection follows the host release and architecture and the
  current upstream asset names (`sunshine_<version>-1+ubuntu26.04_amd64.deb`,
  `+debiantrixie`), with the older `sunshine-ubuntu-24.04-amd64.deb` form still
  accepted. The hard-coded 24.04 name no longer exists upstream, so enabled
  hosts could not install or upgrade Sunshine, and a 24.04 build on 26.04 fails
  to load `libminiupnpc.so.18`.
- Update rollback restores symlinked launchers as symlinks. The pre-update
  snapshot followed links, so a rollback replaced `/usr/local/bin/aws` (a link
  into `/usr/local/aws-cli`) with a copy of the PyInstaller binary that could
  not load its bundled `libpython`, and the next pull failed the aws
  healthcheck. Symlinks are now recorded with `link_target`, skipped by the
  snapshot, and re-linked on rollback.
- `curiosity-display-setup` only considers modes with the aspect ratio of the
  output's preferred mode. UHD panels also list the larger DCI 4096x2160
  (17:9) mode, and choosing by area alone switched a 16:9 display to it.
- `/tmp` hygiene excludes `/tmp/.ziti`, where `ziti-edge-tunnel` keeps its IPC
  and event sockets for the life of the process. A cleaner that removed old
  sockets and then the empty directory left a running tunneler unreachable
  over IPC, so tunnel health metrics failed on every run.
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
- Post-update healthchecks no longer roll the update back for applications
  whose feature is disabled on the host. The checks are opportunistic (any
  contract whose executable exists is run), so a stale, broken
  `/usr/bin/sunshine` on a host with `sunshine_enabled: false` failed every
  pull, rolled the whole transaction back, and wrote an hour-long quarantine
  that blocked unrelated updates. Application contracts accept an optional
  `enabled_when: <variable name>`; the Sunshine, Coder, aws, glab, and Codex
  contracts name `sunshine_enabled`, `coder_cli_enabled`, `cloud_clis_enabled`,
  and `codex_standalone_enabled`. A failing check for a disabled application is
  logged as a warning and recorded as `healthcheck_warnings` in the run
  manifest; contracts for enabled applications, or without a condition, still
  fail hard.

## [0.1.0] - planned

The first supported public collection release. The release is complete only
when the checklist, compatibility evidence, and registry artifact are
published.
