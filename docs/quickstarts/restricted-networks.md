# Proxies, offline caches and restricted networks

For researchers behind a university proxy, on an intermittent link or on an
offline lab network. This page describes what the role does today, including
what it does not do, so you can plan around it.

## What the role does when the network fails

The role fails closed. When it cannot learn what the current release is, or
cannot check a file against the vendor's SHA-256, it stops the run. It never
installs an older release in place of the current one, and it never uses a
file it could not verify.

| Failure | What happens |
| --- | --- |
| The GitHub release API cannot be reached (no route, a refusing proxy, a rate limit) | `latest-releases.yml` retries each request, then **stops the run**: "Could not resolve a current upstream release for `<name>` ... The lab refuses to fall back to a stale version or silently keep an old binary" |
| A release's assets carry no SHA-256 digest (`digest: null` on assets GitHub never hashed) | Those assets are dropped during the lookup, so no installer can select one. A source left with none stops the run with the same message |
| A vendor checksum file (for example the `.sha256` next to a kubectl binary) cannot be read, or holds no SHA-256 | `verified-download.yml` stops before it downloads anything: "refusing to install an unverified download". A file already in the cache is not used instead, because the digest comes from the vendor and never from the cache |
| The download fails, or its SHA-256 does not match | The download is retried, then the run stops. A file that fails its checksum is deleted, not installed |
| A cached file no longer matches the digest in its name | It is downloaded again. With no network that download fails, and the run stops |

When the run stops, `last-run.json` records `status: failed` and the reason, and
the pull fails with "Upstream release resolution failed; no stale fallback was
installed".

