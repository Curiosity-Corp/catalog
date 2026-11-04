#!/usr/bin/env python3
"""curiosity-gpu-status: read-only GPU / accelerator readiness report.

This command changes nothing and installs nothing. It reads the PCI list the
kernel already exposes under /sys, a few /dev nodes and the PATH, then says in
plain words which of these states the machine is in:

  none             no display or accelerator device is present at all
  virtual-only     only a virtual adapter (VM without GPU passthrough)
  needs-driver     a physical GPU is present but no usable kernel driver is bound
  needs-userspace  the driver is bound but the vendor tools/libraries are absent
  ready            driver, device node and vendor user-space tool are all present

The role never ships proprietary drivers; see docs/gpu-accelerators.md for the
boundary between host drivers, user-space libraries and project environments.
Output holds vendor names, PCI ids and driver names only: no serial numbers,
UUIDs, addresses of other machines or credentials.

Exit status is 0 for every state unless --require-gpu is given, in which case a
state other than "ready" exits 3 (so a CI job or script can gate on it).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

VENDORS = {
    "0x10de": "nvidia",
    "0x1002": "amd",
    "0x8086": "intel",
}
# Adapters a hypervisor presents. They draw a screen; they cannot run compute.
VIRTUAL_VENDORS = {
    "0x1af4": "virtio",
    "0x15ad": "vmware",
    "0x1234": "bochs/qemu-std",
    "0x1b36": "qxl",
    "0x1414": "hyper-v",
    "0x80ee": "virtualbox",
}
# PCI base class 0x03 is "display controller" (VGA, 3D, other display).
DISPLAY_CLASS_PREFIX = "0x03"

KERNEL_DRIVERS = {
    "nvidia": {"nvidia": "proprietary", "nouveau": "open-no-compute"},
    "amd": {"amdgpu": "open", "radeon": "legacy-no-compute"},
    "intel": {"i915": "open", "xe": "open"},
}
# Device nodes that prove the compute stack can reach the device.
COMPUTE_NODES = {
    "nvidia": ["dev/nvidiactl", "dev/nvidia0"],
    "amd": ["dev/kfd"],
    "intel": [],  # render nodes are checked below for every vendor
}
USERSPACE_TOOLS = {
    "nvidia": ["nvidia-smi"],
    "amd": ["rocminfo", "rocm-smi"],
    "intel": ["sycl-ls", "clinfo"],
}
GUIDANCE = {
    "none": "No GPU or display adapter was found. This is a normal state for servers and CI; GPU tooling is simply not used here.",
    "virtual-only": (
        "Only a virtual adapter is visible, so this machine has no GPU for compute. "
        "To use a physical GPU in a VM, pass the device through at the hypervisor "
        "and re-run this command inside the VM."
    ),
    "needs-driver": (
        "A physical GPU is present but no compute-capable kernel driver is bound. "
        "Install the host driver from your distribution or the vendor (this role does "
        "not do it), reboot if the vendor says so, then re-run. See docs/gpu-accelerators.md."
    ),
    "needs-userspace": (
        "The kernel driver is bound but the vendor user-space tools are not on PATH. "
        "Install the vendor runtime on the host, or run projects inside a container or "
        "environment that provides it. See docs/gpu-accelerators.md."
    ),
    "ready": "A physical GPU, its driver, a compute device node and a vendor tool are all present.",
}


def read(path: Path) -> str:
    try:
        return path.read_text(errors="replace").strip().lower()
    except OSError:
        return ""


def in_vm(root: Path) -> bool:
    for line in read(root / "proc/cpuinfo").splitlines():
        if line.startswith("flags") and " hypervisor" in line:
            return True
    return False


def scan_devices(root: Path) -> list[dict]:
    devices = []
    base = root / "sys/bus/pci/devices"
    if not base.is_dir():
        return devices
    for entry in sorted(base.iterdir()):
        pci_class = read(entry / "class")
        if not pci_class.startswith(DISPLAY_CLASS_PREFIX):
            continue
        vendor_id = read(entry / "vendor")
        driver_link = entry / "driver"
        driver = os.path.basename(os.readlink(driver_link)) if driver_link.is_symlink() else ""
        if vendor_id in VIRTUAL_VENDORS:
            vendor, kind = VIRTUAL_VENDORS[vendor_id], "virtual"
        elif vendor_id in VENDORS:
            vendor, kind = VENDORS[vendor_id], "physical"
        else:
            vendor, kind = "unknown", "physical"
        devices.append(
            {
                "pci_id": f"{vendor_id.removeprefix('0x')}:{read(entry / 'device').removeprefix('0x')}",
                "vendor": vendor,
                "kind": kind,
                "kernel_driver": driver,
                "driver_class": KERNEL_DRIVERS.get(vendor, {}).get(driver, "other" if driver else "none"),
            }
        )
    return devices


def assess_device(device: dict, root: Path, path: str) -> dict:
    vendor = device["vendor"]
    nodes = [n for n in COMPUTE_NODES.get(vendor, []) if (root / n).exists()]
    render_nodes = sorted(p.name for p in (root / "dev/dri").glob("renderD*")) if (root / "dev/dri").is_dir() else []
    tools = [t for t in USERSPACE_TOOLS.get(vendor, []) if shutil.which(t, path=path)]
    if device["kind"] == "virtual":
        state = "virtual-only"
    elif device["driver_class"] in ("none", "open-no-compute", "legacy-no-compute", "other"):
        state = "needs-driver"
    elif not tools:
        state = "needs-userspace"
    else:
        state = "ready"
    device = dict(device)
    device.update(
        compute_nodes=nodes,
        render_nodes=render_nodes if device["kind"] == "physical" else [],
        userspace_tools=tools,
        state=state,
    )
    return device


def build_report(root: Path, path: str) -> dict:
    devices = [assess_device(d, root, path) for d in scan_devices(root)]
    states = [d["state"] for d in devices]
    for candidate in ("ready", "needs-userspace", "needs-driver", "virtual-only"):
        if candidate in states:
            overall = candidate
            break
    else:
        overall = "none"
    return {
        "schema": "curiosity.gpu-status/v1",
        "state": overall,
        "virtual_machine": in_vm(root),
        "message": GUIDANCE[overall],
        "devices": devices,
    }


def render_text(report: dict) -> str:
    lines = [f"GPU status: {report['state']}", report["message"]]
    if report["virtual_machine"]:
        lines.append("This machine is a virtual machine.")
    for d in report["devices"]:
        lines.append(
            f"- {d['vendor']} {d['pci_id']} ({d['kind']}), kernel driver: {d['kernel_driver'] or 'none'}, "
            f"compute nodes: {', '.join(d['compute_nodes']) or 'none'}, "
            f"tools: {', '.join(d['userspace_tools']) or 'none'} -> {d['state']}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only GPU readiness report (changes nothing).")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--require-gpu", action="store_true", help="exit 3 unless the state is 'ready'")
    parser.add_argument("--root", default="/", help="filesystem root to inspect (for tests)")
    parser.add_argument("--path", default=os.environ.get("PATH", ""), help="PATH used to look for vendor tools")
    args = parser.parse_args(argv)
    report = build_report(Path(args.root), args.path)
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else render_text(report))
    if args.require_gpu and report["state"] != "ready":
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
