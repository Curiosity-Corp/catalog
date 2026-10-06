# Console tuning for large and mixed-size displays

This opt-in task group (tags `console, display`, `tasks/console-tuning.yml`)
makes the Linux text console usable on a large or HiDPI monitor:

- the console fills the whole screen instead of one corner;
- the console uses a readable bitmap font;
- the default text colour can be softened.

It is **off by default** (`console_tuning_enabled: false`), so unrelated
machines are unchanged. It never edits the bootloader or kernel command line,
installs a kernel, or restarts the machine.

## The problem

A 3840x2160 monitor is connected, but the text console only draws in the
top-left 1920x1080 area; the remaining three quarters of the screen are black.
Graphical sessions (X11/Wayland) are unaffected.

## Root cause

The kernel's DRM framebuffer-console emulation (`drm_fb_helper`, used by i915,
amdgpu, and others) allocates one framebuffer big enough for the **largest**
active display, but reports a *visible* size equal to the **smallest** enabled
output. The Linux console sizes its character grid from the visible size.

So a laptop with its lid closed but its internal 1920x1080 panel still lit,
plus an external 3840x2160 monitor, gives a console grid for 1920x1080 inside a
3840x2160 framebuffer. The monitor link itself is fine; it scans out the whole
framebuffer, and the console simply never draws outside its small grid.

You can see this with `FBIOGET_VSCREENINFO` on `/dev/fb0`: `xres` is smaller
than `xres_virtual`. The helper does exactly this check for you.

## What the role installs

When enabled on a Debian-family desktop or thin-client host (not in a
container, not on workspace/kiosk profiles):

| Item | Purpose |
| --- | --- |
| `/usr/local/sbin/curiosity-console-tuning` | Root-owned Python 3 helper (standard library only, no `fbset` needed) |
| `/usr/local/bin/curiosity-console-status` | Read-only diagnostics wrapper |
| `/etc/curiosity/console-tuning.conf` | Rendered configuration |
| `curiosity-console-tuning.service` | systemd oneshot, ordered after `console-setup` |
| `/etc/udev/rules.d/90-curiosity-console-tuning.rules` | Re-run when the KMS framebuffer appears and on display hotplug |

The helper grows `xres`/`yres` to the virtual size with `FBIOPUT_VSCREENINFO`
(the same effect as `fbset -fb /dev/fb0 -xres W -yres H`), loads the font on
each configured VT with `setfont -C /dev/ttyN`, and writes palette escapes
(`ESC ] P <index> <rrggbb>`) to each VT.

It exits 0 with one clear journal line, and changes nothing, when `/dev/fb0` is
absent, the system is a container, the framebuffer is not a DRM fbdev
emulation, the console is already full size, or a graphical session owns the
active VT. Early in boot the framebuffer is the firmware one, which the KMS
driver later replaces, so the service waits (bounded by
`console_tuning_wait_seconds`) and udev re-runs it when the real `fb0` appears.

## Variables

