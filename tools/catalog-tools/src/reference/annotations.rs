//! The curated part of the reference: stability, safety impact, value notes and
//! tag descriptions, kept in `docs/reference-annotations.yml`.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::LazyLock;

use anyhow::{Context, Result, bail};
use regex::Regex;
use serde::Deserialize;
use serde_norway::Value;

/// Stability words and what each means for a user of the collection.
pub const STABILITY: [(&str, &str); 4] = [
    (
        "supported",
        "Part of the public configuration API. Changes follow the deprecation policy.",
    ),
    (
        "experimental",
        "Works today but may change or be removed without a deprecation period.",
    ),
    (
        "internal",
        "Implementation detail (derived value, file path or contract table). Do not set it; it may change in any release.",
    ),
    (
        "private-hook",
        "Integration point for a private service or organisation. The collection ships it empty; the value belongs in protected host variables, not in a public repository.",
    ),
];

/// Safety flags and what each warns about.
pub const SAFETY: [(&str, &str); 7] = [
    (
        "secret",
        "Credential, token or key. Keep it in protected host variables or a vault, never in git.",
    ),
    ("destructive", "Can erase, repartition or reformat data."),
    (
        "security",
        "Changes the host's security posture (firewall, SSH, integrity checks, certificate trust).",
    ),
    (
        "network",
        "Changes networking, DNS or VPN routing. A wrong value can cut off remote access.",
    ),
    (
        "boot",
        "Changes boot, console or display configuration. A wrong value can leave the host without a usable display.",
    ),
    (
        "supply-chain",
        "Downloads and installs software from a configured location. Pin it with the matching checksum or fingerprint.",
    ),
    (
        "account",
        "Creates, changes or grants access to user accounts.",
    ),
];

/// A variable named like a credential is a credential.
pub static SECRET_NAME: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(^|_)(pass|password|psk|token|secret)(_|$)").expect("valid regex")
});
/// A variable named like a checksum is an integrity pin.
static PIN_NAME: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"_(sha256|fingerprint)$").expect("valid regex"));

pub fn stability_rank(word: &str) -> usize {
    STABILITY
        .iter()
        .position(|(name, _)| *name == word)
        .unwrap_or(STABILITY.len())
}

fn safety_rank(word: &str) -> usize {
    SAFETY
        .iter()
        .position(|(name, _)| *name == word)
        .unwrap_or(SAFETY.len())
}

#[derive(Debug, Clone, Deserialize)]
#[serde(untagged)]
enum OneOrMany {
    One(String),
    Many(Vec<String>),
}

impl OneOrMany {
    fn into_vec(self) -> Vec<String> {
        match self {
            OneOrMany::One(word) => vec![word],
            OneOrMany::Many(words) => words,
        }
    }
}

#[derive(Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawFile {
    #[serde(default)]
    patterns: Vec<RawPattern>,
    #[serde(default)]
    variables: BTreeMap<String, RawVariable>,
    #[serde(default)]
    tags: BTreeMap<String, TagNote>,
    #[serde(default)]
    profile_notes: BTreeMap<String, String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawPattern {
    #[serde(rename = "match")]
    pattern: String,
    stability: Option<String>,
    safety: Option<OneOrMany>,
}

#[derive(Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawVariable {
    stability: Option<String>,
    safety: Option<OneOrMany>,
    values: Option<Value>,
    note: Option<String>,
}

/// What the annotations say about a tag.
#[derive(Debug, Default, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TagNote {
    pub stability: Option<String>,
    pub description: Option<String>,
}

#[derive(Debug)]
struct PatternRule {
    regex: Regex,
    stability: Option<String>,
    safety: Vec<String>,
}

#[derive(Debug, Default)]
struct VariableNote {
    stability: Option<String>,
    safety: Vec<String>,
    values: Option<Value>,
    note: String,
}

