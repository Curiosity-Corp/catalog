# Ziti `/etc/hosts` pins and the tunnel lifecycle

`ziti-edge-tunnel` hands out overlay addresses from a pool, and a restart can give a
hostname a different address. An `/etc/hosts` pin written before the restart then points
at nothing, or at another overlay service, and because `files` comes before `dns` in
`nsswitch.conf` nothing second-guesses it. The role therefore refreshes pins when the
tunnel starts, not only when Ansible happens to run (Curiosity-Corp/catalog#1).

## Private hostnames stay host-local

The public defaults name no host: `ziti_etc_hosts_pins` and `ziti_intercept_domains` are
empty, and so are `ziti_health_check_probe_hosts` and `ziti_health_check_identities`.
Set your pins only where the machine that needs them is configured:

- `host_vars/<host>.yml` or `group_vars/<group>.yml` of a **private** inventory,
- a host-local vars file the pull reads (`/etc/ansible/local-vars.yml`), or
- `-e @file` on the command line.

Never put a real hostname in `defaults/main.yml`, in the docs, in a test or fixture, or
in a commit message or MR. Examples use the reserved `example.test` names. The role copies
the list into `/etc/curiosity/ziti-pins.env` on the host itself (mode 0644, host-local, so
`curiosity-ziti-pins check` can be run by hand); that file is not part of this repository.

## What the role installs

With `ziti_enabled: true` and a non-empty `ziti_etc_hosts_pins`:

| Piece | Path | Purpose |
| --- | --- | --- |
| Helper | `/usr/local/sbin/curiosity-ziti-pins` (`ziti_pin_script_path`) | `refresh`, `invalidate`, `check`; edits only the marked `ANSIBLE MANAGED BLOCK: Ziti DNS pins` block |
| Pin list | `/etc/curiosity/ziti-pins.env` (`ziti_pin_env_path`) | host-local names and resolver |
| Service | `/etc/systemd/system/curiosity-ziti-pins.service` | oneshot, `After=` the tunnel unit, `refresh --wait ziti_pin_wait` |
| Drop-in | `/etc/systemd/system/<ziti_service_name>.d/20-curiosity-ziti-pins.conf` | `ExecStartPost=-systemctl --no-block restart curiosity-ziti-pins.service` |

Every start of the tunnel unit, including systemd's automatic restart after a crash,
runs the drop-in, which starts the service. The service:

1. resolves each pin against the tunnel's own resolver (`ziti_pin_resolver`, `100.64.0.2`),
2. **removes at once any pin the resolver cannot answer**, because a missing pin falls
   back to ordinary DNS and fails visibly, while a stale one reaches the wrong service,
3. keeps polling for up to `ziti_pin_wait` seconds (default 120) while the identities
   load, adding each pin as soon as it resolves,
4. accepts only addresses in `100.64.0.0/10`.

It never delays or fails the tunnel (`--no-block`, `-` prefix, no `Before=`), never
restarts it, and the role never restarts it for this change: the drop-in is read at the
tunnel's next start, so live ssh sessions riding it are not closed. The Ansible run also
does one refresh pass itself, and an emptied `ziti_etc_hosts_pins` removes the block, the
helper, the list and the hooks.

## Unit name and opt-out

`ziti_service_name` (default `ziti-edge-tunnel.service`) names the tunnel unit for the
drop-in, the service ordering, the health check and the split-DNS drop-ins. It is checked
against `^[A-Za-z0-9:_.@-]+$` before it reaches a path.

To keep a tunnel unit (for example an operator-managed identity unit) free of this
role's drop-in, set in that host's vars:

```yaml
ziti_pin_lifecycle_enabled: false
```

The role then deletes only its own `20-curiosity-ziti-pins.conf` and
`curiosity-ziti-pins.service`, restarts nothing, and still refreshes the pins during each
run (so pins can again go stale between runs, as before). `ziti_manage_service: false`
separately stops the role from enabling or starting the unit. A masked unit is skipped,
not fought.

## Targeting the role with `--tags ziti`

`--tags ziti` (and `networking`) runs the tasks inside `ziti.yml` and `ziti-split-dns.yml`;
`--tags dns` runs the split-DNS file. `tests/test_ziti_pins.py` proves it with real
`ansible-playbook` runs against the role's own include stanza, with a control that shows
what a missing `apply: tags` would do, and `tests/test_include_tags_apply.py` requires
every tagged include to carry `apply.tags`.

## Before/after measurement (redacted)

Run as root on the workstation. The output shows `pin-N` and `addr-N` labels, not
hostnames or addresses, so it can be pasted into an issue; add `--show-names` only for
your own eyes.

```sh
set -a; . /etc/curiosity/ziti-pins.env; set +a
H=/usr/local/sbin/curiosity-ziti-pins

# BEFORE: how many pins disagree with the tunnel right now?
$H check            # exit 0 = no drift, 1 = drift
#   pin-1 ok    pinned=addr-1 live=addr-1
#   pin-2 stale pinned=addr-2 live=addr-3
#   drift=1 pins=2

# Cause a reallocation in a maintenance window, from a console (not over ssh through
# Ziti, which this closes):
#   systemctl restart <ziti_service_name>
# Without the lifecycle hook (ziti_pin_lifecycle_enabled: false) `check` now reports
# the stale pins; they stay until the next Ansible run.

# AFTER (hook installed): wait for the service, then compare.
journalctl -u curiosity-ziti-pins --since "5 min ago" --no-pager   # "changed: N of N pins resolved"
$H check            # expected: drift=0
```

Record `drift=` before and after, plus the service's journal line. Do not paste output
produced with `--show-names`. `tests/test_ziti_pins.py::test_before_after_a_tunnel_restart_reallocates_the_addresses`
runs the same sequence against a stub resolver in CI: pins swapped by a simulated
restart are `stale` (drift 2), the refresh converges them, `check` returns drift 0.

## Related

[`configuration.md`](configuration.md) lists the variables; the tunnel health check
(`curiosity-ziti-health`) restarts a half-up tunnel, and the pin service then refreshes
after that restart like any other.
