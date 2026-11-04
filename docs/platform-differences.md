# Platform package differences

Dated results of asking each distribution's package archive for the 144 package
names in the role's main install task (`roles/developer_workstation/tasks/packages.yml`,
task "Install all APT packages"). That task installs everything in one `apt` call,
so one name a platform lacks fails the whole task. The data is in
[`evidence/platform-packages-2026-11-04.json`](evidence/platform-packages-2026-11-04.json).

How it was measured, on 2026-11-04: a container per release (`ubuntu:22.04`,
`ubuntu:24.04`, `debian:12`, `debian:13`, amd64), `apt-get update`, then
`apt-cache policy` for each name; for the names that looked missing, `apt-get install
--simulate`. Nothing from the role ran. Repeat it with
`python scripts/probe_platform_packages.py probe ubuntu:24.04 debian:13 --output docs/evidence/platform-packages-<date>.json`.
This is package availability only. It is not a converge, and it does not show that a
service, a driver or a display works.

## Result by platform

| Platform | In the archive | Not in the archive |
| --- | --- | --- |
| Ubuntu 22.04 (jammy) | 133 | vendor packages (below), `eza`, `tealdeer`, `tokei` |
| Ubuntu 24.04 (noble) | 134 | vendor packages (below), `libfuse2` (installable, see below), `tokei` |
| Debian 12 (bookworm) | 133 | vendor packages (below), `firefox`, `eza`, `tokei` |
| Debian 13 (trixie) | 132 | vendor packages (below), `firefox`, `dnsutils` (installable), `libfuse2` (installable), `software-properties-common` |

## Vendor packages are not in any distribution archive

`1password-cli`, `brave-browser`, `code`, `google-chrome-stable`, `google-cloud-cli`,
`microsoft-edge-stable`, `mongodb-mongosh` and `openziti` are missing everywhere, as
expected: the role adds the vendor's apt repository first (`tasks/repos.yml`, each with a
`signed-by` keyring), and the main task runs after that. `firefox` comes from the Mozilla
repository the role adds too, so its absence on Debian (which ships `firefox-esr`) does
not matter once repositories are converged. These names are the optional vendors:
a profile or a platform that drops a vendor repository must drop the package with it.

## Differences that matter

| Package | Ubuntu 22.04 | Ubuntu 24.04 | Debian 12 | Debian 13 | What it means |
| --- | --- | --- | --- | --- | --- |
| `tokei` | absent | absent | absent | present | No other source for it is in the role, so the one-call install would fail on the two supported Ubuntu releases and on Debian 12. Needs a decision: make it optional, or install it from its upstream release like other tools. Not changed here. |
| `eza` | absent | present | absent | present | Absent on 22.04 and Debian 12; same failure mode as `tokei`. |
| `tealdeer` | absent | present | present | present | Absent on 22.04 only. |
| `libfuse2` | present | provided by `libfuse2t64` | present | provided by `libfuse2t64` | `apt install libfuse2` resolves to `libfuse2t64` on 24.04 and 13, so this works; the name is a virtual package there. |
| `dnsutils` | present | present | present | not a package of its own | Installable on 13 in the simulation (apt resolves the name to another package). |
| `software-properties-common` | present | present | present | absent | Not installable on Debian 13. |
| `firefox` | installable | installable | not in the archive | not in the archive | Debian ships `firefox-esr`; the role's Mozilla repository supplies `firefox`. |

Service and unit names were not measured; that needs a converge on each platform.

## What this changes in the compatibility claims

[`compatibility.md`](compatibility.md) lists Ubuntu 22.04 and 24.04 as supported
targets. This measurement shows that the main package task names `tokei` (and `eza` on
22.04), which those archives lack. Until a disposable-machine converge on those releases
shows otherwise, treat the package step on 22.04 and 24.04 as unverified, and do not
promote Debian 12 or 13 from experimental. Fix by making those packages optional or sourcing
them from verified upstream releases, then repeat the probe and a converge.
