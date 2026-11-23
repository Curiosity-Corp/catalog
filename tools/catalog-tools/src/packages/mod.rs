//! Platform package probe (catalog#14, #15): which of the role's apt packages
//! each distribution's archive provides.
//!
//! The role installs its main package set in one `apt` task
//! (`roles/developer_workstation/tasks/packages.yml`, "Install all APT
//! packages"), so one name a platform lacks fails the whole task. `probe` runs
//! inside a container of the platform (the `platform-packages` CI matrix) and
//! asks apt; `assemble` writes the dated evidence file; `check` fails when the
//! committed evidence no longer matches the role's package list.

pub mod apt;
pub mod date;

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};
use serde_norway::Value;

use crate::yaml;

/// The task file and task that install the role's packages.
pub const PACKAGES_TASK: &str = "roles/developer_workstation/tasks/packages.yml";
const INSTALL_TASK: &str = "Install all APT packages";
/// Where dated evidence files live, and their name pattern.
pub const EVIDENCE_DIR: &str = "docs/evidence";
const EVIDENCE_PREFIX: &str = "platform-packages-";
const SOURCE: &str = "apt-cache policy and apt-get --simulate install in a container of each platform after apt-get update; distribution archives only, no repository the role adds";

/// One platform's answer.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlatformResult {
    /// What was probed, for example the container image `ubuntu:24.04`.
    pub image: String,
    pub id: String,
    pub version: String,
    pub codename: String,
    pub architecture: String,
    /// Names with a candidate version of their own.
    pub available: usize,
    /// Candidate version of each available name.
    pub candidates: BTreeMap<String, String>,
    /// Names without a candidate that apt still installs, and what it installs
    /// instead (a virtual or transitional name).
    pub provided_by: BTreeMap<String, String>,
    /// Names apt cannot install from this archive.
    pub missing: Vec<String>,
}

/// A dated evidence file.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Evidence {
    pub date: String,
    pub source: String,
    /// The role's package list the probes asked about.
    pub packages: Vec<String>,
    pub results: Vec<PlatformResult>,
}

/// The static package names of the role's main install task, in file order.
/// Templated names are skipped: they depend on host variables.
pub fn role_packages(root: &Path) -> Result<Vec<String>> {
    let path = root.join(PACKAGES_TASK);
    let tasks = yaml::load(&path)?;
    let Value::Sequence(tasks) = tasks else {
        bail!("{}: a task file must be a list of tasks", path.display());
    };
    for task in &tasks {
        let named = task
            .get("name")
            .and_then(Value::as_str)
            .is_some_and(|name| name.starts_with(INSTALL_TASK));
        if !named {
            continue;
        }
        let module = task.get("ansible.builtin.apt").or_else(|| task.get("apt"));
        let Some(names) = module.and_then(|module| module.get("name")) else {
            bail!(
                "{}: task {INSTALL_TASK:?} has no apt name list",
                path.display()
            );
        };
        return Ok(yaml::as_list(Some(names))
            .into_iter()
            .filter_map(Value::as_str)
            .filter(|name| !name.contains("{{"))
            .map(str::to_owned)
            .collect());
    }
    bail!("{}: task {INSTALL_TASK:?} not found", path.display())
}

/// Turn apt's answers into a platform result.
pub fn classify(
    image: &str,
    release: &apt::OsRelease,
    architecture: &str,
    packages: &[String],
    policy: &BTreeMap<String, Option<String>>,
    mut installs: impl FnMut(&str) -> Result<Option<String>>,
) -> Result<PlatformResult> {
    let mut candidates = BTreeMap::new();
    let mut provided_by = BTreeMap::new();
    let mut missing = Vec::new();
    for name in packages {
        if let Some(Some(version)) = policy.get(name) {
            candidates.insert(name.clone(), version.clone());
            continue;
        }
        match installs(name)? {
            Some(package) if package == *name => bail!(
                "{image}: apt has no candidate for {name} yet would install it without naming a \
                 replacement; the probe does not understand this apt's output"
            ),
            Some(package) => {
                provided_by.insert(name.clone(), package);
            }
            None => missing.push(name.clone()),
        }
    }
    missing.sort();
    Ok(PlatformResult {
        image: image.to_owned(),
        id: release.id.clone(),
        version: release.version.clone(),
        codename: release.codename.clone(),
        architecture: architecture.to_owned(),
        available: candidates.len(),
        candidates,
        provided_by,
        missing,
    })
}

