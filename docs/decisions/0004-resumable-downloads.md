# 0004: Resume only the downloads above 100 MB, with `curl -C -`

- Status: proposed
- Date: 2026-11-25
- Deciders: catalog maintainers
- Affects: the `ansible.builtin.get_url` tasks that fill
  `workstation_update_artifact_cache_dir`; no variable, default, package or
  schema changes in this record
- Issue: catalog#133

## Context

The role downloads release artifacts with `get_url`. get_url writes to a
temporary file and moves it into place only once the download completes, so
an interrupted download leaves nothing behind and the next run starts again
from zero. On a slow link a large artifact can fail every night.

Three facts keep the cost down today:

- Almost every artifact is cached under a name that holds its digest or
  version (`tofu-<digest>.archive`, `vault-<version>.archive`) with
  `force: false`. A completed download is not repeated; only a new upstream
  release is fetched.
- Every GitHub artifact is checked against the digest GitHub publishes, and
  each HashiCorp artifact against its `SHA256SUMS`. Any resume method must
  keep that check.
- GitHub, HashiCorp and AWS answer a ranged request with `206 Partial Content`
  (checked 2026-11-25 with `curl -r 1000-1999`), so resuming works there.

### What the role downloads

These are the Linux x86_64 assets of the latest releases on 2026-11-25. Sizes
come from the GitHub release API and from `Content-Length` on HashiCorp and
AWS. The other GitHub tools (lazygit, delta, zoxide, just, gum, yq, sops, age
and the like) are each well under 20 MB.

| Artifact (task file) | Asset | Size | Resume? |
| --- | --- | --- | --- |
| Argo CD CLI 3.6.3 (github-binary) | `argocd-linux-amd64` | 267 MB | yes |
| Vault 2.2.2 (`devops.yml`) | `vault_<v>_linux_amd64.zip` | 186 MB | yes |
| Bitwarden desktop 2026.11.0 (`bitwarden-desktop.yml`, opt-in) | `Bitwarden-<v>-amd64.deb` | 130 MB | yes |
| Mattermost desktop 6.4.0 (`collaboration.yml`) | `mattermost-desktop-<v>-linux-x64.tar.gz` | 126 MB | yes |
| talosctl 1.14.7 (github-binary) | `talosctl-linux-amd64` | 119 MB | yes |
| AWS CLI v2 (`cloud-clis.yml`) | `awscli-exe-linux-x86_64.zip` | 76 MB | no, see below |
| Trivy 0.76.2 (`security.yml`) | `trivy_<v>_Linux-64bit.tar.gz` | 52 MB | no |
| Bun 1.4.5 | `bun-linux-x64.zip` | 51 MB | no |
| Deno 2.9.8, k9s 0.51.0 | zip / tar.gz | 41 MB each | no |
| Terraform 1.17.2, OpenTofu 1.13.10, Packer 1.16.3 | `<tool>_<v>_linux_amd64.zip` | 38, 35, 34 MB | no |
| Flux 2.10.1 | `flux_<v>_linux_amd64.tar.gz` | 26 MB | no |
| Sunshine (opt-in) | `sunshine_<v>-1+ubuntu<release>_amd64.deb` | 20 MB | no |
| golangci-lint 2.15.0 | `golangci-lint-<v>-linux-amd64.tar.gz` | 15 MB | no |
| JetBrains Mono Nerd Font 3.5.1 | `JetBrainsMono.tar.xz` | 7 MB | no |

Five artifacts are above 100 MB, together 828 MB. At 1 Mbit/s, a rate a field
station or a busy shared link can drop to, 100 MB takes about 14 minutes and
Argo CD alone about 36, which is long enough for a suspend, a Wi-Fi handover or
the update window to end it. Below 100 MB a retry from zero costs a few minutes
and is paid once per release.

