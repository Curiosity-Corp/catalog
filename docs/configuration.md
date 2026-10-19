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

A profile may set `logitech_g604: true` (derived `logitech_g604_enabled`;
`deskmeet-b760` opts in) to install a Logitech G604 mouse button profile. The
role installs `xbindkeys` and `xdotool`, renders
`/etc/curiosity/g604-xbindkeys.rc` and installs `/usr/local/bin/curiosity-g604`,
which the Openbox autostart starts. The launcher detects the mouse by its X
input name at run time and binds the buttons only while a G604 is attached, so
an unplugged or re-paired mouse is picked up again without a re-login. The
defaults are wheel tilt left (X button 6) = copy (`Ctrl+Insert`), tilt right
(button 7) = paste (`Shift+Insert`, which also works in terminals) and the
top thumb button 3 = `litra toggle` for the lightbar (it needs `litra_cli`). The
thumb buttons 1-3 on the top of the side are not mouse buttons: they are key
presses (keycodes 10, 11, 12) from the G604's own keyboard device. The launcher
floats that device so the buttons stop typing digits, and runs the lightbar
command on `logitech_g604_lightbar_keycode` (12). Change
`logitech_g604_copy_button`, `logitech_g604_paste_button`,
`logitech_g604_lightbar_keycode` or `logitech_g604_lightbar_command` to remap;
`xinput test <id>` on the G604 keyboard device shows a button's keycode.

The launcher autostarts in any Openbox (X11) session via `/etc/xdg/autostart/curiosity-g604.desktop`; on GNOME/Wayland it does nothing, so set `logitech_g604_onboard_buttons: false` there to keep the thumb buttons from typing digits (the `logitech_g604_dpi` pointer speed still applies).

Solaar also stores a DPI per device and re-applies it whenever it starts or the
mouse reconnects, which overrode the onboard `logitech_g604_dpi` (a saved 1600
replaced 3200). The role installs `/usr/local/bin/curiosity-solaar-g604-dpi` and,
for each `dev_users` account, `~/.config/autostart/solaar.desktop` (it takes the
place of the packaged `/etc/xdg/autostart/solaar.desktop`). At login, `curiosity-solaar-g604-dpi --launch` marks
Solaar's `dpi` setting for the G604 as `ignore` (and records `logitech_g604_dpi`
there) and then starts `solaar --window=hide` (even if the config cannot be read), so the onboard profile is the only
source of the pointer speed. It changes nothing while Solaar has no G604 entry
yet, and only edits the file while Solaar is not running.

The lower right thumb button (keycode 13) toggles the microphone (`logitech_g604_mic_keycode`) and the button that sends KEY_5 (keycode 14) toggles the focused window between its place and the middle zone (`logitech_g604_zone_keycode`, `openbox-zone toggle`).
Rollback: remove the `curiosity-g604` line from `~/.config/openbox/autostart`
and `pkill -f curiosity-g604; pkill xbindkeys`.

The two buttons on top by the left click are DPI down/up inside the mouse and
send nothing to the computer, so the role reassigns them in the onboard profile
with `ratbagctl` (package `ratbagd`) to F13/F14. `curiosity-g604` turns those
keys into `curiosity-desktop prev` / `curiosity-desktop next`, which rotate
through the Openbox desktops (1 to 4, wrapping around). They never switch
virtual terminals. Thumb button 1 is browser forward (`Alt+Right`) and the lower thumb button
the onboard profile sets to `KEY_6` is back (`Alt+Left`). To give the top buttons back their DPI function:
`ratbagctl <device> profile 0 button 9 action set resolution-down` and
`... button 10 action set resolution-up`.

A profile may also set `double_click_ms` (default `500`), which feeds
`minimal_desktop_double_click_ms` for Openbox's `doubleClickTime` and GTK's
`gtk-double-click-time`. A lower value needs a quicker, more deliberate
double-click (`deskmeet-b760` uses `200`, because its G604 switch chatters; a
shorter interval does not filter chatter, so replacing the switch is the real
fix).

