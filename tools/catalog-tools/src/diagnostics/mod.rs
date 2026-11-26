//! `catalog-tools diagnostics`: a redacted, local support bundle (catalog#41).
//!
//! It reads the last update manifest the role wrote, asks the local `ansible`
//! for its version and `uname` for the architecture, and builds a small JSON
//! bundle in which every free-text value has passed through
//! [`redact::Redactor`]. It changes nothing on the machine, opens no network
//! connection, and writes only one file, after the researcher has read it, in
//! [`bundle_dir`]. The field policy is in docs/troubleshooting.md.

pub mod redact;

use std::fs::{self, DirBuilder, OpenOptions};
use std::io::{ErrorKind, Read, Write};
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::LazyLock;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result, bail};
use regex::Regex;
use serde_json::{Map, Value, json};

use redact::{Known, Redactor};

/// Where the role writes the last update manifest (`workstation_update_manifest_path`).
pub const DEFAULT_MANIFEST: &str = "/var/lib/curiosity/workstation-updates/last-run.json";
/// The bundle format.
pub const SCHEMA: &str = "curiosity.workstation.diagnostics/v1";
/// Bundle files are named `diagnostics-<UTC time>.json`; `delete` removes only these.
pub const FILE_PREFIX: &str = "diagnostics-";

/// The only programs the command runs, each read-only and bounded in time.
const PROBES: [(&str, &[&str]); 3] = [
    ("ansible", &["--version"]),
    ("ansible-community", &["--version"]),
    ("uname", &["-m"]),
];
const PROBE_TIMEOUT: Duration = Duration::from_secs(5);

/// What the bundle describes, before redaction.
#[derive(Debug, Clone)]
pub struct Inputs {
    /// First line of each probe in [`PROBES`], or why it is unavailable.
    pub ansible: Result<String, String>,
    pub ansible_community: Result<String, String>,
    pub architecture: Result<String, String>,
    /// `PRETTY_NAME` from os-release.
    pub os: Option<String>,
    /// The manifest file's text, or why it could not be read.
    pub manifest: Result<String, String>,
    pub manifest_path: PathBuf,
    /// `--profile`, overriding the manifest's profile.
    pub profile: Option<String>,
    /// `--tags`, the tags the failing run selected.
    pub tags: Vec<String>,
}

/// Reads this machine: runs [`PROBES`], os-release and the manifest. Nothing
/// is written and no service is touched.
pub fn gather(manifest_path: &Path, profile: Option<String>, tags: Vec<String>) -> Inputs {
    let [ansible, community, uname] = PROBES.map(|(program, args)| first_line(program, args));
    Inputs {
        ansible,
        ansible_community: community,
        architecture: Ok(uname.unwrap_or_else(|_| std::env::consts::ARCH.to_string())),
        os: os_pretty_name(Path::new("/etc/os-release")),
        manifest: read_manifest(manifest_path),
        manifest_path: manifest_path.to_path_buf(),
        profile,
        tags,
    }
}

/// The names of this machine and its user, for [`Known`].
pub fn known_from_environment(extra: Vec<String>) -> Known {
    let mut users = Vec::new();
    for variable in ["USER", "LOGNAME", "SUDO_USER"] {
        if let Ok(value) = std::env::var(variable) {
            users.push(value);
        }
    }
    if let Some(name) = std::env::var_os("HOME")
        .map(PathBuf::from)
        .and_then(|home| {
            home.file_name()
                .map(|name| name.to_string_lossy().into_owned())
        })
    {
        users.push(name);
    }
    let mut hosts = Vec::new();
    for path in ["/proc/sys/kernel/hostname", "/etc/hostname"] {
        if let Ok(text) = fs::read_to_string(path) {
            let name = text.trim().to_string();
            if !name.is_empty() {
                if let Some((short, _)) = name.split_once('.') {
                    hosts.push(short.to_string());
                }
                hosts.push(name);
            }
        }
    }
    users.sort();
    users.dedup();
    hosts.sort();
    hosts.dedup();
    Known {
        users,
        hosts,
        extra,
    }
}