```yaml
---
console_tuning_enabled: true
# Optional: a PSF font file from /usr/share/consolefonts (or an absolute path).
console_tuning_font: Uni3-Terminus20x10.psf.gz
# Optional: soften the default foreground (7) and bright white (15).
console_tuning_palette:
  7: 857f7a
  15: b4afaa
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `console_tuning_enabled` | `false` | Master switch |
| `console_tuning_fill_framebuffer` | `true` | Grow the visible size to the virtual size |
| `console_tuning_font` | `""` | PSF font file name or absolute path; empty leaves the font alone |
| `console_tuning_palette` | `{}` | Index 0-15 to `rrggbb`; empty leaves colours alone |
| `console_tuning_ttys` | `[1, 2, 3, 4, 5, 6]` | VTs that receive the font and palette |
| `console_tuning_wait_seconds` | `15` | Upper bound for the early-framebuffer wait |
| `console_tuning_settle_seconds` | `2` | Short delay before reading `fb0` |
| `console_tuning_manage_console_setup` | `false` | Also write `FONT=` to `/etc/default/console-setup` |
| `console_tuning_install_packages` | `true` | Install `kbd`, `console-setup`, `python3` |
| `console_tuning_extra_packages` | `[]` | For example `[fonts-spleen]` |
| `console_tuning_apply_now` | `true` | Run the helper when its files change |

### Choosing a font

The kernel accepts only PSF bitmap fonts with exactly 256 or 512 glyphs, and
glyph `0x20` must be blank. The cell size changes the console grid
(3840x2160 with 10x20 cells gives 384x108 characters).

- Outline fonts rasterized to one bit (for example JetBrains Mono at 12x24)
  look uneven on a text console. Prefer true bitmap fonts.
- `Uni3-Terminus20x10.psf.gz` (10x20) ships with `console-setup`; a good middle
  size for a very large 4K display where 8x16 is too small and 12x24 slightly
  too big.
- `Uni3-Terminus32x16.psf.gz` (16x32) suits smaller 4K screens or poor eyesight.
- Spleen (`console_tuning_extra_packages: [fonts-spleen]`) provides clean
  `spleen-5x8`, `6x12`, `8x16`, `12x24`, `16x32`, and `32x64` fonts as
  `spleen-12x24.psfu.gz` and so on.

`console_tuning_font` is a *file name with its extension* because
`setupcon(1)` (console-setup) looks `FONT=` up as a file and does not accept a
bare `setfont` id. `setfont` accepts the same value, so one setting serves both.

### Persisting through console-setup

With `console_tuning_manage_console_setup: true` and a font set, the role writes
`FONT="..."` to `/etc/default/console-setup` and runs `setupcon --save-only`,
which copies the font where early boot can find it and changes nothing on
screen. The oneshot still runs afterwards, because the framebuffer fix and the
palette are not something console-setup can do.

### Palette notes

The palette is per VT and lost on reboot, which is why the same oneshot applies
it. Index 7 is the default foreground and 15 is bright/bold white. Leave
`console_tuning_palette` empty to keep stock colours.

## How to verify

```sh
sudo curiosity-console-status
```

It prints the framebuffer name, `xres`/`yres` against the virtual size,
DRM connector status and first mode, the active VT's mode, character grid and
font cell size, the configured values, and a final `verdict:` line:

- `verdict: console is using only a crop of the framebuffer` means the problem
  is present (the visible size is smaller than the virtual size);
- `verdict: OK, ...` means the console covers the whole framebuffer;
- a graphical session owning the active VT makes `fb0` values unrepresentative,
  so run it from a text console (Ctrl+Alt+F3).

To preview what the service would change without changing anything:

```sh
sudo /usr/local/sbin/curiosity-console-tuning --dry-run
```

Service logs: `journalctl -u curiosity-console-tuning`.

### Limitations

- The runtime fix is lost if the DRM driver reloads, and a graphical session
  that owns a VT can read back the smaller size. The helper skips the
  framebuffer step while a graphical session is on the active VT; run
  `sudo systemctl start curiosity-console-tuning` from a text console
  afterwards (hotplug and `fb0` creation re-run it automatically).
- It does not create one console per monitor and does not change X11/Wayland.
- Removal: set `console_tuning_enabled: false` (the role then no longer manages
  the files), then `systemctl disable --now curiosity-console-tuning` and delete
  the files in the table above.

## Documented only: the kernel command-line option

The stronger fix is to stop the kernel from lighting the small panel at all, so
fbdev is created at the monitor's size from the start. Add a
`video=<connector>:d` argument for the internal panel, for example
`video=eDP-1:d`, to the kernel command line. List connector names with
`curiosity-console-status`.

**This role never edits GRUB or any bootloader file.** If you choose this, do
it by hand and understand the risks:

- With the panel disabled and the external monitor absent at boot there is no
  display output at all.
- Many systems hide the GRUB menu; recovery means holding Shift (BIOS) or
  tapping Esc (UEFI) at boot to edit the entry and remove the argument.
- Keep a way to reach the machine remotely before trying it.
- At runtime a disabled connector can be re-detected with
  `echo detect > /sys/class/drm/<card>-<connector>/status` as root.

## Safety summary

- Default off; no existing default changes.
- Skipped in containers, on workspace and kiosk profiles, on non-Linux hosts,
  and (by the helper) wherever there is no `/dev/fb0`.
- No bootloader, kernel, or reboot actions. Packages come from the distribution.
- The service always exits 0 so it cannot block boot or fail a pull.
