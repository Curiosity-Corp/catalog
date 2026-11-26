# Platform package differences

Which of the `developer_workstation` role's apt packages each platform's own
archives provide (catalog#14, #15). The evidence is generated, not written by
hand:

- `docs/evidence/platform-packages-2026-11-23.json`: probed 2026-11-23, 146
  packages from the "Install all APT packages" task in
  `roles/developer_workstation/tasks/packages.yml`, on 5 platforms.

## How it is made

The `platform-packages` CI job runs `catalog-tools packages probe` inside the
stock image of each platform (`ubuntu:22.04`, `ubuntu:24.04`, `ubuntu:26.04`,
`debian:12`, `debian:13`). The probe runs `apt-get update` with apt's lists and
caches in a private directory, so it needs no root. It then asks
`apt-cache policy` for every package. A name without a candidate gets a
simulated install (`apt-get --simulate install`). When that works, the name is
virtual, and the probe records the package that provides it (from
`apt-cache showpkg`). Otherwise the name is missing. `platform-evidence`
assembles the results into one dated file, its artifact
`evidence/platform-packages-<date>.json`.

Only the distributions' own archives are probed, not the vendor repositories
the role adds (`repos.yml`, `repos-workspace.yml`). That is why these are
missing on every platform: `1password-cli`, `brave-browser`, `code`, `google-chrome-stable`, `google-cloud-cli`, `microsoft-edge-stable`, `mongodb-mongosh`, `openziti`.

## Results (2026-11-23)

Vendor packages are left out of the last column.

| Platform | Candidates | Virtual name → provider | Missing from the archives |
| --- | --- | --- | --- |
| debian:12 (bookworm, amd64) | 135 | none | `eza`, `firefox`, `tokei` |
| debian:13 (trixie, amd64) | 134 | `dnsutils` → `bind9-dnsutils`, `libfuse2` → `libfuse2t64` | `firefox`, `software-properties-common` |
| ubuntu:22.04 (jammy, amd64) | 135 | none | `eza`, `tealdeer`, `tokei` |
| ubuntu:24.04 (noble, amd64) | 136 | `libfuse2` → `libfuse2t64` | `tokei` |
| ubuntu:26.04 (resolute, amd64) | 135 | `dnsutils` → `bind9-dnsutils`, `libfuse2` → `libfuse2t64`, `p7zip-full` → `7zip` | none |

## What it means

The role installs this list in one `apt` task, so a single missing name fails
the whole task. On 2026-11-23 only Ubuntu 26.04 provided every non-vendor
package. Each other platform needs the role to handle its gaps first:

- `tokei` is not in Ubuntu 22.04 or 24.04, or in Debian 12.
- `eza` is not in Ubuntu 22.04 or Debian 12, and `tealdeer` is not in Ubuntu 22.04.
- `firefox` is `firefox-esr` on Debian.
- `software-properties-common` is gone from Debian 13. The role already handles
  it: the install task removes names listed in `apt_packages_not_in_distribution`
  for the host's distribution (catalog#162). The probe still asks about the
  whole list, so this evidence keeps showing why each exclusion exists.

The virtual names (`libfuse2`, `dnsutils`, `p7zip-full`) install through
their providers, so apt handles them without a change.

## Keeping it current

The `tools` CI job runs `catalog-tools packages check`. It fails when the
newest evidence file no longer matches the role's package list, for example
after a package is added. To refresh the evidence:

1. Take `evidence/platform-packages-<date>.json` from the `platform-evidence`
   job of the MR's pipeline.
2. Commit it to `docs/evidence/`.
3. Update the table above from it.

`make packages-check` runs the same check locally.
