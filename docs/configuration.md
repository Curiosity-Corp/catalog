# Configuration guide

The role defaults are deliberately useful for a generic Linux workstation and
conservative for optional integrations. Put site-specific values in inventory,
`group_vars`, `host_vars`, Ansible Vault, or a secret manager. The tracked
`playbooks/host_vars/localhost.yml` is a documentation-safe example, not a
machine inventory.

## Minimal local variables

```yaml
---
dev_user: researcher
dev_user_home: /home/researcher
workstation_profile: desktop
hardware_profile: generic

# Optional: permanently mask obsolete user-scoped systemd units. Keep this in
# protected host/group inventory when the names identify private applications.
workstation_user_systemd_masked_units:
  - obsolete-helper.service
  - obsolete-helper.timer
```

For a VM or an existing user, pass the actual account and home directory. The
role asserts that the account exists and that its home is a directory before it
changes anything else.

## Hardware profile opt-ins

A hardware profile in `hardware_profiles` may set `litra_cli: true` to install
the upstream `litra` CLI (`timrogers/litra-rs`) to `/usr/local/bin/litra` so a
Logitech Litra lightbar is controllable from software. The derived
`litra_cli_enabled` variable follows the selected profile (`deskmeet-b760`
opts in; every other profile is `false`) and can be overridden per host. The
release asset is verified against its GitHub SHA-256 digest, the binary is part
of the update rollback inventory, and the `litra --version` healthcheck only
warns on hosts where the flag is off. The profile's udev rule must still grant
the user hidraw access to the lightbar.

## Optional private services

Keep these values out of the repository:

```yaml
---
coder_cli_enabled: true
coder_deployment_url: https://coder.example.edu

keycloak_sso_enabled: true
keycloak_host: login.example.edu
keycloak_realm: research
keycloak_desktop_client_id: workstation-login

ziti_enabled: true
ziti_oidc_provider: lab-oidc
ziti_intercept_domains:
  - registry.lab.example.edu
  - notebooks.lab.example.edu
ziti_etc_hosts_pins:
  - registry.lab.example.edu
```

Use `ziti_service_name` and `ziti_manage_service: false` when a separate
operator-managed tunnel unit owns the interface. Pins are refreshed only for
names explicitly supplied by the operator.

## User-scoped systemd masks

`workstation_user_systemd_masked_units` is an opt-in list of bare user-unit
names. For each entry the role stops and disables a running unit when the
managed user's systemd session is available, removes stale `default.target`
and `timers.target` links, and converges the unit path to systemd's native
`/dev/null` mask. It also clears any stale failed state recorded for the unit,
so a retired timer cannot continue to appear as a failed user service after it
is masked. A mask prevents a superseded timer or service from being started
again by an installer or helper. The default is empty so unrelated workstations
are unchanged; keep organization-specific unit names in protected inventory
rather than in this public collection.

## Custom CA certificates

The source path is local to the Ansible controller. With `ansible-pull`, that
means a root-readable path on the managed machine, outside the checkout:

```yaml
custom_ca_certificates:
  - name: research-registry
    src: /etc/ansible/private/research-registry.crt
    nss_name: Research registry
```

The role updates the system trust store and, when `nss_name` is set, imports
the certificate into the managed user's NSS database. Do not commit a private
CA certificate merely because it is not a private key: certificate subjects,
names, and topology can still disclose sensitive infrastructure.

## Screen locking

The standard minimal Openbox desktop installs XScreenSaver and `vlock`, and
removes Light Locker, `i3lock`, and `xss-lock`. LightDM starts the graphical
session no earlier than VT8, leaving the lower virtual terminals available for
text consoles. Openbox starts XScreenSaver with a 10-minute idle timeout and
blank-only mode. It uses the host's login PAM policy and displays its prompts in
the unlock dialog, so a machine that requires a password and a second factor
asks for them separately. The lock uses the current account; no username or
password change is needed. Accounts whose login PAM requires Google
Authenticator must be enrolled before automatic locking is enabled; the role
does not create or replace MFA secrets. Press **Super+L** or run
`xscreensaver-command --lock` to lock manually. XScreenSaver also locks on
system suspend through its systemd integration.

