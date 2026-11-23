//! Asking apt what a distribution's archive provides, without root and without
//! touching the system's apt state: lists and caches go to a private directory.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::LazyLock;

use anyhow::{Context, Result, bail};
use regex::Regex;

static SELECTING: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"Note, selecting '([^']+)' instead of '([^']+)'").expect("valid regex")
});

/// The distribution a probe ran on, from `/etc/os-release` and dpkg.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OsRelease {
    pub id: String,
    pub version: String,
    pub codename: String,
}

/// Parse `/etc/os-release` (`KEY=value`, values optionally quoted).
pub fn parse_os_release(text: &str) -> OsRelease {
    let mut fields: BTreeMap<&str, String> = BTreeMap::new();
    for line in text.lines() {
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let value = value.trim();
        let unquoted = value
            .strip_prefix('"')
            .and_then(|rest| rest.strip_suffix('"'))
            .or_else(|| value.strip_prefix('\'').and_then(|rest| rest.strip_suffix('\'')))
            .unwrap_or(value);
        fields.insert(key.trim(), unquoted.to_owned());
    }
    let field = |key: &str| fields.get(key).cloned().unwrap_or_else(|| "unknown".to_owned());
    OsRelease {
        id: field("ID"),
        version: field("VERSION_ID"),
        codename: field("VERSION_CODENAME"),
    }
}

/// Parse `apt-cache policy NAME...`: the candidate version of each package,
/// `None` when apt knows the name but has no candidate (`(none)`). Names apt
/// does not know at all are absent from the output and from the map.
pub fn parse_policy(output: &str) -> BTreeMap<String, Option<String>> {
    let mut found = BTreeMap::new();
    let mut current: Option<String> = None;
    for line in output.lines() {
        if !line.starts_with(char::is_whitespace) {
            current = line
                .strip_suffix(':')
                .filter(|name| !name.is_empty() && !name.contains(' '))
                .map(str::to_owned);
            if let Some(name) = &current {
                found.entry(name.clone()).or_insert(None);
            }
            continue;
        }
        let Some(name) = &current else {
            continue;
        };
        if let Some(candidate) = line.trim().strip_prefix("Candidate:") {
            let candidate = candidate.trim();
            let value = (candidate != "(none)" && !candidate.is_empty()).then(|| candidate.to_owned());
            found.insert(name.clone(), value);
        }
    }
    found
}

/// From a successful `apt-get --simulate install NAME`: the package apt
/// installs for NAME (another one when NAME is virtual or transitional).
pub fn parse_selected(output: &str, name: &str) -> String {
    SELECTING
        .captures_iter(output)
        .find(|captures| &captures[2] == name)
        .map_or_else(|| name.to_owned(), |captures| captures[1].to_owned())
}

/// apt with its lists and caches in a private directory.
pub struct Apt {
    directory: PathBuf,
}

impl Apt {
    /// Prepare the private apt directory.
    pub fn new(directory: &Path) -> Result<Self> {
        for sub in ["lists/partial", "cache/archives/partial"] {
            let path = directory.join(sub);
            std::fs::create_dir_all(&path).with_context(|| format!("cannot create {}", path.display()))?;
        }
        Ok(Self {
            directory: directory.to_path_buf(),
        })
    }

    fn options(&self) -> Vec<String> {
        let lists = self.directory.join("lists");
        let cache = self.directory.join("cache");
        vec![
            "-o".to_owned(),
            format!("Dir::State::Lists={}", lists.display()),
            "-o".to_owned(),
            format!("Dir::Cache={}", cache.display()),
            "-o".to_owned(),
            "Debug::NoLocking=1".to_owned(),
        ]
    }

    fn run(&self, program: &str, args: &[&str]) -> Result<Output> {
        Command::new(program)
            .args(self.options())
            .args(args)
            .env("DEBIAN_FRONTEND", "noninteractive")
            .env("LC_ALL", "C")
            .output()
            .with_context(|| format!("cannot run {program}; is this a Debian-family system?"))
    }

    /// Download the package lists of the configured archives.
    pub fn update(&self) -> Result<()> {
        let output = self.run("apt-get", &["-qq", "update"])?;
        if !output.status.success() {
            bail!(
                "apt-get update failed ({}): {}",
                output.status,
                tail(&output.stderr)
            );
        }
        Ok(())
    }

