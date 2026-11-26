//! Where the roles install from: `ci/sbom-sources.yml`, held to the roles.
//!
//! The inventory is curated (a URL alone does not say whether it is an APT
//! repository, a release asset or an API endpoint), and [`coverage`] keeps it
//! honest: every URL in the role defaults, tasks, vars and handlers and every
//! package-manager module the tasks call must be declared, and every
//! declaration must still match something.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use anyhow::{Context, Result, bail};
use regex::Regex;
use serde::Deserialize;
use serde_norway::Value;

use super::bom::{Component, Kind, Reference};
use crate::yaml;

/// Modules that install packages from a registry. A task calling one needs a
/// source that names it in `modules`.
pub const PACKAGE_MODULES: &[&str] = &[
    "ansible.builtin.apt",
    "ansible.builtin.dnf",
    "ansible.builtin.package",
    "ansible.builtin.pip",
    "community.general.cargo",
    "community.general.flatpak",
    "community.general.gem",
    "community.general.npm",
    "community.general.pipx",
    "community.general.pnpm",
    "community.general.snap",
    "community.general.yarn",
];

/// The role directories whose URLs are held to the inventory.
const SCANNED: &[&str] = &["defaults", "tasks", "vars", "handlers"];

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Sources {
    pub sources: Vec<Source>,
    #[serde(default)]
    pub not_dependencies: Vec<NotDependency>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Source {
    pub name: String,
    pub kind: SourceKind,
    pub source: String,
    #[serde(rename = "type")]
    pub component_type: Option<String>,
    pub description: Option<String>,
    pub url: Option<String>,
    pub url_var: Option<String>,
    pub reference_url: Option<String>,
    pub version_var: Option<String>,
    pub sha256_var: Option<String>,
    pub purl: Option<String>,
    #[serde(default)]
    pub covers: Vec<String>,
    #[serde(default)]
    pub keys: Vec<String>,
    #[serde(default)]
    pub modules: Vec<String>,
    #[serde(default)]
    pub declared_in: Vec<String>,
    pub note: Option<String>,
    pub pinned: bool,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum SourceKind {
    RuntimePackageSource,
    RuntimePackage,
    ExternalInstaller,
}

impl SourceKind {
    fn kind(self) -> Kind {
        match self {
            SourceKind::RuntimePackageSource => Kind::RuntimePackageSource,
            SourceKind::RuntimePackage => Kind::RuntimePackage,
            SourceKind::ExternalInstaller => Kind::ExternalInstaller,
        }
    }

    fn default_type(self) -> &'static str {
        match self {
            SourceKind::RuntimePackageSource => "data",
            SourceKind::RuntimePackage | SourceKind::ExternalInstaller => "application",
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NotDependency {
    pub url: String,
    pub reason: String,
}

/// Load and sanity-check the inventory.
pub fn load(path: &Path) -> Result<Sources> {
    let text = yaml::read(path)?;
    let sources: Sources = serde_norway::from_str(&text)
        .with_context(|| format!("{}: invalid inventory", path.display()))?;
    let mut names = BTreeSet::new();
    for source in &sources.sources {
        if !names.insert(source.name.as_str()) {
            bail!("{}: two sources are named {}", path.display(), source.name);
        }
        if source.url.is_some() && source.url_var.is_some() {
            bail!(
                "{}: {} has both url and url_var",
                path.display(),
                source.name
            );
        }
        if source.declared_in.is_empty() {
            bail!(
                "{}: {} does not say where it is declared (declared_in)",
                path.display(),
                source.name
            );
        }
    }
    for entry in &sources.not_dependencies {
        if entry.reason.trim().is_empty() {
            bail!(
                "{}: not_dependencies {} has no reason",
                path.display(),
                entry.url
            );
        }
    }
    Ok(sources)
}

/// What the roles reference.
#[derive(Default)]
pub struct Roles {
    /// Every role default, by name (all roles share one variable namespace).
    pub defaults: HashMap<String, Value>,
    /// URL as written (cut at the first `{{`) to the first place it appears.
    pub urls: BTreeMap<String, String>,
    /// Fully qualified module names the tasks call, to the first file.
    pub modules: BTreeMap<String, String>,
}

/// Read the defaults, URLs and module calls of every role under `roles_dir`.
pub fn scan_roles(roles_dir: &Path) -> Result<Roles> {
    static URL: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r#"https?://[^\]\[\s"'<>`(){},|\\]+"#).expect("valid regex"));
    static MODULE: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"^\s*(?:-\s+)?((?:ansible\.builtin|community\.general)\.[a-z_]+):")
            .expect("valid regex")
    });
    let mut roles = Roles::default();
    for role in sorted_children(roles_dir)? {
        let defaults = role.join("defaults/main.yml");
        if defaults.is_file()
            && let Some(mapping) = yaml::load(&defaults)?.as_mapping()
        {
            for (name, value) in mapping {
                roles.defaults.insert(yaml::display(name), value.clone());
            }
        }
        for directory in SCANNED {
            for file in yaml_files(&role.join(directory))? {
                let text = yaml::read(&file)?;
                let origin = file
                    .strip_prefix(roles_dir.parent().unwrap_or(roles_dir))
                    .unwrap_or(&file)
                    .display()
                    .to_string();
                for (number, line) in text.lines().enumerate() {
                    if line.trim_start().starts_with('#') {
                        continue;
                    }
                    let place = format!("{origin}:{}", number + 1);
                    // Regular expressions in tasks escape the dots of a host name.
                    let unescaped = line.replace("\\\\.", ".").replace("\\.", ".");
                    for found in URL.find_iter(&unescaped) {
                        roles
                            .urls
                            .entry(found.as_str().to_owned())
                            .or_insert_with(|| place.clone());
                    }
                    if *directory != "defaults"
                        && let Some(captures) = MODULE.captures(line)
                    {
                        roles
                            .modules
                            .entry(captures[1].to_owned())
                            .or_insert_with(|| place.clone());
                    }
                }
            }
        }
    }
    Ok(roles)
}

/// Everything wrong between the inventory and the roles; empty means they agree.
pub fn coverage(sources: &Sources, roles: &Roles) -> Vec<String> {
    let mut problems = Vec::new();
    let mut prefixes: Vec<(String, String)> = Vec::new();
    for source in &sources.sources {
        if let Some(variable) = &source.url_var
            && !roles.defaults.contains_key(variable)
        {
            problems.push(format!(
                "{}: url_var {variable} is not a role default",
                source.name
            ));
        }
        for variable in [&source.version_var, &source.sha256_var]
            .into_iter()
            .flatten()
        {
            if !roles.defaults.contains_key(variable) {
                problems.push(format!("{}: {variable} is not a role default", source.name));
            }
        }
        for prefix in matched_prefixes(source, &roles.defaults) {
            prefixes.push((prefix, source.name.clone()));
        }
    }
    for entry in &sources.not_dependencies {
        prefixes.push((entry.url.clone(), format!("not_dependencies {}", entry.url)));
    }
    let mut used: BTreeSet<&str> = BTreeSet::new();
    for (url, place) in &roles.urls {
        let owners: Vec<&str> = prefixes
            .iter()
            .filter(|(prefix, _)| url.starts_with(prefix.as_str()))
            .map(|(_, owner)| owner.as_str())
            .collect();
        if owners.is_empty() {
            problems.push(format!("undeclared URL {url} ({place})"));
        }
        used.extend(owners);
    }
    for (module, place) in &roles.modules {
        if !PACKAGE_MODULES.contains(&module.as_str()) {
            continue;
        }
        let owners: Vec<&Source> = sources
            .sources
            .iter()
            .filter(|source| source.modules.iter().any(|named| named == module))
            .collect();
        if owners.is_empty() {
            problems.push(format!(
                "package module {module} ({place}) has no source naming it in modules"
            ));
        }
        used.extend(owners.iter().map(|source| source.name.as_str()));
    }
    for source in &sources.sources {
        for module in &source.modules {
            if !PACKAGE_MODULES.contains(&module.as_str()) {
                problems.push(format!(
                    "{}: {module} is not a package module ({})",
                    source.name,
                    PACKAGE_MODULES.join(", ")
                ));
            } else if !roles.modules.contains_key(module) {
                problems.push(format!(
                    "stale source {}: no task calls {module}",
                    source.name
                ));
            }
        }
        if !used.contains(source.name.as_str()) && source.modules.is_empty() {
            problems.push(format!(
                "stale source {}: no URL in the roles matches it",
                source.name
            ));
        }
    }
    for entry in &sources.not_dependencies {
        if !used.contains(format!("not_dependencies {}", entry.url).as_str()) {
            problems.push(format!(
                "stale not_dependencies entry {}: no URL in the roles matches it",
                entry.url
            ));
        }
    }
    problems
}

/// The URL prefixes a source accounts for.
fn matched_prefixes(source: &Source, defaults: &HashMap<String, Value>) -> Vec<String> {
    let mut prefixes = Vec::new();
    if let Some(url) = &source.url {
        prefixes.push(cut_template(url));
    }
    if let Some(value) = source.url_var.as_ref().and_then(|name| defaults.get(name)) {
        prefixes.push(cut_template(&yaml::display(value)));
    }
    prefixes.extend(source.covers.iter().map(|url| cut_template(url)));
    prefixes.extend(source.keys.iter().map(|url| cut_template(url)));
    prefixes.retain(|prefix| !prefix.is_empty());
    prefixes
}

fn cut_template(text: &str) -> String {
    let text = text.trim();
    text.split("{{").next().unwrap_or(text).trim().to_owned()
}

/// `text` with every `{{ name }}` replaced by that role default when it is a
/// scalar; anything else stays as written.
fn render(text: &str, defaults: &HashMap<String, Value>) -> String {
    static VARIABLE: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}").expect("valid regex")
    });
    VARIABLE
        .replace_all(
            text.trim(),
            |captures: &regex::Captures<'_>| match defaults.get(&captures[1]) {
                Some(value @ (Value::String(_) | Value::Number(_) | Value::Bool(_))) => {
                    yaml::display(value)
                }
                _ => captures[0].to_owned(),
            },
        )
        .into_owned()
}