/// The first non-empty output line of `program args`, within [`PROBE_TIMEOUT`].
fn first_line(program: &str, args: &[&str]) -> Result<String, String> {
    let mut child = match Command::new(program)
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
    {
        Ok(child) => child,
        Err(error) if error.kind() == ErrorKind::NotFound => {
            return Err("not installed".to_string());
        }
        Err(error) => return Err(format!("cannot run {program}: {error}")),
    };
    let Some(mut stdout) = child.stdout.take() else {
        return Err(format!("no output pipe from {program}"));
    };
    let reader = std::thread::spawn(move || {
        let mut text = String::new();
        stdout.read_to_string(&mut text).map(|_| text)
    });
    let deadline = Instant::now() + PROBE_TIMEOUT;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                let text = reader
                    .join()
                    .map_err(|_| format!("reading {program} output failed"))?
                    .map_err(|error| format!("reading {program} output failed: {error}"))?;
                if !status.success() {
                    return Err(format!("{program} exited with {status}"));
                }
                return text
                    .lines()
                    .map(str::trim)
                    .find(|line| !line.is_empty())
                    .map(str::to_string)
                    .ok_or_else(|| format!("{program} printed nothing"));
            }
            Ok(None) if Instant::now() >= deadline => {
                // Best effort: the child is abandoned either way and the bundle says so.
                if let Err(error) = child.kill() {
                    return Err(format!(
                        "{program} timed out and could not be stopped: {error}"
                    ));
                }
                // Reaping a killed child cannot fail in a way the bundle could report.
                child.wait().ok();
                return Err(format!(
                    "{program} did not answer within {} s",
                    PROBE_TIMEOUT.as_secs()
                ));
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(20)),
            Err(error) => return Err(format!("waiting for {program} failed: {error}")),
        }
    }
}

fn os_pretty_name(path: &Path) -> Option<String> {
    let text = fs::read_to_string(path).ok()?;
    text.lines()
        .find_map(|line| line.strip_prefix("PRETTY_NAME="))
        .map(|value| value.trim().trim_matches('"').to_string())
}

fn read_manifest(path: &Path) -> Result<String, String> {
    fs::read_to_string(path).map_err(|error| match error.kind() {
        ErrorKind::NotFound => {
            "no update manifest: the role has not finished a run on this machine".to_string()
        }
        ErrorKind::PermissionDenied => format!(
            "permission denied: the manifest is readable by root only; run `sudo cp {} ~/last-run.json && sudo chown $USER ~/last-run.json` and pass `--manifest ~/last-run.json`",
            path.display()
        ),
        _ => format!("unreadable: {error}"),
    })
}

static VERSION: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[0-9][0-9A-Za-z.+-]{0,40}$").expect("valid"));
static WORD: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[a-z0-9][a-z0-9_-]{0,63}$").expect("valid"));
static TAG: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$").expect("valid"));
static TIMESTAMP: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:?[0-9]{2})?$")
        .expect("valid")
});
static COMMIT: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[0-9a-f]{7,64}$").expect("valid"));
static CORE_VERSION: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^ansible(?: \[core ([^\]]+)\]| ([0-9][^ ]*))").expect("valid"));
static COMMUNITY_VERSION: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)^ansible community version ([^ ]+)").expect("valid"));

const UNEXPECTED: &str = "[redacted: unexpected format]";

/// A structured value that passes `pattern`, or a marker. Such values never
/// carry free text, so they skip the redactor.
fn checked(value: &str, pattern: &Regex) -> Value {
    if pattern.is_match(value) {
        Value::String(value.to_string())
    } else {
        Value::String(UNEXPECTED.to_string())
    }
}

fn version_from(probe: &Result<String, String>, pattern: &Regex) -> Value {
    match probe {
        Ok(line) => match pattern
            .captures(line)
            .and_then(|c| c.get(1).or_else(|| c.get(2)))
        {
            Some(version) => checked(version.as_str(), &VERSION),
            None => Value::String("unrecognised version output".to_string()),
        },
        Err(reason) => Value::String(format!("unavailable: {reason}")),
    }
}

fn manifest_field<'a>(manifest: &'a Result<Value, String>, name: &str) -> Option<&'a Value> {
    manifest.as_ref().ok().and_then(|m| m.get(name))
}