    /// Candidate versions of the named packages.
    pub fn policy(&self, packages: &[String]) -> Result<BTreeMap<String, Option<String>>> {
        let mut args = vec!["policy"];
        args.extend(packages.iter().map(String::as_str));
        let output = self.run("apt-cache", &args)?;
        // Unknown names only print a notice; a failure with no output is a real error.
        if !output.status.success() && output.stdout.is_empty() {
            bail!(
                "apt-cache policy failed ({}): {}",
                output.status,
                tail(&output.stderr)
            );
        }
        Ok(parse_policy(&String::from_utf8_lossy(&output.stdout)))
    }

    /// The package apt would install for NAME, or `None` when it cannot.
    pub fn installs(&self, name: &str) -> Result<Option<String>> {
        let output = self.run(
            "apt-get",
            &["--simulate", "--no-install-recommends", "install", name],
        )?;
        if !output.status.success() {
            return Ok(None);
        }
        Ok(Some(parse_selected(&String::from_utf8_lossy(&output.stdout), name)))
    }
}

/// dpkg's architecture name (`amd64`, `arm64`).
pub fn dpkg_architecture() -> Result<String> {
    let output = Command::new("dpkg")
        .arg("--print-architecture")
        .output()
        .context("cannot run dpkg --print-architecture")?;
    if !output.status.success() {
        bail!("dpkg --print-architecture failed: {}", tail(&output.stderr));
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
}

/// The last lines of a command's error output, for an error message.
fn tail(bytes: &[u8]) -> String {
    let text = String::from_utf8_lossy(bytes);
    let lines: Vec<&str> = text.lines().collect();
    lines[lines.len().saturating_sub(5)..].join(" | ")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn os_release_fields() {
        let release = parse_os_release(
            "PRETTY_NAME=\"Ubuntu 24.04.3 LTS\"\nID=ubuntu\nVERSION_ID=\"24.04\"\nVERSION_CODENAME=noble\n",
        );
        assert_eq!(
            release,
            OsRelease {
                id: "ubuntu".into(),
                version: "24.04".into(),
                codename: "noble".into()
            }
        );
        assert_eq!(parse_os_release("ID='debian'\n").version, "unknown");
    }

    #[test]
    fn policy_candidates() {
        let output = "git:
  Installed: (none)
  Candidate: 1:2.43.0-1ubuntu7.3
  Version table:
     1:2.43.0-1ubuntu7.3 500
        500 http://archive.ubuntu.com/ubuntu noble-updates/main amd64 Packages
libfuse2:
  Installed: (none)
  Candidate: (none)
  Version table:
jq:
  Installed: 1.7.1-3build1
  Candidate: 1.7.1-3build1
";
        let found = parse_policy(output);
        assert_eq!(found["git"].as_deref(), Some("1:2.43.0-1ubuntu7.3"));
        assert_eq!(found["libfuse2"], None);
        assert_eq!(found["jq"].as_deref(), Some("1.7.1-3build1"));
        assert!(!found.contains_key("tokei"));
    }

    #[test]
    fn selected_package_for_a_virtual_name() {
        let output = "NOTE: This is only a simulation!\nNote, selecting 'libfuse2t64' instead of 'libfuse2'\nInst libfuse2t64 (2.9.9-8.1build1 Ubuntu:24.04/noble [amd64])\n";
        assert_eq!(parse_selected(output, "libfuse2"), "libfuse2t64");
        assert_eq!(parse_selected("Inst jq (1.7)\n", "jq"), "jq");
    }

    #[test]
    fn private_apt_directory_is_prepared() {
        let directory = tempfile::tempdir().unwrap();
        let apt = Apt::new(directory.path()).unwrap();
        assert!(directory.path().join("lists/partial").is_dir());
        assert!(directory.path().join("cache/archives/partial").is_dir());
        let options = apt.options();
        assert!(options.iter().any(|option| option.starts_with("Dir::State::Lists=")));
        assert!(options.contains(&"Debug::NoLocking=1".to_owned()));
    }
}
