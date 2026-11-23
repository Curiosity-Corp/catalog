//! The supported variable and tag reference (catalog#27): `docs/reference.md`,
//! generated from the role defaults and tasks plus the curated
//! `docs/reference-annotations.yml`, so the documented configuration surface
//! cannot drift from the code.

pub mod annotations;
pub mod defaults;
pub mod render;
pub mod tasks;

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde_norway::Value;

use annotations::Annotations;
use defaults::Defaults;
use tasks::TagUse;

/// Where the reference and its annotations live, relative to the checkout root.
pub const OUTPUT: &str = "docs/reference.md";
pub const ANNOTATIONS: &str = "docs/reference-annotations.yml";

/// What the reference knows about one role.
pub struct Role {
    pub name: String,
    pub defaults: Defaults,
    pub tags: BTreeMap<String, TagUse>,
    pub asserted: HashMap<String, Vec<Value>>,
}

/// Every role under `roles/` that has `defaults/main.yml`, sorted by name.
pub fn load_roles(roles_dir: &Path) -> Result<Vec<Role>> {
    let mut directories: Vec<PathBuf> = std::fs::read_dir(roles_dir)
        .with_context(|| format!("cannot list {}", roles_dir.display()))?
        .map(|entry| entry.map(|entry| entry.path()))
        .collect::<std::io::Result<_>>()
        .with_context(|| format!("cannot list {}", roles_dir.display()))?;
    directories.retain(|path| path.join("defaults/main.yml").is_file());
    directories.sort();
    directories
        .into_iter()
        .map(|directory| {
            let name = directory
                .file_name()
                .map(|name| name.to_string_lossy().into_owned())
                .unwrap_or_default();
            let defaults_path = directory.join("defaults/main.yml");
            let defaults = defaults::parse(
                &crate::yaml::read(&defaults_path)?,
                &defaults_path.display().to_string(),
            )?;
            let files = tasks::load_task_files(&directory)?;
            let tags =
                tasks::collect_tags(&files, &defaults).with_context(|| format!("role {name}"))?;
            let asserted = tasks::collect_asserted_values(&files, &defaults);
            Ok(Role {
                name,
                defaults,
                tags,
                asserted,
            })
        })
        .collect()
}

/// Build the reference text for the checkout at `root`.
pub fn generate(root: &Path) -> Result<String> {
    let roles = load_roles(&root.join("roles"))?;
    let annotations_path = root.join(ANNOTATIONS);
    let annotations = Annotations::parse(
        &crate::yaml::read(&annotations_path)?,
        &annotations_path.display().to_string(),
    )?;
    let variables: BTreeSet<&str> = roles
        .iter()
        .flat_map(|role| role.defaults.names())
        .collect();
    let tags: BTreeSet<&str> = roles
        .iter()
        .flat_map(|role| role.tags.keys().map(String::as_str))
        .collect();
    annotations.check_names(&variables, &tags)?;
    Ok(render::render(&roles, &annotations))
}

/// `None` when the texts are equal, otherwise where they first differ.
pub fn first_difference(committed: &str, generated: &str) -> Option<String> {
    if committed == generated {
        return None;
    }
    let old: Vec<&str> = committed.lines().collect();
    let new: Vec<&str> = generated.lines().collect();
    let line = old
        .iter()
        .zip(&new)
        .position(|(a, b)| a != b)
        .unwrap_or(old.len().min(new.len()));
    let show = |lines: &[&str]| {
        lines
            .get(line)
            .map_or("(end of file)".to_owned(), |text| (*text).to_owned())
    };
    Some(format!(
        "first difference at line {}\n  committed: {}\n  generated: {}\n(committed {} lines, generated {} lines)",
        line + 1,
        show(&old),
        show(&new),
        old.len(),
        new.len()
    ))
}
