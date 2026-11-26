//! The collection's declared dependencies and the locked CI toolchain
//! (catalog#22, docs/ci-toolchain.md).

use std::collections::BTreeSet;
use std::path::Path;
use std::sync::LazyLock;

use anyhow::{Context, Result, bail};
use regex::Regex;
use serde_norway::Value;

use super::bom::{Component, Kind, Reference};
use crate::yaml;

pub const GALAXY: &str = "galaxy.yml";
pub const CI_COLLECTIONS: &str = "ci/requirements.yml";
pub const PYTHON_DIRECT: &str = "ci/requirements.in";
pub const PYTHON_LOCK: &str = "ci/requirements.txt";
pub const PIPELINE: &str = ".gitlab-ci.yml";
pub const CARGO_LOCK: &str = "tools/Cargo.lock";

const GALAXY_URL: &str = "https://galaxy.ansible.com";

/// `galaxy.yml` dependencies, each at the exact version CI installs and tests
/// (`ci/requirements.yml`). A dependency CI does not pin is an error: its
/// resolved version would be unknown.
pub fn collection_dependencies(root: &Path) -> Result<Vec<Component>> {
    let galaxy = yaml::load(&root.join(GALAXY))?;
    let pins = ci_collections(root)?;
    let mut components = Vec::new();
    let Some(dependencies) = galaxy.get("dependencies").and_then(Value::as_mapping) else {
        return Ok(components);
    };
    for (name, range) in dependencies {
        let name = yaml::display(name);
        let range = yaml::display(range);
        let Some((_, version)) = pins.iter().find(|(pinned, _)| *pinned == name) else {
            bail!(
                "{GALAXY} depends on {name} ({range}), but {CI_COLLECTIONS} does not pin it, \
                 so the SBOM cannot name the resolved version"
            );
        };
        let mut component = collection_component(&name, version, Kind::CollectionDependency);
        component.bom_ref = format!("collection:{name}");
        component
            .properties
            .push(("curiosity:declared-range".into(), range));
        component
            .properties
            .push(("curiosity:resolved-from".into(), CI_COLLECTIONS.to_owned()));
        components.push(component);
    }
    Ok(components)
}

/// The locked CI toolchain: Python packages, Ansible collections, job images
/// and the crates of `catalog-tools`.
pub fn toolchain(root: &Path) -> Result<Vec<Component>> {
    let mut components = python_lock(root)?;
    for (name, version) in ci_collections(root)? {
        let mut component = collection_component(&name, &version, Kind::CiTool);
        component.bom_ref = format!("ci-collection:{name}");
        component
            .properties
            .push(("curiosity:locked-in".into(), CI_COLLECTIONS.to_owned()));
        components.push(component);
    }
    components.extend(images(&yaml::read(&root.join(PIPELINE))?));
    components.extend(cargo_lock(&yaml::read(&root.join(CARGO_LOCK))?));
    Ok(components)
}

fn collection_component(name: &str, version: &str, kind: Kind) -> Component {
    let (namespace, short) = name.split_once('.').unwrap_or(("", name));
    Component {
        kind,
        component_type: "library".into(),
        group: Some(namespace.to_owned()),
        name: short.to_owned(),
        version: Some(version.to_owned()),
        purl: Some(format!("pkg:generic/ansible-collection/{name}@{version}")),
        references: vec![Reference {
            kind: "distribution",
            url: format!("{GALAXY_URL}/ui/repo/published/{namespace}/{short}/"),
            comment: Some("Ansible Galaxy".into()),
        }],
        ..Component::default()
    }
}

/// `ci/requirements.yml`: name and exact version of each collection.
fn ci_collections(root: &Path) -> Result<Vec<(String, String)>> {
    let document = yaml::load(&root.join(CI_COLLECTIONS))?;
    let mut pins = Vec::new();
    for entry in yaml::as_list(document.get("collections")) {
        let name = entry
            .get("name")
            .map(yaml::display)
            .with_context(|| format!("{CI_COLLECTIONS}: a collection has no name"))?;
        let version = entry
            .get("version")
            .map(yaml::display)
            .with_context(|| format!("{CI_COLLECTIONS}: {name} has no version"))?;
        pins.push((name, version));
    }
    Ok(pins)
}

/// PyPI's normalised project name (PEP 503).
fn normalise(name: &str) -> String {
    static SEPARATORS: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"[-_.]+").expect("valid regex"));
    SEPARATORS
        .replace_all(&name.to_lowercase(), "-")
        .into_owned()
}