/// The classification of one variable.
#[derive(Debug, Clone, PartialEq)]
pub struct Classification {
    pub stability: String,
    /// Safety flags in the order of [`SAFETY`], without duplicates.
    pub safety: Vec<&'static str>,
    /// Valid values stated by the annotations, overriding asserted ones.
    pub values: Option<Value>,
    pub note: String,
}

/// Parsed and checked annotations.
#[derive(Debug, Default)]
pub struct Annotations {
    patterns: Vec<PatternRule>,
    variables: BTreeMap<String, VariableNote>,
    pub tags: BTreeMap<String, TagNote>,
    pub profile_notes: BTreeMap<String, String>,
}

fn check_stability(where_: &str, stability: Option<&String>) -> Result<()> {
    let Some(word) = stability else {
        return Ok(());
    };
    if stability_rank(word) == STABILITY.len() {
        let known: Vec<&str> = STABILITY.iter().map(|(name, _)| *name).collect();
        bail!(
            "{where_}: unknown stability {word:?} (use one of {})",
            known.join(", ")
        );
    }
    Ok(())
}

fn check_safety(where_: &str, flags: &[String]) -> Result<()> {
    for flag in flags {
        if safety_rank(flag) == SAFETY.len() {
            let known: Vec<&str> = SAFETY.iter().map(|(name, _)| *name).collect();
            bail!(
                "{where_}: unknown safety flag {flag:?} (use one of {})",
                known.join(", ")
            );
        }
    }
    Ok(())
}

impl Annotations {
    /// Parse the annotations file and check its vocabulary.
    pub fn parse(text: &str, origin: &str) -> Result<Self> {
        let raw: RawFile = if text.trim().is_empty() {
            RawFile::default()
        } else {
            serde_norway::from_str::<Option<RawFile>>(text)
                .with_context(|| format!("{origin}: not a valid annotations file"))?
                .unwrap_or_default()
        };
        let mut patterns = Vec::new();
        for rule in raw.patterns {
            let where_ = format!("{origin}: pattern {}", rule.pattern);
            let regex = Regex::new(&rule.pattern)
                .with_context(|| format!("{where_}: bad regular expression"))?;
            let safety = rule.safety.map(OneOrMany::into_vec).unwrap_or_default();
            check_stability(&where_, rule.stability.as_ref())?;
            check_safety(&where_, &safety)?;
            patterns.push(PatternRule {
                regex,
                stability: rule.stability,
                safety,
            });
        }
        let mut variables = BTreeMap::new();
        for (name, entry) in raw.variables {
            let where_ = format!("{origin}: variable {name}");
            let safety = entry.safety.map(OneOrMany::into_vec).unwrap_or_default();
            check_stability(&where_, entry.stability.as_ref())?;
            check_safety(&where_, &safety)?;
            variables.insert(
                name,
                VariableNote {
                    stability: entry.stability,
                    safety,
                    values: entry.values,
                    note: entry.note.unwrap_or_default().trim().to_owned(),
                },
            );
        }
        for (tag, entry) in &raw.tags {
            check_stability(&format!("{origin}: tag {tag}"), entry.stability.as_ref())?;
        }
        Ok(Self {
            patterns,
            variables,
            tags: raw.tags,
            profile_notes: raw.profile_notes,
        })
    }

    /// Fail when an annotation names a variable or tag the roles no longer have,
    /// so a rename cannot leave a stale entry behind.
    pub fn check_names(&self, variables: &BTreeSet<&str>, tags: &BTreeSet<&str>) -> Result<()> {
        for name in self.variables.keys() {
            if !variables.contains(name.as_str()) {
                bail!(
                    "annotations: variable {name:?} is not defined in any role defaults; remove or rename it"
                );
            }
        }
        for tag in self.tags.keys() {
            if !tags.contains(tag.as_str()) {
                bail!("annotations: tag {tag:?} is not used by any role task; remove or rename it");
            }
        }
        Ok(())
    }

