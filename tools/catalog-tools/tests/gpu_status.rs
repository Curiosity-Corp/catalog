//! End-to-end: `catalog-tools gpu-status` against fixture system roots: a
//! temporary directory holding the parts of /sys, /dev and /proc the command
//! reads, and a fixture PATH (catalog#33). CI has no GPU, so every vendor and
//! state is covered here; docs/gpu-accelerators.md has the check a person runs
//! on real hardware.

use std::fs;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};

use serde_json::Value;

const DISPLAY: &str = "0x030000";
const THREE_D: &str = "0x030200";
const ACCELERATOR: &str = "0x120000";
const NETWORK: &str = "0x020000";

/// A fixture machine: `root` stands for `/`, `bin` is the PATH.
struct Machine {
    _temp: tempfile::TempDir,
    root: PathBuf,
    bin: PathBuf,
}

impl Machine {
    fn new() -> Self {
        let temp = tempfile::tempdir().expect("temporary directory");
        let root = temp.path().join("root");
        let bin = temp.path().join("bin");
        fs::create_dir_all(&root).unwrap();
        fs::create_dir_all(&bin).unwrap();
        Self {
            _temp: temp,
            root,
            bin,
        }
    }

    /// A PCI device at `slot` with its class and ids, and its bound driver.
    fn pci(&self, slot: &str, class: &str, vendor: &str, device: &str, driver: Option<&str>) {
        let dir = self.root.join("sys/bus/pci/devices").join(slot);
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("class"), format!("{class}\n")).unwrap();
        fs::write(dir.join("vendor"), format!("0x{vendor}\n")).unwrap();
        fs::write(dir.join("device"), format!("0x{device}\n")).unwrap();
        if let Some(driver) = driver {
            // As in sysfs: a link into the bus's drivers folder.
            symlink(
                format!("../../../bus/pci/drivers/{driver}"),
                dir.join("driver"),
            )
            .unwrap();
        }
    }

    /// The device's DRM render node, in its sysfs folder and under /dev/dri.
    fn render_node(&self, slot: &str, name: &str) {
        let drm = self
            .root
            .join("sys/bus/pci/devices")
            .join(slot)
            .join("drm")
            .join(name);
        fs::create_dir_all(drm).unwrap();
        self.file(&format!("dev/dri/{name}"), "");
    }

    fn file(&self, path: &str, text: &str) {
        let path = self.root.join(path);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, text).unwrap();
    }

    fn tool(&self, dir: &Path, name: &str) {
        fs::create_dir_all(dir).unwrap();
        let path = dir.join(name);
        fs::write(&path, "#!/bin/sh\necho must-not-run >&2\nexit 99\n").unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
    }

    fn run(&self, args: &[&str]) -> Output {
        Command::new(env!("CARGO_BIN_EXE_catalog-tools"))
            .arg("gpu-status")
            .arg("--sysroot")
            .arg(&self.root)
            .args(args)
            .env_clear()
            .env("PATH", &self.bin)
            .stdin(Stdio::null())
            .output()
            .expect("run catalog-tools")
    }

    fn json(&self) -> Value {
        let output = self.run(&["--json"]);
        assert!(
            output.status.success(),
            "gpu-status --json failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        serde_json::from_slice(&output.stdout).expect("gpu-status prints JSON")
    }

    fn text(&self) -> String {
        let output = self.run(&[]);
        assert!(output.status.success());
        String::from_utf8(output.stdout).unwrap()
    }

    fn require_gpu(&self) -> Option<i32> {
        self.run(&["--require-gpu"]).status.code()
    }
}

fn nvidia_with_driver(machine: &Machine) {
    machine.pci("0000:01:00.0", DISPLAY, "10de", "2484", Some("nvidia"));
    machine.file("sys/module/nvidia/version", "550.54.14\n");
    machine.file("dev/nvidiactl", "");
    machine.render_node("0000:01:00.0", "renderD128");
}

fn amd_with_driver(machine: &Machine) {
    machine.pci("0000:03:00.0", DISPLAY, "1002", "744c", Some("amdgpu"));
    machine.file("dev/kfd", "");
    machine.render_node("0000:03:00.0", "renderD128");
}

fn intel_with_driver(machine: &Machine, driver: &str) {
    machine.pci("0000:00:02.0", DISPLAY, "8086", "a7a0", Some(driver));
    machine.render_node("0000:00:02.0", "renderD128");
}

fn detail(report: &Value, index: usize) -> &str {
    report["devices"][index]["detail"].as_str().unwrap()
}

#[test]
fn no_pci_bus_is_the_none_state() {
    let machine = Machine::new();
    let report = machine.json();
    assert_eq!(report["schema"], "curiosity.workstation.gpu-status/v1");
    assert_eq!(report["state"], "none");
    assert_eq!(report["virtual_machine"], false);
    assert_eq!(report["devices"], Value::Array(Vec::new()));
    assert!(
        report["message"]
            .as_str()
            .unwrap()
            .contains("nothing needs doing")
    );
    let text = machine.text();
    assert!(text.starts_with("GPU status: none\n"), "{text}");
    assert!(text.contains("Nothing was changed or sent"), "{text}");
    assert_eq!(machine.require_gpu(), Some(1));
}

#[test]
fn devices_that_are_not_gpus_are_ignored() {
    let machine = Machine::new();
    machine.pci("0000:02:00.0", NETWORK, "8086", "15f3", Some("igc"));
    // An Intel NPU is a processing accelerator, not a GPU.
    machine.pci("0000:00:0b.0", ACCELERATOR, "8086", "7d1d", Some("intel_vpu"));
    machine.file("proc/cpuinfo", "processor\t: 0\nflags\t\t: fpu vme sse2\n");
    let report = machine.json();
    assert_eq!(report["state"], "none");
    assert_eq!(report["devices"].as_array().unwrap().len(), 0);
}

#[test]
fn nvidia_with_driver_and_nvidia_smi_is_ready() {
    let machine = Machine::new();
    nvidia_with_driver(&machine);
    machine.tool(&machine.bin, "nvidia-smi");
    let report = machine.json();
    assert_eq!(report["state"], "ready");
    let device = &report["devices"][0];
    assert_eq!(device["slot"], "0000:01:00.0");
    assert_eq!(device["pci_id"], "10de:2484");
    assert_eq!(device["vendor"], "NVIDIA");
    assert_eq!(device["kind"], "gpu");
    assert_eq!(device["kernel_driver"], "nvidia");
    assert_eq!(device["driver_version"], "550.54.14");
    assert_eq!(
        device["compute_nodes"],
        serde_json::json!(["/dev/nvidiactl", "/dev/dri/renderD128"])
    );
    assert_eq!(
        device["tools"][0]["path"],
        machine.bin.join("nvidia-smi").display().to_string()
    );
    assert_eq!(machine.require_gpu(), Some(0));
    let text = machine.text();
    assert!(text.contains("kernel driver: nvidia 550.54.14"), "{text}");
    assert!(text.contains("tools: nvidia-smi"), "{text}");
}

#[test]
fn nvidia_without_its_tools_needs_userspace() {
    let machine = Machine::new();
    nvidia_with_driver(&machine);
    let report = machine.json();
    assert_eq!(report["state"], "needs-userspace");
    assert_eq!(report["devices"][0]["tools"][0]["path"], Value::Null);
    assert!(detail(&report, 0).contains("nvidia-smi"));
    assert_eq!(machine.require_gpu(), Some(1));
}

#[test]
fn nvidia_on_nouveau_needs_the_nvidia_driver() {
    let machine = Machine::new();
    machine.pci("0000:01:00.0", DISPLAY, "10de", "2484", Some("nouveau"));
    machine.render_node("0000:01:00.0", "renderD128");
    machine.tool(&machine.bin, "nvidia-smi");
    let report = machine.json();
    assert_eq!(report["state"], "needs-driver");
    assert!(detail(&report, 0).contains("nouveau"));
    assert!(detail(&report, 0).contains("no CUDA"));
}

#[test]
fn nvidia_driver_without_its_control_node_needs_driver() {
    let machine = Machine::new();
    machine.pci("0000:01:00.0", THREE_D, "10de", "20b0", Some("nvidia"));
    machine.tool(&machine.bin, "nvidia-smi");
    let report = machine.json();
    assert_eq!(report["state"], "needs-driver");
    assert!(detail(&report, 0).contains("/dev/nvidiactl is missing"));
}

#[test]
fn a_gpu_without_a_driver_needs_driver() {
    let machine = Machine::new();
    machine.pci("0000:01:00.0", DISPLAY, "10de", "2484", None);
    let report = machine.json();
    assert_eq!(report["state"], "needs-driver");
    assert_eq!(report["devices"][0]["kernel_driver"], Value::Null);
    assert!(detail(&report, 0).contains("no kernel driver is bound"));
    assert!(machine.text().contains("kernel driver: none"));
}

#[test]
fn a_gpu_reserved_for_a_virtual_machine_says_so() {
    let machine = Machine::new();
    machine.pci("0000:01:00.0", DISPLAY, "10de", "2484", Some("vfio-pci"));
    let report = machine.json();
    assert_eq!(report["state"], "needs-driver");
    assert!(detail(&report, 0).contains("vfio-pci"));
}

#[test]
fn amd_with_rocm_in_opt_rocm_is_ready() {
    let machine = Machine::new();
    amd_with_driver(&machine);
    // ROCm installs to /opt/rocm/bin, which is often not on PATH.
    let rocm = machine.root.join("opt/rocm/bin");
    machine.tool(&rocm, "rocminfo");
    let report = machine.json();
    assert_eq!(report["state"], "ready");
    let device = &report["devices"][0];
    assert_eq!(device["vendor"], "AMD");
    assert_eq!(
        device["compute_nodes"],
        serde_json::json!(["/dev/kfd", "/dev/dri/renderD128"])
    );
    assert_eq!(device["driver_version"], Value::Null);
    assert_eq!(
        device["tools"][0]["path"],
        rocm.join("rocminfo").display().to_string()
    );
}

#[test]
fn amd_without_the_kfd_needs_driver() {
    let machine = Machine::new();
    machine.pci("0000:03:00.0", DISPLAY, "1002", "744c", Some("amdgpu"));
    machine.render_node("0000:03:00.0", "renderD128");
    machine.tool(&machine.bin, "rocminfo");
    let report = machine.json();
    assert_eq!(report["state"], "needs-driver");
    assert!(detail(&report, 0).contains("/dev/kfd is missing"));
}

#[test]
fn amd_without_a_render_node_needs_driver() {
    let machine = Machine::new();
    machine.pci("0000:03:00.0", ACCELERATOR, "1002", "740c", Some("amdgpu"));
    machine.file("dev/kfd", "");
    machine.tool(&machine.bin, "rocm-smi");
    let report = machine.json();
    assert_eq!(report["state"], "needs-driver");
    assert!(detail(&report, 0).contains("no render node"));
}

#[test]
fn amd_on_the_legacy_radeon_driver_needs_amdgpu() {
    let machine = Machine::new();
    machine.pci("0000:03:00.0", DISPLAY, "1002", "6798", Some("radeon"));
    let report = machine.json();
    assert_eq!(report["state"], "needs-driver");
    assert!(detail(&report, 0).contains("radeon is the legacy driver"));
}

#[test]
fn amd_with_driver_but_no_rocm_needs_userspace() {
    let machine = Machine::new();
    amd_with_driver(&machine);
    let report = machine.json();
    assert_eq!(report["state"], "needs-userspace");
    assert!(detail(&report, 0).contains("ROCm"));
}