/// One component per declared source.
pub fn components(sources: &Sources, defaults: &HashMap<String, Value>) -> Vec<Component> {
    sources
        .sources
        .iter()
        .map(|source| component(source, defaults))
        .collect()
}

fn component(source: &Source, defaults: &HashMap<String, Value>) -> Component {
    let scalar = |name: &Option<String>| {
        name.as_ref()
            .and_then(|name| defaults.get(name))
            .map(yaml::display)
            .filter(|text| !text.is_empty())
    };
    let version = scalar(&source.version_var);
    let url = match (&source.url, &source.url_var) {
        (Some(url), _) => Some(url.trim().to_owned()),
        (None, Some(_)) => scalar(&source.url_var).map(|value| render(&value, defaults)),
        (None, None) => source.reference_url.clone(),
    };
    let mut properties = vec![
        ("curiosity:source".to_owned(), source.source.clone()),
        ("curiosity:pinned".to_owned(), source.pinned.to_string()),
    ];
    properties.extend(
        source
            .declared_in
            .iter()
            .map(|place| ("curiosity:declared-in".to_owned(), place.clone())),
    );
    properties.extend(
        source
            .modules
            .iter()
            .map(|module| ("curiosity:module".to_owned(), module.clone())),
    );
    if let Some(note) = &source.note {
        properties.push(("curiosity:note".to_owned(), note.clone()));
    }
    let mut references = Vec::new();
    match url {
        Some(url) if url.contains("{{") => {
            properties.push(("curiosity:url-template".to_owned(), url));
        }
        Some(url) => references.push(Reference {
            kind: "distribution",
            url,
            comment: None,
        }),
        None => {}
    }
    references.extend(source.keys.iter().map(|key| Reference {
        kind: "other",
        url: key.clone(),
        comment: Some("signing key or signature".into()),
    }));
    let purl = source.purl.as_ref().map(|purl| match &version {
        Some(version) => format!("{purl}@{version}"),
        None => purl.clone(),
    });
    Component {
        bom_ref: format!("source:{}", source.name),
        kind: source.kind.kind(),
        component_type: source
            .component_type
            .clone()
            .unwrap_or_else(|| source.kind.default_type().to_owned()),
        name: source.name.clone(),
        version,
        description: source.description.clone(),
        purl,
        hashes: scalar(&source.sha256_var).into_iter().collect(),
        references,
        properties,
        ..Component::default()
    }
}