    /// Stability, safety flags, value override and note for a variable: pattern
    /// rules first, then the name rules, then the variable's own entry.
    pub fn classify(&self, name: &str) -> Classification {
        let mut stability = "supported".to_owned();
        let mut safety: Vec<&str> = Vec::new();
        for rule in &self.patterns {
            if rule.regex.is_match(name) {
                if let Some(word) = &rule.stability {
                    stability.clone_from(word);
                }
                safety.extend(rule.safety.iter().map(String::as_str));
            }
        }
        if SECRET_NAME.is_match(name) {
            safety.push("secret");
        }
        if PIN_NAME.is_match(name) {
            safety.push("supply-chain");
        }
        let explicit = self.variables.get(name);
        if let Some(entry) = explicit {
            if let Some(word) = &entry.stability {
                stability.clone_from(word);
            }
            safety.extend(entry.safety.iter().map(String::as_str));
        }
        // Ordered as SAFETY lists them; each flag once.
        let ordered: Vec<&'static str> = SAFETY
            .iter()
            .map(|(flag, _)| *flag)
            .filter(|flag| safety.contains(flag))
            .collect();
        Classification {
            stability,
            safety: ordered,
            values: explicit.and_then(|entry| entry.values.clone()),
            note: explicit.map(|entry| entry.note.clone()).unwrap_or_default(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const TEXT: &str = "
patterns:
  - match: '_path$'
    stability: internal
  - match: '^wifi_'
    stability: private-hook
    safety: network
variables:
  wifi_psk: {note: The key.}
  build_path: {stability: supported, safety: [boot, security]}
  mode: {values: [a, b]}
tags:
  kiosk: {description: Kiosk session.}
profile_notes:
  byod-kiosk: Kiosk.
";

    #[test]
    fn classification_layers_patterns_name_rules_and_entries() {
        let annotations = Annotations::parse(TEXT, "test").unwrap();
        let wifi = annotations.classify("wifi_psk");
        assert_eq!(wifi.stability, "private-hook");
        assert_eq!(wifi.safety, ["secret", "network"]);
        assert_eq!(wifi.note, "The key.");

        let build = annotations.classify("build_path");
        assert_eq!(build.stability, "supported");
        assert_eq!(build.safety, ["security", "boot"]);

        let pin = annotations.classify("tool_sha256");
        assert_eq!(pin.safety, ["supply-chain"]);
        assert_eq!(annotations.classify("cache_path").stability, "internal");
        assert!(annotations.classify("mode").values.is_some());
        assert!(annotations.classify("passage").safety.is_empty());
    }

    #[test]
    fn unknown_words_are_rejected() {
        let error =
            Annotations::parse("variables:\n  x: {stability: beta}\n", "a.yml").unwrap_err();
        assert!(error.to_string().contains("unknown stability"), "{error}");
        let error = Annotations::parse("variables:\n  x: {safety: scary}\n", "a.yml").unwrap_err();
        assert!(error.to_string().contains("unknown safety flag"), "{error}");
        let error = Annotations::parse("tags:\n  x: {safety: boot}\n", "a.yml").unwrap_err();
        assert!(format!("{error:#}").contains("safety"), "{error:#}");
        let error = Annotations::parse("patterns:\n  - match: '('\n", "a.yml").unwrap_err();
        assert!(
            error.to_string().contains("bad regular expression"),
            "{error}"
        );
    }

    #[test]
    fn stale_names_are_rejected() {
        let annotations = Annotations::parse(TEXT, "test").unwrap();
        let variables = BTreeSet::from(["wifi_psk", "build_path", "mode"]);
        assert!(
            annotations
                .check_names(&variables, &BTreeSet::from(["kiosk"]))
                .is_ok()
        );
        let error = annotations
            .check_names(
                &BTreeSet::from(["wifi_psk", "mode"]),
                &BTreeSet::from(["kiosk"]),
            )
            .unwrap_err();
        assert!(error.to_string().contains("build_path"), "{error}");
        let error = annotations
            .check_names(&variables, &BTreeSet::new())
            .unwrap_err();
        assert!(error.to_string().contains("kiosk"), "{error}");
    }
}
