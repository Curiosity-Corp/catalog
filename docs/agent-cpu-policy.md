# Agent CPU policy

Concurrent coding agents (Claude Code, Codex), the test runs, builds and
language servers they start, and IDE servers can take every CPU of a research
workstation. Latency-sensitive work then starves: DNS lookups time out, the
networking stack and `ziti-edge-tunnel` miss their deadlines, SSH sessions and
the desktop stutter. The `agent_cpu_policy` task group
(`roles/developer_workstation/tasks/agent-cpu-policy.yml`, tags
`agent-cpu-policy, system`) bounds the **aggregate** CPU of those processes with
native systemd and cgroup v2 features and no custom scheduler.

It is **default off** (`agent_cpu_policy_enabled: false`). Enable it per host or
group, for example in `/etc/ansible/local-vars.yml` or group inventory:

```yaml
agent_cpu_policy_enabled: true
```

It runs on the `desktop`, `thin-client` and `byod-kiosk` profiles on systemd
hosts, and is skipped on `workspace` and inside containers. It also refuses to
install on a host whose root cgroup does not list the `cpu` controller (cgroup
v1), because a quota there would bound nothing; the run prints why.

## How it works

| Path | Purpose |
| --- | --- |
| `/etc/systemd/user/curiosity-agents.slice` | A slice every user's `systemd --user` manager knows. `CPUQuota` is a hard ceiling for everything inside it; `CPUWeight` is lowered below the default 100 |
| `/usr/local/bin/curiosity-agent-run` | Starts a command inside that slice with `systemd-run --user --scope`; fails open (see [Failure modes](#failure-modes)) |
| `/etc/profile.d/curiosity-agent-cpu.sh` | Optional shell functions that send chosen commands (default `claude`) through the launcher |
| `/usr/local/bin/curiosity-agent-cpu-bench` | The measurement tool for [Measuring before and after](#measuring-before-and-after) |

Everything a wrapped process starts (test runners, compilers, language
servers, headless browsers) inherits the slice, so the bound covers the whole
tree, not just the agent binary. The role never moves or restarts a process
that is already running; a session picks the bound up the next time its tool is
started through the launcher.

Why a user slice and a quota: processes of an interactive login sit in
`session-N.scope` under `user-UID.slice`, siblings of the user manager that
hosts the agent slice, and `CPUWeight` only arbitrates between siblings. The
`CPUQuota` is the part that holds against everything else on the machine: with
a quota of Q percent on a machine with N CPUs, the agents can never occupy more
than Q/100 CPUs, so at least `N - Q/100` CPUs stay available to the desktop,
the network stack and the DNS resolver, however many agents run.

The slice is per user (an unprivileged user cannot join a system slice). To keep
the **aggregate over all users** inside the budget, the automatic quota is
divided by the number of `dev_users`.

## Variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `agent_cpu_policy_enabled` | `false` | Master switch; `false` also removes everything the group installed |
| `agent_cpu_policy_quota` | `auto` | `auto`, or an explicit systemd percentage such as `800%` used as written for each user |
| `agent_cpu_policy_share_percent` | `75` | `auto`: the share of all vCPUs the agents may use |
| `agent_cpu_policy_reserve_cpus` | `1` | `auto`: whole CPUs always left free (matters on small machines) |
| `agent_cpu_policy_split_across_users` | `true` | `auto`: divide the budget by `dev_users` |
| `agent_cpu_policy_min_quota_percent` | `50` | `auto`: lower bound per user (100 = one CPU) |
| `agent_cpu_policy_weight` | `50` | `CPUWeight` of the slice (1-10000) |
| `agent_cpu_policy_wrapped_commands` | `[claude]` | Commands that get a shell function; plain identifiers only |
| `agent_cpu_policy_slice` | `curiosity-agents.slice` | Slice name |

### Sizing the quota (`auto`)

`budget = min(vCPUs x share_percent, (vCPUs - reserve_cpus) x 100)`, divided by
the number of `dev_users` when splitting, never below `min_quota_percent`:

| vCPUs | 1 user | 2 users | 4 users |
| ---: | ---: | ---: | ---: |
| 2 | `100%` | `50%` | `50%` |
| 4 | `300%` | `150%` | `75%` |
| 8 | `600%` | `300%` | `150%` |
| 16 | `1200%` | `600%` | `300%` |
| 32 | `2400%` | `1200%` | `600%` |

Rules of thumb for research workloads:

- Interactive-heavy machine (notebooks, plotting, a remote desktop): lower
  `agent_cpu_policy_share_percent` to 50 and keep `reserve_cpus` at 2 or more.
- Batch/agent-heavy machine nobody sits at: raise the share to 90 or give an
  explicit `quota`.
- Many accounts on few CPUs: the 50% floor can make the sum of the per-user
  quotas exceed the budget; set `agent_cpu_policy_split_across_users: false`
  and a lower explicit `quota` instead, or reduce `dev_users`.
- Work that is bursty but short (builds): the quota is enforced over 100 ms
  periods, so short bursts above the average are throttled, not queued for
  long. Raise the quota rather than the weight if builds feel slow.

The effective values are printed by `curiosity-agent-run --status`.

## Using it

```bash
# Explicit
curiosity-agent-run claude
curiosity-agent-run code --no-sandbox ~/work/project      # an IDE server
curiosity-agent-run python3 -m pytest -q

# Implicit: login shells read /etc/profile.d/curiosity-agent-cpu.sh, which
# defines `claude()` (and any other agent_cpu_policy_wrapped_commands) so that
# plain `claude` goes through the launcher. For bash/zsh sessions that are not
# login shells, add one line to the rc file:
. /etc/profile.d/curiosity-agent-cpu.sh
```

Long-lived services can join the slice without the launcher by adding
`Slice=curiosity-agents.slice` to their user unit.

## Escape hatches

- `CURIOSITY_AGENT_CPU_POLICY=off curiosity-agent-run <cmd>` or
  `export CURIOSITY_AGENT_CPU_POLICY=off` in a shell: run outside the slice. The
  shell functions honour it too.
- `command claude ...` bypasses the shell function for one call.
- `agent_cpu_policy_wrapped_commands: []` keeps the slice and launcher but
  installs no shell function.
- An explicit `agent_cpu_policy_quota` per host or group for a bigger or
  smaller ceiling.
- To change the bound of a running slice for the current boot only, without an
  Ansible run: `systemctl --user set-property --runtime curiosity-agents.slice CPUQuota=1200%`.

## Failure modes

The launcher **fails open** by design: a policy problem must never stop work. It
prints one line to stderr and runs the command normally when:

- `systemd-run` is not installed;
- the user's systemd manager is not reachable (no login session, `ssh` without
  a user manager, a minimal container);
- the user manager does not have the slice loaded (the policy was switched off,
  or the unit was added after the manager started and a `daemon-reload` has not
  happened yet; the role triggers one for running managers).

Set `CURIOSITY_AGENT_CPU_STRICT=1` to fail closed instead: the launcher exits
with status 70 and runs nothing. Use it in automation where an unbounded run is
worse than a failed one.

Other things that can go wrong:

| Symptom | Cause | Check and fix |
| --- | --- | --- |
| Quota exists but agents still use more | The `cpu` controller is not delegated to the user manager | `curiosity-agent-run --status` prints `delegated controllers:`; it must list `cpu`. The stock `user@.service` delegates `pids memory cpu` (confirmed on systemd 259); if a host overrides it, restore `Delegate=cpu` in a `user@.service` drop-in |
| Role run prints "cannot enforce" and installs nothing | cgroup v1 or no cpu controller | Boot with the unified hierarchy (`systemd.unified_cgroup_hierarchy=1`) |
| Agent is slow in bursts | The quota throttles above-average bursts over 100 ms periods | Raise the quota; `nr_throttled` in the slice's `cpu.stat` shows it |
| Some processes are not bounded | They were started outside the launcher (an IDE launched from the desktop menu, a daemon) | Start them with `curiosity-agent-run` or `Slice=` |
| `claude` runs twice nested | Not an issue: a command already inside the slice is run directly | None |
| A process survives a policy change | Already-running processes keep their cgroup | Restart the tool |
| Several users each use their share | The slice is per user | Keep `agent_cpu_policy_split_across_users: true` |

Things the policy deliberately does not do: no memory, I/O or task limits, no
killing, no priority changes to processes outside the slice, no edits to
`user.slice`, `system.slice` or any existing unit, and no changes to the kernel
command line.

## Safe disable

1. Set `agent_cpu_policy_enabled: false` (group or host inventory) and let the
   next pull converge. The role removes the slice, launcher, shell functions and
   measurement tool, then runs `systemctl --user daemon-reload` for each
   `dev_users` account that has a running manager. Nothing is restarted.
2. Shells that still define `claude()` keep working: the function checks that
   `curiosity-agent-run` exists and otherwise runs the tool directly, and the
   launcher itself falls back to a plain run when the slice is gone.
3. Processes already inside the slice keep running; once the slice file is gone
   and the manager reloaded, their limit is removed. To lift it at once without
   waiting: `systemctl --user set-property --runtime curiosity-agents.slice
   CPUQuota= CPUWeight=`.
4. Nothing needs a reboot. To undo by hand on one host: delete the four files in
   the table above and run `systemctl --user daemon-reload` in each session.

## Measuring before and after

The claim to test is: with the policy, the agents' aggregate CPU stays at or
below the quota while the machine stays responsive, at an acceptable cost in
agent throughput. `curiosity-agent-cpu-bench` runs the same real workload in
the same number of concurrent workers with and without the launcher and
compares them.

**Workloads.** Use real work, not only a busy loop. The tool repeats any shell
command in every worker; presets run a real test suite (`catalog-tests`,
`python-compile`) in `--workdir`. Good choices, in order of realism:

1. Headless agent sessions on a real task, for example
   `--workload 'claude -p "run the test suite and summarise failures"'` in a
   checkout (this exercises the agent, its tools and the test runner together).
2. The project's own test suite or build (`pytest`, `npm test`, `cargo build`,
   `make -j`) in a real checkout, many copies at once.
3. A synthetic loop (`--workload 'python3 -c "while True: pass"'`), only as a
   sanity check that the quota is enforced.

**What it measures** while the workers run, sampled every 100 ms from a process
outside the slice: scheduler wake-up latency of a 10 ms sleep, the time to
start and reap `true` (what each shell command pays), the time of
`getent hosts localhost` (the NSS/DNS path; use `--resolver-name` for a real
name), kernel CPU pressure (`/proc/pressure/cpu`), the aggregate CPU the workers
consumed, completed workload iterations, and the slice's `cpu.stat` throttling
counters in policy mode.

**Procedure** (one machine, a quiet moment, AC power, same checkout and
workload both times):

```bash
curiosity-agent-run --status                       # confirm: loaded, quota, `cpu` delegated
W=$(( 2 * $(nproc) ))                              # oversubscribe: 2 workers per CPU

# 1. Warm up once (fills page caches), discard the result
curiosity-agent-cpu-bench run --label warmup --mode bare --workers "$W" --duration 30 \
  --workload-preset catalog-tests --workdir ~/src/catalog

# 2. Before: workers started directly (the launcher is bypassed)
curiosity-agent-cpu-bench run --label before --mode bare --workers "$W" --duration 120 \
  --workload-preset catalog-tests --workdir ~/src/catalog --json before.json

# 3. After: the same workers, started through curiosity-agent-run
curiosity-agent-cpu-bench run --label after --mode policy --workers "$W" --duration 120 \
  --workload-preset catalog-tests --workdir ~/src/catalog --json after.json

# 4. Compare against the quota shown by --status (CPUQuotaPerSecUSec 6s = 600%)
curiosity-agent-cpu-bench compare before.json after.json --quota-percent 600%
```

Repeat steps 2-3 at least three times, alternating (before, after, before,
after, ...), and compare the medians, so thermal state and other tenants do not
decide the result. Record the output of `uname -r`, `nproc` and `--status` with
the numbers. Add a human check: during the "after" run, open a terminal and an
SSH session and type; note whether it feels any different.

**Pass criteria** (`compare` exits non-zero when one fails):

| Check | Passes when |
| --- | --- |
| Quota enforced | After: aggregate agent CPU is at most the quota plus 10% |
| Responsiveness | After: wake-up latency p99 and fork+exec p99 are not worse than before (expected: clearly better when the machine was saturated) |
| Cost visible | The table also lists iterations completed and CPU pressure, so the throughput given up for the headroom is on record |

The `before` run saturates the machine on purpose; on a busy shared machine run
it only when others can tolerate a minute or two of load, or on a spare host.

The tool never changes system settings: `--mode bare` sets
`CURIOSITY_AGENT_CPU_POLICY=off` for its workers so a shell function cannot leak
the policy into the baseline, and `--mode policy` only adds the launcher.

### Recorded results

None are recorded yet. This change was developed and tested without applying the
policy to any live machine (tests use stub `systemd-run`/`systemctl` programs).
The first host that enables it should add its table here: kernel, vCPUs, quota,
workload, the `compare` output, and the date.
