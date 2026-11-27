//! `catalog-tools gpu-status`: which accelerator this machine has, whether its
//! kernel driver and the vendor's user-space tools are present, and what to do
//! when they are not (catalog#33, docs/gpu-accelerators.md).
//!
//! Read-only by construction: it lists the PCI devices the kernel exposes under
//! `/sys/bus/pci/devices`, follows their `driver` links, looks for the compute
//! device nodes under `/dev`, reads `/proc/cpuinfo` for the hypervisor flag and
//! checks whether the vendor tools are executable files on `PATH`. It starts no
//! program (a hung driver cannot hang it), writes nothing and opens no network
//! connection. The output holds vendor names, PCI ids, PCI slots, driver names
//! and versions only: no serial number, UUID or user data.
//!
//! Every path is read under a system root (`/` on a real machine), so the tests
//! run the same code against fixture trees (tests/gpu_status.rs).

use std::ffi::OsStr;
use std::fs;
use std::io::ErrorKind;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::Serialize;

/// The JSON document's schema name.
pub const SCHEMA: &str = "curiosity.workstation.gpu-status/v1";

/// Where vendors install their tools outside `PATH`, read under the system root.
const VENDOR_TOOL_DIRS: &[&str] = &["opt/rocm/bin", "usr/local/cuda/bin"];

/// The longest driver version string reported; anything longer is not a version.
const VERSION_LIMIT: usize = 64;

/// The accelerator vendors this catalog describes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Vendor {
    Nvidia,
    Amd,
    Intel,
}

impl Vendor {
    fn from_pci(vendor_id: &str) -> Option<Self> {
        match vendor_id {
            "10de" => Some(Self::Nvidia),
            "1002" => Some(Self::Amd),
            "8086" => Some(Self::Intel),
            _ => None,
        }
    }

    /// The name people know the vendor by.
    pub fn label(self) -> &'static str {
        match self {
            Self::Nvidia => "NVIDIA",
            Self::Amd => "AMD",
            Self::Intel => "Intel",
        }
    }

    /// The vendor's user-space tools; any one of them counts.
    fn tools(self) -> &'static [&'static str] {
        match self {
            Self::Nvidia => &["nvidia-smi"],
            Self::Amd => &["rocminfo", "rocm-smi", "amd-smi"],
            Self::Intel => &["sycl-ls", "clinfo", "xpu-smi"],
        }
    }

    /// The driver whose absence a person should fix, for the message.
    fn driver_hint(self) -> &'static str {
        match self {
            Self::Nvidia => "NVIDIA's driver (the `nvidia` kernel module)",
            Self::Amd => "the `amdgpu` kernel driver with ROCm's KFD (`/dev/kfd`)",
            Self::Intel => "the `i915` or `xe` kernel driver",
        }
    }

    fn userspace_hint(self) -> &'static str {
        match self {
            Self::Nvidia => "NVIDIA's user-space driver package, which provides nvidia-smi",
            Self::Amd => "ROCm, which provides rocminfo and rocm-smi",
            Self::Intel => "Intel's compute runtime (Level Zero or OpenCL) with clinfo or sycl-ls",
        }
    }
}

/// Adapters a hypervisor presents: they draw a screen and cannot run compute.
fn virtual_adapter(vendor_id: &str) -> Option<&'static str> {
    match vendor_id {
        "1af4" => Some("virtio"),
        "15ad" => Some("VMware"),
        "1234" => Some("QEMU standard VGA"),
        "1b36" => Some("QXL"),
        "1414" => Some("Hyper-V"),
        "80ee" => Some("VirtualBox"),
        "1013" => Some("Cirrus (emulated)"),
        _ => None,
    }
}

/// Display-only adapters common on servers; named so the message is clear.
fn display_only_adapter(vendor_id: &str) -> Option<&'static str> {
    match vendor_id {
        "1a03" => Some("ASPEED (server management controller)"),
        "102b" => Some("Matrox"),
        _ => None,
    }
}

/// What a device, or the machine as a whole, is ready for. The order is the
/// machine's precedence: the most capable device decides its state.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum State {
    /// No display adapter or accelerator at all.
    None,
    /// Only adapters a hypervisor presents.
    VirtualOnly,
    /// Only display adapters this catalog has no compute support for.
    Unsupported,
    /// A supported GPU without a compute-capable kernel driver or device node.
    NeedsDriver,
    /// The driver is bound, the vendor's user-space tools are missing.
    NeedsUserspace,
    /// Driver, device node and a vendor tool are all present.
    Ready,
}