/// Every package of the hash-checked lock, marked direct when
/// `ci/requirements.in` names it.
fn python_lock(root: &Path) -> Result<Vec<Component>> {
    static PIN: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([^\s;\\]+)")
            .expect("valid regex")
    });
    static HASH: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"--hash=sha256:([0-9a-f]{64})").expect("valid regex"));
    let direct: BTreeSet<String> = yaml::read(&root.join(PYTHON_DIRECT))?
        .lines()
        .filter_map(|line| PIN.captures(line.trim()))
        .map(|captures| normalise(&captures[1]))
        .collect();
    let lock = yaml::read(&root.join(PYTHON_LOCK))?;
    let mut components: Vec<Component> = Vec::new();
    for line in lock.lines() {
        if let Some(captures) = PIN.captures(line) {
            let name = normalise(&captures[1]);
            let version = captures[2].to_owned();
            components.push(Component {
                bom_ref: format!("ci-python:{name}"),
                kind: Kind::CiTool,
                component_type: "library".into(),
                purl: Some(format!("pkg:pypi/{name}@{version}")),
                name: name.clone(),
                version: Some(version),
                properties: vec![
                    ("curiosity:locked-in".into(), PYTHON_LOCK.to_owned()),
                    (
                        "curiosity:direct".into(),
                        direct.contains(&name).to_string(),
                    ),
                ],
                ..Component::default()
            });
        } else if let (Some(captures), Some(current)) = (HASH.captures(line), components.last_mut())
        {
            current.hashes.push(captures[1].to_owned());
        }
    }
    for name in &direct {
        if !components.iter().any(|component| &component.name == name) {
            bail!("{PYTHON_DIRECT} pins {name}, but {PYTHON_LOCK} does not lock it");
        }
    }
    Ok(components)
}

/// Every digest-pinned image a CI job runs in.
fn images(pipeline: &str) -> Vec<Component> {
    static IMAGE: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r#"(?m)^\s*(?:image|name):\s*["']?([^\s"'#@]+)@sha256:([0-9a-f]{64})"#)
            .expect("valid regex")
    });
    let mut seen = BTreeSet::new();
    let mut components = Vec::new();
    for captures in IMAGE.captures_iter(pipeline) {
        let reference = captures[1].to_owned();
        let digest = captures[2].to_owned();
        if !seen.insert((reference.clone(), digest.clone())) {
            continue;
        }
        let (repository, tag) = match reference.rsplit_once(':') {
            Some((repository, tag)) if !tag.contains('/') => (repository.to_owned(), tag),
            _ => (reference.clone(), "latest"),
        };
        let short = repository
            .rsplit('/')
            .next()
            .unwrap_or(&repository)
            .to_owned();
        components.push(Component {
            bom_ref: format!("ci-image:{reference}@sha256:{digest}"),
            kind: Kind::CiTool,
            component_type: "container".into(),
            name: short.clone(),
            version: Some(tag.to_owned()),
            purl: Some(format!(
                "pkg:oci/{short}@sha256%3A{digest}?repository_url={repository}&tag={tag}"
            )),
            hashes: vec![digest],
            properties: vec![("curiosity:locked-in".into(), PIPELINE.to_owned())],
            ..Component::default()
        });
    }
    components
}

/// The registry crates in `tools/Cargo.lock` (the workspace's own crates have
/// no source and are left out).
fn cargo_lock(lock: &str) -> Vec<Component> {
    let mut components = Vec::new();
    for block in lock.split("[[package]]").skip(1) {
        let field = |key: &str| {
            block.lines().find_map(|line| {
                let (name, value) = line.split_once(" = ")?;
                (name.trim() == key).then(|| value.trim().trim_matches('"').to_owned())
            })
        };
        let (Some(name), Some(version), Some(source)) =
            (field("name"), field("version"), field("source"))
        else {
            continue;
        };
        if !source.starts_with("registry+") {
            continue;
        }
        components.push(Component {
            bom_ref: format!("ci-cargo:{name}@{version}"),
            kind: Kind::CiTool,
            component_type: "library".into(),
            purl: Some(format!("pkg:cargo/{name}@{version}")),
            name,
            version: Some(version),
            hashes: field("checksum").into_iter().collect(),
            properties: vec![("curiosity:locked-in".into(), CARGO_LOCK.to_owned())],
            ..Component::default()
        });
    }
    components
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pypi_names_are_normalised() {
        assert_eq!(normalise("PyYAML"), "pyyaml");
        assert_eq!(normalise("ruamel.yaml.clib"), "ruamel-yaml-clib");
        assert_eq!(normalise("typing_extensions"), "typing-extensions");
    }

    #[test]
    fn images_need_a_digest_and_are_listed_once() {
        let digest = "a".repeat(64);
        let pipeline = format!(
            "image: example.org/library/python:3.13-slim@sha256:{digest}\n\
             job:\n  image: example.org/library/python:3.13-slim@sha256:{digest}\n\
             other:\n  image: python:${{VERSION}}-slim\n\
             third:\n  image:\n    name: example.org/rust:1@sha256:{digest}\n"
        );
        let found = images(&pipeline);
        assert_eq!(found.len(), 2);
        assert_eq!(found[0].name, "python");
        assert_eq!(found[0].version.as_deref(), Some("3.13-slim"));
        assert_eq!(found[1].name, "rust");
    }

    #[test]
    fn cargo_lock_skips_workspace_crates() {
        let lock = "version = 4\n\n[[package]]\nname = \"anyhow\"\nversion = \"1.0.100\"\n\
                    source = \"registry+https://github.com/rust-lang/crates.io-index\"\n\
                    checksum = \"abc\"\n\n[[package]]\nname = \"catalog-tools\"\nversion = \"0.1.0\"\n";
        let found = cargo_lock(lock);
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].bom_ref, "ci-cargo:anyhow@1.0.100");
        assert_eq!(found[0].hashes, vec!["abc".to_owned()]);
    }
}
