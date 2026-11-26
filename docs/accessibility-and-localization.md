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

### Common layouts

Copy one row into your variables file. `""` means leave the value empty. Every
row is applied by the test suite with the role's own validation and template,
so the values below are known to pass. Keep `keyboard_model: pc105` unless your
keyboard is a different model.

| Keyboard | `keyboard_layout` | `keyboard_variant` | `keyboard_options` |
| --- | --- | --- | --- |
| US English (the default) | `us` | `""` | `""` |
| UK English with a Compose key on right Alt | `gb` | `""` | `compose:ralt` |
| French AZERTY | `fr` | `""` | `""` |
| Spanish | `es` | `""` | `""` |
| German without dead keys, Compose on right Alt | `de` | `nodeadkeys` | `compose:ralt` |
| US Dvorak | `us` | `dvorak` | `""` |
| US International (accents with dead keys) | `us` | `intl` | `""` |
| US and Russian, Alt+Shift switches | `us,ru` | `,` | `grp:alt_shift_toggle` |

With several layouts, give one variant per layout separated by commas: `,`
means "no variant" for both `us` and `ru`, and `,phonetic` would choose the
phonetic Russian layout. Quote `keyboard_options` in YAML when it contains a
colon, as in the example above.

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

Not configured, to be honest about it: the role never sets up a screen reader,
on-screen keyboard, sticky keys or other assistive input for anyone. It can
install them when you ask (next section); configuring them stays with the
person who uses them, and the role will not overwrite their settings, because
it only writes the files named here.

## Assistive technology packages (opt-in)

A screen reader, a braille display driver, an on-screen keyboard or sticky keys
can be installed from the first run, which matters most on a `thin-client` or
`byod-kiosk` appliance where nobody can install them afterwards. Nothing is
installed unless you list it:

| Variable | Default | Notes |
| --- | --- | --- |
| `accessibility_packages` | empty list | Packages to install, from the names below only |

| Name | What it gives |
| --- | --- |
| `orca` | Screen reader, speech and braille output |
| `espeak-ng` | Speech synthesiser (usually pulled in by `orca`) |
| `brltty` | Braille display driver. Only list it when a braille display is used: its udev rules have claimed some USB-serial adapters (CH340/CH341) |
| `onboard` | On-screen keyboard |
| `xkbset` | Sticky keys, slow keys and bounce keys on X11 |

```yaml
accessibility_packages: [orca, onboard]
```

Apply with `--tags accessibility` (the full run installs them too). It works on
the `desktop`, `thin-client` and `byod-kiosk` profiles, on every supported
Ubuntu and Debian release. A `workspace` has no desktop session, so a non-empty
list there is refused.

What to expect:

- Any other name, including a different spelling, stops the run before
  anything changes, with a message that lists the accepted names.
- On Ubuntu, `espeak-ng`, `onboard` and `xkbset` come from the `universe`
  component. If it is not enabled, the run stops before installing anything and
  says so; enable it with `add-apt-repository universe`.
- Packages only. The role starts nothing, adds no autostart entry and writes
  none of the tools' settings: Orca's speech rate and key bindings, the braille
  table, Onboard's layout and whether sticky keys are on (`xkbset sticky` in
  your own session start) stay with the person who uses them.
- Removing a name from the list does not uninstall the package, because
  removing a screen reader can lock someone out. Uninstall it by hand when it
  is really no longer needed.

Why these names and no others is
[decision record 0003](decisions/0003-opt-in-assistive-technology-packages.md).
It is still "proposed": no review by a person who uses assistive technology has
happened yet. If you use one of these tools, your review on catalog#136 is
welcome, especially whether Orca speaks under the minimal desktop's Openbox
session.

## Checked by

`tests/test_accessibility_defaults.py` renders and applies the role's own task
text with synthetic values, into a temporary directory instead of `/etc`:
every row of the common-layouts table (read from this page, so the table and the
test cannot drift), quotes and upper-case refused, locales accepted and refused, and the display scale at 1 (low
resolution, file untouched), 1.5, 2.25 and 4 (rendered) and at 0.5, 1.1, 5 and a
word (refused). It also checks which profiles force a keyboard and that the
defaults leave other profiles alone. For `accessibility_packages` it checks that
the empty default skips the install entirely, that the accepted names are exactly
the decision record's list, that an unknown name, a string instead of a list and
a list on a `workspace` are refused before any later task, that a package with no
install candidate (no `universe`) stops the run before installing, and that the
task file can only install packages. No physical display is involved, so the
screen itself, a real non-US keyboard, a real high-DPI monitor and a
screen reader actually speaking are still
worth one manual look; see the
[clean-room run template](evidence/clean-room-run-template.md).
