# 0004: Resumable downloads

- Status: proposed
- Date: 2026-11-22
- Deciders: catalog maintainers
- Affects: `ansible.builtin.get_url` tasks that fill
  `workstation_update_artifact_cache_dir`; no variable, default or schema
  changes yet
- Issue: catalog#133

## Context

The role fetches release artifacts with `ansible.builtin.get_url`. get_url
writes to a temporary file and moves it into place only when the download
completes, so an interrupted download leaves nothing behind and the next run
starts again from zero. On a slow or flaky link a large artifact can fail the
same way every night.

Two facts limit how much that costs today:

- Almost every artifact is cached under a name that contains its SHA-256 digest
  (for example `tofu-<digest>.archive`) with `force: false`. A completed
  download is never repeated; only a new upstream release is fetched.
- Every GitHub artifact is verified against the digest GitHub publishes, and
  the HashiCorp ones against their `SHA256SUMS`. Whatever resumes must keep that.

## What the role downloads, and how big it is

Sizes are the Linux x86_64 assets of the latest releases on 2026-11-22, read
from the public release APIs. Only the large ones are listed; the other GitHub
tools (lazygit, delta, zoxide, just, gum, yq, sops, age and the like) are each
well under 20 MB.

| Artifact (task file) | Asset the role takes | Size |
| --- | --- | --- |
| Argo CD CLI (`argocd`, github-binary) | `argocd-linux-amd64` | 267 MB |
| Bitwarden desktop (`bitwarden-desktop.yml`, opt-in) | `Bitwarden-<version>-amd64.deb` | 130 MB |
| Mattermost desktop (`collaboration.yml`) | `mattermost-desktop-<version>-linux-x64.tar.gz` | 126 MB |
| talosctl (`talos`, github-binary) | `talosctl-linux-amd64` | 119 MB |
| AWS CLI v2 (`cloud-clis.yml`) | `awscli-exe-linux-x86_64.zip` | not published by AWS; measure with `curl -sI` |
| Vault, Terraform, Packer (`devops.yml`) | `<tool>_<version>_linux_amd64.zip` | not published by the HashiCorp release API |
| Trivy (`security.yml`) | `trivy_<version>_Linux-64bit.tar.gz` | about 50 MB (the matching .deb is 52.7 MB) |
| Bun | `bun-linux-x64.zip` | 51 MB |
| Deno | `deno-x86_64-unknown-linux-gnu.zip` | 42 MB |
| k9s | `k9s_Linux_amd64.tar.gz` | 41 MB |
| OpenTofu | `tofu_<version>_linux_amd64.zip` | about 35 MB (the matching .deb is 35.1 MB) |
| Flux | `flux_<version>_linux_amd64.tar.gz` | 26 MB |
| golangci-lint | `golangci-lint-<version>-linux-amd64.tar.gz` | 16 MB |
| Sunshine (opt-in) | `sunshine_<version>_ubuntu<release>_amd64.deb` | 11-14 MB |
| JetBrains Mono Nerd Font | `JetBrainsMono.tar.xz` | a few MB (every .tar.xz in that release is 2-5 MB) |

Four artifacts are above 100 MB. At 1 Mbit/s, which a field station or a
congested hotel link can fall to, 100 MB takes about 14 minutes and Argo CD
alone about 36.

**A bigger cost found on the way.** The AWS CLI installer is fetched from AWS's
moving "latest" URL with `force: true`, so get_url downloads the whole archive
on every run, whether or not it changed (`force` sends `Cache-Control:
no-cache` and no `If-Modified-Since`). That is one full download per machine
per night, more traffic than every interrupted download put together.

## Ways to resume, compared

| | Resumes | Checksum kept | No new default dependency | Works behind a proxy (`https_proxy`, `no_proxy`) |
| --- | --- | --- | --- | --- |
| `curl -C -` into `<dest>.part`, then verify and rename | Yes | Yes: verify the finished `.part` with the same digest before the rename, delete it on mismatch | Yes: every profile's package list already installs `curl` | Yes: `curl` reads both |
| `aria2c --continue` | Yes | Yes (`--checksum=sha-256=...`) | No: `aria2` becomes a new package on every machine | Mostly: reads `https_proxy`, but authenticated proxies need its own options |
| get_url with `retries` / `until` | No: each retry starts from zero | Yes | Yes | Yes |

## Decision

Recommend, in this order:

1. **Fix the AWS CLI re-download first.** Keep the archive under its own
   name and let get_url send `If-Modified-Since` (`force: false` plus a
   periodic refresh, or comparing the `ETag`), so an unchanged installer is
   not fetched again. This saves more than resuming would.
2. **Resume only artifacts above 100 MB, with `curl -C -`.** A small task
   include that downloads into `<dest>.part` with `curl --fail --location
   --continue-at -`, verifies the finished file against the same digest the
   get_url task uses, and renames it into the digest-addressed name. A
   mismatch deletes the `.part` and fails the run, so a corrupted partial
   never survives. It applies to Argo CD, Bitwarden, Mattermost and talosctl
   today; everything smaller keeps plain get_url.
3. **Do not adopt aria2c**: the only gain over `curl -C -` is parallel
   segments, and it costs a new package on every machine.

Until someone on a slow link reports the same artifact failing on consecutive
nights, item 2 stays proposed; item 1 is worth doing now.

## Safety impact

Item 1 changes only when the AWS CLI archive is fetched; the signature check
stays. Item 2 adds a `.part` file in the artifact cache between runs; the
digest check is the same as today, so a resumed file is trusted exactly as
much as a fresh one. Neither adds a variable or a package.

## Alternatives considered

Doing nothing: acceptable for everything under 100 MB, because a completed
download is cached by digest and never repeated. A local HTTP cache or mirror
(squid, a lab apt-cacher-style proxy) would help a whole lab more than resuming
on each machine, and fits the restricted-network guidance rather than this
role.

## Consequences

Item 1 is a small change to `tasks/cloud-clis.yml` with a test that the
archive task does not use `force: true`. Item 2, if accepted, adds
`tasks/resumable-download.yml` and a test in the style of
`tests/test_restricted_network.py`: serve a file from a local
`python3 -m http.server`, truncate the `.part`, and check the second run
finishes it and the digest check still rejects a corrupted one. Revisit the
100 MB threshold if the table above changes.