/// Probe the system this runs on.
pub fn probe(
    image: &str,
    packages: &[String],
    apt_dir: &Path,
    update: bool,
) -> Result<PlatformResult> {
    let apt = apt::Apt::new(apt_dir)?;
    if update {
        apt.update()?;
    }
    let release = apt::parse_os_release(&yaml::read(Path::new("/etc/os-release"))?);
    let architecture = apt::dpkg_architecture()?;
    let policy = apt.policy(packages)?;
    classify(image, &release, &architecture, packages, &policy, |name| {
        apt.installs(name)
    })
}

/// Problems with one platform result against the package list.
fn result_problems(result: &PlatformResult, packages: &BTreeSet<&str>) -> Vec<String> {
    let mut problems = Vec::new();
    let mut seen: BTreeSet<&str> = BTreeSet::new();
    let answered = result
        .candidates
        .keys()
        .chain(result.provided_by.keys())
        .chain(result.missing.iter());
    for name in answered {
        if !seen.insert(name) {
            problems.push(format!("{}: {name} is listed twice", result.image));
        }
        if !packages.contains(name.as_str()) {
            problems.push(format!(
                "{}: {name} is not in the role's package list",
                result.image
            ));
        }
    }
    let unanswered: Vec<&str> = packages.difference(&seen).copied().collect();
    if !unanswered.is_empty() {
        problems.push(format!(
            "{}: no answer for {}",
            result.image,
            unanswered.join(", ")
        ));
    }
    if result.available != result.candidates.len() {
        problems.push(format!(
            "{}: available is {} but {} candidates are listed",
            result.image,
            result.available,
            result.candidates.len()
        ));
    }
    problems
}

/// Combine platform results into an evidence file, refusing incomplete ones.
pub fn assemble(date: &str, packages: &[String], results: Vec<PlatformResult>) -> Result<Evidence> {
    date::validate(date)?;
    let evidence = Evidence {
        date: date.to_owned(),
        source: SOURCE.to_owned(),
        packages: packages.to_vec(),
        results,
    };
    let problems = check(&evidence, packages);
    if !problems.is_empty() {
        bail!(
            "the probe results are incomplete:\n  {}",
            problems.join("\n  ")
        );
    }
    Ok(evidence)
}

/// Problems with an evidence file against the role's current package list.
pub fn check(evidence: &Evidence, packages: &[String]) -> Vec<String> {
    let mut problems = Vec::new();
    if let Err(error) = date::validate(&evidence.date) {
        problems.push(error.to_string());
    }
    let current: BTreeSet<&str> = packages.iter().map(String::as_str).collect();
    let recorded: BTreeSet<&str> = evidence.packages.iter().map(String::as_str).collect();
    if current != recorded {
        let added: Vec<&str> = current.difference(&recorded).copied().collect();
        let removed: Vec<&str> = recorded.difference(&current).copied().collect();
        problems.push(format!(
            "the role's package list changed since the probe (new: {}; gone: {}); probe again",
            if added.is_empty() {
                "none".to_owned()
            } else {
                added.join(", ")
            },
            if removed.is_empty() {
                "none".to_owned()
            } else {
                removed.join(", ")
            },
        ));
        return problems;
    }
    if evidence.results.is_empty() {
        problems.push("no results".to_owned());
    }
    let mut images = BTreeSet::new();
    for result in &evidence.results {
        if !images.insert(result.image.as_str()) {
            problems.push(format!("{} is probed twice", result.image));
        }
        problems.extend(result_problems(result, &current));
    }
    problems
}

/// The newest committed evidence file, by the date in its name.
pub fn newest_evidence(root: &Path) -> Result<PathBuf> {
    let directory = root.join(EVIDENCE_DIR);
    let mut found: Vec<PathBuf> = std::fs::read_dir(&directory)
        .with_context(|| format!("cannot list {}", directory.display()))?
        .filter_map(|entry| entry.ok().map(|entry| entry.path()))
        .filter(|path| {
            path.file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| name.starts_with(EVIDENCE_PREFIX) && name.ends_with(".json"))
        })
        .collect();
    found.sort();
    found.pop().with_context(|| {
        format!(
            "no {EVIDENCE_PREFIX}*.json in {}; run the platform-packages CI jobs and commit the evidence",
            directory.display()
        )
    })
}

/// Read a JSON file into `T`.
pub fn read_json<T: for<'de> Deserialize<'de>>(path: &Path) -> Result<T> {
    serde_json::from_str(&yaml::read(path)?)
        .with_context(|| format!("{}: not a valid file of this kind", path.display()))
}