/// The bundle as JSON. Pure: every machine value comes from `inputs`.
pub fn build(inputs: &Inputs, redactor: &mut Redactor, created_at: &str) -> Value {
    let manifest: Result<Value, String> = match &inputs.manifest {
        Ok(text) => serde_json::from_str(text)
            .map_err(|error| format!("not valid JSON: {error}"))
            .and_then(|value: Value| {
                if value.is_object() {
                    Ok(value)
                } else {
                    Err("not a JSON object".to_string())
                }
            }),
        Err(reason) => Err(reason.clone()),
    };
    let field = |name: &str| manifest_field(&manifest, name);

    let profile = match (&inputs.profile, field("profile").and_then(Value::as_str)) {
        (Some(profile), _) => checked(profile, &WORD),
        (None, Some(profile)) => checked(profile, &WORD),
        (None, None) => Value::String("unknown".to_string()),
    };

    let last_run = match &manifest {
        Err(reason) => json!({ "status": "unknown", "error": redactor.redact(reason) }),
        Ok(_) => {
            let mut run = Map::new();
            let text_or_unknown =
                |value: Option<&Value>, pattern: &Regex| match value.and_then(Value::as_str) {
                    Some(text) => checked(text, pattern),
                    None => Value::String("unknown".to_string()),
                };
            run.insert("status".into(), text_or_unknown(field("status"), &WORD));
            run.insert(
                "started_at".into(),
                text_or_unknown(field("started_at"), &TIMESTAMP),
            );
            run.insert(
                "finished_at".into(),
                text_or_unknown(field("finished_at"), &TIMESTAMP),
            );
            run.insert(
                "update_ring".into(),
                text_or_unknown(field("update_ring"), &WORD),
            );
            run.insert(
                "catalog_commit".into(),
                text_or_unknown(field("catalog_commit"), &COMMIT),
            );
            for flag in [
                "catalog_commit_verified",
                "quarantine_active",
                "install_allowed",
            ] {
                run.insert(
                    flag.into(),
                    field(flag)
                        .and_then(Value::as_bool)
                        .map_or(Value::Null, Value::Bool),
                );
            }
            let failure = field("failure");
            for (key, source) in [("failed_task", "task"), ("failure_message", "message")] {
                if let Some(text) = failure.and_then(|f| f.get(source)).and_then(Value::as_str) {
                    run.insert(key.into(), Value::String(redactor.redact(text)));
                }
            }
            for list in [
                "healthcheck_failures",
                "healthcheck_warnings",
                "ring_blockers",
            ] {
                let items: Vec<Value> = field(list)
                    .and_then(Value::as_array)
                    .map(|items| {
                        items
                            .iter()
                            .map(|item| {
                                let text = match item {
                                    Value::String(text) => text.clone(),
                                    other => other.to_string(),
                                };
                                Value::String(redactor.redact(&text))
                            })
                            .collect()
                    })
                    .unwrap_or_default();
                run.insert(list.into(), Value::Array(items));
            }
            Value::Object(run)
        }
    };

    let tags: Vec<Value> = inputs.tags.iter().map(|tag| checked(tag, &TAG)).collect();
    let architecture = match &inputs.architecture {
        Ok(arch) => checked(arch, &WORD),
        Err(reason) => Value::String(format!("unavailable: {reason}")),
    };
    let os = inputs
        .os
        .as_deref()
        .map_or(Value::Null, |os| Value::String(redactor.redact(os)));
    let manifest_path = redactor.redact(&inputs.manifest_path.display().to_string());

    let mut bundle = json!({
        "schema": SCHEMA,
        "created_at": created_at,
        "tool": format!("catalog-tools {}", env!("CARGO_PKG_VERSION")),
        "ansible": {
            "ansible": version_from(&inputs.ansible_community, &COMMUNITY_VERSION),
            "ansible_core": version_from(&inputs.ansible, &CORE_VERSION),
        },
        "profile": profile,
        "architecture": architecture,
        "os": os,
        "selected_tags": tags,
        "last_run": last_run,
        "manifest_path": manifest_path,
    });
    let counts: Map<String, Value> = redactor
        .counts()
        .iter()
        .map(|(kind, count)| ((*kind).to_string(), json!(count)))
        .collect();
    bundle["redacted"] = Value::Object(counts);
    bundle
}

/// The bundle exactly as it is shown and written.
pub fn render(bundle: &Value) -> String {
    let mut text = serde_json::to_string_pretty(bundle).expect("a JSON value always serialises");
    text.push('\n');
    text
}

