# Generic VM and cloud-init examples

Bootstrap a maintained research VM from a public Ubuntu cloud image with
cloud-init, using libvirt/QEMU or any tool that can attach a NoCloud seed. No
vendor account is involved. The example files are in
[`examples/cloud-init/`](../../examples/cloud-init).

## What the example does and does not do

[`user-data`](../../examples/cloud-init/user-data) creates a `researcher` user
with a key-only login, installs `git`, Python tooling and `chrony`, writes a
host-local variables file, clones a pinned catalog tag and prepares a virtual
environment with `ansible-core`. It deliberately **does not apply the role**:
applying it stays a reviewed, manual step (see [Research VM](research-vm.md)).

## Build a seed and boot it (libvirt/QEMU)

Download an Ubuntu 24.04 cloud image from Ubuntu's public image server and
verify it against the published `SHA256SUMS`. Then:

```bash
# 1. Replace the placeholder public key in user-data with your own.
# 2. Build the seed ISO (any of these tools works):
cloud-localds seed.iso examples/cloud-init/user-data examples/cloud-init/meta-data
#   or: genisoimage -output seed.iso -volid cidata -joliet -rock \
#         examples/cloud-init/user-data examples/cloud-init/meta-data
# 3. Create a copy-on-write disk from the base image:
qemu-img create -f qcow2 -F qcow2 -b noble-server-cloudimg-amd64.img research-vm.qcow2 40G
# 4. Define and start the VM:
virt-install --name research-vm --memory 4096 --vcpus 2 \
  --disk research-vm.qcow2 --disk seed.iso,device=cdrom \
  --os-variant ubuntu24.04 --import --network network=default --graphics none
```

The seed volume label must be `cidata` and the files must be named exactly
`user-data` and `meta-data`. The sizes above are examples; change them for your
workload.

## Storage, snapshots, time and SSH

| Topic | Guidance |
| --- | --- |
| Storage | Keep the base cloud image read-only and use a qcow2 overlay per VM. Put research data on a second virtual disk so you can rebuild the OS disk without touching data. The role does not manage VM disks; `workstation_storage` is an opt-in layout for a physical second drive and is off by default |
| Snapshots | Use `virsh snapshot-create-as research-vm before-catalog --disk-only` (or `qemu-img snapshot`) before the first run. A VM revert also reverts the role's state directory, which is the intended behavior ([Research VM](research-vm.md#snapshots-and-the-roles-own-rollback)) |
| Time synchronization | The example installs and enables `chrony`. Release metadata and TLS need a correct clock, and the update ring compares upstream publication times with the VM clock, so a wrong clock changes ring decisions. Check with `timedatectl` and `chronyc tracking`. Set `workstation_timezone` for the zone |
| SSH boundary | The example sets `ssh_pwauth: false` and `disable_root: true`, so only the key you supply can log in, and only as `researcher`. The VM gets no inbound service besides SSH. The role's `firewall` task group is not part of the `workspace` profile; use the hypervisor network or a host firewall to limit who can reach port 22 |

## Which role tasks need a graphical session or hardware

On a headless cloud image use the `workspace` profile and do not select these:

- `minimal-desktop`, `display`, `fonts`, `console`, `grub`, `t460`: need a
  desktop, a console or particular hardware;
- `hardware`, `litra`, `streamstack`, `streaming`, `sunshine`: need physical
  devices or a GPU;
- the `desktop`, `thin-client` and `byod-kiosk` profiles: install a desktop
  session or a browser appliance.

The `workspace` profile skips desktop and thin-client package sets, and the
hardware profile `generic` has no hardware-specific packages.

## Validation status

The structure of the example is checked in CI (`tests/test_quickstarts.py`):
the user-data parses as YAML, starts with `#cloud-config`, keeps SSH key-only,
and every variable in the host-local file is a real role variable. The full
boot-and-apply run on a disposable VM is a manual step recorded in
`docs/evidence/` when someone performs it, using
[`docs/evidence/clean-room-run-template.md`](../evidence/clean-room-run-template.md).