A profile may also set `display_scale` (an integer, default `1`) and
`broadcast_rgb` (`Full`, `Limited 16:235` or `Automatic`, default unset), which
feed `minimal_desktop_display_scale` and `minimal_desktop_display_broadcast_rgb`.
At a scale above 1 the role renders each managed user's `~/.Xresources`
(`Xft.dpi` of 96 times the scale, `Xcursor.size` of 24 times the scale, plus
the font rendering keys), the Openbox autostart merges it with `xrdb`, and the
tint2 panel height and launcher icon size are multiplied by the scale (for
example the `top` layout becomes a 72px bar with 50px icons at 2x). At 1 an
existing hand-made `~/.Xresources` and tint2 config are not touched.
`broadcast_rgb` is written to `/etc/curiosity/display-broadcast-rgb` and
applied by `curiosity-display-setup` to its chosen output through
`xrandr --set "Broadcast RGB"`, only when that output has the property (Intel
i915). i915 chooses limited range for HDMI/CEA modes by default, which washes
out a PC desktop on a TV; `Full` fixes that. `deskmeet-b760` sets
`display_scale: 2` and `broadcast_rgb: Full`.

### Samsung TV remote control

A profile that sets `samsung_tv_cli: true` (`deskmeet-b760`; derived variable
`samsung_tv_cli_enabled`) installs `python3-websocket` and
`/usr/local/bin/curiosity-tv`, which controls the Samsung QN55QN700B (Tizen) TV
at `192.168.1.250` (MAC `70:09:71:93:5A:20`) without the physical remote. It uses
the REST API (`:8001/api/v2/`), the WebSocket remote (`wss://:8002`), UPnP
RenderingControl (`:9197`) and Wake-on-LAN. `--host`/`--mac` (or
`CURIOSITY_TV_HOST`/`CURIOSITY_TV_MAC`) override the address. The WebSocket
client name is `Curiosity DeskMeet`, the name the TV already trusts.

```
curiosity-tv status                      # power, model, resolution (no token)
curiosity-tv on                          # Wake-on-LAN, wait up to --wait 30 s for PowerState on
curiosity-tv off                         # KEY_POWER, only if the TV is on
curiosity-tv key KEY_VOLUP sleep:1 KEY_VOLDOWN --repeat 2
curiosity-tv volume up 3 | down | mute   # keys; `volume get|set 20` uses UPnP
curiosity-tv channel up|down|7           # also: home, back
curiosity-tv input hdmi1..hdmi4          # changes what the TV shows; `input source` opens the list
curiosity-tv apps                        # installed apps (appId and name)
curiosity-tv launch youtube              # alias, app name or appId
curiosity-tv browse https://example.org  # TV browser at a URL
curiosity-tv text "hello"                # while an on-screen keyboard is open
curiosity-tv cursor move 40 0 | click    # pointer
curiosity-tv pair                        # new pairing; press Allow on the TV
```

`launch` posts to `/api/v2/applications/<id>` and falls back to the WebSocket
`ed.apps.launch` event. Built-in aliases: youtube, netflix, browser (internet),
plex, prime (primevideo, amazon), disney, hulu, spotify, appletv, youtubetv;
`curiosity-tv apps` shows the ids this TV really has. `input hdmiN` launches the
TV's source app `org.tizen.hdmiN` over the WebSocket, which is best effort and
untested on this firmware; `input source` (KEY_HDMI) is the reliable way to open
the source list. `volume get|set` needs the TV's UPnP service on port 9197, which
is only reachable while the TV is awake.

Everything over the WebSocket needs the pairing token. It is read from
`$CURIOSITY_TV_TOKEN`, else the cache `$XDG_STATE_HOME/curiosity-tv/token`
(mode 0600, default `~/.local/state`), else Azure Key Vault through `az keyvault secret show` (the value is then
cached). Set `samsung_tv_key_vault` (default empty, so no private name is in
this repository; `samsung_tv_key_vault_secret` defaults to
`deskmeet-samsung-tv-ws-token`) and the role renders the names, not the value,
into `/etc/curiosity/samsung-tv.env`; `CURIOSITY_TV_KEY_VAULT` and
`CURIOSITY_TV_KEY_VAULT_SECRET` override them per user. The token is never printed, logged or passed on a command
line; it only appears in the TLS URL inside the process. If the TV issues a new
token on connect, only the cache is updated and the tool says so on stderr:
write the new value to the Key Vault secret (`az keyvault secret set`) so other machines
stay in sync. Exit status is 0 on success, 1 on failure, 2 for a usage error and
3 when there is no usable token or the TV refuses it. When the TV is in standby
its ports are closed, so run `curiosity-tv on` first.

