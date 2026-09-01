# T460 console support

This role now provides a safe, hardware-scoped diagnostic path for Lenovo
ThinkPad T460 systems. It does not turn the T460 into a different graphics
driver or automatically install a custom kernel.

## What the experimental mode is

The related patch changes the Linux DRM fbdev helper so that selected outputs
can use adjacent horizontal slices of one framebuffer when the kernel is
booted with:

```text
drm_kms_helper.fbdev_layout_horizontal=1
```

That is useful only for a raw TTY spanning independent displays. It is not an
i915 performance upgrade and has no useful effect on a single display,
ordinary mirroring, or an X11/Wayland desktop. It produces one wide TTY, not
one VT per monitor; use `tmux` for keyboard-only multitasking.

The patch and T460-specific test procedure live in
[`FocusApiary/x86-byod-laptop`](https://git.developerdojo.org/FocusApiary/x86-byod-laptop).

## Default behavior

For `hardware_profile: t460` and `hardware_profile: x86-byod-laptop`, the role
installs `/usr/local/bin/curiosity-t460-console-status`. It only reads
`/proc/cmdline` and DRM/fbcon sysfs state; it does not change display modes,
GRUB, the kernel, or the bootloader.

The horizontal mode is disabled by default:

```yaml
t460_fbcon_horizontal_enabled: false
```

This preserves the stock kernel and display behavior for normal T460 and BYOD
kiosk deployments.

## Deliberate test handoff

After a signed custom kernel containing the patch has been installed and
booted once, an operator may provide machine-local values in
`/etc/ansible/local-vars.yml`:

```yaml
hardware_profile: t460
t460_fbcon_horizontal_enabled: true
t460_fbcon_video_args:
  - video=eDP-1:d
  - video=HDMI-A-1:3840x2160@30
  - video=HDMI-A-2:3840x2160@30
```

Ansible then writes the complete test command line to
`/etc/curiosity/t460-fbcon-horizontal.cmdline`. It does not inject that line
into GRUB or reboot the machine. The operator must review the connector names,
keep a stock-kernel fallback, and perform the signed boot manually.

Set `t460_fbcon_horizontal_enabled: false` to remove the generated command-line
artifact and return to the default policy.

## Validation command

```sh
curiosity-t460-console-status
```

Record its output with the exact kernel release and physical monitor topology
when testing. Connector names can differ between T460 units, docks, and
adapters.
