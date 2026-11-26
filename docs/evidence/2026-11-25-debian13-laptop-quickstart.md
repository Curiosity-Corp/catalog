# Clean-room run record: 2026-11-25 (Debian 13, CI container)

Record for catalog#135, based on
[`clean-room-run-template.md`](clean-room-run-template.md). It is **not** a VM
run: no disposable VM is available to agents, so the guide was followed in a
throwaway CI container by the `debian13-quickstart` job (Draft !238). Every
value below comes from that job's artifacts; nothing is invented. Read
[What a container cannot show](#what-a-container-cannot-show) before treating
any of it as a Debian 13 support statement.

## Machine

- Guide followed: `docs/quickstarts/laptop.md` at commit `a78f6934`, rechecked at `e2174579` (branch
  `agent/135-debian13-quickstart`), pipelines 170823 (job 3077667) and 170868 (job 3078977).
- Hardware: none. A Kubernetes pod on the shared GitLab runner (Talos Linux
  node, kernel `6.18.63-talos`), image `python:3.13-slim-trixie`.
- Release: Debian GNU/Linux 13 (trixie), `/etc/debian_version` 13.8.
- Image source: Docker Hub `python:3.13-slim-trixie` (debian:trixie-slim plus
  an upstream CPython). The plain `debian:trixie` image was used first
  (pipelines 170680 to 170798); it has no python3, and without root the
  guide's apt-installed python3 cannot be added, so the job moved to the image
  that already carries Python.
- Network: direct, through the cluster egress.
- Privileges: uid 1000, `CapEff` 0, no sudo, `kernel.yama.ptrace_scope` 2, a
  seccomp profile that refuses `unshare`. Root could not be emulated: PRoot
  fails with `ptrace(TRACEME): Operation not permitted` (pipeline 170741),
  fakechroot could not exec any command (170776), and
  `unshare --user --map-root-user` fails with `Operation not permitted`
  (170798).

## Versions

- Collection: the job's own checkout (the guide clones tag `v0.1.0`).
- `ansible-core` 2.22.0 (installed with pip as in step 1).
- Python 3.13.18 (`/usr/local/bin/python3` of the image).

## Run

Command lines, as run by `scripts/debian_quickstart_run.sh` (apt state in
user-owned directories, shown here as `<apt-user-dirs>`):

| Guide step | What the job ran | Result |
| --- | --- | --- |
| 1 `sudo apt-get update` | `apt-get <apt-user-dirs> update` | exit 0 against `deb.debian.org` trixie, trixie-updates, trixie-security |
| 1 `sudo apt-get install -y git python3-venv python3-pip` | the same with `--simulate` | exit 0: all three resolve on Debian 13 |
| 1 venv, `pip install ansible-core`, `ansible-galaxy collection install` | as typed | exit 0 |
| 2 copy `examples/laptop.yml` | as typed | exit 0 |
| role APT steps (`repos`, `packages`) | `apt-get install --simulate` of the 146 names in `packages.yml`, first on the Debian archive, then with the role's 13 vendor repositories added | see [Packages](#packages) |
| 3 `--syntax-check`, `--list-tasks` | as typed | exit 0 |
| 3 `--check --diff` | as typed, plus `-e ansible_become=false` (no sudo) | exit 2: stops at the first APT keyring; after #164, at the first repository task, see [Preview](#preview) |
| 4 apply with `--become` | not run | the runner cannot become root |

- Preview output kept at: job 3077667 artifacts, `debian-quickstart/`
  (`3-check-diff.log`, `apt-findings.json`, `report.md`), kept 30 days.
- Wall-clock time of the first run: not measured, because the apply did not
  run. The preview took 87 s.
- `status` in `last-run.json`: none, because the apply did not run.
- Second run changed count: not measured, because the apply did not run.

## Expected high-level changes observed

### Packages

On the Debian 13 archive alone, 12 of the 146 names have no candidate.
Adding the role's vendor repositories supplies nine of them (`code`,
`google-cloud-cli`, `google-chrome-stable`, `microsoft-edge-stable`,
`brave-browser`, `firefox`, `1password-cli`, `mongodb-mongosh`, `openziti`).
Three still have no candidate of their own. Each was then simulated alone
(pipeline 170868, job 3078977):

| Name in the role | `apt-get install --simulate` on Debian 13 | Issue |
| --- | --- | --- |
| `dnsutils` | exit 0: virtual, apt installs `bind9-dnsutils` | none |
| `libfuse2` | exit 0: virtual, apt installs `libfuse2t64` (64-bit time_t transition) | none |
| `software-properties-common` | exit 100: `has no installation candidate` | #162 |

