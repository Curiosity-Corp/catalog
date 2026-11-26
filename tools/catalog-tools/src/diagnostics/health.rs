//! Health checks for the diagnostic bundle (catalog#18): whether the managed
//! commands are there and which version and release they are, the state of the
//! role's maintenance services, free disk space, and whether the files the
//! last update installed are still the ones it installed.
//!
//! [`gather`] reads the machine only through [`Host`]; [`render`] turns the
//! facts into bundle JSON and plain-language findings, validating every
//! structured value and passing every free-text value through the redactor.
//!
//! Every check is bounded (a capped number of items, [`super::host`]'s time
//! and output limits, a total time budget for version queries) and read-only:
//! services are only ever asked `systemctl show`, disks `df`, and a managed
//! command runs only when its contract's health check is a version query.
//! StreamStack and Sunshine are left out entirely: nothing named after them
//! is run, queried or hashed.

use std::path::{Path, PathBuf};
use std::sync::LazyLock;
use std::time::{Duration, Instant};

use regex::Regex;
use serde::Deserialize;
use serde_json::{Map, Value, json};

use super::host::{FileDigest, Host, Presence, first_line};
use super::redact::Redactor;
use super::{TAG, VERSION, WORD, checked};

/// The check list the role installs (`catalog_tools_diagnostics_checks_path`).
pub const DEFAULT_CHECKS: &str = "/etc/curiosity/diagnostics-checks.json";
/// Its format.
pub const CHECKS_SCHEMA: &str = "curiosity.workstation.diagnostics-checks/v1";
/// Days after which the last update manifest counts as stale when the check
/// list does not say otherwise.
pub const DEFAULT_STALE_AFTER_DAYS: u64 = 14;
/// Free space below which a disk is reported when the check list does not say otherwise.
pub const DEFAULT_MIN_FREE_MIB: u64 = 2048;

const MAX_COMMANDS: usize = 64;
const MAX_SERVICES: usize = 32;
const MAX_DISKS: usize = 8;
const MAX_ARTIFACTS: usize = 256;
/// Total time all version queries together may take.
const VERSION_BUDGET: Duration = Duration::from_secs(60);

/// Contract health-check arguments that only ask for a version.
const VERSION_QUERIES: [&[&str]; 7] = [
    &["--version"],
    &["-v"],
    &["-V"],
    &["version"],
    &["version", "--client"],
    &["version", "--client-only"],
    &["version", "--short"],
];
/// The only `systemctl` call the checks make.
const SYSTEMCTL_SHOW: [&str; 3] = [
    "show",
    "--no-pager",
    "--property=LoadState,ActiveState,SubState,Result",
];
const UNIT_PROPERTIES: [&str; 4] = ["LoadState", "ActiveState", "SubState", "Result"];

/// Names the checks never touch (Admiral order: StreamStack and Sunshine stay out).
const EXCLUDED: [&str; 2] = ["streamstack", "sunshine"];

/// Whether `text` names something the checks must leave alone.
pub fn excluded(text: &str) -> bool {
    let lower = text.to_ascii_lowercase();
    EXCLUDED.iter().any(|name| lower.contains(name))
}

/// The check list the role writes next to the binary.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Checks {
    pub schema: String,
    /// systemd units whose state is reported.
    #[serde(default)]
    pub services: Vec<String>,
    /// Paths whose file system's free space is reported.
    #[serde(default)]
    pub disk_paths: Vec<String>,
    #[serde(default = "default_min_free_mib")]
    pub disk_min_free_mib: u64,
    #[serde(default = "default_stale_after_days")]
    pub stale_after_days: u64,
}

fn default_min_free_mib() -> u64 {
    DEFAULT_MIN_FREE_MIB
}

fn default_stale_after_days() -> u64 {
    DEFAULT_STALE_AFTER_DAYS
}

/// Parses a check list.
pub fn parse_checks(text: &str) -> Result<Checks, String> {
    let checks: Checks =
        serde_json::from_str(text).map_err(|error| format!("not a valid check list: {error}"))?;
    if checks.schema != CHECKS_SCHEMA {
        return Err(format!(
            "unsupported check list schema; expected {CHECKS_SCHEMA}"
        ));
    }
    Ok(checks)
}

/// One managed command.
#[derive(Debug, Clone)]
pub struct CommandFact {
    pub name: String,
    pub path: String,
    pub presence: Presence,
    /// The first line of its version query; `None` when none was run.
    pub version: Option<Result<String, String>>,
    /// Why no version query was run, when it was not.
    pub version_skipped: Option<&'static str>,
    pub repository: Option<String>,
    pub release_tag: Option<String>,
    pub verification: Option<String>,
}

/// One systemd unit: its properties, or why they could not be read.
#[derive(Debug, Clone)]
pub struct ServiceFact {
    pub unit: String,
    pub state: Result<Vec<(String, String)>, String>,
}

