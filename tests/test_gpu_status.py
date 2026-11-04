"""curiosity-gpu-status: the no-GPU path plus fixture trees for every state.

Fixture trees imitate /sys, /dev and PATH, so every state is testable on a CI
runner that has no GPU. Physical-GPU validation is a documented manual step
(docs/gpu-accelerators.md).
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "roles/developer_workstation/files/curiosity_gpu_status.py"


def pci(root: Path, addr: str, vendor: str, device: str, driver: str = "", cls: str = "0x030000") -> None:
    d = root / "sys/bus/pci/devices" / addr
    d.mkdir(parents=True)
    (d / "vendor").write_text(vendor + "\n")
    (d / "device").write_text(device + "\n")
    (d / "class").write_text(cls + "\n")
    if driver:
        target = root / "sys/bus/pci/drivers" / driver
        target.mkdir(parents=True, exist_ok=True)
        (d / "driver").symlink_to(target)


def tool(root: Path, name: str) -> Path:
    bindir = root / "fakebin"
    bindir.mkdir(exist_ok=True)
    (bindir / name).write_text("#!/bin/sh\nexit 0\n")
    (bindir / name).chmod(0o755)
    return bindir


def node(root: Path, rel: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("")


def report(root: Path, extra=(), path: str | None = None):
    path = path if path is not None else str(root / "fakebin")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--json", "--root", str(root), "--path", path, *extra],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result, json.loads(result.stdout)


def test_no_gpu_path_is_a_normal_state(tmp_path: Path) -> None:
    result, data = report(tmp_path)
    assert result.returncode == 0
    assert data["state"] == "none"
    assert data["devices"] == []
    assert "normal state" in data["message"]


def test_the_real_host_never_crashes_and_exits_zero() -> None:
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0
    assert result.stdout.startswith("GPU status: ")


def test_require_gpu_gates_on_ready(tmp_path: Path) -> None:
    result, data = report(tmp_path, ["--require-gpu"])
    assert result.returncode == 3
    assert data["state"] == "none"


def test_virtual_adapter_in_a_vm(tmp_path: Path) -> None:
    pci(tmp_path, "0000:00:02.0", "0x1af4", "0x1050", driver="virtio-pci")
    node(tmp_path, "proc/cpuinfo")
    (tmp_path / "proc/cpuinfo").write_text("flags : fpu vme hypervisor lm\n")
    _, data = report(tmp_path)
    assert data["state"] == "virtual-only"
    assert data["virtual_machine"] is True
    assert "pass the device through" in data["message"]


def test_nvidia_without_driver_needs_driver_and_names_the_doc(tmp_path: Path) -> None:
    pci(tmp_path, "0000:01:00.0", "0x10de", "0x2204")
    _, data = report(tmp_path)
    assert data["state"] == "needs-driver"
    assert "docs/gpu-accelerators.md" in data["message"]
    assert "does not do it" in data["message"]


def test_nouveau_is_not_treated_as_compute_ready(tmp_path: Path) -> None:
    pci(tmp_path, "0000:01:00.0", "0x10de", "0x2204", driver="nouveau")
    _, data = report(tmp_path)
    assert data["state"] == "needs-driver"
    assert data["devices"][0]["driver_class"] == "open-no-compute"


def test_nvidia_driver_without_tools_needs_userspace(tmp_path: Path) -> None:
    pci(tmp_path, "0000:01:00.0", "0x10de", "0x2204", driver="nvidia")
    node(tmp_path, "dev/nvidiactl")
    _, data = report(tmp_path, path=str(tmp_path / "empty"))
    assert data["state"] == "needs-userspace"
    assert data["devices"][0]["compute_nodes"] == ["dev/nvidiactl"]


def test_nvidia_ready(tmp_path: Path) -> None:
    pci(tmp_path, "0000:01:00.0", "0x10de", "0x2204", driver="nvidia")
    node(tmp_path, "dev/nvidiactl")
    tool(tmp_path, "nvidia-smi")
    result, data = report(tmp_path, ["--require-gpu"])
    assert result.returncode == 0
    assert data["state"] == "ready"


def test_amd_rocm_ready_and_render_nodes_listed(tmp_path: Path) -> None:
    pci(tmp_path, "0000:03:00.0", "0x1002", "0x744c", driver="amdgpu", cls="0x038000")
    node(tmp_path, "dev/kfd")
    node(tmp_path, "dev/dri/renderD128")
    tool(tmp_path, "rocminfo")
    _, data = report(tmp_path)
    assert data["state"] == "ready"
    assert data["devices"][0]["render_nodes"] == ["renderD128"]


def test_intel_igpu_needs_userspace_without_tools(tmp_path: Path) -> None:
    pci(tmp_path, "0000:00:02.0", "0x8086", "0x9a49", driver="i915")
    _, data = report(tmp_path, path=str(tmp_path / "empty"))
    assert data["state"] == "needs-userspace"


def test_best_state_wins_across_devices(tmp_path: Path) -> None:
    pci(tmp_path, "0000:00:02.0", "0x8086", "0x9a49", driver="i915")
    pci(tmp_path, "0000:01:00.0", "0x10de", "0x2204", driver="nvidia")
    tool(tmp_path, "nvidia-smi")
    _, data = report(tmp_path)
    assert data["state"] == "ready"
    assert {d["vendor"] for d in data["devices"]} == {"intel", "nvidia"}


def test_non_display_pci_devices_are_ignored(tmp_path: Path) -> None:
    pci(tmp_path, "0000:00:1f.3", "0x8086", "0x9d71", driver="snd_hda_intel", cls="0x040300")
    _, data = report(tmp_path)
    assert data["state"] == "none"


def test_output_has_no_identifiers_beyond_vendor_ids(tmp_path: Path) -> None:
    pci(tmp_path, "0000:01:00.0", "0x10de", "0x2204", driver="nvidia")
    (tmp_path / "sys/bus/pci/devices/0000:01:00.0/serial_number").write_text("SERIAL-SENTINEL-99\n")
    result, _ = report(tmp_path)
    assert "SERIAL-SENTINEL-99" not in result.stdout


def test_script_has_no_network_or_install_code() -> None:
    text = SCRIPT.read_text()
    for forbidden in ("import socket", "urllib", "requests", "subprocess", "apt-get", "pip install", "os.system"):
        assert forbidden not in text


@pytest.mark.parametrize("name", ["gpu_status_cli_enabled"])
def test_default_enables_only_the_read_only_command(name: str) -> None:
    import yaml

    defaults = yaml.safe_load((ROOT / "roles/developer_workstation/defaults/main.yml").read_text())
    assert defaults[name] is True
    assert not any("nvidia" in k and "driver" in k and defaults[k] is True for k in defaults)
