# Encrypted btrfs /home volume

`workstation_storage` is an opt-in role for a workstation with a large second
drive. It puts `/home` and the write-heavy system paths on an encrypted btrfs volume
and ships the scripts for a one-time, scripted cutover. It keeps no snapshots
and no backups (see Recovery). Nothing happens unless `workstation_storage_enabled`
is true in host variables.

## Layout

| Item | Where |
|---|---|
| OS, `/boot`, `/boot/efi`, `/var`, `/opt`, swap | stays on the OS drive |
| `/home` | `@home` subvolume on the data volume (`nodev,nosuid`) |
| Per-user home | nested subvolume `@home/<user>`, one per account in `workstation_storage_users` |
| Hot write paths | `@var-tmp` on `/var/tmp` (`nodev,nosuid`) and `@containers` on `/var/lib/containers` (`workstation_storage_hot_paths`); `/tmp` stays tmpfs |
| Snapshots | none kept; `@snapshots` is only the mountpoint the cutover used for its one baseline |
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
  `workstation_storage_fs_uuid`, `workstation_storage_users` (list of `{name, retention?}`).
- `workstation_storage_provision`, `workstation_storage_device` (a `/dev/disk/by-id`
  path), `workstation_storage_luks_size`, `workstation_storage_luks_key_file`
  (root-only passphrase file): partition, encrypt and format a blank device.
  `hh-storage-provision` refuses a device with any signature or partition.

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

With the marker present the role keeps the active fstab/crypttab entries (the
crypttab line carries `nofail,headless=true`, so a missing or locked data volume
never blocks boot), a `user@.service.d` drop-in (`RequiresMountsFor=/home`, so no
session starts on an unmounted `/home`), the hot-path subvolumes and mounts, and
two timers: `hh-btrfs-scrub.timer` (monthly scrub) and `fstrim.timer` (weekly
trim). The retired snapshot and backup timers are disabled and their units removed.
`hh-storage-postboot` checks the result; `hh-storage-postreboot` runs it, then a
scrub, skips finished steps on a rerun, and reports whether a clean memory test
was recorded (`/var/lib/hh-storage/ram-cleared`). Its header carries the rollback
note. After the soak period (`workstation_storage_soak_days`)
`hh-storage-reclaim --confirm` removes the old copy of `/home` from the OS drive.

## Recovery

There are no local backups and no snapshots, by decision of the CEO (2026-10-24).
Everything that matters lives in GitLab (code, work in branches and merge
requests) and in chezmoi (dotfiles and machine configuration). If the data volume
or the machine is lost, rebuild the machine (Ansible, `workstation_storage`),
then resync: clone from GitLab and `chezmoi apply`. Uncommitted work is not
recoverable, so commit and push early. The LUKS recovery passphrase is in Azure
Key Vault (`omlab-secrets`, item `dm313red-data-luks-recovery-key`).

## Notes from the dm313red cutover

- The staged `crypttab` needs `nofail`; without it a locked volume drops boot
  into emergency mode.
- The pre-cutover checksum verify aborts on a live host (timers and caches keep
  writing). The 2026-10-24 cutover bound `/home` read-only, ran a final
  `rsync --delete`, then a fast sanity check (file counts, sizes, key paths).
- The data volume was grown online after the cutover (`growpart`, then
  `cryptsetup resize`, then `btrfs filesystem resize max`). The LUKS device uses
  4096-byte sectors, so the partition size must be a multiple of 8 sectors;
  `growpart` fills the disk to an unaligned end and `cryptsetup resize` then
  fails with "Device size is not aligned"; trim the partition end with
  `sfdisk --no-reread -N 1`, run `partx -u`, and retry.
- Mount the hot-path subvolumes through fstab and systemd (`mount` by hand
  refuses a second subvolume of an already-mounted filesystem with a different
  option set, and bind mounts of `/mnt/hh-data` inherit `noexec`).
- The host firewall needs a LAN ssh rule for the WiFi interface (dm313red has no
  cable): `22/tcp in on wlp8s0 from 192.168.4.0/22`. It is set on the host;
  express it through `firewall_ssh_interfaces` or a source-scoped rule in the
  host variables.

## Memory

Copies and checksum passes read and write hundreds of gigabytes. Always run
`hh-storage-sync` in a transient unit with
`MemoryHigh`/`MemoryMax` (the shipped units set them), so page cache is
reclaimed inside the job's cgroup instead of pressuring the whole host. On a
workstation running many agent sessions, an unbounded run was followed by a
host-wide out-of-memory storm.