**AWS CLI is a different problem.** It comes from AWS's moving "latest" URL
with `force: true`, so all 76 MB are downloaded on every run whether or not
the file changed. Resuming would not help, because the cost comes from
re-downloading a file that already finished, not from interruptions.

### Ways to resume, compared

| | Resumes | Checksum kept | No new default dependency | Works behind the proxy settings |
| --- | --- | --- | --- | --- |
| **`curl -C -`** into `<dest>.part`, verify, rename | yes | yes: the same SHA-256 runs on the finished `.part` before the rename; on a mismatch the `.part` is deleted and the run fails | yes: `packages.yml`, `packages-workspace.yml` and `packages-thin-client.yml` (all four profiles) already install `curl` | yes: curl reads the lowercase `http_proxy`, `https_proxy` and `no_proxy` that `examples/restricted-network-site.yml` exports to every task; HTTPS goes through `CONNECT`, so `Range` travels inside the tunnel; a TLS-inspecting proxy's CA comes from the system store as for get_url |
| **`aria2c --continue`** | yes, with parallel segments | yes (`--checksum=sha-256=...`) | **no**: no profile installs `aria2`, so it would become a new package on every machine | mostly: reads `http_proxy`/`https_proxy`, but `no_proxy` and authenticated proxies need its own `--all-proxy`/`--no-proxy` options |
| **get_url with `retries`/`until`** | **no**: every attempt starts from zero | yes | yes | yes |

`tests/test_resumable_download_standin.py` runs the `curl -C -` procedure
against local stand-in servers. Through a stand-in forwarding proxy set with
`http_proxy`, the proxy forwards `Range: bytes=300000-` and only the missing
700 kB are sent. A corrupted partial resumes but fails the digest check and
leaves neither `.part` nor the destination. An origin that ignores `Range`
(plain `python3 -m http.server`) makes curl exit 33, and the procedure starts
again from zero. A partial that is already complete is verified and renamed
without another request.

## Decision

Resume the artifacts above 100 MB with `curl -C -`, and leave everything else
on plain get_url. One task include downloads into `<dest>.part` with
`curl --fail --location --continue-at -`. On exit 33 it deletes the `.part`
and downloads again from zero. It then checks the finished file against the
digest the get_url task uses today, and renames it to the cached name. Today
that covers Argo CD, Vault, Bitwarden, Mattermost and talosctl. Do not adopt
aria2c, and do not count on get_url retries as resuming.

## Safety impact

The only new state is a `.part` file in the artifact cache between runs. It is
never installed: a file reaches its cached name only after the same digest
check as today, so a resumed file is trusted exactly as much as a fresh one,
and a corrupted partial is deleted. There is no new variable or package.
Artifacts under 100 MB do not change. To fall back, an artifact goes back to
plain get_url.

## Alternatives considered

- **Do nothing.** Fine below 100 MB, because a finished download is cached and
  not repeated. Not fine above it: on a slow link the same 119-267 MB can fail
  every night and never finish.
- **aria2c.** Its only advantage over curl is parallel segments, and it costs a
  new package on every machine and its own proxy options.
- **get_url retries.** They help against a single dropped connection, but
  every retry downloads the whole file again.
- **A lab HTTP cache or mirror** (squid and the like). It helps a whole lab
  more than resuming on each machine does, but it belongs in the
  restricted-network guidance, not in this role.

## Consequences

- Implementation is a separate change. It adds `tasks/resumable-download.yml`,
  uses it in the five tasks above, and turns the stand-in test into a test of
  that include. The threshold is a fixed 100 MB in the include, not a variable.
- The AWS CLI re-download is a separate issue: cache the installer under its
  version (`awscli-exe-linux-x86_64-<version>.zip`, like Vault) instead of
  using `force: true` on the moving URL. AWS publishes most weekdays (a new
  `Last-Modified` on 2026-11-25), so the saving is real but smaller than it
  looks.
- Look again when the size table changes. An artifact that crosses 100 MB
  moves to the resumable include.
