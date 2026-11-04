# Accessibility and localization

The baseline does not assume one keyboard, language, screen size or way of
working. Each setting below is an explicit variable. Nothing is forced on a
machine unless its profile says so, or you ask for it.

Set these in `/etc/ansible/local-vars.yml` (or pass `-e @your-file.yml`), then
run the tag shown.

## Keyboard

| Variable | Default | Notes |
| --- | --- | --- |
| `keyboard_manage` | `true` on `thin-client` and `byod-kiosk`, `false` elsewhere | Whether the role writes `/etc/default/keyboard` at all |
| `keyboard_layout` | `us` | An XKB layout code such as `us`, `de` or `gb`; several layouts separated by commas, for example `us,ru` |
| `keyboard_model` | `pc105` | The physical keyboard model |
| `keyboard_variant` | empty | For example `nodeadkeys`; give one entry per layout when you list several layouts |
| `keyboard_options` | empty | For example `compose:ralt` (a Compose key) or `grp:alt_shift_toggle` (switch layouts) |

Why the kiosk profiles force a value: an appliance has nobody to fix a wrong
layout at the login prompt, so they write one. A desktop or workspace is left
alone, because the person using it chose their layout.

```yaml
keyboard_manage: true            # also needed on desktop or workspace
keyboard_layout: de
keyboard_variant: nodeadkeys
keyboard_options: "compose:ralt"
```

Apply with `--tags keyboard`. Values are checked before anything is written:
a layout that is not a lower-case code, or a value with a quote or other shell
character in it, stops the run with a message that names the value.

## Language and locale

| Variable | Default | Notes |
| --- | --- | --- |
| `locale_manage` | `false` | The role never changes a machine's language unless this is `true` |
| `locale_lang` | empty | The system default, for example `en_GB.UTF-8`, `de_DE.UTF-8` or `C.UTF-8`. Required when `locale_manage` is `true` |
| `locale_extra` | empty list | More locales to generate so users can pick them, same format |

```yaml
locale_manage: true
locale_lang: en_GB.UTF-8
locale_extra: [de_DE.UTF-8, fr_FR.UTF-8]
```

Apply with `--tags locale`. Only UTF-8 locales are accepted. A user's own `LANG`
setting in their shell files overrides the system default, and their desktop
language setting is theirs to change.

## Display size and scale

The desktop scale comes from the hardware profile's `display_scale`, or you set
the variable it feeds directly:

```yaml
minimal_desktop_display_scale: 2      # 1 to 4 in steps of 0.25
```

| Scale | Effect |
| --- | --- |
| `1` (default) | Nothing is written. A hand-made `~/.Xresources` stays as it is. Use this for ordinary and low-resolution screens |
| above `1` | `~/.Xresources` gets `Xft.dpi` = 96 x scale and `Xcursor.size` = 24 x scale, and the tint2 panel height and icon size grow by the same factor. Scale 2 gives 192 dpi, a 48 px cursor and a 72 px panel; 1.5 gives 144 and 36 |

A value outside 1 to 4, or not a multiple of 0.25, stops the run with a message
(`1.1`, `0.5`, `5` and `large` are all refused), so a typo cannot produce an
unreadable screen. This applies to the minimal desktop; other desktops use
their own scaling settings.

## Accessible overrides: what is and is not covered

You can do these today by setting the variables above:

- A non-US or multi-layout keyboard, a Compose key and a layout switch.
- A system language and extra locales.
- Large text and cursor through `minimal_desktop_display_scale`, which also
  makes the panel and icons bigger.

Not covered, to be honest about it: screen readers, on-screen keyboards, sticky
keys and other assistive input are not installed or configured by the role.
Install what the person needs and keep it in their own configuration; the role
will not overwrite it, because it only writes the files named here.

## Checked by

`tests/test_accessibility_defaults.py` renders and applies the role's own task
text with synthetic values, into a temporary directory instead of `/etc`: a
German keyboard with a variant, a two-layout keyboard, quotes and upper-case
refused, locales accepted and refused, and the display scale at 1 (low
resolution, file untouched), 1.5, 2.25 and 4 (rendered) and at 0.5, 1.1, 5 and a
word (refused). It also checks which profiles force a keyboard and that the
defaults leave other profiles alone. No physical display is involved, so the
screen itself, a real non-US keyboard and a real high-DPI monitor are still
worth one manual look; see the
[clean-room run template](evidence/clean-room-run-template.md).
