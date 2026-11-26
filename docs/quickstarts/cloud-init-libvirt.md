# Generic VM and cloud-init examples

Bootstrap a maintained research VM from a public Ubuntu cloud image with
cloud-init, using libvirt/QEMU or any tool that can attach a NoCloud seed. No
vendor account is involved. The example files are in
[`examples/cloud-init/`](../../examples/cloud-init).

## What the example does and does not do

[`user-data`](../../examples/cloud-init/user-data) creates a `researcher` user
with a key-only login, installs `git`, Python tooling and `chrony`, formats and
mounts a separate data disk, writes a host-local variables file, clones a pinned
catalog tag and prepares a virtual environment with `ansible-core`. It
deliberately **does not apply the role**: applying it stays a reviewed, manual
step (see [Research VM](research-vm.md)).

[`virt-install.sh`](../../examples/cloud-init/virt-install.sh) is the libvirt
example. It builds the seed from that user-data with your public key in place of
the placeholder, creates a copy-on-write OS disk and an empty data disk, and
defines one new VM. It refuses to reuse an existing domain or disk, never
deletes anything, and `--dry-run` prints every command without running it.

## Build a seed and boot it (libvirt/QEMU)

Download an Ubuntu 24.04 cloud image from Ubuntu's public image server and
verify it against the published `SHA256SUMS`. Then:

```bash
examples/cloud-init/virt-install.sh \
  --image noble-server-cloudimg-amd64.img \
  --ssh-key ~/.ssh/id_ed25519.pub \
  --name research-vm --os-size 40G --data-size 100G --dry-run
# Drop --dry-run to create it. The files land in ./research-vm/ (--workdir).
```

The script needs `qemu-img`, `virt-install` and `cloud-localds`
(`cloud-image-utils`) or `genisoimage`. Doing the same by hand:

```bash
# 1. Copy user-data and replace the placeholder public key with your own.
# 2. Build the seed ISO (the volume label must be cidata):
cloud-localds seed.iso user-data examples/cloud-init/meta-data
# 3. A copy-on-write OS disk and a separate data disk:
qemu-img create -f qcow2 -F qcow2 -b noble-server-cloudimg-amd64.img research-vm.qcow2 40G
qemu-img create -f qcow2 research-vm-data.qcow2 100G
# 4. Define and start the VM (the data disk must be the second virtio disk, vdb):
virt-install --name research-vm --memory 4096 --vcpus 2 --os-variant ubuntu24.04 --import \
  --disk research-vm.qcow2,bus=virtio --disk research-vm-data.qcow2,bus=virtio \
  --disk seed.iso,device=cdrom --network network=default,model=virtio \
  --graphics none --noautoconsole
```

The files on the seed must be named exactly `user-data` and `meta-data`. The
sizes are examples; change them for your workload. The default libvirt network
gives the VM a NAT address by DHCP. If you need a fixed address on a bridge,
give the seed a `network-config` too, for example address `192.0.2.10/24`,
gateway `192.0.2.1` and resolver `192.0.2.53` (documentation addresses;
replace them with your own).

## Storage, snapshots, time and SSH

| Topic | Guidance |
| --- | --- |
| Storage | Keep the base cloud image read-only and use a qcow2 overlay per VM. Research data goes on the second disk: the user-data formats `/dev/vdb` as ext4 labelled `research-data` only when it holds no filesystem yet (`overwrite: false`) and mounts it at `/srv/research-data` with `nofail`, so you can rebuild the OS disk and reattach the data disk unchanged. The role does not manage VM disks; `workstation_storage` is an opt-in layout for a physical second drive and is off by default |
| Snapshots | After `cloud-init status --wait` reports `done`, run `virsh snapshot-create-as research-vm before-catalog --disk-only --atomic --diskspec vdb,snapshot=no` before the first role run. That snapshots the OS disk only, so a revert never rolls back research data. A VM revert also reverts the role's state directory, which is the intended behavior ([Research VM](research-vm.md#snapshots-and-the-roles-own-rollback)) |
| Time synchronization | The example installs and enables `chrony`. Release metadata and TLS need a correct clock, and the update ring compares upstream publication times with the VM clock, so a wrong clock changes ring decisions. A VM restored from a snapshot or resumed after a long pause starts with an old clock; `chrony` steps it on start, so check `chronyc tracking` before a role run. Set `workstation_timezone` for the zone |
| SSH boundary | The example sets `ssh_pwauth: false` and `disable_root: true` and locks the password, so only the key you supply can log in, and only as `researcher`. The VM gets no inbound service besides SSH. The role's `firewall` task file runs only for the `thin-client` and `byod-kiosk` profiles or an opted-in `desktop`, never for `workspace`; use the hypervisor network (the default NAT network is not reachable from outside the host) or a host firewall to limit who can reach port 22. `researcher` has passwordless `sudo`, so the SSH key is the whole boundary: keep it on the machines you use to administer the VM |