/// The documented bundle folder: `$XDG_STATE_HOME/curiosity/diagnostics`, or
/// `~/.local/state/curiosity/diagnostics` when `XDG_STATE_HOME` is unset or relative.
pub fn bundle_dir() -> Result<PathBuf> {
    if let Some(state) = std::env::var_os("XDG_STATE_HOME").map(PathBuf::from)
        && state.is_absolute()
    {
        return Ok(state.join("curiosity/diagnostics"));
    }
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .filter(|home| home.is_absolute())
        .context("HOME is not set to an absolute path, so there is no bundle folder")?;
    Ok(home.join(".local/state/curiosity/diagnostics"))
}

/// Writes `text` to a new `diagnostics-<stamp>.json` (mode 0600) in `dir`
/// (created 0700). Never overwrites and refuses a symlinked folder.
pub fn write_bundle(dir: &Path, stamp: &str, text: &str) -> Result<PathBuf> {
    DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(dir)
        .with_context(|| format!("cannot create {}", dir.display()))?;
    let meta =
        fs::symlink_metadata(dir).with_context(|| format!("cannot inspect {}", dir.display()))?;
    if !meta.is_dir() {
        bail!(
            "{} is not a plain folder (a symlink?); refusing to write there",
            dir.display()
        );
    }
    if meta.permissions().mode() & 0o077 != 0 {
        fs::set_permissions(dir, fs::Permissions::from_mode(0o700))
            .with_context(|| format!("cannot make {} private", dir.display()))?;
    }
    let path = dir.join(format!("{FILE_PREFIX}{stamp}.json"));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&path)
        .with_context(|| format!("cannot create {}", path.display()))?;
    file.write_all(text.as_bytes())
        .with_context(|| format!("cannot write {}", path.display()))?;
    Ok(path)
}

/// Bundle files in `dir`, oldest first. A missing folder holds none.
pub fn list_bundles(dir: &Path) -> Result<Vec<PathBuf>> {
    let entries = match fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => return Err(error).with_context(|| format!("cannot list {}", dir.display())),
    };
    let mut found = Vec::new();
    for entry in entries {
        let entry = entry.with_context(|| format!("cannot list {}", dir.display()))?;
        let name = entry.file_name().to_string_lossy().into_owned();
        let is_file = entry
            .file_type()
            .with_context(|| format!("cannot inspect {}", entry.path().display()))?
            .is_file();
        if is_file && name.starts_with(FILE_PREFIX) && name.ends_with(".json") {
            found.push(entry.path());
        }
    }
    found.sort();
    Ok(found)
}

/// UTC time as (`2026-11-25T09:30:00Z`, `20261125T093000Z`).
pub fn utc_now() -> (String, String) {
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| elapsed.as_secs());
    utc_from(seconds)
}