impl State {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::None => "none",
            Self::VirtualOnly => "virtual-only",
            Self::Unsupported => "unsupported",
            Self::NeedsDriver => "needs-driver",
            Self::NeedsUserspace => "needs-userspace",
            Self::Ready => "ready",
        }
    }

    /// What the state means for a person, and what to do about it.
    pub fn guidance(self) -> &'static str {
        match self {
            Self::None => {
                "No GPU or accelerator was found. That is normal for servers, CI and \
                 virtual machines without a passed-through GPU; nothing needs doing."
            }
            Self::VirtualOnly => {
                "Only a virtual display adapter is visible, so this virtual machine has no GPU \
                 for compute. To use one, pass a physical GPU through at the hypervisor and run \
                 this command again inside the virtual machine."
            }
            Self::Unsupported => {
                "Only display adapters without compute support were found (for example a \
                 server's management controller). This catalog supports NVIDIA, AMD and \
                 Intel GPUs; this machine has none, so GPU tooling does not apply here."
            }
            Self::NeedsDriver => {
                "A supported GPU is present but its compute-capable kernel driver is not \
                 loaded. Install the host driver from your distribution or the vendor (this \
                 catalog installs none by default), reboot if the vendor says so, and run \
                 this command again. See docs/gpu-accelerators.md."
            }
            Self::NeedsUserspace => {
                "The kernel driver is loaded but the vendor's user-space tools are missing. \
                 Install the vendor runtime on the host, or run projects in a container or \
                 environment that provides it. See docs/gpu-accelerators.md."
            }
            Self::Ready => {
                "A GPU, its kernel driver, its compute device node and a vendor tool are all \
                 present. Validate it with a real workload (docs/gpu-accelerators.md)."
            }
        }
    }
}

/// What kind of adapter a device is.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum Kind {
    Gpu,
    Virtual,
    DisplayOnly,
}

/// A vendor tool and where it was found.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Tool {
    pub name: String,
    /// The executable file, or none when it is not installed.
    pub path: Option<String>,
}

/// One display adapter or accelerator.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Device {
    /// The PCI slot, for example `0000:01:00.0`.
    pub slot: String,
    /// `vendor:device`, the ids `lspci -nn` prints.
    pub pci_id: String,
    pub vendor: String,
    pub kind: Kind,
    pub kernel_driver: Option<String>,
    pub driver_version: Option<String>,
    /// The compute device nodes that exist (`/dev/nvidiactl`, `/dev/kfd`, `/dev/dri/renderD128`).
    pub compute_nodes: Vec<String>,
    pub tools: Vec<Tool>,
    pub state: State,
    /// Why the device is in its state, in one sentence.
    pub detail: String,
}

/// The whole machine.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Report {
    pub schema: &'static str,
    pub state: State,
    pub message: &'static str,
    pub virtual_machine: bool,
    pub devices: Vec<Device>,
}

/// Where to look: the system root and the directories searched for tools.
pub struct Inputs {
    pub sysroot: PathBuf,
    /// `PATH` as given, searched as is; the vendor directories are added under `sysroot`.
    pub path: Option<std::ffi::OsString>,
}

/// Reads the machine under `inputs.sysroot` and assesses every device.
pub fn report(inputs: &Inputs) -> Result<Report> {
    let search = search_dirs(inputs);
    let devices = pci_devices(&inputs.sysroot)?
        .into_iter()
        .filter_map(|slot| assess(&inputs.sysroot, &slot, &search).transpose())
        .collect::<Result<Vec<_>>>()?;
    let state = devices
        .iter()
        .map(|device| device.state)
        .max()
        .unwrap_or(State::None);
    Ok(Report {
        schema: SCHEMA,
        state,
        message: state.guidance(),
        virtual_machine: in_virtual_machine(&inputs.sysroot),
        devices,
    })
}

fn search_dirs(inputs: &Inputs) -> Vec<PathBuf> {
    let mut dirs: Vec<PathBuf> = inputs
        .path
        .as_deref()
        .map(|path| std::env::split_paths(path).collect())
        .unwrap_or_default();
    dirs.extend(VENDOR_TOOL_DIRS.iter().map(|dir| inputs.sysroot.join(dir)));
    dirs
}