## Which role tasks need a graphical session or hardware

Read from `roles/developer_workstation/tasks/main.yml`. On a headless cloud
image use the `workspace` profile with the `generic` hardware profile: every task
file below is then skipped by its own condition, so nothing needs a display or a
physical device. The VM is not a container, so tasks guarded only against
containers do run in a VM with another profile.

| Needs | Task files | Runs when |
| --- | --- | --- |
| A graphical session (X11 desktop) | `minimal-desktop.yml` and its `minimal-desktop-*.yml` parts, `kiosk-session.yml` | `thin-client` or `byod-kiosk` profile, or `desktop` with `minimal_desktop_enabled` |
| | `display-config.yml` (GDM, VNC, GNOME) | `desktop` with `display_config_enabled` |
| | `media-folder.yml` (desktop launchers) | `desktop` profile |
| | `streaming.yml` (Sunshine) | `desktop` with `sunshine_enabled`; also wants a GPU |
| A physical monitor or console | `console-video.yml`, `monitor-sleep.yml`, `grub-autoboot.yml`, `console-tuning.yml` | `desktop`, `thin-client` or `byod-kiosk` on x86_64 (`console-tuning.yml` only with `console_tuning_enabled`) |
| | `t460-console.yml` | `t460` or `x86-byod-laptop` hardware profile |
| | `i915-pcon-frl-444.yml` | an Intel GPU driving an HDMI 2.1 adapter; on the three non-workspace profiles |
| | `samsung-tv-cli.yml` | `samsung_tv_cli_enabled`: a TV on the local network used as the monitor |
| Physical input or capture devices | `pointer-accel.yml`, `mouse-debounce.yml`, `logitech-g604.yml` | named mice in the hardware profile; on the three non-workspace profiles |
| | `streamstack.yml` and its `streamstack-*.yml` parts | `desktop` with `streamstack_enabled`: camera, capture card and microphone |
| Physical hardware: CPU, GPU, radio | `hardware-drivers.yml`, `browser-video-decode.yml` | non-workspace profiles; what they install comes from the hardware profile |
| | `cpu-thermal-guard.yml` | `developer_workstation_cpu_max_perf_pct` set: caps a physical Intel CPU |
| | `wifi-netplan.yml` | `wifi_ssid` set: needs a wireless adapter |

The `desktop` profile's own package list (`packages.yml`, `fonts.yml`) assumes
a desktop but installs without one. The `workspace` profile installs the
container-safe package set
(`packages-workspace.yml`) and the workspace helpers, and the `generic` hardware
profile adds no hardware-specific packages.

## Validation status

**No run in a real VM has happened yet** (catalog#30 stays open for it). What
is checked today, on every merge request:

- the `cloud-init-schema` CI job runs cloud-init's own `schema` command on
  [`user-data`](../../examples/cloud-init/user-data) with a pinned,
  digest-checked cloud-init release, in a throwaway container;
- `tests/test_quickstarts.py` checks that the user-data starts with
  `#cloud-config`, keeps SSH key-only and the key a placeholder, never applies
  the role, formats the data disk only when empty, and that every variable in
  the host-local file is a real role variable; it also runs
  `virt-install.sh --dry-run` and checks the commands it would run.

The full boot-and-apply run on a disposable VM is a manual step: boot it with
`virt-install.sh`, run the documented bootstrap and the role, and record the
result in a copy of
[`docs/evidence/clean-room-run-template.md`](../evidence/clean-room-run-template.md).