The locker uses the host's PAM authentication factors. The packaged
XScreenSaver 6.08 implementation does not reliably enforce PAM account-expiry
or account-disable checks for an already-running desktop session; terminate
the user session to revoke an existing session immediately.

To lock a text console, log in on that TTY and run `vlock`. It locks the
current virtual console; other VTs remain available. Do not use `vlock --all`
unless you intentionally want to disable VT switching. The role checks that
the graphical account has a usable password hash before enabling automatic
locking and never changes account passwords.

For the `desktop` profile, this Openbox setup is opt-in:

```yaml
minimal_desktop_enabled: true
```

The `thin-client` profile includes the minimal desktop automatically. The
`byod-kiosk` profile is deliberately separate: it replaces the normal Openbox
startup, disables blanking and system sleep, and does not install or start
these locking tools. The kiosk also disables the lower virtual terminals as
part of its lockdown policy. The role records original TTY/sleep-unit and user
group state so leaving kiosk mode can restore it without unmasking units that
were already masked by an administrator.

After `ansible-pull` updates a running machine, reboot once so LightDM restarts
on VT8 and the new Openbox autostart and keybinding load. An existing graphical
session keeps its already-started processes until it restarts.

## Shared workstations

`dev_user` stays the primary account for every user-scoped task that is not
listed here. To converge the desktop for several interactive accounts, list
them in `dev_users` (the primary account must be included):

```yaml
dev_users:
  - { name: user-a, home: /home/user-a }
  - { name: user-b, home: /home/user-b }
workstation_user_linger: [user-a]
```

The default is a one-item list built from `dev_user` and `dev_user_home`, so
single-user hosts are unchanged. Per-user home checks, ownership repair, the
user-systemd mask policy, the Openbox, picom, tint2, dunst, rofi, GTK and
Firefox configuration, and the XScreenSaver settings (including the unlockable
password check) run once for each listed account. Everything else, including
LightDM autologin, remains primary-account or system-wide; set
`minimal_desktop_autologin: false` on hosts where several people log in.
`workstation_user_linger` enables `loginctl enable-linger` for the named users.

### LightDM

| Variable | Default | Effect |
| --- | --- | --- |
| `minimal_desktop_greeter` | `gtk` | `slick` installs and configures slick-greeter instead of the GTK greeter. |
| `minimal_desktop_lightdm_minimum_vt` | `8` | First VT LightDM may use. |
| `minimal_desktop_lightdm_vtswitch_wrapper` | `false` | Installs `/usr/local/bin/lightdm-xserver-wrapper`, which drops `-novtswitch` so Ctrl+Alt+Fn keeps working. |
| `minimal_desktop_lightdm_remove_dropins` | `[]` | File names to delete from `/etc/lightdm/lightdm.conf.d`. |

The role starts the Openbox user session, selects the greeter explicitly, and
keeps `lightdm.service` enabled and running. It does not restart LightDM, so
an active session survives a pull; reboot to apply a greeter or VT change.

### Pinned per-user VT sessions

```yaml
minimal_desktop_lightdm_minimum_vt: 10
minimal_desktop_pinned_vt_sessions:
  - { user: user-a, vt: 8 }
  - { user: user-b, vt: 9 }
```

Each VT gets a getty that skips the user-name prompt and starts `login` for the
pinned account, which still asks for the password. The account's login shell
then runs `startx /usr/bin/openbox-session -- vtN -keeptty`, but only on that
user's own VT and outside SSH or an existing graphical session. Every pinned VT
must be below `minimal_desktop_lightdm_minimum_vt`, and every pinned user must be
listed in `dev_users`, so the per-user Openbox autostart starts the same PAM
screen locker as a LightDM session. The hook is a `/etc/profile.d` script and
therefore needs a POSIX or bash login shell. The getty is enabled but not
started during the pull: reboot or run `systemctl start getty@ttyN` to use a
VT that is currently idle. Removing an entry deletes its getty drop-in.