/// The PCI slots under `/sys/bus/pci/devices`, sorted. A machine without a PCI
/// bus (some ARM boards, some containers) has none.
fn pci_devices(sysroot: &Path) -> Result<Vec<PathBuf>> {
    let base = sysroot.join("sys/bus/pci/devices");
    let entries = match fs::read_dir(&base) {
        Ok(entries) => entries,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => {
            return Err(error).with_context(|| format!("cannot list {}", base.display()));
        }
    };
    let mut slots = entries
        .map(|entry| entry.map(|entry| entry.path()))
        .collect::<std::io::Result<Vec<_>>>()
        .with_context(|| format!("cannot list {}", base.display()))?;
    slots.sort();
    Ok(slots)
}

/// A sysfs attribute, lowercased, without a leading `0x`; none when unreadable.
fn attribute(dir: &Path, name: &str) -> Option<String> {
    let text = fs::read_to_string(dir.join(name)).ok()?;
    let value = text.trim().to_ascii_lowercase();
    let value = value.strip_prefix("0x").unwrap_or(&value).to_string();
    (!value.is_empty()).then_some(value)
}

/// Whether a PCI class is one this command reports: display controllers
/// (`03xxxx`) of any vendor, and processing accelerators (`12xxxx`) from
/// NVIDIA and AMD (data-centre parts). Intel's `12xxxx` devices are NPUs,
/// which are not GPUs.
fn reported_class(class: &str, vendor: Option<Vendor>) -> bool {
    class.starts_with("03")
        || (class.starts_with("12") && matches!(vendor, Some(Vendor::Nvidia | Vendor::Amd)))
}

fn assess(sysroot: &Path, dir: &Path, search: &[PathBuf]) -> Result<Option<Device>> {
    let Some(class) = attribute(dir, "class") else {
        return Ok(None);
    };
    let vendor_id = attribute(dir, "vendor").unwrap_or_default();
    let vendor = Vendor::from_pci(&vendor_id);
    if !reported_class(&class, vendor) {
        return Ok(None);
    }
    let device_id = attribute(dir, "device").unwrap_or_default();
    let slot = dir
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default();
    let kernel_driver = fs::read_link(dir.join("driver")).ok().and_then(|target| {
        target
            .file_name()
            .map(|name| name.to_string_lossy().into_owned())
    });
    let mut device = Device {
        slot,
        pci_id: format!("{vendor_id}:{device_id}"),
        vendor: String::new(),
        kind: Kind::Gpu,
        driver_version: kernel_driver
            .as_deref()
            .and_then(|driver| driver_version(sysroot, driver)),
        kernel_driver,
        compute_nodes: Vec::new(),
        tools: Vec::new(),
        state: State::None,
        detail: String::new(),
    };
    let Some(vendor) = vendor else {
        if let Some(name) = virtual_adapter(&vendor_id) {
            device.vendor = name.to_string();
            device.kind = Kind::Virtual;
            device.state = State::VirtualOnly;
            device.detail = "a virtual adapter: it draws a screen and cannot run compute".into();
        } else {
            device.vendor = display_only_adapter(&vendor_id)
                .map_or_else(|| format!("unknown vendor {vendor_id}"), str::to_string);
            device.kind = Kind::DisplayOnly;
            device.state = State::Unsupported;
            device.detail =
                "not an NVIDIA, AMD or Intel GPU, so this catalog has no compute support for it"
                    .into();
        }
        return Ok(Some(device));
    };
    device.vendor = vendor.label().to_string();
    device.compute_nodes = compute_nodes(sysroot, dir, vendor);
    device.tools = vendor
        .tools()
        .iter()
        .map(|name| Tool {
            name: (*name).to_string(),
            path: find_tool(name, search).map(|path| path.display().to_string()),
        })
        .collect();
    let (state, detail) = judge(vendor, &device);
    device.state = state;
    device.detail = detail;
    Ok(Some(device))
}

