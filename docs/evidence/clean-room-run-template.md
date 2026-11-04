# Clean-room run record: YYYY-MM-DD

This template is a blank record. Fill it only from a real run on a disposable
Ubuntu LTS virtual machine or a freshly installed laptop, and do not invent
values. Use one copy per guide:
`docs/quickstarts/laptop.md`, `research-vm.md` or `cloud-init-libvirt.md`.

## Machine

- Guide followed (file and commit SHA):
- Hypervisor or hardware, vCPU, RAM, disk:
- Ubuntu release and `uname -r` before the run:
- Image source and checksum (for a VM):
- Network (direct, proxy, offline):

## Versions

- Collection tag or commit:
- `ansible-core` version:
- Python version:

## Run

- Command lines used, exactly as typed:
- Preview (`--check --diff` or `--list-tasks`) output kept at:
- Wall-clock time of the first run:
- `status` in `/var/lib/curiosity/workstation-updates/last-run.json`:
- Second run changed count (expect 0 changed apart from update checks):

## Expected high-level changes observed

- Packages added (count and notable names):
- Services enabled or disabled:
- Files written outside the home directory:
- Anything the guide did not predict:

## Snapshot and rollback

- Snapshot taken before the run (name):
- Role rollback exercised (yes/no, outcome):
- Hypervisor snapshot restore exercised (yes/no, outcome):

## Result

- Guide steps that were wrong or unclear (open an issue for each):
- Reviewer and date:
