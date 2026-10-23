# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and releases use
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Security

- Hardened SSH never took effect on cloud-init images: sshd keeps the first value it reads, and `50-cloud-init.conf` (`PasswordAuthentication yes`) loaded before the role's `60-pro-mimic.conf`. The drop-in is now `00-pro-mimic.conf`, the legacy file is removed, and every run checks the effective `sshd -T` output and fails if password or keyboard-interactive login is still offered (#96).
- Inbound firewall for hosts on an untrusted LAN: a hardware profile can set `firewall: true`, limit SSH (and mosh with `firewall_mosh`) to `firewall_ssh_interfaces`, and set `untrusted_lan: true` to stop avahi publishing and keep Postfix on loopback. Host vars `firewall_ssh_interfaces` / `firewall_mosh` take precedence. `deskmeet-b760` enables all of these and trusts only ziti0 and the onboard Ethernet cable port, so the shared Wi-Fi gets no inbound access (#96).
- `tests/test_pro_mimic.py` and the new `tests/test_firewall_posture.py` now run in the CI `policy` job.
- Untrusted-LAN hosts resolve DNS only over TLS on the links in the profile's `dns_over_tls_interfaces` (strict `DNSOverTLS=yes`, `dns_over_tls_servers`, DHCP/RA resolvers ignored), so the shared network can neither read nor spoof lookups. This is per link, so the ziti0 resolver keeps plain DNS. `deskmeet-b760` enables it on `wlp8s0` (#96).

### Added

- PIA VPN daemon settings (`tasks/pia-vpn.yml`): Brave-only include split tunnel, split DNS, `auto` killswitch and a persistent daemon, converged idempotently from `piactl dump daemon-settings`; login only from a root-only `pia_login_file`. Managed WiFi profile (`tasks/wifi-netplan.yml`): an interface-agnostic `wl*` netplan profile with a system-owned PSK that replaces legacy interface-pinned files, after a card slot change left a pinned profile orphaned. See `docs/pia-vpn-and-wifi.md`.
- `hhtest` unprivileged test-bed account (`tasks/hhtest.yml`, `docs/hhtest.md`, `--tags hhtest`): a fixed-uid account with no sudo or privileged groups, lingering user manager, a capped `user-<uid>.slice` (16G/14G memory, 1200% CPU, 4096 tasks), tmux 3.7b built from a checksum-pinned release into `/opt/hhtest`, uv, Playwright Chromium, and a nightly cleanup timer that runs as the account. Opt-in through the hardware profile's `hhtest_host`; `deskmeet-b760` enables it for `deskmeet1` only (#109).
- Ziti tunnel health check (`curiosity-ziti-health.timer`, `ziti_health_check_*`): a root check every 2 minutes restarts `ziti-edge-tunnel` when an identity is Active but not Loaded (the half-up state after a crash that kills ziti0 DNS) or a per-host probe hostname (`ziti_health_check_probe_hosts`, empty in the public role) does not resolve, at most once per 10 minutes, logging to the journal. It queries only the system tunnel's default IPC socket and never touches a hand-started tunneler with another prefix (#108).
- Microsoft Edge (`microsoft-edge-stable`) on desktop installs, from packages.microsoft.com with the existing Microsoft keyring. `/etc/default/microsoft-edge` stops the package's cron job from re-adding its own repo file with a conflicting Signed-By.
- StreamStack call and stream studio (`streamstack_enabled`, `tasks/streamstack.yml`, `docs/streamstack.md`). The application is the StreamStack project, vendored at `roles/developer_workstation/files/streamstack` as a squashed `git subtree` of release v0.1.0 and installed self-contained under `/usr/local/lib/streamstack` with `streamstack`, `streamstack-auto` and `streamstack-tray` in `/usr/local/bin`. The role wires it into the host: OBS Studio, Easy Effects with LSP/Calf plugins, qpwgraph and camera tools; two `exclusive_caps` v4l2loopback cameras (`video42` "Canon EOS 50D" for the Canon bridge, `video43` "StreamStack Camera" for the OBS virtual camera) that Teams on the web and Zoom both list; a `/dev/streamstack/camlink` path for the Elgato Cam Link 4K; `/etc/streamstack/streamstack.env` from the role variables; the user units, menu entry and hicolor icons; and per-user OBS scenes and voice presets that are seeded once and never overwritten. `deskmeet-b760` opts in, and StreamStack replaces its single-camera loopback options. `streamstack_camera_external` adopts a camera node another tool owns (no module options, no bridge, only the program node, added at run time and at boot by `curiosity-streamstack-loopback.service`). `streamstack_microphone` names the call microphone. Auto mode (`streamstack auto on|off|once|status|check|reset`; off by default, `streamstack_auto_*`) is an auto-cinematographer and auto-audio engineer: local loops keep face exposure and microphone gain right, and a periodic Claude pass (the Claude Code CLI restricted to reading one still, the Anthropic API, or none) proposes bounded, schema-validated changes to contrast, saturation, warmth, LUT, sharpening and the voice preset. Every change is clamped, ramped, logged and undoable; audio never leaves the host.
- StreamStack low-latency tuning (`streamstack_low_latency`, `tasks/streamstack-low-latency.yml`, docs/streamstack.md "Low latency"), hardware-gated and applied with `--tags streamstack-low-latency`: PipeWire at 48 kHz with quantum 256 (min 128, max 1024) instead of 1024, matching PulseAudio-client requests, WirePlumber rules that pin the ALSA period/headroom and never suspend the Focusrite or Elgato Wave XLR node, rtkit plus `audio` group realtime limits, no USB autosuspend for Focusrite, Wave XLR, Cam Link 4K and Canon devices, current Intel media stack (iHD, libvpl2, libmfx-gen1.2, firmware, gpu tools) on Intel GPUs without touching the i915/xe choice, and opt-in kernel arguments through a GRUB drop-in (#98).
- StreamStack AI helper dependencies (`tasks/streamstack-background-removal.yml`, `tasks/streamstack-framing.yml`, docs/streamstack.md "AI helper dependencies"), applied with `--tags streamstack-background-removal` and `--tags streamstack-framing` and switched off with `streamstack_background_removal_enabled` / `streamstack_framing_enabled`. The obs-backgroundremoval 1.4.1 OBS plugin (the upstream forky `.deb`, pinned by sha256, with `libonnxruntime1.23` and OpenCV 4.10 from the archive) gives the Console's "Blur background" its `background_removal` filter (#100). Auto-framing gets `python3-numpy`/`-opencv`/`-packaging`, OpenVINO 2026.4.1 and `openvino-telemetry` from checksummed wheels installed with `pip --target /usr/local/lib/streamstack` for the isolated launcher, and the commit-pinned YuNet face model for each desktop user; the Intel GPU compute runtime and `render` group membership are added only where an Intel GPU is present, other hosts run on the CPU (#101). `tests/test_streamstack_ai_deps.py` runs in the CI `policy` job.
- StreamStack DeepFilterNet voice option: `tasks/streamstack-deepfilter.yml` (`--tags streamstack-deepfilter`, `streamstack_deepfilter_enabled`) installs `libdeep_filter_ladspa.so` 0.5.6 (upstream x86_64 release asset, MIT or Apache-2.0, pinned by sha256) into `/usr/local/lib/ladspa`, where Easy Effects 8.1.2's Deep Noise Remover looks for it. The installed release is recorded in `/usr/local/share/streamstack/libdeep_filter_ladspa.release`; a newer or unrecorded library is never replaced, and Easy Effects is not restarted (#102).
- `deskmeet-b760`: the `60-logitech-litra` udev rule now covers every Litra light the Litra CLI and StreamStack drive (Glow `c900`, Beam `c901` and `b901`, Beam LX `c903`) on both the usb and hidraw nodes, matched by exact product id so the Lightspeed receiver (`046d:c539`) and other Logitech devices stay untouched (#98).

### Fixed

- Bitwarden Desktop could freeze a whole startx/Openbox session. The `.deb` does not ship its polkit policy, so the app ran `pkexec` to write `com.bitwarden.Bitwarden.policy` itself the first time it needed it; with no graphical polkit agent running, `pkexec` prompted on the console tty from a background process group, the kernel stopped the entire Openbox process group, and nothing on the display responded (dm313red vt8, 2026-10-19). `bitwarden-desktop.yml` now installs the policy as a root-owned `0644` file, checks with `pkaction` that polkit resolves `com.bitwarden.Bitwarden.unlock` to `auth_self` for the active session, and `tests/test_bitwarden_desktop.py` guards the file, its ownership, the task order and the check. The agent that turns the prompt into a window is `lxpolkit`, already installed and started by XDG autostart.
- Workstations had two Claude Code installs: the role refreshed `@anthropic-ai/claude-code` from npm into `~/.npm-global/bin/claude` while Claude Code's native installer owned `~/.local/bin/claude`, so every launch warned about multiple installations and PATH order decided which one ran (seen on dm313red). Claude Code now follows the Codex pattern: the native installer is the only managed install (`claude_native_*`, contract `claude`), every run removes the npm package and retired launchers (`claude_retired_launchers`, root-prefix npm copies), moves the native install to `claude_native_channel` (`latest`, decision D910, enforced by the policy test), and fails if more than one `claude` is on the user's PATH. This applies to every `dev_users` account, so a second desktop user cannot keep an unmanaged, outdated install (`tasks/claude-code-user.yml`). `tests/test_update_policy.py` guards the rule.
- Desktops ignored their hardware profile's `disable_sleep` and `performance_mode`: both were gated on laptop chassis types, so `deskmeet-b760` hosts could still suspend and ran the balanced power profile. The no-sleep logind drop-in and masked sleep targets (now including `suspend-then-hibernate.target`) apply to any chassis whose profile sets `disable_sleep`, and desktops with `performance_mode` get power-profiles-daemon's `performance` profile (laptops keep TLP).
- A failed run dropped the handlers that earlier tasks had notified, and later runs never notified them again because the files were already in place. On dm313red the DNS-over-TLS drop-in for the Wi-Fi link was written but systemd-networkd was never reloaded, so the untrusted LAN kept serving DNS in clear text. `playbooks/site.yml` now sets `force_handlers: true`.
- StreamStack could not bring up its loopback cameras on a host that had installed a newer kernel but not rebooted yet: `linux-modules-v4l2loopback-generic` only carries the module for the newest kernel ("Module v4l2loopback not found in directory /lib/modules/<running>"). The role now also installs the running kernel's `linux-main-modules-v4l2loopback-<kernel>` package, with DKMS still the fallback.
- LightDM still failed to start right after the role repointed `display-manager.service` from gdm3: systemd kept the stale alias and refused to load `lightdm.service` ("File exists") until a daemon reload. The enable task now reloads systemd first.
- Fresh Ubuntu Server installs never reached LightDM: the desktop package set pulls gdm3 in as a recommend, gdm3 claims `display-manager.service`, and enabling lightdm failed ("display-manager.service already exists and is a symlink to gdm3.service"). The role now selects LightDM in debconf (`shared/default-x-display-manager`) and repoints the alias before enabling it.
- `deskmeet-b760`: a host-level `dns_over_tls_interfaces` overrides the profile's list, for boards whose Wi-Fi enumerates under another name (the second DeskMeet's RTL8851BE is `wlp6s0`, not DeskMeet1's `wlp8s0`).

### Added

- `deskmeet-b760` installs the Arc A380 compute and Quick Sync runtime: `intel-opencl-icd`, `libze1`, `libze-intel-gpu1`, `libvpl2`, `libmfx-gen1.2` and `clinfo`, so OBS/ffmpeg QSV encoding and OpenCL work out of the box.

- G604 pointer speed: Solaar re-applied its own saved DPI (1600) on every start or reconnect, overriding the onboard `logitech_g604_dpi` (3200). A per-user `~/.config/autostart/solaar.desktop` now starts Solaar through `curiosity-solaar-g604-dpi --launch`, marking Solaar's G604 `dpi` setting `ignore` so the onboard profile wins.
- G604 launcher now autostarts in any Openbox session through `/etc/xdg/autostart/curiosity-g604.desktop` (it previously started only from the catalog-managed minimal-desktop autostart) and exits quietly on Wayland. `logitech_g604_onboard_buttons: false` skips the onboard button macros on hosts without an X11 Openbox session (pointer speed still applies).
- Fix: write the authd-oidc and authd-msentraid `broker.conf` as 0600; authd-oidc 0.5+ refuses to start with 0644, which left OIDC sign-in down after a broker restart.
- DeskMeet1 (`deskmeet-b760`) 8K TV still blanked about every 20 minutes on the
  stock (`pcon_frl_444=0`) path: both paths trained the VL605 PCON at FRL 40G,
  the TV's EDID maximum (#88). The i915 patch adds the module parameter
  `pcon_max_frl_gbps` (int, default 0 = no cap; rounded down to an FRL rate, so
  35 trains at 32G) that caps the rate in `intel_dp_pcon_start_frl_training()`,
  and the profile's modprobe option is now `options i915 pcon_frl_444=0
  pcon_max_frl_gbps=32`. 8K30 HDMI is 8 bpc RGB (~28.5 Gbps incl. blanking,
  ~23.9 Gbps active), which fits FRL 32G (8G x 4) with more per-lane margin;
  the running VL605 only echoes the requested rate in DPCD 0x3036 and keeps
  the HDMI link at 40G (TV SCDC 0x31 = FRL_Rate 5), so the cap is unverified
  until a reboot shows SCDC 0x31 = 0x34. The changed
  patch SHA makes the next pull rebuild the module; it takes effect after a
  reboot.
- DeskMeet1 (`deskmeet-b760`) 8K TV blacked out about once a minute with the
  patched i915's RGB 4:4:4 path on (8K30 DSC 14 bpp over HBR2 to the PCON,
  ~28.5 Gbps uncompressed on FRL 40G, the TV's maximum). The PCON's DPCD kept
  reporting the HDMI link up and zero FRL errors through each drop. The stock
  path also trains FRL 40G and runs without drops, so the 4:4:4 + DSC payload,
  not the link rate, is what the TV loses. The profile now installs
  `/etc/modprobe.d/curiosity-i915-pcon-frl-444.conf` (`options i915
  pcon_frl_444=0`): the patched module stays built, but i915 sends 8K30 the
  stock way (RGB 6 bpc dithered, HBR3, no DSC). Resolution stays 7680x4320@30. To try 4:4:4 again
  (certified 48G cable, Samsung Input Signal Plus), change the option to 1 and
  reboot. Live rollback: `echo 1 | sudo tee /sys/module/i915/parameters/pcon_frl_444`
  and modeset (`xrandr --output DP-3 --mode 7680x4320 --rate 25`, then `--rate 30`).

### Added

- `curiosity-tv` (`/usr/local/bin/curiosity-tv`, #91): controls the DeskMeet1 Samsung QN55QN700B (Tizen) TV without the physical remote. Subcommands `status` (REST, no token), `on` (Wake-on-LAN to both broadcasts, then polls PowerState), `off` (KEY_POWER only if on), `key`/`input`/`volume`/`channel`/`home`/`back` (WebSocket remote), `apps`, `launch`, `browse`, `text`, `cursor`, `pair`, and UPnP `volume get|set`. The pairing token comes from `$CURIOSITY_TV_TOKEN`, a 0600 cache in `$XDG_STATE_HOME/curiosity-tv/token`, or the Key Vault named by `samsung_tv_key_vault` (`CURIOSITY_TV_KEY_VAULT`), and is never printed. Installed by the `samsung_tv_cli` hardware-profile flag (`samsung_tv_cli_enabled`; `deskmeet-b760` opts in), with `python3-websocket` as its dependency.
- Hardware-profile key `idle_lock_minutes` sets the XScreenSaver idle lock timeout (`minimal_desktop_idle_lock_minutes`, default 10); `deskmeet-b760` uses 60.
- G604 thumb buttons: the lower right button (onboard KEY_4, keycode 13) runs `curiosity-mic-mute` (toggles the default microphone, shows a notice); the one sending KEY_5 (keycode 14) runs `openbox-zone toggle browser` (snap the focused window to the middle zone, press again to restore its exact previous geometry and maximized state). `openbox-zone` now uses `wmctrl` for maximize states. The G604 pointer speed is the onboard resolution `logitech_g604_dpi` (default 3200, was 800). Thumb 2 (keycode 11) runs `flameshot gui` (`logitech_g604_screenshot_keycode`/`_command`). Variables: `logitech_g604_mic_keycode`, `logitech_g604_zone_keycode`.
- Logitech G604 button profile: new hardware-profile key `logitech_g604`
  (derived `logitech_g604_enabled`, on for `deskmeet-b760`) installs
  `xbindkeys`, `/etc/curiosity/g604-xbindkeys.rc` and
  `/usr/local/bin/curiosity-g604`, started from the Openbox autostart. While a
  G604 is attached (detected by X input name) wheel tilt left/right send
  `Ctrl+Insert`/`Shift+Insert` (copy/paste) and thumb button 3 (keycode 12 from the
  G604's keyboard device, floated so it stops typing digits) runs
  `litra toggle`; buttons and the command are `logitech_g604_*` variables. New
  profile key `double_click_ms` (derived `minimal_desktop_double_click_ms`,
  default 500, `200` on `deskmeet-b760`) sets Openbox `doubleClickTime` and
  `gtk-double-click-time`, so a pull no longer reverts a hand-tuned interval.
  The two top buttons (stock DPI up/down) are reassigned in the mouse's onboard
  profile (libratbag/`ratbagd`) to F13/F14 and rotate through the
  Openbox desktops via the new `/usr/local/bin/curiosity-desktop`.
  Thumb button 1 is forward and the lower thumb button set to `KEY_6` is back
  (`Alt+Right` / `Alt+Left`).
  Rollback: drop the `curiosity-g604` autostart line and `pkill xbindkeys`;
  `ratbagctl <dev> profile 0 button 9 action set resolution-down` (and `10`
  `resolution-up`) restores the DPI buttons.
- DeskMeet1 (`deskmeet-b760`) console parity with X (#86): new hardware-profile
  key `console_video: "DP-3:7680x4320@30"` (derived variable `console_video`,
  empty elsewhere) installs
  `/etc/default/grub.d/92-curiosity-console-video.cfg`, which appends
  `video=DP-3:7680x4320@30` to `GRUB_CMDLINE_LINUX_DEFAULT` so every VT comes up
  at the TV's own CTA 8K30 mode and VT switches cause no modeset. The value is
  validated (`connector:WxH@Hz`, no `R`, no spaces); GRUB is regenerated only
  when the drop-in changes, and an empty value removes the drop-in and
  regenerates. Never use reduced-blanking or custom timings: the 8K TV rejects
  non-CTA 8K modes ("mode not supported"). The profile also opts in to console
  tuning (`console_tuning` mapping; `console_tuning_enabled`, `_font`, `_ttys`
  and `_manage_console_setup` are now derived from the profile) with
  `Uni3-Terminus32x16.psf.gz` on VT 1-9. Other profiles are unchanged.
  Rollback: `sudo rm /etc/default/grub.d/92-curiosity-console-video.cfg && sudo update-grub && reboot`.
- DeskMeet1 (`deskmeet-b760`) 8K display: the profile now sets
  `i915_pcon_frl_444`, `broadcast_rgb: Full` and `display_scale: 2`.
  `i915_pcon_frl_444_enabled` installs a patch for Intel i915 that keeps RGB
  4:4:4 behind a DisplayPort to HDMI 2.1 PCON cable (stock i915 sends YCbCr
  4:2:0), plus `/usr/local/sbin/i915-pcon-frl-444-build`, which fetches the
  matching Ubuntu kernel source, builds only i915, checks vermagic and
  modversions against the stock module and installs it to
  `/lib/modules/<kver>/updates`. Kernel postinst and postrm hooks rebuild and
  remove it with each kernel; a failed build is loud, never fatal to the kernel
  upgrade or the pull, and leaves the stock module. A reboot (or the manual
  `i915-reload` helper) activates a new module. `curiosity-display-setup` sets
  the xrandr `Broadcast RGB` property from `minimal_desktop_display_broadcast_rgb`
  (`/etc/curiosity/display-broadcast-rgb`) when the output has it, and
  `minimal_desktop_display_scale` renders `~/.Xresources` (`Xft.dpi`,
  `Xcursor.size`) and scales the tint2 panel height and launcher icons, so the
  2x desktop is no longer a hand edit that a pull could revert. The Openbox
  autostart now merges `~/.Xresources` with `xrdb` for startx sessions.
- GRUB autoboot on x86_64 `desktop`, `thin-client` and `byod-kiosk` hosts that
  have `/etc/default/grub`: the role installs
  `/etc/default/grub.d/90-curiosity-autoboot.cfg` (`GRUB_TIMEOUT_STYLE=hidden`,
  `GRUB_TIMEOUT=2`, `GRUB_RECORDFAIL_TIMEOUT=2`) and runs `update-grub` when it
  changes, so a boot Ubuntu considers failed no longer waits at the menu.
  Disable with `grub_autoboot_enabled: false`.
- Six-zone Openbox window snapping on every managed minimal desktop (laptop and
  DeskMeet1, all users): `/usr/local/bin/openbox-zone` (ported from the
  seantech workstation) and managed `rc.xml` bindings Super+Alt+1..6 for
  `agent-top`, `agent-bottom`, `browser`, `chats`, `top` and `bottom`. The
  script needs `xdotool` and `x11-utils` (`xprop`), now installed; run
  `openbox-zone --list` for the zone names.
- Bitwarden desktop app on every `desktop`-profile workstation, system-wide, so
  every managed user (for example `mrh` and `seantech` on DeskMeet1) finds
  Bitwarden in the application menu (`/usr/share/applications/bitwarden.desktop`,
  `/usr/bin/bitwarden`). The role installs the official amd64 `.deb` from the
  newest stable `desktop-v*` release of `bitwarden/clients` (new
  `latest_github_release_tag_prefixes`, because that repository also publishes
  `cli-`, `browser-` and `web-` releases and `/releases/latest` is not
  reliably the desktop app) and refuses to install unless the asset carries a
  SHA-256 digest, which the download is verified against. It is skipped for
  `byod-kiosk`, `thin-client` and `workspace`, on non-x86_64 hosts, and when
  `bitwarden_desktop_enabled` is `false`. It joins the update contract with a
  non-launching `chrome_crashpad_handler --version` healthcheck and rollback
  inventory. Users sign in themselves; the role never touches credentials or
  vault data.
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

- Google Chrome and other Secret Service clients hung on every page load in
  console (`startx`) sessions, such as the pinned VT sessions on DeskMeet1,
  while the same account worked under LightDM. Two causes: `/etc/pam.d/login`
  never unlocked the user's gnome-keyring "login" collection, and the systemd
  user / D-Bus activation environment kept the LightDM greeter's `DISPLAY` (or
  none), so the keyring unlock prompt could not be shown. While
  `minimal_desktop_pinned_vt_sessions` is non-empty the role now installs
  `libpam-gnome-keyring` and adds `auth optional pam_gnome_keyring.so` and
  `session optional pam_gnome_keyring.so auto_start` to `/etc/pam.d/login`
  (`optional`, so a keyring failure never blocks login; removed when no VT is
  pinned), and the Openbox autostart runs
  `dbus-update-activation-environment --systemd` for `DISPLAY` and
  `XAUTHORITY` first thing. Auto-unlock still requires the keyring password to
  match the account's login password.
- The autostart screen-lock harness tests no longer time out under full-suite
  load: the two worst-case handoff paths take ~11 s against a 15 s
  `subprocess.run` timeout, so the timeout is now a 60 s hang guard (#80).
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