/// The driver's version from `/sys/module/<driver>/version`, when it has a
/// plain one (NVIDIA's does; in-tree drivers usually have none).
fn driver_version(sysroot: &Path, driver: &str) -> Option<String> {
    let text = fs::read_to_string(sysroot.join("sys/module").join(driver).join("version")).ok()?;
    let version = text.trim();
    let plain = !version.is_empty()
        && version.len() <= VERSION_LIMIT
        && version
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | '_' | '+'));
    plain.then(|| version.to_string())
}

/// The compute device nodes present for this device: NVIDIA's control node,
/// AMD's KFD, and the device's own DRM render node (from its `drm/` folder).
fn compute_nodes(sysroot: &Path, dir: &Path, vendor: Vendor) -> Vec<String> {
    let mut nodes = Vec::new();
    let shared = match vendor {
        Vendor::Nvidia => Some("dev/nvidiactl"),
        Vendor::Amd => Some("dev/kfd"),
        Vendor::Intel => None,
    };
    if let Some(node) = shared
        && sysroot.join(node).exists()
    {
        nodes.push(format!("/{node}"));
    }
    if let Ok(entries) = fs::read_dir(dir.join("drm")) {
        let mut render: Vec<String> = entries
            .filter_map(|entry| entry.ok())
            .map(|entry| entry.file_name().to_string_lossy().into_owned())
            .filter(|name| name.starts_with("renderD"))
            .filter(|name| sysroot.join("dev/dri").join(name).exists())
            .map(|name| format!("/dev/dri/{name}"))
            .collect();
        render.sort();
        nodes.extend(render);
    }
    nodes
}

/// The first executable regular file called `name` in `search`.
fn find_tool(name: &str, search: &[PathBuf]) -> Option<PathBuf> {
    search
        .iter()
        .filter(|dir| !dir.as_os_str().is_empty())
        .map(|dir| dir.join(OsStr::new(name)))
        .find(|path| {
            fs::metadata(path)
                .map(|meta| meta.is_file() && meta.permissions().mode() & 0o111 != 0)
                .unwrap_or(false)
        })
}

/// The state of a supported GPU and the one-sentence reason.
fn judge(vendor: Vendor, device: &Device) -> (State, String) {
    let Some(driver) = device.kernel_driver.as_deref() else {
        return (
            State::NeedsDriver,
            format!(
                "no kernel driver is bound; install {}",
                vendor.driver_hint()
            ),
        );
    };
    let compute_driver = match (vendor, driver) {
        (Vendor::Nvidia, "nvidia") | (Vendor::Amd, "amdgpu") | (Vendor::Intel, "i915" | "xe") => {
            true
        }
        (_, "vfio-pci") => {
            return (
                State::NeedsDriver,
                "bound to vfio-pci, so it is reserved for a virtual machine; run this command \
                 inside that virtual machine"
                    .into(),
            );
        }
        _ => false,
    };
    if !compute_driver {
        let why = match driver {
            "nouveau" => "nouveau drives the display but has no CUDA support",
            "radeon" => "radeon is the legacy driver and has no ROCm support",
            _ => "it is not a compute-capable driver",
        };
        return (
            State::NeedsDriver,
            format!(
                "kernel driver {driver} is bound: {why}; install {}",
                vendor.driver_hint()
            ),
        );
    }
    // CUDA needs NVIDIA's control node; ROCm needs the KFD and the device's
    // render node; Level Zero and OpenCL need the render node.
    let (shared, render) = match vendor {
        Vendor::Nvidia => (Some("/dev/nvidiactl"), false),
        Vendor::Amd => (Some("/dev/kfd"), true),
        Vendor::Intel => (None, true),
    };
    if let Some(node) = shared
        && !device.compute_nodes.iter().any(|have| have == node)
    {
        return (
            State::NeedsDriver,
            format!("{driver} is bound but {node} is missing, so no compute device is exposed"),
        );
    }
    if render
        && !device
            .compute_nodes
            .iter()
            .any(|node| node.starts_with("/dev/dri/renderD"))
    {
        return (
            State::NeedsDriver,
            format!("{driver} is bound but the device has no render node under /dev/dri"),
        );
    }
    if device.tools.iter().all(|tool| tool.path.is_none()) {
        let names: Vec<&str> = device.tools.iter().map(|tool| tool.name.as_str()).collect();
        return (
            State::NeedsUserspace,
            format!(
                "{driver} is loaded but none of {} is installed; install {}",
                names.join(", "),
                vendor.userspace_hint()
            ),
        );
    }
    let found: Vec<&str> = device
        .tools
        .iter()
        .filter(|tool| tool.path.is_some())
        .map(|tool| tool.name.as_str())
        .collect();
    (
        State::Ready,
        format!(
            "{driver} is loaded with {} and {} is installed",
            device.compute_nodes.join(", "),
            found.join(", ")
        ),
    )
}

