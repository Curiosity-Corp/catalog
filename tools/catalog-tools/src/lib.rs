//! Tools for the `curiosity.workstation` Ansible collection (catalog#160).
//!
//! The collection itself stays Ansible; this crate holds the tooling around it,
//! one subcommand per tool (docs/decisions/0005-rust-tool-crate.md).

pub mod diagnostics;
pub mod packages;
pub mod reference;
pub mod yaml;

use std::path::{Path, PathBuf};

use anyhow::{Result, bail};

/// The catalog checkout that contains `start`: the nearest directory with both
/// `galaxy.yml` and `roles/`.
pub fn find_root(start: &Path) -> Result<PathBuf> {
    for directory in start.ancestors() {
        if directory.join("galaxy.yml").is_file() && directory.join("roles").is_dir() {
            return Ok(directory.to_path_buf());
        }
    }
    bail!(
        "no catalog checkout (galaxy.yml and roles/) at or above {}; pass --root",
        start.display()
    )
}
