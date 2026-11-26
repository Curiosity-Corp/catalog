//! The release SBOM (catalog#21, docs/sbom.md): a CycloneDX 1.6 document for
//! the built collection artifact that answers "what does this collection pull
//! in?". It lists, each marked with a `curiosity:component-kind` property:
//!
//! - `source-file`: every file in the artifact, with its SHA-256;
//! - `collection-dependency`: the `galaxy.yml` dependencies, with the declared
//!   range and the version CI resolves and tests (`ci/requirements.yml`);
//! - `ci-tool`: the locked CI toolchain (`ci/requirements.txt`,
//!   `ci/requirements.yml`, the digest-pinned job images, `tools/Cargo.lock`);
//! - `runtime-package-source`, `runtime-package`, `external-installer`: where
//!   the roles install from, declared in `ci/sbom-sources.yml` and held to the
//!   roles by [`sources::coverage`].
//!
//! [`check`] recomputes the declared components and reports every one that a
//! given SBOM lacks or describes differently, so CI fails when a declared
//! dependency is missing from the published SBOM.

pub mod archive;
pub mod bom;
pub mod ci;
pub mod sources;

use std::collections::BTreeMap;
use std::path::Path;

use anyhow::{Context, Result};
use serde_json::Value as Json;

pub use bom::{Component, Kind};

/// Where the runtime source inventory lives, relative to the checkout root.
pub const SOURCES: &str = "ci/sbom-sources.yml";

/// Everything the SBOM declares, before it is rendered.
pub struct Inventory {
    pub collection: archive::Collection,
    pub components: Vec<Component>,
}

/// Gather the components for `archive` from the checkout at `root`. Fails when
/// `ci/sbom-sources.yml` and the roles disagree.
pub fn inventory(root: &Path, archive_path: &Path) -> Result<Inventory> {
    let collection = archive::read(archive_path)?;
    let mut components = collection.files.clone();
    components.extend(ci::collection_dependencies(root)?);
    components.extend(ci::toolchain(root)?);
    let declared = sources::load(&root.join(SOURCES))?;
    let roles = sources::scan_roles(&root.join("roles"))?;
    let problems = sources::coverage(&declared, &roles);
    if !problems.is_empty() {
        anyhow::bail!(
            "{SOURCES} does not match the roles:\n  - {}\nDeclare each source in {SOURCES} \
             (or list it under not_dependencies with a reason); see docs/sbom.md",
            problems.join("\n  - ")
        );
    }
    components.extend(sources::components(&declared, &roles.defaults));
    bom::sort(&mut components);
    bom::ensure_unique(&components)?;
    Ok(Inventory {
        collection,
        components,
    })
}

/// The CycloneDX document for `archive_path`.
pub fn generate(root: &Path, archive_path: &Path) -> Result<Json> {
    let inventory = inventory(root, archive_path)?;
    Ok(bom::render(&inventory, &timestamp()?))
}

/// Every declared component that `published` lacks or describes differently,
/// and every component it has that is not declared. Empty means complete.
pub fn check(root: &Path, archive_path: &Path, published: &Json) -> Result<Vec<String>> {
    let inventory = inventory(root, archive_path)?;
    let expected: BTreeMap<String, Json> = inventory
        .components
        .iter()
        .map(|component| {
            (
                component.bom_ref.clone(),
                bom::identity(&component.to_json()),
            )
        })
        .collect();
    let mut actual: BTreeMap<String, Json> = BTreeMap::new();
    let listed = published
        .get("components")
        .and_then(Json::as_array)
        .context("the SBOM has no components array")?;
    for component in listed {
        let reference = component
            .get("bom-ref")
            .and_then(Json::as_str)
            .context("an SBOM component has no bom-ref")?;
        actual.insert(reference.to_owned(), bom::identity(component));
    }
    let mut problems = Vec::new();
    for (reference, identity) in &expected {
        match actual.get(reference) {
            None => problems.push(format!("missing: {reference}")),
            Some(found) if found != identity => problems.push(format!(
                "differs: {reference}: expected {identity}, found {found}"
            )),
            Some(_) => {}
        }
    }
    for reference in actual.keys() {
        if !expected.contains_key(reference) {
            problems.push(format!("not declared: {reference}"));
        }
    }
    let subject = &inventory.collection.archive_sha256;
    let published_digest = published
        .pointer("/metadata/component/hashes/0/content")
        .and_then(Json::as_str);
    if published_digest != Some(subject.as_str()) {
        problems.push(format!(
            "the SBOM describes another artifact: expected SHA-256 {subject}, found {}",
            published_digest.unwrap_or("none")
        ));
    }
    Ok(problems)
}

/// RFC 3339 time of `SOURCE_DATE_EPOCH` (CI sets it, so builds reproduce), or
/// of now when it is unset.
fn timestamp() -> Result<String> {
    let seconds = match std::env::var("SOURCE_DATE_EPOCH") {
        Ok(text) => text
            .trim()
            .parse::<i64>()
            .with_context(|| format!("SOURCE_DATE_EPOCH is not a number: {text:?}"))?,
        Err(_) => std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .context("the system clock is before 1970")?
            .as_secs()
            .try_into()
            .context("the system clock is out of range")?,
    };
    Ok(bom::rfc3339(seconds))
}
