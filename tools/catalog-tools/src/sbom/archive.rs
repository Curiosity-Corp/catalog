//! The built collection artifact: its identity from `MANIFEST.json` and one
//! `source-file` component per file it contains.

use std::fs::File;
use std::io::Read;
use std::path::Path;

use anyhow::{Context, Result, bail};
use flate2::read::GzDecoder;
use sha2::{Digest, Sha256};

use super::bom::{Component, Kind};

/// The collection an artifact holds.
pub struct Collection {
    pub namespace: String,
    pub name: String,
    pub version: String,
    pub license: Vec<String>,
    pub archive_sha256: String,
    pub files: Vec<Component>,
}

/// Read a `namespace-name-version.tar.gz` built by `ansible-galaxy collection build`.
pub fn read(path: &Path) -> Result<Collection> {
    let bytes = std::fs::read(path).with_context(|| format!("cannot read {}", path.display()))?;
    let archive_sha256 = sha256_hex(&bytes);
    let file = File::open(path).with_context(|| format!("cannot open {}", path.display()))?;
    let mut archive = tar::Archive::new(GzDecoder::new(file));
    let mut files = Vec::new();
    let mut manifest = None;
    let entries = archive
        .entries()
        .with_context(|| format!("{} is not a gzip-compressed tar archive", path.display()))?;
    for entry in entries {
        let mut entry =
            entry.with_context(|| format!("cannot read an entry of {}", path.display()))?;
        if !entry.header().entry_type().is_file() {
            continue;
        }
        let name = entry
            .path()
            .with_context(|| format!("an entry of {} has an unreadable path", path.display()))?
            .to_string_lossy()
            .into_owned();
        let mut contents = Vec::new();
        entry
            .read_to_end(&mut contents)
            .with_context(|| format!("cannot read {name} in {}", path.display()))?;
        if name == "MANIFEST.json" {
            manifest = Some(
                serde_json::from_slice::<serde_json::Value>(&contents)
                    .with_context(|| format!("MANIFEST.json in {} is not JSON", path.display()))?,
            );
        }
        files.push(Component {
            bom_ref: format!("file:{name}"),
            kind: Kind::SourceFile,
            component_type: "file".into(),
            name: name.clone(),
            hashes: vec![sha256_hex(&contents)],
            ..Component::default()
        });
    }
    let Some(manifest) = manifest else {
        bail!("{} does not contain MANIFEST.json", path.display());
    };
    let info = manifest
        .get("collection_info")
        .with_context(|| format!("MANIFEST.json in {} has no collection_info", path.display()))?;
    let text = |key: &str| -> Result<String> {
        info.get(key)
            .and_then(serde_json::Value::as_str)
            .map(str::to_owned)
            .with_context(|| format!("MANIFEST.json collection_info has no {key}"))
    };
    let license = info
        .get("license")
        .and_then(serde_json::Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(serde_json::Value::as_str)
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default();
    Ok(Collection {
        namespace: text("namespace")?,
        name: text("name")?,
        version: text("version")?,
        license,
        archive_sha256,
        files,
    })
}

/// Lower-case hexadecimal SHA-256 of `bytes`.
pub fn sha256_hex(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sha256_of_empty_input() {
        assert_eq!(
            sha256_hex(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
    }
}