### Display setup

```yaml
minimal_desktop_display_setup_enabled: true
minimal_desktop_display_outputs: [DP-1, HDMI-A-1]
```

`/usr/local/bin/curiosity-display-setup` reads the output names (one per line in
`/etc/curiosity/display-outputs`), picks the first connected one (or the first
connected output when none matches), sets its largest progressive mode at the
highest refresh rate, and makes it primary. Other outputs are switched off only
when a listed output matched. LightDM runs it as `display-setup-script`, and the
Openbox autostart runs it for startx sessions. It reports problems on stderr but
never blocks a login. Disabling the option removes the helper and its hooks.

### Panel layouts

| Variable | Default | Effect |
| --- | --- | --- |
| `minimal_desktop_tint2_layout` | `bottom` | tint2 layout deployed to each user's `~/.config/tint2/tint2rc`. |
| `dev_users[].tint2_layout` | unset | Per-user override of the variable above. |

```yaml
minimal_desktop_tint2_layout: bottom
dev_users:
  - { name: user-a, home: /home/user-a }
  - { name: user-b, home: /home/user-b, tint2_layout: top }
```

`bottom` is a dark 32px bar at the bottom of the screen with the desktop
taskbar, system tray and clock. `top` is a 36px bar at the top with a launcher
(file manager, Firefox, terminal, display settings, volume mixer), a
multi-desktop task list, the tray, a PulseAudio volume item and the clock. The
volume item runs `/usr/local/bin/tint2-volume-status`, installed by the role,
and needs `pactl` (installed as `pulseaudio-utils`; PipeWire-pulse works too).
Any other value fails the run before a config is written. The layouts are
`roles/developer_workstation/files/minimal-desktop/tint2/<layout>.tint2rc`; each
background block must be defined before an id refers to it, because tint2 17
crashes on an undefined `panel_background_id`.

### XDG autostart

The role installs `python3-xdg`, so Openbox's `openbox-xdg-autostart` runs the
entries in `/etc/xdg/autostart` and `~/.config/autostart` (for example the
keyring and device tray helpers). The Openbox autostart already starts
nm-applet, blueman, pasystray and picom itself, and XScreenSaver handles
locking, so each `dev_users` account gets a managed
`~/.config/autostart/<id>.desktop` with `Hidden=true` for every id in
`minimal_desktop_xdg_autostart_hidden`:

```yaml
minimal_desktop_xdg_autostart_hidden:
  - nm-applet
  - blueman
  - pasystray
  - picom
  - light-locker
  - xfce4-screensaver
  - solaar   # example host-specific addition: replaces the list, so repeat the defaults
```

The PolicyKit authentication agent for GUI privilege prompts is `lxpolkit`
(`policykit-1-gnome` is not in Debian trixie). Its own autostart entry is not
restricted to other desktops, so the XDG autostart starts it once under Openbox;
the Openbox autostart does not launch it a second time. Sessions that were
already running keep their current processes until they restart.

The branded FocusPass screen-lock package is an optional local-PAM integration.
It is installed only when all three variables below are supplied for a
desktop or thin-client profile with the minimal desktop enabled:

```yaml
focuspass_screenlock_enabled: true
focuspass_screenlock_install_url: https://artifact-host.example/focuspass-screenlock.deb
focuspass_screenlock_install_sha256: <64-hex-character-sha256>
```