### Patched i915 for an HDMI 2.1 PCON

A profile that sets `i915_pcon_frl_444: true` (`deskmeet-b760`; derived variable
`i915_pcon_frl_444_enabled`) keeps RGB 4:4:4 on a DisplayPort to HDMI 2.1 PCON
cable at 8 bpc. At 8K30 stock i915 fits the mode into HBR3 without DSC by
dropping to 6 bpc RGB with dithering. The role installs
`/usr/local/share/i915-pcon-frl-444/i915-pcon-frl-444.patch` (only
`drivers/gpu/drm/i915/display/intel_dp.c`) and
`/usr/local/sbin/i915-pcon-frl-444-build <kver>`. The build script fetches the
Ubuntu kernel source that matches the installed `linux-modules-<kver>` with
`apt-get source` against a private `deb-src` list derived from the host's Ubuntu
archive entries (no system apt change, no extra repository), applies the patch
(dry run first), builds only i915 against `/lib/modules/<kver>/build`, requires
the same vermagic and the same imported modversion CRCs as the stock module,
strips it and installs it to `/lib/modules/<kver>/updates/i915.ko` (depmod
searches `updates/` first) with a marker holding the patch SHA-256. Any failure
exits non-zero, logs at error level to the journal (tag `i915-pcon-frl-444`),
writes `/var/lib/i915-pcon-frl-444/<kver>.failed` and leaves the stock module in
place. `/etc/kernel/postinst.d/zz-i915-pcon-frl-444` starts the build for every
newly installed kernel as a transient systemd unit and always exits 0 so it
cannot fail a kernel upgrade; `/etc/kernel/postrm.d/zz-i915-pcon-frl-444` removes
the module with its kernel. The role builds for the running kernel when the
marker is missing or the patch changed, and reports a failed build without
failing the pull. It never reloads i915: reboot to use a new module, or run
`/usr/local/sbin/i915-reload` from a console (it ends every graphical
session). `i915-pcon-frl-444-build --check <kver>` reports whether a build is
needed, and `--destdir DIR` builds and verifies without touching `/lib/modules`.