/// Whether the CPU reports the `hypervisor` flag (this is a virtual machine).
fn in_virtual_machine(sysroot: &Path) -> bool {
    fs::read_to_string(sysroot.join("proc/cpuinfo"))
        .map(|text| {
            text.lines()
                .filter(|line| line.starts_with("flags"))
                .any(|line| line.split_whitespace().any(|flag| flag == "hypervisor"))
        })
        .unwrap_or(false)
}

/// The report for a person.
pub fn render(report: &Report) -> String {
    let mut out = format!(
        "GPU status: {}\n{}\n",
        report.state.as_str(),
        report.message
    );
    if report.virtual_machine {
        out.push_str("This machine is a virtual machine.\n");
    }
    for device in &report.devices {
        let driver = match (&device.kernel_driver, &device.driver_version) {
            (Some(driver), Some(version)) => format!("{driver} {version}"),
            (Some(driver), None) => driver.clone(),
            (None, _) => "none".to_string(),
        };
        let tools: Vec<&str> = device
            .tools
            .iter()
            .filter(|tool| tool.path.is_some())
            .map(|tool| tool.name.as_str())
            .collect();
        out.push_str(&format!(
            "\n{} {} [{}]\n  kernel driver: {driver}\n  compute nodes: {}\n  tools: {}\n  {}: {}\n",
            device.slot,
            device.vendor,
            device.pci_id,
            list_or_none(&device.compute_nodes),
            if device.kind == Kind::Gpu {
                list_or_none(&tools)
            } else {
                "not applicable".to_string()
            },
            device.state.as_str(),
            device.detail
        ));
    }
    out.push_str(
        "\nNothing was changed or sent: this command only reads /sys, /dev, /proc/cpuinfo and PATH.\n",
    );
    out
}

fn list_or_none<T: AsRef<str>>(items: &[T]) -> String {
    if items.is_empty() {
        "none".to_string()
    } else {
        items
            .iter()
            .map(AsRef::as_ref)
            .collect::<Vec<_>>()
            .join(", ")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_most_capable_device_decides_the_machine() {
        let mut states = [State::VirtualOnly, State::Ready, State::NeedsDriver];
        states.sort();
        assert_eq!(states.last(), Some(&State::Ready));
        assert!(State::Unsupported > State::VirtualOnly);
        assert!(State::NeedsDriver > State::Unsupported);
    }

    #[test]
    fn reports_display_controllers_and_data_centre_accelerators_only() {
        assert!(reported_class("030000", Some(Vendor::Intel)));
        assert!(reported_class("030200", Some(Vendor::Nvidia)));
        assert!(reported_class("120000", Some(Vendor::Amd)));
        assert!(!reported_class("120000", Some(Vendor::Intel)));
        assert!(!reported_class("020000", Some(Vendor::Nvidia)));
    }

    #[test]
    fn an_odd_driver_version_is_not_reported() {
        let dir = tempfile::tempdir().unwrap();
        let module = dir.path().join("sys/module/nvidia");
        fs::create_dir_all(&module).unwrap();
        fs::write(module.join("version"), "550.54.14\n").unwrap();
        assert_eq!(
            driver_version(dir.path(), "nvidia"),
            Some("550.54.14".to_string())
        );
        fs::write(module.join("version"), "550 <with spaces>\n").unwrap();
        assert_eq!(driver_version(dir.path(), "nvidia"), None);
        assert_eq!(driver_version(dir.path(), "amdgpu"), None);
    }

    #[test]
    fn every_state_has_a_name_and_guidance() {
        for state in [
            State::None,
            State::VirtualOnly,
            State::Unsupported,
            State::NeedsDriver,
            State::NeedsUserspace,
            State::Ready,
        ] {
            assert!(!state.as_str().is_empty());
            assert!(state.guidance().ends_with('.'));
            assert_eq!(
                serde_json::to_value(state).unwrap(),
                serde_json::Value::String(state.as_str().to_string())
            );
        }
    }
}
