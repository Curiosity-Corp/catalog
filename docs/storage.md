# Encrypted btrfs /home volume

`workstation_storage` is an opt-in role for a workstation with a large second
drive. It puts `/home` (and optionally hot write paths such as `/var/tmp`) on an
encrypted btrfs volume, keeps it healthy with a monthly scrub and a weekly
fstrim, and ships the scripts for a one-time, scripted cutover. It takes no
snapshots and makes no backups: workstations are disposable (see Recovery). Nothing happens unless `workstation_storage_enabled`
is true in host variables.

## Layout

| Item | Where |
|---|---|
| OS, `/boot`, `/boot/efi`, `/var`, `/opt`, swap | stays on the OS drive |
| Hot write paths (optional) | one subvolume each from `workstation_storage_extra_mounts`; dm313red moves `/var/tmp` (`@var-tmp`) and `/var/lib/containers` (`@containers`) |
| `/home` | `@home` subvolume on the data volume (`nodev,nosuid`) |
| Per-user home | nested subvolume `@home/<user>`, one per account in `workstation_storage_users` |
| Cutover baselines | `@snapshots`, mounted at `/.snapshots`: only the one-off read-only baselines `hh-storage-cutover` takes, used by the strict compare; no snapshot schedule |
| Top-level volume | `/mnt/hh-data` (`nodev,nosuid,noexec`), for tooling only |

The data volume is LUKS2 (aes-xts-plain64, 512-bit key, argon2id, 4096-byte
sectors) with TPM2 auto-unlock sealed to PCR 7 and a typed recovery passphrase.
btrfs uses metadata DUP, single data, `noatime,compress=zstd:1,discard=async`.
Repositories, caches and `node_modules` are deliberately not separate
subvolumes: hardlinks and renames fail across subvolumes.

## Variables

Set them in host variables (for example `/etc/ansible/local-vars.yml`), never in
the role. See `roles/workstation_storage/defaults/main.yml`.

- `workstation_storage_enabled`, `workstation_storage_luks_uuid`,
  `workstation_storage_fs_uuid`, `workstation_storage_users` (list of `{name}`).
  The crypttab entry carries `nofail,headless=true`, so a missing or locked data
  volume never drops boot into emergency mode.
- `workstation_storage_provision`, `workstation_storage_device` (a `/dev/disk/by-id`
  path), `workstation_storage_luks_size`, `workstation_storage_luks_key_file`
  (root-only passphrase file): partition, encrypt and format a blank device.
  `hh-storage-provision` refuses a device with any signature or partition.
- `workstation_storage_extra_mounts` (list of `{subvol, path, options?}`): more
  subvolumes of the data volume mounted outside `/home`, written into the
  managed fstab block after the cutover with `nofail` and a dependency on the
  crypt unit. The role never creates or fills them; it fails if the subvolume
  is missing. Create it, copy the data with `rsync -aHAX`, then list it. Lines
  added by hand below the managed block (`subvol=` as the first option) are
  removed when the role adopts the same path.

## Before the cutover

The role installs packages, scripts, units and configs, and stages `fstab` and `crypttab` under
`/var/lib/hh-storage/staged/`. It never edits the live `/etc/fstab` or
`/etc/crypttab`, never mounts or unmounts, and never touches a session.

1. `hh-storage-sync seed` copies each home into its subvolume while the system
   is live (repeatable, never deletes). `hh-storage-sync verify` compares by checksum.
2. `hh-storage-cutover --preflight` checks the staged files and the volume.

## Cutover

`hh-storage-cutover --confirm` ends every session of the listed users, runs a
final `--delete` copy, requires zero checksum differences, takes a baseline
snapshot, installs the staged `fstab`/`crypttab`, writes
`/var/lib/hh-storage/cutover-complete` and reboots. Any failure before the fstab
switch aborts with the live files untouched. Run it as a transient system unit
(`systemd-run -p MemoryHigh=6G -p MemoryMax=10G`) so ending the user sessions cannot kill it and the copy's page cache cannot starve other sessions. A session manifest
is written to `/var/lib/hh-storage/manifests/`.

Rollback: `cp /etc/fstab.pre-cutover /etc/fstab`, `cp /etc/crypttab.pre-cutover /etc/crypttab`,
remove the marker and `user@.service.d/10-require-home.conf`, reboot. The old
`/home` is still on the OS drive.

## After the cutover

With the marker present the role keeps the active fstab/crypttab entries, a
`user@.service.d` drop-in (`RequiresMountsFor=/home`, so no session starts on an
unmounted `/home`) and two timers: `hh-btrfs-scrub.timer` (monthly scrub) and
`fstrim.timer` (weekly trim). Hosts set up by earlier versions of the role had
`hh-btrbk.timer` (hourly snapshots) and `hh-backup.timer` (nightly restic); the
role stops and disables them and removes their units, scripts and configs. It
leaves their existing data (`/.snapshots/<user>.<timestamp>`, the restic
repository) in place.
`hh-storage-postboot` checks the result. `hh-storage-cutover-fast` is the variant used on dm313red on 2026-10-24: it makes `/home` read-only after stopping users and replaces the pre-switch checksum pass with counts, sizes and an mtime dry-run, leaving the exact compare to `hh-storage-postreboot`; `hh-storage-postreboot` enables the scrub and trim timers, runs it, compares the frozen old `/home` on the OS drive with the cutover baseline snapshots by full checksum (must be zero differences), then runs a scrub, skips finished steps on a rerun, and reports whether a clean memory test was recorded (`/var/lib/hh-storage/ram-cleared`). Its header carries the rollback note. After the soak period
(`workstation_storage_soak_days`) `hh-storage-reclaim --confirm` removes the old
copy of `/home` from the OS drive.

## Recovery

There are no local backups and no snapshot schedules on workstations, by CEO
decision (2026-10-24). GitLab (code, branches, merge requests) and chezmoi
(dotfiles) are the source of truth; machines resync daily and are rebuilt. If
the data volume or the machine is lost, rebuild it (this catalog, including
`workstation_storage`), then clone from GitLab and `chezmoi apply`. Uncommitted
work is not recoverable, so commit and push early.

## Memory

Copies and checksum passes read and write hundreds of gigabytes. Always run
`hh-storage-sync` in a transient unit or timer unit with
`MemoryHigh`/`MemoryMax` (the shipped units set them), so page cache is
reclaimed inside the job's cgroup instead of pressuring the whole host. On a
workstation running many agent sessions, an unbounded run was followed by a
host-wide out-of-memory storm.