/// Free space of the file system holding `path`, in KiB.
#[derive(Debug, Clone)]
pub struct DiskFact {
    pub path: String,
    pub space: Result<DiskSpace, String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DiskSpace {
    pub total_kib: u64,
    pub available_kib: u64,
}

/// One file the last update recorded, and its digest now.
#[derive(Debug, Clone)]
pub struct ArtifactFact {
    pub path: String,
    pub recorded: String,
    pub now: FileDigest,
}

/// Which inventory of the manifest the artifacts were compared with.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Reference {
    #[default]
    None,
    /// What the update installed (`after`).
    Installed,
    /// What the rollback restored (`before`).
    Restored,
}

/// Everything the health checks found, before redaction.
#[derive(Debug, Clone, Default)]
pub struct Health {
    /// Seconds since the epoch when the checks ran; 0 skips the age check.
    pub now: u64,
    /// Why there is no check list, when there is none.
    pub checks_error: Option<String>,
    /// 0 skips the age check.
    pub stale_after_days: u64,
    pub min_free_mib: u64,
    pub commands: Vec<CommandFact>,
    pub services: Vec<ServiceFact>,
    pub disks: Vec<DiskFact>,
    pub reference: Reference,
    pub artifacts: Vec<ArtifactFact>,
    /// Checks left out because a bound was reached.
    pub not_checked: Vec<String>,
}

/// Runs the checks against `host`. `manifest` is the parsed update manifest,
/// `home` the user's home folder for user-scoped commands.
pub fn gather<H: Host>(
    host: &H,
    manifest: Option<&Value>,
    checks: Result<Checks, String>,
    home: Option<&Path>,
    now: u64,
) -> Health {
    let mut health = Health {
        now,
        stale_after_days: DEFAULT_STALE_AFTER_DAYS,
        min_free_mib: DEFAULT_MIN_FREE_MIB,
        ..Health::default()
    };
    if let Some(manifest) = manifest {
        gather_commands(host, manifest, home, &mut health);
        gather_artifacts(host, manifest, &mut health);
    }
    match checks {
        Ok(checks) => {
            health.stale_after_days = checks.stale_after_days;
            health.min_free_mib = checks.disk_min_free_mib;
            gather_services(host, &checks.services, &mut health);
            gather_disks(host, &checks.disk_paths, &mut health);
        }
        Err(reason) => health.checks_error = Some(reason),
    }
    health
}

fn strings(value: Option<&Value>) -> Vec<&str> {
    value
        .and_then(Value::as_array)
        .map(|items| items.iter().filter_map(Value::as_str).collect())
        .unwrap_or_default()
}

fn gather_commands<H: Host>(host: &H, manifest: &Value, home: Option<&Path>, health: &mut Health) {
    let Some(contracts) = manifest
        .get("application_contracts")
        .and_then(Value::as_object)
    else {
        return;
    };
    let switched_off = strings(manifest.get("skipped_integrations"));
    let releases = manifest.get("upstream_releases");
    let started = Instant::now();
    let mut considered = 0;
    for (name, contract) in contracts {
        let argv = strings(contract.get("healthcheck"));
        if excluded(name)
            || argv.iter().any(|arg| excluded(arg))
            || switched_off.contains(&name.as_str())
        {
            continue;
        }
        let Some((program, args)) = argv.split_first() else {
            continue;
        };
        considered += 1;
        if considered > MAX_COMMANDS {
            continue;
        }
        let path = if program.starts_with('/') {
            PathBuf::from(program)
        } else if let Some(home) = home {
            home.join(program)
        } else {
            health
                .not_checked
                .push(format!("command {name}: no home folder to find it in"));
            continue;
        };
        let presence = host.presence(&path);
        let is_version_query = VERSION_QUERIES.contains(&args);
        let (version, version_skipped) = if presence != Presence::Executable {
            (None, None)
        } else if !is_version_query {
            (None, Some("its health check is not a version query"))
        } else if started.elapsed() >= VERSION_BUDGET {
            (
                None,
                Some("the time budget for version queries was used up"),
            )
        } else {
            let output = host
                .run(&path.display().to_string(), args)
                .and_then(|text| {
                    first_line(&text)
                        .map(str::to_string)
                        .ok_or_else(|| "printed nothing".to_string())
                });
            (Some(output), None)
        };
        let release = releases.and_then(|r| r.get(name));
        let text = |value: Option<&Value>| value.and_then(Value::as_str).map(str::to_string);
        health.commands.push(CommandFact {
            name: name.clone(),
            path: path.display().to_string(),
            presence,
            version,
            version_skipped,
            repository: text(release.and_then(|r| r.get("repository"))),
            release_tag: text(release.and_then(|r| r.get("tag_name"))),
            verification: text(contract.get("verification")),
        });
    }
    if considered > MAX_COMMANDS {
        health.not_checked.push(format!(
            "{} commands beyond the first {MAX_COMMANDS}",
            considered - MAX_COMMANDS
        ));
    }
}

static SHA256_HEX: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[0-9a-f]{64}$").expect("valid"));

