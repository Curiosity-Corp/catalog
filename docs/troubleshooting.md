# Troubleshooting

The failures a new user meets first, with the exact message the role prints.
`<name>` marks a part of the message that changes from run to run. The
messages are checked against the role in CI (`tests/test_troubleshooting.py`),
so a message quoted here is still the one you will see.

To read a failed run: `journalctl -u ansible-pull.service` for a pulled
machine, or the `ansible-playbook` output for a run by hand. The run's record is
`/var/lib/curiosity/workstation-updates/last-run.json` (the manifest).

## No current upstream release

```text
Could not resolve a current upstream release for <name>. The lab refuses to fall back to a stale version or silently keep an old binary.
```

**Likely cause.** The GitHub releases API did not answer for that tool: the
unauthenticated rate limit (60 requests an hour per address, shared by every
machine behind one NAT), no route to `api.github.com` (a proxy or firewall), or
an upstream release published without assets.

**Check.** `curl -sI https://api.github.com/rate_limit` from the machine shows
`x-ratelimit-remaining`; a proxy error or timeout points at the network.

**Next.** Give the role a token so it gets the authenticated limit: set
`github_api_token` from a protected variable, or export `GITHUB_TOKEN` /
`GH_TOKEN`, or sign in with `gh auth login` as root. Then run again. The role
does not install an older version instead, on purpose.

## No SHA-256 digest

```text
GitHub returned no SHA-256 asset digests. The workstation refuses to install release artifacts without a verifiable digest.
```

**Likely cause.** The releases API answered, but without the `digest` field on
the assets: a proxy or mirror that rewrites the API response, or a GitHub
Enterprise server too old to publish asset digests.

**Check.** `curl -s https://api.github.com/repos/jesseduffield/lazygit/releases/latest`
should show `"digest": "sha256:...` on each asset. Compare the same request
through your proxy.

**Next.** Let the request reach `api.github.com` unchanged. The role never
downloads a release artifact it cannot verify, so there is no switch to turn
this off.

## Python package list without ipykernel

```text
science_python_packages must be a non-empty list of pip requirements that includes ipykernel (the "Python 3 (Science)" kernel and the jupyter and ipython links depend on it); science_python_upgrade_packages must be a list, science_python_pip_extra_args a string and science_python_venv an absolute path. Requirement specifiers such as 'numpy>=2' are allowed.
```

**Likely cause.** A host or group variable replaced `science_python_packages`
with a list that leaves out `ipykernel`, or set one of the variables to the
wrong type (a single string instead of a list, a relative venv path).

**Check.** `ansible-inventory --host <host> --yaml` shows the effective values.

**Next.** Add `ipykernel` back to the list; start from
[`examples/python-research.yml`](../examples/python-research.yml). The check runs
before any venv is touched, so nothing on the machine changed. See the
[Python research profile](python-research-profile.md).

## Invalid time zone

```text
workstation_timezone=<value> is not an IANA zone in /usr/share/zoneinfo (for example America/New_York).
```

**Likely cause.** `workstation_timezone` holds something that is not a zone
name: a locale such as `en_US.UTF-8`, an abbreviation such as `EST`, a typo
(`America/New-York`), or a region directory such as `America` on its own.

**Check.** `timedatectl list-timezones | grep -i <city>` on the machine lists
the names it accepts.

**Next.** Set the exact name, for example `workstation_timezone: Europe/Berlin`,
or leave it empty (`""`, the default) to keep the machine's own zone. The check
runs before the zone is changed, so nothing on the machine changed.

The keyboard layout has no variable yet: `thin-client` and `byod-kiosk`
machines are always set to the US layout (`tasks/keyboard.yml`), so there is no
keyboard value to get wrong. If `@` and `"` are swapped on such a machine, the
keyboard task has not run: run the pull again and look for
`Write /etc/default/keyboard (XKBLAYOUT=us)` in its output.

## Update rolled back after a failed health check

```text
Workstation update transaction failed and was rolled back: <reason>
```

**Likely cause.** A newly installed tool failed its
health check (for example `lazygit --version`
exited non-zero), or a step of the update failed. The role put the previous
files back from the run's snapshot and the machine
works as before.

**Check.** The reason after the colon names the failing step. The same run's
`/var/lib/curiosity/workstation-updates/quarantine.json` records the run and the
failure, and `last-run.json` records the status.

**Next.** Nothing, if the upstream release was simply broken: the role installs
nothing new for `workstation_update_quarantine_seconds` (one hour by default,
the quarantine) and then tries the newest release
again. If it fails the same way, open an issue with the reason and the
`failure` block of `quarantine.json`.

## An update is held for observation

This one is not a failure, but it reads like one:

```text
Workstation update ring '<ring>' is holding the newest upstream release(s) for observation: <names>
```

**Cause.** The machine is on the `early` or `general`
update ring, which waits until a release is six
hours or a day old before installing it.

**Next.** Nothing; a later run installs it. Move a test machine to
`workstation_update_ring: canary` to take releases at once.
