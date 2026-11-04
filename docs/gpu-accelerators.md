# GPU and accelerator support boundary

This role does not install GPU drivers. It documents where responsibility sits
and ships one read-only command, `curiosity-gpu-status`, that tells a person which
layer is missing. No proprietary driver, CUDA, ROCm or oneAPI package, and no
vendor credential, is installed or fetched by default.

## The three layers

| Layer | Examples | Who provides it |
|---|---|---|
| Host kernel driver and firmware | `nvidia`, `amdgpu`, `i915`/`xe`, `/dev/nvidia*`, `/dev/kfd`, `/dev/dri/renderD*` | The host operator, from the distribution or the vendor. Not this role. |
| User-space runtime on the host | `nvidia-smi` and the CUDA runtime, `rocminfo` and ROCm, Level Zero and `sycl-ls`/`clinfo` | The host operator, or a container image that carries it. |
| Project environment | A conda/pip environment, container or devcontainer with the project's framework build | The project. Pin the framework build to the runtime above. |

Keeping them apart means a driver upgrade on a lab host cannot silently change a
project's environment, and a project can be reproduced on a machine whose driver
is newer than the one it was written on.

## Detection and states

`curiosity-gpu-status` reads `/sys/bus/pci/devices`, a few `/dev` nodes and `PATH`.
It runs no other program and changes nothing.

| State | Meaning | What to do |
|---|---|---|
| `none` | No display or accelerator device. Normal for servers and CI. | Nothing. |
| `virtual-only` | Only a hypervisor adapter (virtio, VMware, QXL, Hyper-V, VirtualBox). | Pass a physical GPU through at the hypervisor, then re-run in the guest. |
| `needs-driver` | A physical GPU with no compute-capable kernel driver (no driver, `nouveau`, legacy `radeon`). | Install the host driver. This role will not. |
| `needs-userspace` | Driver bound, vendor tool absent from `PATH`. | Install the vendor runtime on the host, or use a container that has it. |
| `ready` | Driver, device node and vendor tool present. | Validate with a real workload (below). |

```
curiosity-gpu-status            # human readable
curiosity-gpu-status --json     # for scripts
curiosity-gpu-status --require-gpu   # exit 3 unless ready
```

The output contains vendor names, PCI ids and driver names only. Serial numbers,
UUIDs and addresses are never collected. Turn the command off with
`gpu_status_cli_enabled: false`.

## Physical hosts and VMs

- Physical host: the driver and runtime are installed on the host.
- VM with passthrough: the guest sees a real PCI device and behaves like a
  physical host; install the driver in the guest. The hypervisor needs IOMMU
  and the device bound to the passthrough driver, which is outside this role.
- VM without passthrough, or a virtual GPU slice: `curiosity-gpu-status` reports
  `virtual-only` (or the vendor's vGPU driver state). Compute is not expected.
- Containers: the host provides the kernel driver; the container image provides
  user-space libraries, and the runtime must expose the device nodes.

## What CI covers and what it cannot

CI has no GPU. `tests/test_gpu_status.py` covers the no-GPU path and every state
with fixture trees that imitate `/sys`, `/dev` and `PATH` for NVIDIA, AMD, Intel,
virtual adapters, `nouveau`, missing tools and mixed hosts. It also asserts the
script contains no network or install code.

Physical-GPU validation is a manual checklist, to be run once per vendor on real
hardware and recorded (date, host model, driver version) in this file:

1. `curiosity-gpu-status --require-gpu` exits 0.
2. The vendor tool lists the device (`nvidia-smi`, `rocminfo`, `sycl-ls`).
3. A small framework workload runs on the device in a project environment.
4. After a role run with no GPU variables changed, steps 1-3 still pass (the role
   must not disturb the driver).

No physical validation has been recorded yet: it needs a person with a host for
each vendor. Open items: a per-vendor project-environment recipe, and a decision
on whether to offer an opt-in, documented-only install helper for one vendor.
