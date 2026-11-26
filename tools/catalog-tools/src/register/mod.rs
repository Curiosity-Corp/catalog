//! The dependency lifecycle and ownership register (catalog#51,
//! docs/dependency-register.md).
//!
//! `ci/dependency-register.yml` records, for every upstream the collection
//! depends on, who maintains it, its licence, our owner role, how updates
//! arrive and are verified, its support or end-of-life dates as read from the
//! vendor's own page, and the replacement plan. The register is curated, but
//! the list of dependencies is not: [`declarations`] reads it from what the
//! checkout already declares, and [`check`] fails when the two disagree.
//! [`alerts`] reports abandoned, moved, ending and unverified dependencies and
//! an overdue review.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use anyhow::{Context, Result, bail};
use serde::Deserialize;
use serde_norway::Value;

use crate::sbom::{self, ci, sources};
use crate::yaml;

/// Where the register lives, relative to the checkout root.
pub const REGISTER: &str = "ci/dependency-register.yml";

/// Role defaults whose entries are dependencies in their own right.
pub const VENDOR_REPOS_VAR: &str = "apt_vendor_repos";
pub const GITHUB_RELEASES_VAR: &str = "latest_github_repositories";

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Register {
    /// Date of the last full review, YYYY-MM-DD.
    pub reviewed: String,
    /// Days after `reviewed` before the review is overdue.
    pub review_interval_days: i64,
    /// Days before a support end date that it starts to alert.
    pub warn_days: i64,
    /// The roles that may own an entry, with what the role is.
    pub owner_roles: BTreeMap<String, String>,
    pub entries: Vec<Entry>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Entry {
    /// Our name for the upstream.
    pub name: String,
    /// Who maintains it upstream (an organisation or project, never a person).
    pub upstream: String,
    /// The project's own page.
    pub source: String,
    /// Declaration id to what the declaration names (URL, repository, range).
    pub declarations: BTreeMap<String, String>,
    /// SPDX expression, or `proprietary`, or `various` with a note.
    pub licence: String,
    /// Where the licence was read.
    pub licence_source: String,
    /// A key of `owner_roles`.
    pub owner: String,
    /// How new versions reach a host.
    pub update_channel: String,
    pub verification: Verification,
    #[serde(default)]
    pub security_critical: bool,
    pub support: Support,
    /// What we do when the upstream is archived, relicensed or ends support.
    pub replacement: String,
    /// The issue that tracks this entry's open alerts.
    pub alert_tracked_in: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Support {
    pub status: Status,
    /// The page the status and dates were read from.
    pub evidence: String,
    /// When that page was read, YYYY-MM-DD.
    pub checked: String,
    /// Published end dates (YYYY-MM-DD, or YYYY-MM for the end of that month).
    #[serde(default)]
    pub ends: Vec<Milestone>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Milestone {
    /// What ends (a release line, a support phase).
    pub scope: String,
    pub date: String,
    /// The page this date was read from, when it is not `support.evidence`.
    pub evidence: Option<String>,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum Status {
    /// Released and maintained; a published end date may still apply.
    Maintained,
    /// Archived or read-only upstream.
    Archived,
    /// The project moved; the declaration still names the old home.
    Moved,
    /// No releases or maintenance, but not formally archived.
    Unmaintained,
    /// Past its published end of support.
    EndOfLife,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum Verification {
    /// The distribution's or package manager's own signatures (APT, Snap, Flatpak).
    PackageManagerSignature,
    /// A vendor repository whose signing key the role installs.
    VendorRepositorySignature,
    /// A vendor signature or checksum file checked at install time.
    VendorSignature,
    /// A SHA-256 pinned in the role defaults or a hash-checked lock.
    PinnedChecksum,
    /// Nothing beyond HTTPS to the vendor's host.
    TlsOnly,
}

/// Every declared dependency: id to what the declaration names, as read from
/// `ci/sbom-sources.yml`, `galaxy.yml`, `ci/requirements.yml`,
/// `ci/requirements.in` and the role defaults.
pub fn declarations(root: &Path) -> Result<BTreeMap<String, String>> {
    let mut found = BTreeMap::new();
    let inventory = sources::load(&root.join(sbom::SOURCES))?;
    let roles = sources::scan_roles(&root.join("roles"))?;
    for source in &inventory.sources {
        let locator = match (&source.url, &source.url_var, &source.reference_url) {
            (Some(url), _, _) => url.trim().to_owned(),
            (None, Some(variable), _) => roles
                .defaults
                .get(variable)
                .map(yaml::display)
                .with_context(|| {
                    format!("{}: url_var {variable} is not a role default", source.name)
                })?,
            (None, None, Some(url)) => url.trim().to_owned(),
            (None, None, None) => source.modules.join(", "),
        };
        found.insert(format!("source:{}", source.name), locator);
    }
    let galaxy = yaml::load(&root.join(ci::GALAXY))?;
    if let Some(dependencies) = galaxy.get("dependencies").and_then(Value::as_mapping) {
        for (name, range) in dependencies {
            found.insert(
                format!("collection:{}", yaml::display(name)),
                yaml::display(range),
            );
        }
    }
    for (name, _) in ci::ci_collections(root)? {
        let (namespace, short) = name.split_once('.').unwrap_or(("", &name));
        found.insert(
            format!("ci-collection:{name}"),
            format!("https://galaxy.ansible.com/ui/repo/published/{namespace}/{short}/"),
        );
    }
    for name in ci::python_direct(root)? {
        found.insert(
            format!("ci-python:{name}"),
            format!("https://pypi.org/project/{name}/"),
        );
    }
    if let Some(repos) = roles
        .defaults
        .get(VENDOR_REPOS_VAR)
        .and_then(Value::as_mapping)
    {
        for (key, platforms) in repos {
            let uris: Vec<String> = platforms
                .as_mapping()
                .into_iter()
                .flatten()
                .filter_map(|(_, repo)| repo.get("uri").map(yaml::display))
                .collect();
            found.insert(
                format!("vendor-repo:{}", yaml::display(key)),
                uris.join(" "),
            );
        }
    }
    if let Some(repos) = roles
        .defaults
        .get(GITHUB_RELEASES_VAR)
        .and_then(Value::as_mapping)
    {
        for (key, repository) in repos {
            found.insert(
                format!("github-release:{}", yaml::display(key)),
                yaml::display(repository),
            );
        }
    }
    Ok(found)
}

/// Load and parse the register.
pub fn load(path: &Path) -> Result<Register> {
    let text = yaml::read(path)?;
    serde_norway::from_str(&text).with_context(|| format!("{}: invalid register", path.display()))
}

/// Everything wrong between the register and the declarations, and inside the
/// register itself. Empty means the register is complete and current.
pub fn check(register: &Register, declared: &BTreeMap<String, String>) -> Vec<String> {
    let mut problems = Vec::new();
    if let Err(error) = day_number(&register.reviewed) {
        problems.push(format!("reviewed: {error}"));
    }
    if register.review_interval_days <= 0 || register.warn_days < 0 {
        problems.push("review_interval_days must be positive and warn_days not negative".into());
    }
    let mut names = BTreeSet::new();
    let mut covered: BTreeMap<&str, &str> = BTreeMap::new();
    for entry in &register.entries {
        let name = entry.name.as_str();
        if !names.insert(name) {
            problems.push(format!("two entries are named {name}"));
        }
        if entry.declarations.is_empty() {
            problems.push(format!("{name}: covers no declaration"));
        }
        for (id, locator) in &entry.declarations {
            if let Some(other) = covered.insert(id, name) {
                problems.push(format!("{id} is in both {other} and {name}"));
            }
            match declared.get(id) {
                None => problems.push(format!(
                    "{name}: {id} is no longer declared; remove it or fix the id"
                )),
                Some(actual) if actual != locator => problems.push(format!(
                    "{name}: {id} now declares {actual:?}, the register says {locator:?}; \
                     re-check the upstream and update the entry"
                )),
                Some(_) => {}
            }
        }
        if !register.owner_roles.contains_key(&entry.owner) {
            problems.push(format!(
                "{name}: owner {:?} is not one of owner_roles ({})",
                entry.owner,
                register
                    .owner_roles
                    .keys()
                    .cloned()
                    .collect::<Vec<_>>()
                    .join(", ")
            ));
        }
        for (field, value) in [
            ("upstream", &entry.upstream),
            ("licence", &entry.licence),
            ("update_channel", &entry.update_channel),
            ("replacement", &entry.replacement),
        ] {
            if value.trim().is_empty() {
                problems.push(format!("{name}: {field} is empty"));
            }
        }
        for (field, value) in [
            ("source", Some(&entry.source)),
            ("licence_source", Some(&entry.licence_source)),
            ("support.evidence", Some(&entry.support.evidence)),
            ("alert_tracked_in", entry.alert_tracked_in.as_ref()),
        ] {
            if let Some(value) = value
                && !value.starts_with("https://")
            {
                problems.push(format!("{name}: {field} is not an https:// URL: {value}"));
            }
        }
        match day_number(&entry.support.checked) {
            Err(error) => problems.push(format!("{name}: support.checked: {error}")),
            Ok(checked) => {
                if let Ok(reviewed) = day_number(&register.reviewed)
                    && checked > reviewed
                {
                    problems.push(format!(
                        "{name}: support.checked {} is after the register's reviewed date {}",
                        entry.support.checked, register.reviewed
                    ));
                }
            }
        }
        for milestone in &entry.support.ends {
            if let Err(error) = day_number(&milestone.date) {
                problems.push(format!("{name}: {}: {error}", milestone.scope));
            }
            if let Some(evidence) = &milestone.evidence
                && !evidence.starts_with("https://")
            {
                problems.push(format!(
                    "{name}: {}: evidence is not an https:// URL: {evidence}",
                    milestone.scope
                ));
            }
        }
    }
    for id in declared.keys() {
        if !covered.contains_key(id.as_str()) {
            problems.push(format!(
                "{id} ({}) is declared but not in the register",
                declared[id]
            ));
        }
    }
    problems
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum AlertKind {
    ReviewOverdue,
    Abandoned,
    Moved,
    SupportEnding,
    Unverified,
}

impl AlertKind {
    pub fn as_str(self) -> &'static str {
        match self {
            AlertKind::ReviewOverdue => "review-overdue",
            AlertKind::Abandoned => "abandoned",
            AlertKind::Moved => "moved",
            AlertKind::SupportEnding => "support-ending",
            AlertKind::Unverified => "unverified-security-critical",
        }
    }
}

#[derive(Debug)]
pub struct Alert {
    pub kind: AlertKind,
    pub entry: String,
    pub message: String,
    /// The issue tracking it; `None` fails `alerts --strict`.
    pub tracked_in: Option<String>,
}

/// The register's alerts on `today` (YYYY-MM-DD). An overdue review is never
/// tracked: only a review clears it.
pub fn alerts(register: &Register, today: &str) -> Result<Vec<Alert>> {
    let today_number = day_number(today)?;
    let mut found = Vec::new();
    let reviewed = day_number(&register.reviewed)?;
    if today_number - reviewed > register.review_interval_days {
        found.push(Alert {
            kind: AlertKind::ReviewOverdue,
            entry: "(register)".into(),
            message: format!(
                "last reviewed {}, more than {} days ago; review every entry \
                 (docs/dependency-register.md) and set reviewed",
                register.reviewed, register.review_interval_days
            ),
            tracked_in: None,
        });
    }
    for entry in &register.entries {
        let mut raise = |kind: AlertKind, message: String| {
            found.push(Alert {
                kind,
                entry: entry.name.clone(),
                message,
                tracked_in: entry.alert_tracked_in.clone(),
            });
        };
        match entry.support.status {
            Status::Maintained => {}
            Status::Moved => raise(
                AlertKind::Moved,
                format!(
                    "the upstream moved to {} (the declaration still names {}); \
                     point the declaration at its new home",
                    entry.source, entry.support.evidence
                ),
            ),
            status => raise(
                AlertKind::Abandoned,
                format!(
                    "upstream is {} ({}); replacement: {}",
                    match status {
                        Status::Archived => "archived",
                        Status::Unmaintained => "unmaintained",
                        _ => "past end of life",
                    },
                    entry.support.evidence,
                    entry.replacement
                ),
            ),
        }
        for milestone in &entry.support.ends {
            let ends = day_number(&milestone.date)?;
            if ends - today_number <= register.warn_days {
                let when = if ends < today_number { "ended" } else { "ends" };
                raise(
                    AlertKind::SupportEnding,
                    format!(
                        "{} {when} {} ({}); replacement: {}",
                        milestone.scope,
                        milestone.date,
                        milestone
                            .evidence
                            .as_ref()
                            .unwrap_or(&entry.support.evidence),
                        entry.replacement
                    ),
                );
            }
        }
        if entry.security_critical && entry.verification == Verification::TlsOnly {
            raise(
                AlertKind::Unverified,
                "security-critical, but nothing beyond HTTPS verifies what is installed; \
                 prefer a distribution or package-manager signature"
                    .into(),
            );
        }
    }
    found.sort_by(|a, b| (a.kind, &a.entry).cmp(&(b.kind, &b.entry)));
    Ok(found)
}

/// Today's date (UTC) as YYYY-MM-DD.
pub fn today() -> Result<String> {
    let seconds: i64 = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .context("the system clock is before 1970")?
        .as_secs()
        .try_into()
        .context("the system clock is out of range")?;
    Ok(sbom::bom::rfc3339(seconds)[..10].to_owned())
}

/// Days since 1970-01-01 of `YYYY-MM-DD`, or of the last day of `YYYY-MM`.
pub fn day_number(text: &str) -> Result<i64> {
    let parts: Vec<&str> = text.trim().split('-').collect();
    let number = |part: &str, what: &str| {
        part.parse::<i64>()
            .with_context(|| format!("{text:?}: {what} is not a number"))
    };
    let (year, month, day) = match parts.as_slice() {
        [year, month] if year.len() == 4 && month.len() == 2 => {
            let (year, month) = (number(year, "year")?, number(month, "month")?);
            (year, month, days_in_month(year, month))
        }
        [year, month, day] if year.len() == 4 && month.len() == 2 && day.len() == 2 => (
            number(year, "year")?,
            number(month, "month")?,
            number(day, "day")?,
        ),
        _ => bail!("{text:?} is not YYYY-MM-DD or YYYY-MM"),
    };
    if !(1..=12).contains(&month) || day < 1 || day > days_in_month(year, month) {
        bail!("{text:?} is not a calendar date");
    }
    // Days from civil (H. Hinnant), the inverse of sbom::bom::rfc3339.
    let year = if month <= 2 { year - 1 } else { year };
    let era = year.div_euclid(400);
    let year_of_era = year.rem_euclid(400);
    let month_index = if month > 2 { month - 3 } else { month + 9 };
    let day_of_year = (153 * month_index + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    Ok(era * 146_097 + day_of_era - 719_468)
}

fn days_in_month(year: i64, month: i64) -> i64 {
    match month {
        2 if (year % 4 == 0 && year % 100 != 0) || year % 400 == 0 => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dates_round_trip_with_the_sbom_clock() {
        for date in ["1970-01-01", "2000-02-29", "2026-11-26", "2028-04-30"] {
            let days = day_number(date).expect("valid date");
            assert_eq!(&sbom::bom::rfc3339(days * 86_400)[..10], date);
        }
        assert_eq!(
            day_number("2027-05").expect("month"),
            day_number("2027-05-31").expect("day")
        );
        assert_eq!(
            day_number("2028-02").expect("month"),
            day_number("2028-02-29").expect("day")
        );
        assert!(day_number("2026-02-30").is_err());
        assert!(day_number("May 2027").is_err());
    }
}
