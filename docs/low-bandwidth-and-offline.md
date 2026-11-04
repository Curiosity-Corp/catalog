# Low-bandwidth and offline-first maintenance

For researchers on a slow, metered or intermittent link. This page says what to
turn on, what to prepare on a connected machine, and what is not done yet. The
longer description of proxies, caches and what still needs the network is in
[restricted networks](quickstarts/restricted-networks.md).

## Skip release lookups for integrations you have switched off

By default the role asks GitHub about the latest release of every binary source
on every run, even for an application you disabled. On a slow link, opt in to
skipping the ones that are off:

```yaml
latest_release_skip_disabled_sources: true    # default: false
```

A source is skipped only when its application contract names a feature flag
(`enabled_when`, for example `sunshine_enabled`, `coder_cli_enabled` or
`cloud_clis_enabled`) and that flag is false on this host. Sources with no flag
are always resolved. This is the same rule the post-update health check and the
[maintenance summary](maintenance-summary.md) use, which lists what was skipped
under "Optional integrations skipped".

`tests/test_python_research.py` runs the selection task with a source whose flag
is off and one that is on: with the variable `false` both are resolved, with it
`true` only the enabled one is.

## Prepare on a connected machine

| What | How | Checked |
| --- | --- | --- |
| The catalog and the controller's own dependencies | Clone at a tag, `ansible-galaxy collection download`, `pip download ansible-core`, then carry the folder | Run end to end, see [restricted networks](quickstarts/restricted-networks.md#offline-dependency-preparation-controller-side) |
| Scientific Python packages | On the connected machine: `pip download -d wheels <packages>`. On the workstation: set `science_python_pip_extra_args: "--no-index --find-links /srv/wheels"` | [Recorded once](evidence/python-research-sample-install.md): the install used only the folder, and a package missing from it failed instead of reaching the network |
| Operating system packages | A local APT mirror or cache, configured through APT's own settings | Not exercised here |
| Release binaries (Go, Deno, Trivy and others) | Not prepared offline: release discovery needs GitHub, and a failed discovery stops the run rather than using a stale copy | See [what still needs the network](quickstarts/restricted-networks.md#what-still-needs-the-network) |

Download the wheels with the same Python version and CPU architecture as the
workstation. With `--no-index`, the list in `science_python_packages` must be
fully covered by the folder, dependencies included (`pip download` fetches
those).

## Already cache-aware

Installers that use `get_url` with a SHA-256 download only when the installed
tool is out of date and the file is missing from `/var/cache/curiosity/artifacts`
or fails its checksum. The nightly pull is spread over a randomized delay on the
`general` ring, so a shared link is not hit by every machine at once. Set
`GITHUB_TOKEN` to avoid unauthenticated API rate limits.

## Not done yet

These parts of the original request are not implemented, and the page does not
pretend otherwise:

- **Resumable downloads.** An interrupted download starts again on the next run;
  `get_url` has no resume. A fix means choosing a download tool for the large
  artifacts and testing it against a real interruption.
- **A tested interrupted-update recovery.** The health check rolls back a
  failed update, but killing a run half way and re-running has not been
  tested. After an interrupted run, re-run the same tags and read the
  [maintenance summary](maintenance-summary.md).
- **A fully offline mode for release discovery.** Tracked in
  [the roadmap](roadmap.md).