/// UTC time for `seconds` since the epoch, as (RFC 3339, file stamp).
pub fn utc_from(seconds: u64) -> (String, String) {
    let days = (seconds / 86_400) as i64;
    let rest = seconds % 86_400;
    let (hour, minute, second) = (rest / 3600, rest % 3600 / 60, rest % 60);
    // Howard Hinnant's days-to-civil algorithm.
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    (
        format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z"),
        format!("{year:04}{month:02}{day:02}T{hour:02}{minute:02}{second:02}Z"),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn inputs(manifest: &str) -> Inputs {
        Inputs {
            ansible: Ok("ansible [core 2.19.4]".into()),
            ansible_community: Ok("Ansible community version 12.2.0".into()),
            architecture: Ok("x86_64".into()),
            os: Some("Ubuntu 26.04 LTS".into()),
            manifest: Ok(manifest.into()),
            manifest_path: PathBuf::from(DEFAULT_MANIFEST),
            profile: None,
            tags: vec!["packages".into(), "update".into()],
        }
    }

    fn redactor() -> Redactor {
        Redactor::new(&Known {
            users: vec!["researcher".into()],
            hosts: vec!["lab-ws01".into()],
            extra: vec![],
        })
    }

    #[test]
    fn collects_the_acceptance_fields() {
        let manifest = r#"{"status":"rolled_back","profile":"desktop","update_ring":"stable",
            "started_at":"2026-11-24T08:00:00Z","finished_at":"2026-11-24T08:20:11Z",
            "catalog_commit":"0743a9d6","catalog_commit_verified":true,"quarantine_active":false,
            "failure":{"task":"Install StreamStack","message":"GET https://192.0.2.7/x failed for lab-ws01"},
            "healthcheck_failures":["streamstack"],"host":"lab-ws01","run_id":"1764000000-abcdef123456",
            "package_inventory":{"dpkg":"secret-package 1.0"}}"#;
        let bundle = build(&inputs(manifest), &mut redactor(), "2026-11-25T09:30:00Z");
        assert_eq!(bundle["schema"], SCHEMA);
        assert_eq!(bundle["ansible"]["ansible_core"], "2.19.4");
        assert_eq!(bundle["ansible"]["ansible"], "12.2.0");
        assert_eq!(bundle["profile"], "desktop");
        assert_eq!(bundle["architecture"], "x86_64");
        assert_eq!(bundle["selected_tags"], json!(["packages", "update"]));
        assert_eq!(bundle["last_run"]["status"], "rolled_back");
        assert_eq!(bundle["last_run"]["catalog_commit"], "0743a9d6");
        assert_eq!(bundle["last_run"]["failed_task"], "Install StreamStack");
        let text = render(&bundle);
        for never in [
            "192.0.2.7",
            "lab-ws01",
            "1764000000",
            "secret-package",
            "\"host\"",
        ] {
            assert!(!text.contains(never), "{never} in {text}");
        }
        assert_eq!(bundle["redacted"]["IP address"], 1);
    }

    #[test]
    fn unexpected_structured_values_are_not_copied() {
        let manifest =
            r#"{"status":"failed for researcher","catalog_commit":"not-a-commit 192.0.2.1"}"#;
        let mut i = inputs(manifest);
        i.tags = vec!["update".into(), "x; rm -rf /home/researcher".into()];
        i.profile = Some("Desktop Of researcher".into());
        let text = render(&build(&i, &mut redactor(), "2026-11-25T09:30:00Z"));
        assert!(!text.contains("researcher"), "{text}");
        assert!(!text.contains("192.0.2.1"), "{text}");
        assert!(text.contains(UNEXPECTED), "{text}");
    }

    #[test]
    fn a_missing_or_broken_manifest_is_reported_not_fatal() {
        let mut i = inputs("");
        i.manifest =
            Err("no update manifest: the role has not finished a run on this machine".into());
        let bundle = build(&i, &mut redactor(), "t");
        assert_eq!(bundle["last_run"]["status"], "unknown");
        assert_eq!(bundle["profile"], "unknown");
        let bundle = build(&inputs("[1, 2"), &mut redactor(), "t");
        assert!(
            bundle["last_run"]["error"]
                .as_str()
                .unwrap()
                .starts_with("not valid JSON")
        );
    }

    #[test]
    fn probe_failures_are_stated() {
        let mut i = inputs("{}");
        i.ansible = Err("not installed".into());
        i.ansible_community = Ok("something else".into());
        let bundle = build(&i, &mut redactor(), "t");
        assert_eq!(
            bundle["ansible"]["ansible_core"],
            "unavailable: not installed"
        );
        assert_eq!(bundle["ansible"]["ansible"], "unrecognised version output");
    }

    #[test]
    fn old_style_core_version_is_read() {
        let mut i = inputs("{}");
        i.ansible = Ok("ansible 2.9.27".into());
        let bundle = build(&i, &mut redactor(), "t");
        assert_eq!(bundle["ansible"]["ansible_core"], "2.9.27");
    }

    #[test]
    fn utc_conversion() {
        assert_eq!(utc_from(0).0, "1970-01-01T00:00:00Z");
        assert_eq!(
            utc_from(1_764_063_000),
            ("2025-11-25T09:30:00Z".into(), "20251125T093000Z".into())
        );
        assert_eq!(utc_from(951_782_400).0, "2000-02-29T00:00:00Z");
    }

    #[test]
    fn runs_only_the_listed_read_only_probes() {
        let source = include_str!("mod.rs");
        assert_eq!(source.matches(concat!("Command", "::new(")).count(), 1);
        let redact = include_str!("redact.rs");
        let networking = [
            concat!("Tcp", "Stream"),
            concat!("Udp", "Socket"),
            concat!("Tcp", "Listener"),
            concat!("req", "west"),
            concat!("ur", "eq"),
        ];
        for network in networking {
            assert!(
                !source.contains(network) && !redact.contains(network),
                "{network}"
            );
        }
        let programs: Vec<&str> = PROBES.iter().map(|(program, _)| *program).collect();
        assert_eq!(programs, ["ansible", "ansible-community", "uname"]);
    }
}
