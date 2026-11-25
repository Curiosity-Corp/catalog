# 0003: Opt-in assistive technology packages

- Status: proposed (not yet reviewed by a person who uses assistive
  technology, see "Review")
- Date: 2026-11-22 (package archives checked 2026-11-25)
- Deciders: catalog maintainers; needs review by at least one person who uses
  assistive technology before it is accepted
- Affects: new variable `accessibility_packages`, tag `accessibility`;
  `desktop`, `thin-client` and `byod-kiosk` profiles; Ubuntu and Debian
- Issue: catalog#136

## Context

The role installs no screen reader, on-screen keyboard or sticky-keys helper,
and the accessibility guide says so plainly. A researcher who needs one from day
one installs it by hand after the first run, and on a `byod-kiosk` or
`thin-client` appliance there may be nobody able to do that. The people
affected are exactly the ones a missing tool locks out of the machine.

The role must not decide for anyone which tools they use, and must not touch
the settings a person has tuned for themselves: a screen reader user's speech
rate, verbosity and key bindings are theirs.

## Decision

Add one list variable, `accessibility_packages` (default `[]`). When it is
non-empty the role installs the listed packages with the distribution's
package manager under the `accessibility` tag, and does nothing else: it
starts no service for a user, writes no user configuration and adds no
autostart entry.

Only names from a short, reviewed list are accepted; any other name stops the
run before anything is installed, with a message that lists the accepted
names. The first list:

| Package | What it gives | Notes |
| --- | --- | --- |
| `orca` | Screen reader (speech and braille output) | Pulls in the AT-SPI accessibility bus and a speech synthesiser |
| `espeak-ng` | Speech synthesiser | Usually pulled in by `orca`; listed so a lab can choose it explicitly |
| `brltty` | Braille display driver | Opt-in only: its udev rules have claimed some USB-serial adapters (for example CH340/CH341 on Ubuntu 22.04), which breaks lab hardware that uses them |
| `onboard` | On-screen keyboard | In `universe` on Ubuntu; if a later release drops it, the install fails loudly rather than silently skipping it |
| `xkbset` | Sticky keys, slow keys and bounce keys on X11 | Enabling them stays the user's choice (`xkbset sticky` in their own session start) |

### Availability on the supported distributions

Checked on 2026-11-25 against the real archives: Launchpad's published
binaries for Ubuntu (amd64) and the Debian archive (`qa.debian.org/madison`).
Every accepted name is published on every release in the
[compatibility matrix](../compatibility.md); the versions are what an install
would get that day.

| Release | `orca` | `espeak-ng` | `brltty` | `onboard` | `xkbset` |
| --- | --- | --- | --- | --- | --- |
| Ubuntu 22.04 `jammy` (supported) | 42.0 (main) | 1.50 (universe) | 6.4 (main) | 1.4.1 (universe) | 0.6 (universe) |
| Ubuntu 24.04 `noble` (supported) | 46.1 (main) | 1.51 (universe) | 6.6 (main) | 1.4.1 (universe) | 0.8 (universe) |
| Ubuntu 25.10 `questing` (experimental) | 49.1 (main) | 1.52.0 (universe) | 6.7 (main) | 1.4.1 (universe) | 0.8 (universe) |
| Ubuntu 26.04 `resolute` (experimental) | 50.3 (main, updates) | 1.52.0 (universe) | 6.7 (main) | 1.4.3 git snapshot (universe) | 0.8 (universe) |
| Debian 12 `bookworm` (experimental) | 43.1 | 1.51 | 6.5 | 1.4.1 | 0.6 |
| Debian 13 `trixie` (experimental) | 48.1 | 1.52.0 | 6.7 | 1.4.1 | 0.8 |

On Ubuntu, `espeak-ng`, `onboard` and `xkbset` come from `universe`, so the
task must check that component is enabled and stop with a message if it is not.
All five are built for arm64 on Debian 12 and 13. `orca` is the GNOME screen
reader and runs under the minimal desktop's Openbox session only with the
AT-SPI bus, which its package pulls in; whether speech actually works there is
exactly what an assistive-technology user's review must check. The same check
is repeated, with the date, whenever the list or the compatibility matrix
changes.

Adding a package to the list is a reviewed change to this record and the role,
not a host variable, so the list stays something a maintainer can test.

## What the role must not overwrite

- Anything under the user's home: `~/.config/orca/`, `~/.local/share/orca/`,
  `~/.config/onboard/`, `~/.xkbset`-style start scripts, autostart entries.
- Per-user desktop accessibility settings (GSettings `org.gnome.desktop.a11y.*`,
  `org.gnome.desktop.interface toolkit-accessibility`).
- System configuration the packages install as configuration files, such as
  `/etc/brltty.conf`: the package manager keeps a local edit, and the role does
  not template these files.
- The minimal desktop's Openbox key bindings: no new global shortcut is added,
  so nothing a person already bound changes meaning.

Removing a name from the list does not uninstall the package: a person may
depend on it, and removing a screen reader can lock them out. Uninstalling is a
deliberate manual step.

## Safety impact

With the default `[]` nothing changes on any machine. With a list, the only
change is installed packages; the existing accessibility settings (keyboard,
locale, display scale) are unaffected. Turning it off is setting the list back
to `[]`; installed packages stay, as above.

## Alternatives considered

- **A generic extra-packages list.** Simpler, but it would not say anything
  about assistive technology, could not be tested meaningfully, and would let
  a typo install an unrelated package.
- **Installing a default set on every desktop.** Costs every machine disk,
  attack surface and, with `brltty`, a known hardware conflict, for tools most
  users never start.
- **Configuring the tools (auto-starting Orca, enabling sticky keys).** Takes
  a decision away from the person who uses them, and gets it wrong for every
  setup but one.

## Consequences

When accepted: `tasks/accessibility-packages.yml` with the validation and one
`apt` task, `accessibility_packages: []` in the defaults, a section in the
accessibility guide, and tests in the style of
`tests/test_accessibility_defaults.py` (empty list: no task runs; unknown name:
refused with the accepted list; each accepted name renders one apt install).
Revisit when a reviewer asks for a package that is not on the list, or when the
minimal desktop moves off X11 (`xkbset` is X11-only).

## Review

No review by a person who uses assistive technology has happened yet. Nobody
has been asked on the project's behalf so far. The record stays "proposed"
until at least one such person has reviewed it; their comments are recorded
here only with their permission. To review it, comment on catalog#136.