fn gather_artifacts<H: Host>(host: &H, manifest: &Value, health: &mut Health) {
    // A rolled-back run restored the files it found before the update.
    let rolled_back = manifest.get("status").and_then(Value::as_str) == Some("rolled_back");
    let (reference, key) = if rolled_back {
        (Reference::Restored, "before")
    } else {
        (Reference::Installed, "after")
    };
    let Some(entries) = manifest.get(key).and_then(Value::as_array) else {
        return;
    };
    let recorded: Vec<(&str, &str)> = entries
        .iter()
        .filter(|entry| entry.get("exists").and_then(Value::as_bool) == Some(true))
        .filter_map(|entry| {
            let path = entry.get("path").and_then(Value::as_str)?;
            let checksum = entry.get("checksum").and_then(Value::as_str)?;
            (path.starts_with('/') && SHA256_HEX.is_match(checksum) && !excluded(path))
                .then_some((path, checksum))
        })
        .collect();
    if recorded.is_empty() {
        return;
    }
    health.reference = reference;
    for (path, checksum) in recorded.iter().take(MAX_ARTIFACTS) {
        health.artifacts.push(ArtifactFact {
            path: (*path).to_string(),
            recorded: (*checksum).to_string(),
            now: host.sha256(Path::new(path)),
        });
    }
    if recorded.len() > MAX_ARTIFACTS {
        health.not_checked.push(format!(
            "{} managed files beyond the first {MAX_ARTIFACTS}",
            recorded.len() - MAX_ARTIFACTS
        ));
    }
}

static UNIT: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^[A-Za-z0-9][A-Za-z0-9@_.:-]{0,200}\.(?:service|timer|socket|path|mount)$")
        .expect("valid")
});

fn gather_services<H: Host>(host: &H, units: &[String], health: &mut Health) {
    let units: Vec<&String> = units.iter().filter(|unit| !excluded(unit)).collect();
    for unit in units.iter().take(MAX_SERVICES) {
        let state = if UNIT.is_match(unit) {
            let mut args = SYSTEMCTL_SHOW.to_vec();
            args.extend(["--", unit.as_str()]);
            host.run("systemctl", &args)
                .and_then(|text| parse_unit(&text))
        } else {
            Err("not a systemd unit name; not queried".to_string())
        };
        health.services.push(ServiceFact {
            unit: (*unit).clone(),
            state,
        });
    }
    if units.len() > MAX_SERVICES {
        health.not_checked.push(format!(
            "{} services beyond the first {MAX_SERVICES}",
            units.len() - MAX_SERVICES
        ));
    }
}

/// The `KEY=value` lines of `systemctl show` for [`UNIT_PROPERTIES`].
pub fn parse_unit(text: &str) -> Result<Vec<(String, String)>, String> {
    let mut found = Vec::new();
    for key in UNIT_PROPERTIES {
        let value = text
            .lines()
            .find_map(|line| line.strip_prefix(key)?.strip_prefix('='))
            .ok_or_else(|| format!("systemctl did not report {key}"))?;
        found.push((key.to_string(), value.trim().to_string()));
    }
    Ok(found)
}

fn gather_disks<H: Host>(host: &H, paths: &[String], health: &mut Health) {
    for path in paths.iter().take(MAX_DISKS) {
        let space = if path.starts_with('/') && !path.split('/').any(|part| part == "..") {
            host.run("df", &["-P", "-k", "--", path.as_str()])
                .and_then(|text| parse_df(&text))
        } else {
            Err("not an absolute path; not queried".to_string())
        };
        health.disks.push(DiskFact {
            path: path.clone(),
            space,
        });
    }
    if paths.len() > MAX_DISKS {
        health.not_checked.push(format!(
            "{} disk paths beyond the first {MAX_DISKS}",
            paths.len() - MAX_DISKS
        ));
    }
}

/// Total and available KiB from `df -P -k` output. The capacity column (the
/// one ending in `%`) anchors the numbers, so a file system name with spaces
/// still parses.
pub fn parse_df(text: &str) -> Result<DiskSpace, String> {
    let line = text
        .lines()
        .skip(1)
        .find(|line| !line.trim().is_empty())
        .ok_or_else(|| "df printed no file system".to_string())?;
    let fields: Vec<&str> = line.split_whitespace().collect();
    let capacity = fields
        .iter()
        .rposition(|field| field.ends_with('%'))
        .filter(|&index| index >= 4)
        .ok_or_else(|| "unrecognised df output".to_string())?;
    let number = |index: usize| {
        fields[index]
            .parse::<u64>()
            .map_err(|_| "unrecognised df output".to_string())
    };
    Ok(DiskSpace {
        total_kib: number(capacity - 3)?,
        available_kib: number(capacity - 1)?,
    })
}

static NAME: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$").expect("valid"));
static REPOSITORY: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_.-]{1,100}$").expect("valid")
});
static STATE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[a-z0-9][a-z0-9-]{0,39}$").expect("valid"));
static VERSION_IN_TEXT: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?:^|[^0-9A-Za-z.])v?([0-9]+(?:\.[0-9]+)+(?:[-+][0-9A-Za-z.]+)?)").expect("valid")
});

/// The version number in a version query's first line.
pub fn version_in(line: &str) -> Option<&str> {
    VERSION_IN_TEXT
        .captures(line)
        .and_then(|captures| captures.get(1))
        .map(|version| version.as_str())
}

