# GPU and accelerator support

The catalog installs no GPU driver, no CUDA, ROCm or oneAPI runtime, and no
vendor credential, by default or otherwise. It ships one read-only command that
tells a person which accelerator the machine has and which layer is missing:

```bash
curiosity-gpu-status                 # for a person
curiosity-gpu-status --json          # for a script (schema curiosity.workstation.gpu-status/v1)
curiosity-gpu-status --require-gpu   # exit 1 unless the state is "ready"
```

`curiosity-gpu-status` is `catalog-tools gpu-status` (catalog#33). The role
installs both when `catalog_tools_enabled` is true (see
[troubleshooting](troubleshooting.md#sending-a-maintainer-a-diagnostic-bundle)
for the release asset). `catalog_tools_gpu_status_enabled: false` removes the
short command, and `catalog_tools_gpu_status_command_path` moves it.

## The three layers

| Layer | Examples | Who provides it |
| --- | --- | --- |
| Host kernel driver and device nodes | `nvidia` with `/dev/nvidiactl`; `amdgpu` with `/dev/kfd`; `i915` or `xe`; each GPU's `/dev/dri/renderD*` | The host operator, from the distribution or the vendor. Not this catalog. |
| User-space runtime on the host | `nvidia-smi` and the CUDA user-space driver; ROCm (`rocminfo`, `rocm-smi`, `amd-smi`); Intel's compute runtime (`clinfo`, `sycl-ls`, `xpu-smi`) | The host operator, or a container image that carries it. |
| Project environment | A conda or pip environment, container or devcontainer with the project's framework build | The project. Pin the framework build to the runtime above. |

Keeping them apart means a driver upgrade on a lab host cannot silently change
a project's environment, and a project can be rebuilt on a machine whose driver
is newer than the one it was written on.

## What the command reads, and the states

It lists the PCI devices under `/sys/bus/pci/devices` (display controllers of
any vendor, and NVIDIA and AMD processing accelerators), follows each device's
`driver` link, reads a driver version from `/sys/module/<driver>/version` where
there is one, looks for the compute device nodes under `/dev`, reads the
`hypervisor` flag in `/proc/cpuinfo`, and checks whether the vendor tools are
executable files on `PATH` or in `/opt/rocm/bin` and `/usr/local/cuda/bin`.
It runs none of those tools, writes nothing and opens no connection, so a hung
driver cannot hang it. The output holds vendor names, PCI ids and slots, driver
names and versions; no serial number or UUID.

The machine's state is that of its most capable device:

| State | Meaning | What to do |
| --- | --- | --- |
| `none` | No display adapter or accelerator. Normal for servers, CI and VMs without passthrough. | Nothing. |
| `virtual-only` | Only adapters a hypervisor presents (virtio, VMware, QEMU, QXL, Hyper-V, VirtualBox, emulated Cirrus). | Pass a physical GPU through at the hypervisor and run the command inside the VM. |
| `unsupported` | Only display adapters without compute support in this catalog, such as a server's ASPEED management controller or an unknown vendor (named by its PCI id). | Nothing: GPU tooling does not apply. NVIDIA, AMD and Intel GPUs are supported. |
| `needs-driver` | A supported GPU with no driver bound, a display-only driver (`nouveau`, legacy `radeon`), `vfio-pci` (reserved for a VM), or a driver without its compute node (`/dev/nvidiactl`, `/dev/kfd`, or the render node). | Install the host driver; the device line says which. |
| `needs-userspace` | Driver and device nodes are there; no vendor tool is installed. | Install the vendor runtime on the host, or use a container that has it. |
| `ready` | Driver, compute node and at least one vendor tool are present. | Validate with a real workload (below). |

Exit status: `0` after printing the report, `1` with `--require-gpu` when the
state is not `ready`, `2` when the system root cannot be read.

## Physical hosts, VMs and containers

- Physical host: the driver and runtime are installed on the host.
- VM with passthrough: the guest sees the real PCI device and behaves like a
  physical host; install the driver in the guest. The host shows the device on
  `vfio-pci`. IOMMU and the passthrough binding are the hypervisor's, outside
  this catalog.
- VM without passthrough: `virtual-only`; compute is not expected.
- Containers: the host provides the kernel driver and device nodes; the image
  provides the user-space runtime, and the container runtime must expose the
  device nodes.

## What CI covers, and the check a person runs on real hardware

CI has no GPU. The `tools` job runs `tools/catalog-tools/tests/gpu_status.rs`,
which builds fixture `/sys`, `/dev` and `/proc` trees and a fixture `PATH` for
every state: no PCI bus and non-GPU devices only (`none`), NVIDIA, AMD and
Intel ready, each without its tools, on `nouveau`, `radeon` and `vfio-pci`,
without `/dev/nvidiactl`, `/dev/kfd` or a render node, virtual adapters in a
VM, an ASPEED and an unknown adapter, and a mixed Intel and NVIDIA machine. It
also checks that a vendor tool is found but never run. The job then runs
`catalog-tools gpu-status` on its own runner pod, a real read of whatever the
node exposes: on 2026-11-27 the node's Intel iGPU with no driver bound
(`needs-driver`). The command exits 0 whatever the state.

Validation on physical GPUs needs a person with the hardware, once per vendor
(NVIDIA, AMD, Intel) and again after a driver or catalog major change:

1. Converge the host with `catalog_tools_enabled: true`.
2. Run `curiosity-gpu-status`. The state is `ready`, and the device line names
   the GPU, its driver and the vendor tool.
3. Run the vendor tool itself (`nvidia-smi`, `rocminfo`, `sycl-ls` or
   `clinfo`): it lists the same device.
4. Run a small framework workload on the device in a project environment.
5. Converge the host again with no GPU setting changed, then repeat 2 to 4: the
   role must not disturb the driver.
6. Record the date, vendor, PCI id, driver and version (from
   `curiosity-gpu-status --json`) and the result as a note on catalog#33 or a
   follow-up issue. Leave out the host name and serial numbers.

No physical validation is recorded yet.