/// Pretty JSON with a final newline.
pub fn to_json<T: Serialize>(value: &T) -> Result<String> {
    Ok(serde_json::to_string_pretty(value).context("cannot serialise")? + "\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn names(list: &[&str]) -> Vec<String> {
        list.iter().map(|name| (*name).to_owned()).collect()
    }

    fn sample() -> PlatformResult {
        let policy = BTreeMap::from([
            ("git".to_owned(), Some("2.43".to_owned())),
            ("libfuse2".to_owned(), None),
        ]);
        let release = apt::OsRelease {
            id: "ubuntu".into(),
            version: "24.04".into(),
            codename: "noble".into(),
        };
        classify(
            "ubuntu:24.04",
            &release,
            "amd64",
            &names(&["git", "libfuse2", "tokei"]),
            &policy,
            |name| Ok((name == "libfuse2").then(|| "libfuse2t64".to_owned())),
        )
        .unwrap()
    }

    #[test]
    fn classify_separates_candidates_providers_and_missing() {
        let result = sample();
        assert_eq!(result.available, 1);
        assert_eq!(result.candidates["git"], "2.43");
        assert_eq!(result.provided_by["libfuse2"], "libfuse2t64");
        assert_eq!(result.missing, ["tokei"]);
    }

    #[test]
    fn a_provider_without_a_name_is_an_error() {
        let release = apt::OsRelease {
            id: "debian".into(),
            version: "13".into(),
            codename: "trixie".into(),
        };
        let error = classify(
            "debian:13",
            &release,
            "amd64",
            &names(&["dnsutils"]),
            &BTreeMap::new(),
            |name| Ok(Some(name.to_owned())),
        )
        .unwrap_err();
        assert!(
            error.to_string().contains("without naming a replacement"),
            "{error}"
        );
    }

    #[test]
    fn assemble_and_check() {
        let packages = names(&["git", "libfuse2", "tokei"]);
        let evidence = assemble("2026-11-23", &packages, vec![sample()]).unwrap();
        assert!(check(&evidence, &packages).is_empty());

        let grown = names(&["git", "libfuse2", "tokei", "usbmuxd"]);
        let problems = check(&evidence, &grown);
        assert_eq!(problems.len(), 1);
        assert!(problems[0].contains("new: usbmuxd"), "{problems:?}");

        let mut twice = evidence.clone();
        twice.results.push(sample());
        assert!(
            check(&twice, &packages)
                .iter()
                .any(|problem| problem.contains("probed twice"))
        );

        let mut partial = sample();
        partial.missing.clear();
        let error = assemble("2026-11-23", &packages, vec![partial]).unwrap_err();
        assert!(error.to_string().contains("no answer for tokei"), "{error}");

        assert!(assemble("23.11.2026", &packages, vec![sample()]).is_err());
    }

    #[test]
    fn evidence_round_trips_through_json() {
        let packages = names(&["git", "libfuse2", "tokei"]);
        let evidence = assemble("2026-11-23", &packages, vec![sample()]).unwrap();
        let text = to_json(&evidence).unwrap();
        assert!(text.ends_with("}\n"));
        let parsed: Evidence = serde_json::from_str(&text).unwrap();
        assert_eq!(parsed, evidence);
    }

    #[test]
    fn role_packages_come_from_the_install_task() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join(PACKAGES_TASK);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(
            &path,
            "---
- name: Something else
  ansible.builtin.apt: {name: [nope]}
- name: Install all APT packages  # noqa: args
  ansible.builtin.apt:
    name:
      # --- Build ---
      - gcc
      - \"{{ java_package }}\"
      - jq
    state: present
",
        )
        .unwrap();
        assert_eq!(role_packages(directory.path()).unwrap(), ["gcc", "jq"]);
        std::fs::write(&path, "---\n- name: Other\n  ansible.builtin.debug: {}\n").unwrap();
        assert!(role_packages(directory.path()).is_err());
    }

    #[test]
    fn newest_evidence_by_name() {
        let directory = tempfile::tempdir().unwrap();
        let evidence = directory.path().join(EVIDENCE_DIR);
        std::fs::create_dir_all(&evidence).unwrap();
        assert!(newest_evidence(directory.path()).is_err());
        for name in [
            "platform-packages-2026-11-04.json",
            "platform-packages-2026-11-23.json",
            "other.json",
        ] {
            std::fs::write(evidence.join(name), "{}").unwrap();
        }
        let newest = newest_evidence(directory.path()).unwrap();
        assert!(newest.ends_with("platform-packages-2026-11-23.json"));
    }
}