fn sorted_children(directory: &Path) -> Result<Vec<PathBuf>> {
    let mut children: Vec<PathBuf> = std::fs::read_dir(directory)
        .with_context(|| format!("cannot list {}", directory.display()))?
        .map(|entry| entry.map(|entry| entry.path()))
        .collect::<std::io::Result<_>>()
        .with_context(|| format!("cannot list {}", directory.display()))?;
    children.retain(|path| path.is_dir());
    children.sort();
    Ok(children)
}

/// Every `.yml` and `.yaml` file under `directory`, recursively and sorted;
/// nothing when it does not exist.
fn yaml_files(directory: &Path) -> Result<Vec<PathBuf>> {
    let mut files = Vec::new();
    if !directory.is_dir() {
        return Ok(files);
    }
    let mut pending = vec![directory.to_path_buf()];
    while let Some(current) = pending.pop() {
        for entry in std::fs::read_dir(&current)
            .with_context(|| format!("cannot list {}", current.display()))?
        {
            let path = entry
                .with_context(|| format!("cannot list {}", current.display()))?
                .path();
            if path.is_dir() {
                pending.push(path);
            } else if matches!(
                path.extension().and_then(|extension| extension.to_str()),
                Some("yml" | "yaml")
            ) {
                files.push(path);
            }
        }
    }
    files.sort();
    Ok(files)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn defaults(text: &str) -> HashMap<String, Value> {
        yaml::parse(text, "test")
            .expect("valid YAML")
            .as_mapping()
            .expect("mapping")
            .iter()
            .map(|(name, value)| (yaml::display(name), value.clone()))
            .collect()
    }

    #[test]
    fn templates_render_from_scalar_defaults() {
        let defaults = defaults("release: '0.5.6'\narch: {amd64: x}\n");
        assert_eq!(
            render("https://x/v{{ release }}/a-{{release}}.so", &defaults),
            "https://x/v0.5.6/a-0.5.6.so"
        );
        assert_eq!(
            render("https://x/{{ arch }}/{{ _fact }}", &defaults),
            "https://x/{{ arch }}/{{ _fact }}"
        );
    }

    #[test]
    fn prefixes_stop_at_the_first_template() {
        assert_eq!(cut_template(" https://x/v{{ a }}/b "), "https://x/v");
    }
}