/// Whether `version` is the release `tag` (`v1.2.3`, `1.2.3`, `cli-v1.2.3`).
fn matches_release(version: &str, tag: &str) -> bool {
    version_in(tag).is_some_and(|tagged| tagged == version)
}

/// The `health` part of the bundle and the findings it supports, in plain words.
pub fn render(health: &Health, redactor: &mut Redactor) -> (Value, Vec<String>) {
    let mut findings = Vec::new();
    let mut section = Map::new();

    if let Some(reason) = &health.checks_error {
        section.insert("check_list".into(), Value::String(redactor.redact(reason)));
    }

    let mut commands = Vec::new();
    for command in &health.commands {
        let name = checked(&command.name, &NAME);
        let shown = name.as_str().unwrap_or_default().to_string();
        let path = redactor.redact(&command.path);
        let mut entry = Map::new();
        entry.insert("name".into(), name);
        entry.insert("path".into(), Value::String(path.clone()));
        let status = match command.presence {
            Presence::Executable => "present",
            Presence::NotExecutable => "not executable",
            Presence::NotAFile => "not a file",
            Presence::Missing => "missing",
        };
        entry.insert("status".into(), Value::String(status.into()));
        match command.presence {
            Presence::Executable => {}
            Presence::Missing => findings.push(format!(
                "Command `{shown}` is missing: {path} does not exist."
            )),
            _ => findings.push(format!("Command `{shown}` is {status}: {path}.")),
        }
        let mut version_value = Value::Null;
        match &command.version {
            Some(Ok(line)) => match version_in(line) {
                Some(version) => version_value = checked(version, &VERSION),
                None => {
                    entry.insert(
                        "version_error".into(),
                        Value::String("unrecognised version output".into()),
                    );
                }
            },
            Some(Err(reason)) => {
                let reason = redactor.redact(reason);
                findings.push(format!(
                    "Command `{shown}` did not report its version: {reason}."
                ));
                entry.insert("version_error".into(), Value::String(reason));
            }
            None => {
                if let Some(reason) = command.version_skipped {
                    entry.insert("version_not_checked".into(), Value::String(reason.into()));
                }
            }
        }
        entry.insert("version".into(), version_value.clone());
        if let Some(repository) = &command.repository {
            entry.insert("repository".into(), checked(repository, &REPOSITORY));
        }
        if let Some(tag) = &command.release_tag {
            entry.insert("release_tag".into(), checked(tag, &TAG));
            if let Some(version) = version_value.as_str().filter(|v| VERSION.is_match(v)) {
                entry.insert(
                    "matches_release".into(),
                    Value::Bool(matches_release(version, tag)),
                );
            }
        }
        if let Some(verification) = &command.verification {
            entry.insert("verification".into(), checked(verification, &WORD));
        }
        commands.push(Value::Object(entry));
    }
    section.insert("commands".into(), Value::Array(commands));

    let mut services = Vec::new();
    for service in &health.services {
        let unit = checked(&service.unit, &UNIT);
        let shown = unit.as_str().unwrap_or_default().to_string();
        let mut entry = Map::new();
        entry.insert("unit".into(), unit);
        match &service.state {
            Ok(properties) => {
                let get = |key: &str| {
                    let value = properties
                        .iter()
                        .find(|(name, _)| name == key)
                        .map_or("", |(_, value)| value.as_str());
                    checked(value, &STATE)
                };
                let load = get("LoadState");
                let active = get("ActiveState");
                let sub = get("SubState");
                let result = get("Result");
                let text = |value: &Value| value.as_str().unwrap_or_default().to_string();
                if text(&load) == "not-found" {
                    findings.push(format!("Service `{shown}` is not installed."));
                } else if text(&active) == "failed" {
                    findings.push(format!(
                        "Service `{shown}` has failed (result: {}).",
                        text(&result)
                    ));
                } else if shown.ends_with(".timer") && text(&active) != "active" {
                    findings.push(format!(
                        "Timer `{shown}` is {}, so what it schedules does not run.",
                        text(&active)
                    ));
                }
                entry.insert("load".into(), load);
                entry.insert("active".into(), active);
                entry.insert("sub".into(), sub);
                entry.insert("result".into(), result);
            }
            Err(reason) => {
                let reason = redactor.redact(reason);
                findings.push(format!(
                    "The state of service `{shown}` could not be read: {reason}."
                ));
                entry.insert("error".into(), Value::String(reason));
            }
        }
        services.push(Value::Object(entry));
    }
    section.insert("services".into(), Value::Array(services));

    let mut disks = Vec::new();
    for disk in &health.disks {
        let path = redactor.redact(&disk.path);
        let entry = match &disk.space {
            Ok(space) => {
                let available_mib = space.available_kib / 1024;
                if available_mib < health.min_free_mib {
                    findings.push(format!(
                        "Low disk space on {path}: {available_mib} MiB free, below {} MiB.",
                        health.min_free_mib
                    ));
                }
                json!({
                    "path": path,
                    "available_mib": available_mib,
                    "total_mib": space.total_kib / 1024,
                })
            }
            Err(reason) => {
                let reason = redactor.redact(reason);
                findings.push(format!("Free space on {path} could not be read: {reason}."));
                json!({ "path": path, "error": reason })
            }
        };
        disks.push(entry);
    }
    section.insert("disks".into(), Value::Array(disks));

    if health.reference != Reference::None {
        let verb = match health.reference {
            Reference::Restored => "restored",
            _ => "installed",
        };
        let mut unchanged = 0;
        let mut problems = Vec::new();
        for artifact in &health.artifacts {
            let state = match &artifact.now {
                FileDigest::Sha256(now) if *now == artifact.recorded => {
                    unchanged += 1;
                    continue;
                }
                FileDigest::Sha256(_) => "changed",
                FileDigest::Missing => "missing",
                FileDigest::TooLarge => "too large to check",
                FileDigest::Unreadable(_) => "unreadable",
            };
            let path = redactor.redact(&artifact.path);
            let mut entry = json!({ "path": path, "state": state });
            match &artifact.now {
                FileDigest::Sha256(_) => findings.push(format!(
                    "{path} differs from the file the last update {verb}: it is stale or was replaced outside the catalog."
                )),
                FileDigest::Missing => findings.push(format!(
                    "{path} was recorded by the last update but is gone."
                )),
                FileDigest::TooLarge => {}
                FileDigest::Unreadable(reason) => {
                    let reason = redactor.redact(reason);
                    findings.push(format!("{path} could not be read: {reason}."));
                    entry["error"] = Value::String(reason);
                }
            }
            problems.push(entry);
        }
        let compared_with = match health.reference {
            Reference::Restored => "the files the rollback restored",
            _ => "the files the last update installed",
        };
        section.insert(
            "artifacts".into(),
            json!({
                "compared_with": compared_with,
                "checked": health.artifacts.len(),
                "unchanged": unchanged,
                "problems": problems,
            }),
        );
    }

    if !health.not_checked.is_empty() {
        let skipped: Vec<Value> = health
            .not_checked
            .iter()
            .map(|reason| Value::String(redactor.redact(reason)))
            .collect();
        section.insert("not_checked".into(), Value::Array(skipped));
    }
    (Value::Object(section), findings)
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;
    use std::collections::BTreeMap;

    use super::*;
    use crate::diagnostics::redact::Known;

    const LAZYGIT: &str = "1111111111111111111111111111111111111111111111111111111111111111";
    const DELTA: &str = "2222222222222222222222222222222222222222222222222222222222222222";

    /// A machine that exists only in memory. It records every program it is
    /// asked to run, so a test can prove what the checks did.
    #[derive(Default)]
    struct FakeHost {
        executables: BTreeMap<String, Presence>,
        outputs: BTreeMap<String, Result<String, String>>,
        digests: BTreeMap<String, FileDigest>,
        calls: RefCell<Vec<Vec<String>>>,
    }

    impl FakeHost {
        fn output(mut self, command: &str, result: Result<&str, &str>) -> Self {
            self.outputs.insert(
                command.to_string(),
                result.map(str::to_string).map_err(str::to_string),
            );
            self
        }
    }

    impl Host for FakeHost {
        fn run(&self, program: &str, args: &[&str]) -> Result<String, String> {
            let mut call = vec![program.to_string()];
            call.extend(args.iter().map(|arg| (*arg).to_string()));
            let key = call.join(" ");
            self.calls.borrow_mut().push(call);
            self.outputs
                .get(&key)
                .cloned()
                .unwrap_or_else(|| Err("not installed".to_string()))
        }

        fn presence(&self, path: &Path) -> Presence {
            self.executables
                .get(&path.display().to_string())
                .copied()
                .unwrap_or(Presence::Missing)
        }

        fn sha256(&self, path: &Path) -> FileDigest {
            self.digests
                .get(&path.display().to_string())
                .cloned()
                .unwrap_or(FileDigest::Missing)
        }
    }

    fn redactor() -> Redactor {
        Redactor::new(&Known {
            users: vec!["researcher".into()],
            hosts: vec!["lab-ws01".into()],
            extra: vec![],
        })
    }

    fn manifest() -> Value {
        json!({
            "status": "success",
            "finished_at": "2026-11-24T08:20:11Z",
            "application_contracts": {
                "lazygit": {"verification": "github-asset-digest", "healthcheck": ["/usr/local/bin/lazygit", "--version"]},
                "delta": {"verification": "github-asset-digest", "healthcheck": ["/usr/local/bin/delta", "--version"]},
                "krew": {"scope": "user", "healthcheck": [".krew/bin/kubectl-krew", "help"]},
                "litra": {"healthcheck": ["/usr/local/bin/litra", "--version"]},
                "sunshine": {"healthcheck": ["/usr/bin/sunshine", "--version"]},
                "danger": {"healthcheck": ["/usr/local/bin/danger", "--purge-everything"]}
            },
            "skipped_integrations": ["litra"],
            "upstream_releases": {
                "lazygit": {"repository": "jesseduffield/lazygit", "tag_name": "v0.44.1"},
                "delta": {"repository": "dandavison/delta", "tag_name": "0.18.2"}
            },
            "after": [
                {"path": "/usr/local/bin/lazygit", "exists": true, "checksum": LAZYGIT},
                {"path": "/usr/local/bin/delta", "exists": true, "checksum": DELTA},
                {"path": "/usr/bin/sunshine", "exists": true, "checksum": LAZYGIT},
                {"path": "/usr/local/bin/never-installed", "exists": false, "checksum": ""}
            ],
            "before": [
                {"path": "/usr/local/bin/lazygit", "exists": true, "checksum": DELTA}
            ]
        })
    }

    fn checks() -> Checks {
        Checks {
            schema: CHECKS_SCHEMA.into(),
            services: vec![
                "ansible-pull.timer".into(),
                "ansible-pull.service".into(),
                "streamstack-console.service".into(),
                "sunshine.service".into(),
            ],
            disk_paths: vec!["/".into(), "/var/lib/curiosity".into()],
            disk_min_free_mib: 2048,
            stale_after_days: 14,
        }
    }

    const DF_ROOT: &str = "Filesystem 1024-blocks Used Available Capacity Mounted on\n\
                           /dev/mapper/vg-root 102400000 51200000 51200000 50% /\n";
    const DF_LOW: &str = "Filesystem 1024-blocks Used Available Capacity Mounted on\n\
                          /dev/sda3 10240000 9728000 512000 95% /var\n";

    /// A healthy machine with every managed command and file in place.
    fn healthy() -> FakeHost {
        let mut host = FakeHost::default()
            .output(
                "/usr/local/bin/lazygit --version",
                Ok("commit=, build date=, build source=, version=0.44.1, os=linux, arch=amd64\n"),
            )
            .output("/usr/local/bin/delta --version", Ok("delta 0.18.2\n"))
            .output(
                "systemctl show --no-pager --property=LoadState,ActiveState,SubState,Result -- ansible-pull.timer",
                Ok("LoadState=loaded\nActiveState=active\nSubState=waiting\nResult=success\n"),
            )
            .output(
                "systemctl show --no-pager --property=LoadState,ActiveState,SubState,Result -- ansible-pull.service",
                Ok("Result=success\nLoadState=loaded\nActiveState=inactive\nSubState=dead\n"),
            )
            .output("df -P -k -- /", Ok(DF_ROOT))
            .output("df -P -k -- /var/lib/curiosity", Ok(DF_ROOT));
        for path in ["/usr/local/bin/lazygit", "/usr/local/bin/delta"] {
            host.executables.insert(path.into(), Presence::Executable);
        }
        host.executables.insert(
            "/home/researcher/.krew/bin/kubectl-krew".into(),
            Presence::Executable,
        );
        host.executables
            .insert("/usr/local/bin/danger".into(), Presence::Executable);
        host.digests.insert(
            "/usr/local/bin/lazygit".into(),
            FileDigest::Sha256(LAZYGIT.into()),
        );
        host.digests.insert(
            "/usr/local/bin/delta".into(),
            FileDigest::Sha256(DELTA.into()),
        );
        host
    }

    fn run(host: &FakeHost, manifest: &Value) -> (Value, Vec<String>, Health) {
        let health = gather(
            host,
            Some(manifest),
            Ok(checks()),
            Some(Path::new("/home/researcher")),
            1_764_000_000,
        );
        let (section, findings) = render(&health, &mut redactor());
        (section, findings, health)
    }

    #[test]
    fn a_healthy_machine_has_no_findings_and_reports_versions_and_provenance() {
        let host = healthy();
        let (section, findings, _) = run(&host, &manifest());
        assert!(findings.is_empty(), "{findings:?}");
        let commands = section["commands"].as_array().unwrap();
        let lazygit = commands.iter().find(|c| c["name"] == "lazygit").unwrap();
        assert_eq!(lazygit["status"], "present");
        assert_eq!(lazygit["version"], "0.44.1");
        assert_eq!(lazygit["repository"], "jesseduffield/lazygit");
        assert_eq!(lazygit["release_tag"], "v0.44.1");
        assert_eq!(lazygit["matches_release"], true);
        assert_eq!(lazygit["verification"], "github-asset-digest");
        let krew = commands.iter().find(|c| c["name"] == "krew").unwrap();
        assert_eq!(krew["path"], "/home/[user]/.krew/bin/kubectl-krew");
        assert_eq!(
            krew["version_not_checked"],
            "its health check is not a version query"
        );
        assert_eq!(section["services"][0]["active"], "active");
        assert_eq!(section["disks"][0]["available_mib"], 50_000);
        assert_eq!(section["artifacts"]["checked"], 2);
        assert_eq!(section["artifacts"]["unchanged"], 2);
    }

    #[test]
    fn a_missing_binary_is_found() {
        let mut host = healthy();
        host.executables.remove("/usr/local/bin/delta");
        host.digests.remove("/usr/local/bin/delta");
        let (section, findings, _) = run(&host, &manifest());
        let delta = section["commands"]
            .as_array()
            .unwrap()
            .iter()
            .find(|c| c["name"] == "delta")
            .unwrap();
        assert_eq!(delta["status"], "missing");
        assert_eq!(delta["version"], Value::Null);
        assert!(
            findings.contains(
                &"Command `delta` is missing: /usr/local/bin/delta does not exist.".to_string()
            ),
            "{findings:?}"
        );
        assert!(
            findings.contains(
                &"/usr/local/bin/delta was recorded by the last update but is gone.".to_string()
            ),
            "{findings:?}"
        );
        assert!(
            !host
                .calls
                .borrow()
                .iter()
                .any(|call| call[0].ends_with("delta")),
            "a missing command is never run"
        );
    }

    #[test]
    fn a_failed_service_and_a_stopped_timer_are_found() {
        let host = healthy()
            .output(
                "systemctl show --no-pager --property=LoadState,ActiveState,SubState,Result -- ansible-pull.service",
                Ok("LoadState=loaded\nActiveState=failed\nSubState=failed\nResult=exit-code\n"),
            )
            .output(
                "systemctl show --no-pager --property=LoadState,ActiveState,SubState,Result -- ansible-pull.timer",
                Ok("LoadState=loaded\nActiveState=inactive\nSubState=dead\nResult=success\n"),
            );
        let (section, findings, _) = run(&host, &manifest());
        assert_eq!(section["services"][1]["active"], "failed");
        assert_eq!(section["services"][1]["result"], "exit-code");
        assert!(
            findings.contains(
                &"Service `ansible-pull.service` has failed (result: exit-code).".to_string()
            ),
            "{findings:?}"
        );
        assert!(
            findings.contains(
                &"Timer `ansible-pull.timer` is inactive, so what it schedules does not run."
                    .to_string()
            ),
            "{findings:?}"
        );
    }

    #[test]
    fn a_missing_unit_and_an_unreadable_state_are_reported() {
        let host = healthy()
            .output(
                "systemctl show --no-pager --property=LoadState,ActiveState,SubState,Result -- ansible-pull.service",
                Ok("LoadState=not-found\nActiveState=inactive\nSubState=dead\nResult=success\n"),
            )
            .output(
                "systemctl show --no-pager --property=LoadState,ActiveState,SubState,Result -- ansible-pull.timer",
                Err("did not answer within 5 s"),
            );
        let (_, findings, _) = run(&host, &manifest());
        assert!(findings.contains(&"Service `ansible-pull.service` is not installed.".to_string()));
        assert!(findings.contains(
            &"The state of service `ansible-pull.timer` could not be read: did not answer within 5 s."
                .to_string()
        ));
    }

    #[test]
    fn a_stale_artifact_is_found() {
        let mut host = healthy();
        host.digests.insert(
            "/usr/local/bin/lazygit".into(),
            FileDigest::Sha256(DELTA.into()),
        );
        let (section, findings, _) = run(&host, &manifest());
        assert_eq!(section["artifacts"]["unchanged"], 1);
        assert_eq!(
            section["artifacts"]["problems"],
            json!([{"path": "/usr/local/bin/lazygit", "state": "changed"}])
        );
        assert!(
            findings.contains(
                &"/usr/local/bin/lazygit differs from the file the last update installed: it is stale or was replaced outside the catalog."
                    .to_string()
            ),
            "{findings:?}"
        );
        let text = section.to_string();
        assert!(
            !text.contains(LAZYGIT) && !text.contains(DELTA),
            "checksums stay out of the bundle"
        );
    }

    #[test]
    fn after_a_rollback_files_are_compared_with_what_was_restored() {
        let mut host = healthy();
        let mut manifest = manifest();
        manifest["status"] = json!("rolled_back");
        host.digests.insert(
            "/usr/local/bin/lazygit".into(),
            FileDigest::Sha256(DELTA.into()),
        );
        let (section, findings, health) = run(&host, &manifest);
        assert_eq!(health.reference, Reference::Restored);
        assert_eq!(section["artifacts"]["checked"], 1);
        assert_eq!(section["artifacts"]["unchanged"], 1);
        assert!(findings.is_empty(), "{findings:?}");
    }

    #[test]
    fn low_disk_space_is_found() {
        let host = healthy().output("df -P -k -- /var/lib/curiosity", Ok(DF_LOW));
        let (section, findings, _) = run(&host, &manifest());
        assert_eq!(section["disks"][1]["available_mib"], 500);
        assert!(
            findings.contains(
                &"Low disk space on /var/lib/curiosity: 500 MiB free, below 2048 MiB.".to_string()
            ),
            "{findings:?}"
        );
    }

    #[test]
    fn checks_are_read_only_bounded_and_leave_streamstack_and_sunshine_alone() {
        let host = healthy();
        let (section, _, _) = run(&host, &manifest());
        let calls = host.calls.borrow();
        for call in calls.iter() {
            let line = call.join(" ");
            assert!(!excluded(&line), "{line}");
            match call[0].as_str() {
                "systemctl" => assert_eq!(&call[1..4], SYSTEMCTL_SHOW, "{line}"),
                "df" => assert_eq!(&call[1..4], ["-P", "-k", "--"], "{line}"),
                program => {
                    assert!(program.starts_with("/usr/local/bin/"), "{line}");
                    let args: Vec<&str> = call[1..].iter().map(String::as_str).collect();
                    assert!(VERSION_QUERIES.contains(&args.as_slice()), "{line}");
                }
            }
        }
        // `danger --purge-everything` is not a version query, so it never runs.
        assert!(!calls.iter().any(|call| call[0].ends_with("danger")));
        let text = section.to_string();
        assert!(!text.to_ascii_lowercase().contains("sunshine"), "{text}");
        assert!(!text.to_ascii_lowercase().contains("streamstack"), "{text}");
        assert!(
            !text.contains("litra"),
            "switched-off integrations are skipped"
        );
    }

    #[test]
    fn item_counts_are_capped() {
        let host = FakeHost::default();
        let mut contracts = Map::new();
        for index in 0..(MAX_COMMANDS + 3) {
            contracts.insert(
                format!("tool{index:03}"),
                json!({"healthcheck": [format!("/usr/local/bin/tool{index:03}"), "--version"]}),
            );
        }
        let manifest = json!({ "application_contracts": contracts });
        let mut list = checks();
        list.disk_paths = (0..MAX_DISKS + 1).map(|i| format!("/d{i}")).collect();
        let health = gather(&host, Some(&manifest), Ok(list), None, 0);
        assert_eq!(health.commands.len(), MAX_COMMANDS);
        assert_eq!(health.disks.len(), MAX_DISKS);
        assert!(
            health
                .not_checked
                .contains(&format!("3 commands beyond the first {MAX_COMMANDS}"))
        );
        assert!(
            health
                .not_checked
                .contains(&format!("1 disk paths beyond the first {MAX_DISKS}"))
        );
    }

    #[test]
    fn private_values_in_probe_results_are_redacted() {
        let mut host = healthy()
            .output(
                "/usr/local/bin/delta --version",
                Err("cannot open /home/researcher/.config/delta on lab-ws01 (192.0.2.9)"),
            )
            .output(
                "df -P -k -- /var/lib/curiosity",
                Err("exited with status 1"),
            );
        host.digests.insert(
            "/usr/local/bin/lazygit".into(),
            FileDigest::Unreadable("permission denied for researcher".into()),
        );
        let (section, findings, _) = run(&host, &manifest());
        let text = format!("{section} {findings:?}");
        for never in ["researcher", "lab-ws01", "192.0.2.9"] {
            assert!(!text.contains(never), "{never} in {text}");
        }
    }

    #[test]
    fn unexpected_unit_names_and_disk_paths_are_not_queried() {
        let host = healthy();
        let list = Checks {
            services: vec!["x; reboot".into()],
            disk_paths: vec!["relative".into(), "/var/../etc".into()],
            ..checks()
        };
        let health = gather(&host, None, Ok(list), None, 0);
        assert!(host.calls.borrow().is_empty(), "{:?}", host.calls.borrow());
        let (section, _) = render(&health, &mut redactor());
        assert_eq!(
            section["services"][0]["unit"],
            crate::diagnostics::UNEXPECTED
        );
    }

    #[test]
    fn no_check_list_still_checks_commands_and_files() {
        let host = healthy();
        let health = gather(
            &host,
            Some(&manifest()),
            Err("no check list".into()),
            None,
            0,
        );
        assert!(health.services.is_empty() && health.disks.is_empty());
        assert_eq!(health.artifacts.len(), 2);
        let (section, _) = render(&health, &mut redactor());
        assert_eq!(section["check_list"], "no check list");
    }

    #[test]
    fn check_list_parsing() {
        let list = parse_checks(&format!(
            r#"{{"schema":"{CHECKS_SCHEMA}","services":["ansible-pull.timer"]}}"#
        ))
        .unwrap();
        assert_eq!(list.stale_after_days, DEFAULT_STALE_AFTER_DAYS);
        assert_eq!(list.disk_min_free_mib, DEFAULT_MIN_FREE_MIB);
        assert!(parse_checks(r#"{"schema":"other/v1"}"#).is_err());
        assert!(parse_checks(&format!(r#"{{"schema":"{CHECKS_SCHEMA}","extra":1}}"#)).is_err());
    }

    #[test]
    fn df_and_version_parsing() {
        assert_eq!(
            parse_df(DF_ROOT),
            Ok(DiskSpace {
                total_kib: 102_400_000,
                available_kib: 51_200_000
            })
        );
        assert_eq!(
            parse_df("h\nmy disk 100 40 60 40% /mnt/my disk\n"),
            Ok(DiskSpace {
                total_kib: 100,
                available_kib: 60
            })
        );
        assert!(parse_df("only a header\n").is_err());
        assert_eq!(version_in("OpenTofu v1.8.0"), Some("1.8.0"));
        assert_eq!(
            version_in("Client Version: v1.31.2+k3s1"),
            Some("1.31.2+k3s1")
        );
        assert_eq!(version_in("no digits"), None);
        assert!(matches_release("2025.1.0", "desktop-v2025.1.0"));
        assert!(!matches_release("0.44.0", "v0.44.1"));
    }
}