#[test]
fn intel_on_i915_with_clinfo_is_ready() {
    let machine = Machine::new();
    intel_with_driver(&machine, "i915");
    machine.tool(&machine.bin, "clinfo");
    let report = machine.json();
    assert_eq!(report["state"], "ready");
    assert_eq!(report["devices"][0]["vendor"], "Intel");
    assert_eq!(
        report["devices"][0]["compute_nodes"],
        serde_json::json!(["/dev/dri/renderD128"])
    );
}

#[test]
fn intel_on_xe_without_a_runtime_needs_userspace() {
    let machine = Machine::new();
    intel_with_driver(&machine, "xe");
    let report = machine.json();
    assert_eq!(report["state"], "needs-userspace");
    assert!(detail(&report, 0).contains("compute runtime"));
}

#[test]
fn a_virtual_adapter_in_a_vm_is_virtual_only() {
    let machine = Machine::new();
    machine.pci("0000:00:01.0", DISPLAY, "1af4", "1050", Some("virtio-pci"));
    machine.file(
        "proc/cpuinfo",
        "processor\t: 0\nflags\t\t: fpu sse2 hypervisor lahf_lm\n",
    );
    let report = machine.json();
    assert_eq!(report["state"], "virtual-only");
    assert_eq!(report["virtual_machine"], true);
    assert_eq!(report["devices"][0]["kind"], "virtual");
    assert_eq!(report["devices"][0]["vendor"], "virtio");
    let text = machine.text();
    assert!(text.contains("This machine is a virtual machine."), "{text}");
    assert!(text.contains("tools: not applicable"), "{text}");
}

#[test]
fn a_server_management_adapter_is_unsupported() {
    let machine = Machine::new();
    machine.pci("0000:05:00.0", DISPLAY, "1a03", "2000", Some("ast"));
    let report = machine.json();
    assert_eq!(report["state"], "unsupported");
    assert_eq!(report["devices"][0]["kind"], "display-only");
    assert!(
        report["devices"][0]["vendor"]
            .as_str()
            .unwrap()
            .contains("ASPEED")
    );
    assert!(
        report["message"]
            .as_str()
            .unwrap()
            .contains("supports NVIDIA, AMD and Intel")
    );
}

#[test]
fn an_unknown_vendor_is_named_by_its_id() {
    let machine = Machine::new();
    machine.pci("0000:05:00.0", DISPLAY, "abcd", "0001", None);
    let report = machine.json();
    assert_eq!(report["state"], "unsupported");
    assert_eq!(report["devices"][0]["vendor"], "unknown vendor abcd");
}

#[test]
fn the_most_capable_gpu_decides_a_mixed_machine() {
    let machine = Machine::new();
    intel_with_driver(&machine, "i915");
    machine.tool(&machine.bin, "sycl-ls");
    machine.pci("0000:01:00.0", DISPLAY, "10de", "25a2", Some("nouveau"));
    let report = machine.json();
    assert_eq!(report["state"], "ready");
    let devices = report["devices"].as_array().unwrap();
    assert_eq!(devices.len(), 2);
    // Sorted by PCI slot.
    assert_eq!(devices[0]["vendor"], "Intel");
    assert_eq!(devices[0]["state"], "ready");
    assert_eq!(devices[1]["vendor"], "NVIDIA");
    assert_eq!(devices[1]["state"], "needs-driver");
}

#[test]
fn the_vendor_tools_are_found_but_never_run() {
    let machine = Machine::new();
    nvidia_with_driver(&machine);
    // The fixture tool exits 99 and prints to stderr; a run would show.
    machine.tool(&machine.bin, "nvidia-smi");
    let output = machine.run(&[]);
    assert!(output.status.success());
    assert!(output.stderr.is_empty(), "{:?}", output.stderr);
    assert!(!String::from_utf8_lossy(&output.stdout).contains("must-not-run"));
}

#[test]
fn a_tool_that_is_not_executable_does_not_count() {
    let machine = Machine::new();
    intel_with_driver(&machine, "i915");
    fs::write(machine.bin.join("clinfo"), "not a program").unwrap();
    let report = machine.json();
    assert_eq!(report["state"], "needs-userspace");
}