`software-properties-common` alone fails the role's single
`Install all APT packages` task. Without it, `apt-get install --simulate`
resolves the rest: 1,099 packages would be installed. The newest-JDK pick
resolves to `openjdk-25-jdk` 25.0.5.1+2-1~deb13u1 (201 packages).

### Repositories

All 13 vendor repository lines pass `apt-get update` on Debian 13. Three of
them are Ubuntu repositories that the role adds on Debian because the lines
are hard-coded to Ubuntu paths and to `apt_fallback_release: noble`. Nothing
fails, but a Debian machine gets builds made for Ubuntu 24.04 (#163):

- .NET: `https://packages.microsoft.com/ubuntu/24.04/prod noble main`
- Azure CLI: `https://packages.microsoft.com/repos/azure-cli/ noble main`
- MongoDB: `https://repo.mongodb.org/apt/ubuntu noble/mongodb-org/8.3 multiverse`

### Preview

`--check --diff` ran 30 tasks (4 changed, 5 skipped) and then stopped at
`Download vendor APT signing key for github-cli` with
`Destination /var/lib/curiosity/apt-keyrings does not exist`: in check mode
the role never creates its keyring staging directory, so on any clean machine
the preview the guide recommends ends at the first keyring (#164).

After #164 (catalog!240, pipeline 170998, job 3081753) the same step ran 32
tasks (5 changed, 13 skipped): the github-cli keyring shows as a change
("Would download ...") and the preview stops at the next task,
`Add GitHub CLI repository`, with `python3-apt must be installed to use check
mode`. That stop belongs to this job, not to the guide: the job points
`ansible_python_interpreter` at its own venv, which cannot import
`python3-apt`, while on a laptop Ansible uses the system Python, which
imports the distribution's `python3-apt` once that package is installed
(Ubuntu installs it by default). The `quickstart-check-preview` job, which adds
`python3-apt` to its venv, gates the rest of the guide's preview: it must
pass the whole `repos` group and stop only where laptop.md step 3 says.

With the guide's tags, none of the role's pre-flight checks run (the root
check, the managed-user check and the profile check have no tags), so the
guide's troubleshooting message `This role must run as root` cannot appear,
and a missing account is not reported up front (#165).

- Services enabled or disabled: not observable (see below).
- Files written outside the home directory: not observable, because the apply
  did not run.
- Anything the guide did not predict: the four issues above. Step 1 needed no
  change on Debian 13: `git`, `python3-venv` and `python3-pip` exist under the
  same names.

## Snapshot and rollback

- Snapshot taken before the run: none. The container is discarded when the
  job ends.
- Role rollback exercised: no, because the apply did not run.
- Hypervisor snapshot restore exercised: no hypervisor.

## What a container cannot show

A VM run on Debian 13 is still needed before the compatibility matrix says
anything stronger than "experimental". This container could not show:

- **The apply itself.** Nothing was installed, no file was written as root,
  and the update transaction (snapshot, health check, rollback, quarantine,
  `last-run.json`) never ran. The runner has no root and refuses every way of
  emulating it.
- **systemd services and timers.** PID 1 is the job's shell, not systemd, so
  no unit can be enabled, started or checked (for example the `ansible-pull`
  timer, `systemd-networkd` drop-ins, or user services under lingering).
- **A desktop session.** There is no display server, display manager, session
  bus or user login, so the desktop packages, autostart entries, browser
  policies and the minimal-desktop profile were not exercised.
- **Real hardware.** No firmware, GPU, Wi-Fi, audio, input devices, power
  management or kernel modules. The kernel is the Talos node's, not Debian's.
- **Package maintainer scripts.** `--simulate` resolves dependencies but runs
  no `postinst`, so a package that installs but fails to configure on Debian 13
  would not show here.
- **Ansible configuration from the checkout.** Ansible ignored the
  repository's `ansible.cfg` because the CI build directory is world-writable;
  the job sets `ANSIBLE_ROLES_PATH` itself. On a laptop checkout the file is
  read.

## Result

- Guide steps that were wrong or unclear: step 3 (`--check --diff` stopped
  at the first keyring on a clean machine; fixed by #164, see
  [Preview](#preview)); step 4 on Debian 13 would fail
  at `Install all APT packages` (#162) and would add Ubuntu repositories
  (#163); the troubleshooting table names a message the guide's tags never
  produce (#165).
- Reviewer and date: not reviewed yet; recorded 2026-11-25 from pipeline
  170823.