The role downloads the package into its digest-addressed artifact cache and
installs it with APT. The supplied URL must use HTTPS, its SHA-256 must match,
the host must be amd64, and the package must identify as
`focuspass-screenlock` and provide both expected launchers. The role records
the installed artifact digest and
reinstalls when that pin changes, even if the Debian version does not.
Ansible check mode validates the architecture, URL, and digest settings, then
skips downloading and installing the package; run a normal pull to apply it.
Disabling the option, disabling the minimal desktop, or switching to another
profile removes the package when Catalog previously installed it and recorded
its ownership marker. An unmanaged package already on the host is left in
place. The default `false` setting prevents Ansible from installing
FocusPass; Openbox still prefers its launcher if it was installed separately.
When FocusPass is installed, Openbox starts
`focuspass-screenlock` and Super+L runs `focuspass-screenlock-command`; both
commands fall back to the distribution XScreenSaver launcher if FocusPass is
missing. The package
uses the same `/etc/pam.d/xscreensaver` contract shown above and does not
authenticate to a network IdP or require a FocusPass account. It is excluded
from `byod-kiosk`.

## Safety-sensitive switches

`sunshine_enabled`, `profile_cleanup_enabled`, `tmp_hygiene_enabled`,
`ziti_enabled`, and all
identity-provider integrations default to `false`. If enabling Sunshine,
provide a non-default credential from a protected variable or configure it
interactively. If enabling cleanup, test the retention window on a disposable
shared machine. If enabling automatic updates, choose an update ring and keep
the local rollback state on a persistent filesystem.

## Console tuning

`console_tuning_enabled` (default `false`) opts a large or mixed-size-display
workstation into a full-screen text console, a readable PSF bitmap font
(`console_tuning_font`), and softer console colours (`console_tuning_palette`).
It never edits the bootloader or kernel command line and is skipped in
containers and on workspace/kiosk profiles. See
[`docs/console-tuning.md`](console-tuning.md) for the cause, variables,
verification (`curiosity-console-status`), and the documented-only kernel
command-line option.

## /tmp hygiene

`tmp_hygiene_enabled` (default `false`, because it deletes files) gives `/tmp`
an age-based cleanup through a `/etc/tmpfiles.d` drop-in that the native
`systemd-tmpfiles-clean.timer` already runs daily, plus an hourly
`curiosity-tmp-pressure` timer that runs the same cleanup early when the
tmpfs holding `/tmp` is nearly full. Variables:

| Variable | Default |
| --- | --- |
| `tmp_hygiene_enabled` | `false` |
| `tmp_hygiene_max_age` | `3d` |
| `tmp_hygiene_pressure_enabled` | `true` |
| `tmp_hygiene_pressure_threshold` | `85` |
| `tmp_hygiene_pressure_age` | `1d` (empty: warn only, no second pass) |
| `tmp_hygiene_pressure_interval` | `1h` |
| `tmp_hygiene_exclusions` / `tmp_hygiene_extra_exclusions` | live-session paths / `[]` |

It is skipped on the `workspace` profile and in containers. See
[`docs/tmp-hygiene.md`](tmp-hygiene.md) for the design, safety properties, and
how to retire a hand-installed cleaner.

## Free Ubuntu-Pro equivalents (pro-mimic)

The `pro-mimic` plane (tags `pro-mimic, security`, `tasks/pro-mimic.yml`)
converges free Pro-equivalent posture on every pull without attaching
machines to Ubuntu Pro (`ubuntu_pro_token` stays empty). It is report-only
by default: automatic reboots stay disabled, CIS content stays
scan/report-only (`pro_mimic_cis_remediate: false`), and no livepatch DIY is
attempted — pending reboots surface via MOTD and
`/var/lib/curiosity/ansible-pull-status.json` instead.

```yaml
---
# Fleet laptop posture: GUI starts only on explicit request.
display_manager_mode: ondemand
# Restart services automatically after library upgrades (default true).
pro_mimic_needrestart_auto: true
# Never auto-restart these services, e.g. ['^docker$', '^libvirtd$'].
pro_mimic_needrestart_blacklist: []
# Allow password SSH logins (default false keeps key-only hardening).
pro_mimic_ssh_password_auth: false
```

Per-host values like `display_manager_mode` belong in the machine-local
`/etc/ansible/local-vars.yml` (or host/group vars), never in the role
defaults. Run a focused convergence with
`ansible-playbook playbooks/site.yml --tags pro-mimic`.
