# Proxies, offline caches and restricted networks

For researchers behind a university proxy, on an intermittent link or on an
offline lab network. This page describes what the role does today, including
what it does not do, so you can plan around it.

## What the role does when the network fails

- Upstream release discovery (`latest-releases.yml`) asks GitHub for the latest
  release of every binary source, retries each request, and then **stops the
  run** if any source cannot be resolved: "The lab refuses to fall back to a
  stale version or silently keep an old binary".
- It also stops when GitHub returns no SHA-256 digests: "The workstation
  refuses to install release artifacts without a verifiable digest".
- The failure is recorded in `last-run.json` (`status: failed`, `failure`), and
  the pull fails with "Upstream release resolution failed; no stale fallback
  was installed". Nothing is installed from a cache in that case.
- `tests/test_restricted_network.py` reproduces this with a local stand-in proxy
  that refuses the connection, so no real network is needed.

This is the safe default, and the reason a lab with no route to GitHub cannot
run the transaction-bearing tags at all. See [what still needs the
network](#what-still-needs-the-network).

## Proxy settings

The role has no proxy variables. Tools read their own settings:

| Component | Where to set the proxy |
| --- | --- |
| Ansible `uri` and `get_url` (release discovery, downloads) | `http_proxy`, `https_proxy`, `no_proxy` environment variables of the module |
| APT | `Acquire::http::Proxy` and `Acquire::https::Proxy` in a file under `/etc/apt/apt.conf.d/`, or the environment variables |
| `pip`, `pipx`, `uv` | `HTTPS_PROXY` or the tool's own config |
| `git` | `git config --system http.proxy` |
| `npm` | `npm config set proxy` / `https-proxy`, or the environment variables |
| Snap | `sudo snap set system proxy.http=... proxy.https=...` |

A proxy exported in your shell does **not** reach tasks that run through
`sudo`: `sudo` resets the environment by default (checked: a task run with
`become` saw no proxy variables), and some current Ubuntu releases ship
`sudo-rs`, which ignores `-E`. Put the variables in the play instead, where
Ansible exports them to every task, including tasks that use `become`.
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
and provide the three `lab_*` variables in `ansible_pull_extra_vars`, which is
a string of `ansible-pull` arguments:

```yaml
ansible_pull_extra_vars: "-e lab_http_proxy=http://proxy.example.edu:3128 -e lab_https_proxy=http://proxy.example.edu:3128 -e lab_no_proxy=localhost,127.0.0.1"
```

A proxy that inspects TLS needs its CA certificate in the system trust store.
The role installs `custom_ca_certificates` for the `thin-client` and
`byod-kiosk` profiles only; on `desktop` and `workspace` machines install the
CA with your normal lab process (`update-ca-certificates`).

## Cache boundaries

| Cache | Location | Behavior |
| --- | --- | --- |
| Downloaded artifacts | `/var/cache/curiosity/artifacts` (`workstation_update_artifact_cache_dir`) | Files older than `workstation_update_cache_retention_days` (30) days are deleted at the start of a run. Installers such as Go, Deno, Bun and Trivy use `get_url` with a SHA-256 and `force: false`, so they download only when the tool is out of date **and** the file is missing from the cache or fails its checksum |
| APT | `/var/cache/apt` | Standard APT behavior; `apt_upgrade_enabled` controls whether the role upgrades packages at all |
| Release metadata | `/var/lib/curiosity/workstation-updates/observations/<run>-upstream.json` | A **record** of what was resolved for audit, not a source the role installs from |
| Update snapshots | `/var/lib/curiosity/workstation-updates/snapshots/` | For rollback only |

A cache never substitutes for failed release discovery.

## Offline dependency preparation (controller side)

You can prepare everything the *controller* needs on a connected machine and
carry it to a restricted one. This workflow was run end to end for the
collection step.

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

Run the collection install from inside `offline-collections`: the generated
`requirements.yml` names the tarballs relative to that directory. Download the
wheels with the same Python version and CPU architecture as the restricted
machine, and compare the tarball's `sha256sum` with the digest you recorded on
the connected machine before unpacking.

## What still needs the network

Be explicit about this when you plan an offline lab:

| Activity | Network need |
| --- | --- |
| Tags in the update transaction (`updates`, `packages`, `python`, `science`, `data-science`, `cli`, `cloud`, `languages`, `kubernetes`, `devops`, `releases`, ...) | GitHub release API, release assets, PyPI, APT, depending on the group |
| `repos` | Vendor APT signing keys and repositories |
| Tags outside that list, for example `git`, `user`, `timezone`, `firewall`, `keyboard`, `tmp-hygiene`, `profile-cleanup` | They do not run release discovery, but some still install a package with APT (the firewall installs UFW, for example). Check with `--list-tasks` and run on a disposable machine first |

For a lab with a local APT mirror or a Python index mirror, point APT and `pip`
at it with the settings in the proxy table; the role does not need to know.
A fully offline mode for release discovery is not provided yet, and
[`docs/roadmap.md`](../roadmap.md) tracks reliability work.

## Low bandwidth

- The role asks GitHub about every binary release source in one pass; set
  `GITHUB_TOKEN` in the environment to avoid unauthenticated rate limits.
- `workstation_update_ring: general` spreads the nightly pull over a
  randomized delay of up to 24 hours (`workstation_update_ring_randomized_delay_seconds`),
  so a shared link is not hit by every machine at once.
- Artifacts already in the checksum-verified cache are not downloaded
  again (see the cache table). Resuming a partial download is not implemented:
  an interrupted download starts again on the next run.
- The managed binaries are snapshotted before an update and restored when the
  post-update health check fails ([architecture notes](../architecture.md)).
  An interrupted run is not a tested recovery path yet: after one, re-run the
  same tags and read `last-run.json`.