`tests/test_restricted_network.py` reproduces each case on loopback, with no real
network: a stand-in proxy that refuses every connection, release metadata with
unhashed assets, a checksum server on a port nothing listens on, and a cache
filled ahead of time (see [preparing the verified-download cache
offline](#preparing-the-verified-download-cache-offline)).

## Proxy settings

The role has no proxy variables. Each tool reads its own settings:

| Component | Where to set the proxy |
| --- | --- |
| Ansible `uri` and `get_url` (release lookups, the verified-download cache) | `http_proxy`, `https_proxy`, `no_proxy` in the environment the module runs in (see below) |
| APT | `Acquire::http::Proxy` and `Acquire::https::Proxy` in a file under `/etc/apt/apt.conf.d/`, or the environment variables |
| `pip`, `pipx`, `uv` | `HTTPS_PROXY` or the tool's own config |
| `git` | `git config --system http.proxy` |
| `npm` | `npm config set proxy` / `https-proxy`, or the environment variables |
| Snap | `sudo snap set system proxy.http=... proxy.https=...` |

A proxy exported in your shell does **not** reach tasks that run through
`sudo`. `sudo` resets the environment by default (we checked: a task run with
`become` saw no proxy variables), and some current Ubuntu releases ship
`sudo-rs`, which ignores `-E`. Put the variables in the play instead. Ansible
exports a play-level `environment` to every task, including tasks that use
`become` and the per-user downloads that switch to your account with
`become_user`.
[`examples/restricted-network-site.yml`](../../examples/restricted-network-site.yml)
is a drop-in replacement for `playbooks/site.yml`:

```bash
ansible-playbook -i localhost, -c local examples/restricted-network-site.yml \
  -e lab_http_proxy=http://proxy.example.edu:3128 \
  -e lab_https_proxy=http://proxy.example.edu:3128 \
  -e lab_no_proxy=localhost,127.0.0.1 \
  -e @$HOME/laptop-vars.yml --tags python --become
```

For the nightly timer, set `ansible_pull_playbook: examples/restricted-network-site.yml`
and put the three `lab_*` variables in `ansible_pull_extra_vars`, which is a
string of `ansible-pull` arguments:

```yaml
ansible_pull_extra_vars: "-e lab_http_proxy=http://proxy.example.edu:3128 -e lab_https_proxy=http://proxy.example.edu:3128 -e lab_no_proxy=localhost,127.0.0.1"
```

A proxy that inspects TLS needs its CA certificate in the system trust store.
The role installs `custom_ca_certificates` only for the `thin-client` and
`byod-kiosk` profiles. On `desktop` and `workspace` machines, install the CA
with your lab's usual process (`update-ca-certificates`).

## Where downloads go

Software reaches the machine in two ways, and each has its own cache.

**Through a package manager.** APT packages (and the vendor APT repositories the
`repos` tag adds), Snap, Flatpak, and Python and Node packages installed with
`pip`, `pipx`, `uv` or `npm`. These tools verify their own signatures and use
their own caches and mirrors. Point them at a local mirror with the settings in
the proxy table; the role does not need to know.

**Through the role's verified-download cache.** Release binaries (Go, kubectl,
Helm, the Kubernetes and DevOps CLIs, Deno, Bun, Trivy and others) and the
Claude Code and Codex installers. Each is downloaded with the vendor's SHA-256
and stored under a name that contains that SHA-256.

| Cache | Location | What is in it |
| --- | --- | --- |
| Verified-download cache (system) | `/var/cache/curiosity/artifacts` (`workstation_update_artifact_cache_dir`) | Release binaries, named `<tool>-<sha256>` with a suffix such as `.archive`, `.binary` or `.deb`. Files from `verified-download.yml` are named exactly `<name>-<sha256>` |
| Claude Code release manifests | `/var/cache/curiosity/artifacts/claude-code` (`claude_native_release_cache_dir`) | The signed manifest that names the binary's SHA-256 |
| Verified-download cache (per user) | `~/.claude/downloads` (Claude Code binary), `~/.cache/curiosity-corp/downloads` (Codex installer) | Same `<name>-<sha256>` naming, owned by the user |
| APT | `/var/cache/apt` | Standard APT behavior; `apt_upgrade_enabled` controls whether the role upgrades packages at all |
| Release metadata | `/var/lib/curiosity/workstation-updates/observations/<run>-upstream.json` | A **record** of what the lookup resolved, kept for audit. The role never installs from it |
| Update snapshots | `/var/lib/curiosity/workstation-updates/snapshots/` | For rollback only |

At the start of a run, files in the system cache older than
`workstation_update_cache_retention_days` (30) days, by modification time, are
deleted.

A cached file is reused only when its content hashes to the SHA-256 that this
run expects for it. The expected value always comes from upstream (the release
API, the vendor's checksum file or signed manifest) or from a digest pinned in
the role, never from the file name. A cache therefore saves bandwidth, but it
never replaces a failed lookup.

A few installers (rclone, Terraform, Vault, Packer, the GitLab CLI) name their
cached file by version and read the vendor's checksum file on every run. They
need that small request even when the file is already cached.

## Preparing the verified-download cache offline

The cache is keyed by digest, so you can fill it on a connected machine and
carry it to a restricted one. A later run checks every file against the digest
it expects and reuses it without fetching anything.

**What still needs a link.** For release-tracking tools, the expected digest
comes from that run's release lookup. On an intermittent or slow link, the run
then needs only those small metadata requests, because the large downloads
come from the cache. On a machine with no route at all, the lookup fails and
the run stops, as described [above](#what-the-role-does-when-the-network-fails).
The role has no release pins, so there is no offline substitute for that
lookup. Downloads whose digest is pinned in the role need no lookup.

### Carry a filled cache

On a connected machine, run the same profile and tags that the restricted
machine will run. Then pack the cache:

```bash
sudo tar -C /var/cache/curiosity -cf artifacts.tar artifacts
sha256sum artifacts.tar    # write this down and carry it separately
```

On the restricted machine:

```bash
sha256sum artifacts.tar    # must match the value you wrote down
sudo tar -C /var/cache/curiosity --no-same-owner --touch -xf artifacts.tar
sudo chown -R root:root /var/cache/curiosity/artifacts
sudo chmod 0750 /var/cache/curiosity/artifacts
```

`--touch` gives the files the time of unpacking. Without it, a file older than
the retention period keeps its old time and is deleted at the start of the next
run.

### Add one file by its digest

When you have a vendor artifact and the SHA-256 the vendor publishes for it,
check the file, then store it under the name the role expects
(`<name>-<sha256>`, see the cache table):

```bash
expected=<the SHA-256 the vendor publishes>
echo "$expected  kubectl" | sha256sum --check -
sudo install -D -o root -g root -m 0644 kubectl \
  "/var/cache/curiosity/artifacts/kubectl-v1.31.0-$expected"
```

A file under the wrong name is ignored and downloaded again. A wrong file under
the right name fails its checksum and is never used. Either way, a mistake
costs a download, not a broken install.

## Offline dependency preparation (controller side)

You can also prepare everything the *controller* needs on a connected machine
and carry it to a restricted one. The collection step of this workflow has been
run end to end.

On a machine with internet access:

```bash
git clone --branch v0.1.0 https://github.com/Curiosity-Corp/catalog.git
cd catalog
ansible-galaxy collection download -r requirements.yml -p offline-collections
python3 -m pip download -d offline-wheels ansible-core
cd .. && tar czf catalog-offline.tar.gz catalog && sha256sum catalog-offline.tar.gz
```

On the restricted machine, after unpacking:

```bash
python3 -m venv .venv && . .venv/bin/activate
python -m pip install --no-index --find-links offline-wheels ansible-core
(cd offline-collections && ansible-galaxy collection install -r requirements.yml --offline)
```

Run the collection install from inside `offline-collections`, because the
generated `requirements.yml` names the tarballs relative to that directory.
Download the wheels with the same Python version and CPU architecture as the
restricted machine. Before unpacking, compare the tarball's `sha256sum` with
the digest you recorded on the connected machine.

## What still needs the network

Plan for this when you set up an offline lab:

| Activity | Network need |
| --- | --- |
| Tags in the update transaction (`updates`, `packages`, `python`, `science`, `data-science`, `cli`, `cloud`, `languages`, `kubernetes`, `devops`, `releases`, ...) | The GitHub release API and the vendors' checksum files on every run. Release assets only when they are not already in the verified-download cache. PyPI and APT, depending on the group |
| `repos` | Vendor APT signing keys and repositories |
| Tags outside that list, for example `git`, `user`, `timezone`, `firewall`, `keyboard`, `tmp-hygiene`, `profile-cleanup` | They do not look up releases, but some still install a package with APT (the firewall installs UFW, for example). Check with `--list-tasks`, and run on a disposable machine first |

A fully offline mode for release lookups is not provided.
[`docs/roadmap.md`](../roadmap.md) tracks reliability work.

## Low bandwidth

- The role asks GitHub about every binary release source in one pass. Set
  `GITHUB_TOKEN` in the environment to avoid unauthenticated rate limits, and
  see [low-bandwidth and offline-first maintenance](../low-bandwidth-and-offline.md)
  to skip sources for integrations you have switched off.
- `workstation_update_ring: general` spreads the nightly pull over a
  randomized delay of up to 24 hours (`workstation_update_ring_randomized_delay_seconds`),
  so a shared link is not hit by every machine at once.
- Artifacts already in the verified-download cache are not downloaded again
  (see [where downloads go](#where-downloads-go)). Resuming a partial download
  is not implemented: an interrupted download starts again on the next run.
- The managed binaries are snapshotted before an update and restored when the
  post-update health check fails ([architecture notes](../architecture.md)).
  An interrupted run is not a tested recovery path yet. After one, re-run the
  same tags and read `last-run.json`.
