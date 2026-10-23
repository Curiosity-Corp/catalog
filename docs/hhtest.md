# hhtest: unprivileged test-bed account

Local test suites must not run as a real user on a production host. A
runaway or careless test can kill tmux servers and `systemd --user` units that
belong to people's live sessions. The `hhtest` account gives such runs their
own uid, so a collision is physically impossible:

- own `/tmp/tmux-<uid>` socket directory and own `systemd --user` manager
  (lingering is enabled for scope/unit tests);
- no sudo, no supplementary groups, locked password, no production
  credentials, no secret-store access;
- a capped `user-<uid>.slice`: `MemoryMax=16G`, `MemoryHigh=14G`,
  `CPUQuota=1200%`, `TasksMax=4096`;
- toolchain: tmux 3.7b built from the checksum-pinned upstream release into
  `/opt/hhtest` (the distribution tmux is never replaced), `uv` from PyPI in an
  isolated virtualenv, the system Node.js, and Playwright with its Chromium
  cache in `~/.cache/ms-playwright`;
- `~/work` as the sync target; a nightly timer that runs as the account and
  removes files under `~/work` and `/tmp` that the account owns and that are
  older than `hhtest_cleanup_age_days` (3).

## Opting a machine in

The task group is skipped unless the selected hardware profile has an
`hhtest_host` key, and then only converges when the live hostname equals it.
`deskmeet-b760` sets `hhtest_host: deskmeet1` and lists the operator public key
in `hhtest_authorized_keys` (public keys only). Nothing else is gated on or
shared with any other account, linger setting, user slice or unit.

| Variable | Default |
| --- | --- |
| `hhtest_uid` | `1500` |
| `hhtest_memory_max` / `hhtest_memory_high` | `16G` / `14G` |
| `hhtest_cpu_quota` | `1200%` |
| `hhtest_tasks_max` | `4096` |
| `hhtest_cleanup_age_days` | `3` |
| `hhtest_tmux_version` / `hhtest_tmux_sha256` | `3.7b` / pinned |

Run only this group with `--tags hhtest`.

## Reaching the account

Over the machine's private overlay only, using a listed key:
`ssh hhtest@<host>`.

## Verification

```bash
id hhtest                      # only the private group
sudo -l -U hhtest              # not allowed to run sudo
systemctl show user-1500.slice -p MemoryMax,MemoryHigh,CPUQuotaPerSecUSec,TasksMax
ssh hhtest@<host> 'tmux -L <any-name> ls'   # no sessions of other users
```
