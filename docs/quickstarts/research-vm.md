# Research VM quickstart

A minimal, reproducible workflow for a researcher who needs compilers, Python,
data tools and secure maintenance on a virtual machine, without a proprietary
cloud account. It uses the `workspace` profile, which is container-safe and
headless, and the variables in
[`examples/research-vm.yml`](../../examples/research-vm.yml).

## What you get

| Group | Installed by | Notes |
| --- | --- | --- |
| Compilers and build tools | `repos`, `packages` tags (`workspace` list) | `build-essential`, `gcc`, `g++`, `clang`, `make`, `cmake`, `meson`, `ninja-build`, `pkg-config`, autotools |
| Python | `packages`, `python` tags | `python3`, `python3-venv`, `python3-dev`, `pipx`; `uv`, `ruff`, `poetry`, `pre-commit`, JupyterLab and IPython through `pipx` |
| Data and science libraries | `science`, `data-science` tags | NumPy, SciPy, pandas, matplotlib, scikit-learn, seaborn, h5py, astropy and more in `/opt/science-venv`, plus the "Python 3 (Science)" Jupyter kernel |
| Secure maintenance | update transaction | Release discovery with digest checks, update ring, snapshot, health check, rollback, local manifest and SBOM |

## Suggested VM resources

The role was developed on workstations, not on a fixed VM size, so treat these
as a starting point and adjust after your first run. They are sizing advice,
not a tested guarantee.

- 2 or more vCPUs and 4 GiB of RAM. Compiling scientific wheels is the
  memory-hungry step; `/opt/science-venv` exists partly because building C
  extensions on constrained machines ran out of memory.
- A disk with room for the packages, `/opt/science-venv`, the update
  snapshots (a copy of every managed binary per run, newest three kept) and
  the artifact cache. Keep research data on a separate volume.
- Any hypervisor that boots an Ubuntu 24.04 image: libvirt/QEMU, VirtualBox,
  a university cloud, or a laptop hypervisor. See
  [cloud-init and libvirt](cloud-init-libvirt.md) for an image-based setup.

## Steps

1. Create the VM from an Ubuntu 24.04 (amd64) server or cloud image and make
   sure you can SSH to it as a normal user with `sudo`.
2. Take a **VM snapshot named `before-catalog`**.
3. On the VM, prepare the controller exactly as in the
   [laptop quickstart, step 1](laptop.md#1-prepare-the-controller).
4. Copy `examples/research-vm.yml` to `~/research-vm-vars.yml` and edit
   `dev_user` and `dev_user_home`.
5. Preview:

   ```bash
   TAGS=repos,packages,python,science,data-science
   ansible-playbook -i localhost, -c local playbooks/site.yml \
     -e @$HOME/research-vm-vars.yml --tags "$TAGS" --list-tasks
   ```

6. Apply:

   ```bash
   ansible-playbook -i localhost, -c local playbooks/site.yml \
     -e @$HOME/research-vm-vars.yml --tags "$TAGS" --become
   ```

7. Check the result:

   ```bash
   sudo cat /var/lib/curiosity/workstation-updates/last-run.json | python3 -m json.tool | head -40
   /opt/science-venv/bin/python -c "import numpy, pandas, scipy; print(numpy.__version__)"
   ~/.local/bin/uv --version
   ```

   `status` in `last-run.json` should be `success`. `ring_blockers` lists any
   upstream release the `early` ring is still holding back for six hours.
8. Take a second snapshot, `after-catalog-first-run`.

## Snapshots and the role's own rollback

These are two different safety nets and they do not replace each other.

- **VM snapshot**: restores the whole disk, including the role's state
  directory `/var/lib/curiosity/workstation-updates`. After a VM revert the
  role forgets everything that happened after the snapshot, which is correct.
  Revert the VM to undo a bad run or to reset an experiment.
- **Role snapshot** (`snapshots/<run>`): copies of the managed binaries from
  before one update run. The role restores them automatically when a health
  check fails after an update, and it never touches APT packages, your home
  directory or research data.
- Take a VM snapshot before the first run and before any major release or
  profile change. Do not rely on the role's snapshots for data recovery.
- The role never reboots the VM; kernel and service updates stay pending until
  you reboot.
- For long experiments, set `workstation_update_ring: general` and run the
  update at a time you choose (for example by not enabling a timer on the VM at
  all), so nothing changes under a running job. See [update rings](../update-rings.md).

## Expected high-level changes (what to look for)

On a fresh Ubuntu 24.04 image the first run, in this order:

1. adds APT repositories and installs the `workspace` package list;
2. resolves the current upstream release of every binary source and records it
   in `/var/lib/curiosity/workstation-updates/observations/`;
3. installs `pipx` tools into `~/.local` for `dev_user` and builds
   `/opt/science-venv`;
4. writes `last-run.json`, `sbom.cdx.json` and a snapshot directory.

Record your own versions in a run log like this, so the next researcher can
compare. This repository keeps such records under `docs/evidence/`.

```text
Date:                 YYYY-MM-DD
Image:                Ubuntu 24.04 cloud image build <id>
VM:                   <vCPUs>, <RAM>, <disk>
Catalog version:      v0.1.0 (commit <sha>)
ansible-core:         <version>
Tags:                 repos,packages,python,science,data-science
Result:               status=success, <n> tasks changed
Python / numpy / uv:  <versions>
```

## No cloud account needed

Nothing in this guide needs a Curiosity account, a registry login or a cloud
provider. The only outbound services are Ubuntu and vendor APT mirrors, PyPI
and the GitHub release API. When one of them is not reachable, see
[restricted networks](restricted-networks.md).