The module parameter `i915.pcon_frl_444` turns the 4:4:4 path on or off without
a rebuild. `deskmeet-b760` sets it to `0` through the profile's
`curiosity-i915-pcon-frl-444` modprobe option: on its Samsung 8K TV, 8K30 RGB
4:4:4 runs FRL at 40G (the TV's maximum) and the TV lost the signal about once a
minute while the PCON still reported the HDMI link up with no FRL errors. With
the parameter at `0`, i915 sends 8K30 the stock way (HBR3, no DSC) at the same
resolution. To test 4:4:4 live, write `1` to
`/sys/module/i915/parameters/pcon_frl_444` and force a modeset (for example
`xrandr --output DP-3 --mode 7680x4320 --rate 25`, then `--rate 30`); write `0`
and modeset again to go back.

The module parameter `i915.pcon_max_frl_gbps` (int, default `0` = no cap)
caps the HDMI FRL rate the PCON is asked to train at, below the lower of the
PCON's and the sink's (EDID) maximum. It only changes FRL link training in
`intel_dp_pcon_start_frl_training()`, not mode validation, and is independent of
`pcon_frl_444`. FRL only has the rates 9, 18, 24, 32, 40 and 48 Gbps, so any other
value is rounded down to the next of these (`35` trains at 32G; `1` to `8` train
at 9G); `dmesg` with `drm.debug=0x4` (KMS) logs the cap and the rate used.
`deskmeet-b760` sets `pcon_max_frl_gbps=32` in the same modprobe option
(`options i915 pcon_frl_444=0 pcon_max_frl_gbps=32`): both paths trained its
VL605 PCON at FRL 40G (the TV's EDID maximum) and the TV still blanked about every
20 minutes on the stock path. 8K30 over HDMI is 8 bpc RGB (~28.5 Gbps including
blanking, ~23.9 Gbps active), which fits FRL 32G (8G x 4 lanes) with more per-lane
margin. Caveat: on the running link the VL605 does not honour a lower rate. It
reports the requested rate in DPCD `0x3036` (`0x91`) within milliseconds, but
the TV's SCDC `0x31` (read through the PCON's DDC, I2C address `0x54`) stays at
FRL_Rate 5 (40G): the PCON trains the HDMI link on its own at the sink's EDID
maximum. Whether it honours the cap during the boot-time training is untested;
check SCDC `0x31` after a reboot (`0x34` means 32G). The SCDC character error
counters (`0x50`-`0x58`) are the reliable view of link health, the PCON's DPCD
FRL error counters stay at zero. The parameter arrived with a new patch, so the next pull rebuilds the
module (the marker's patch SHA-256 changes) and a reboot is needed before
`/sys/module/i915/parameters/pcon_max_frl_gbps` exists. After that it can be
written at runtime (root, mode `0600`) and applies at the next FRL training,
i.e. the next modeset or hotplug.

### StreamStack call and stream studio

`streamstack_enabled` (from the hardware profile's `streamstack` flag; only
`deskmeet-b760` sets it) installs OBS Studio, Easy Effects and the camera
tools, creates two `exclusive_caps` loopback cameras
(`streamstack_camera_video_nr`/`_label` for the camera bridge,
`streamstack_program_video_nr`/`_label` for the OBS virtual camera) and
installs the vendored StreamStack app (`streamstack`, `streamstack-tray`,
`streamstack-auto`). `streamstack_microphone` names the call microphone (part
of its PipeWire name; empty picks an Elgato Wave, then the default input). Enable it on any other desktop with
`streamstack_enabled: true` in `/etc/ansible/local-vars.yml`. The Canon bridge
is `streamstack_canon_bridge` (`gphoto2`, `command` with
`streamstack_canon_bridge_command`, or `none`); `streamstack_width`,
`streamstack_height` and `streamstack_fps` set the OBS canvas and bridge frame.
`streamstack_camera_external: true` adopts a camera node another tool on the
host already configures and feeds: StreamStack then adds only its program node.
Auto mode (off until `streamstack auto on`) is set by `streamstack_auto_backend`
(`claude-cli`, `api` or `local`), `streamstack_auto_model`,
`streamstack_auto_interval` (seconds, at least 5), `streamstack_auto_ai_interval`
(seconds, at least 60), `streamstack_auto_video`, `streamstack_auto_audio`,
`streamstack_auto_notify` and `streamstack_auto_autostart`.
See [`streamstack.md`](streamstack.md) for the Teams and Zoom setup.

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
text consoles. Openbox starts XScreenSaver with a 10-minute idle timeout (a hardware profile can set `idle_lock_minutes`; `deskmeet-b760` uses 60) and
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

While any VT is pinned the role also installs `libpam-gnome-keyring` and adds two
`optional` lines to `/etc/pam.d/login`, placed as in Ubuntu's `/etc/pam.d/lightdm`:
`auth optional pam_gnome_keyring.so` right after `@include common-auth`, and
`session optional pam_gnome_keyring.so auto_start` right after
`@include common-session`. This unlocks the user's gnome-keyring at console
login so Secret Service clients such as Google Chrome do not hang. Auto-unlock
only works when the keyring password matches the account password. The lines
are removed again when no VT is pinned, and the run fails rather than edit a
login stack that lacks the stock `@include` anchors. The Openbox autostart also
exports `DISPLAY` and `XAUTHORITY` to systemd and the D-Bus activation
environment, so the keyring prompt opens on the session's own display.

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
entries in `/etc/xdg/autostart` that are not restricted to other desktops, and
those in `~/.config/autostart` (for example device helpers such as Solaar or a
user's own entries). The `byod-kiosk` profile keeps XDG autostart off. The Openbox autostart already starts
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
