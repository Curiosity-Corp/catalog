//! End-to-end: the package list, evidence files and the drift check on a
//! small catalog checkout in a temporary directory.

use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

use catalog_tools::packages::{self, Evidence, PlatformResult};

const PACKAGES: &str = "---
- name: Add a repository first
  ansible.builtin.debug: {msg: unrelated}

- name: Install all APT packages
  ansible.builtin.apt:
    name:
      - git
      - libfuse2
      - \"{{ extra_package }}\"
      - tokei
    state: present
";

fn write(root: &Path, path: &str, text: &str) {
    let path = root.join(path);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, text).unwrap();
}

fn checkout() -> tempfile::TempDir {
    let directory = tempfile::tempdir().unwrap();
    write(directory.path(), "galaxy.yml", "namespace: curiosity\n");
    write(directory.path(), packages::PACKAGES_TASK, PACKAGES);
    directory
}

fn result(image: &str) -> PlatformResult {
    PlatformResult {
        image: image.to_owned(),
        id: "ubuntu".into(),
        version: "24.04".into(),
        codename: "noble".into(),
        architecture: "amd64".into(),
        available: 1,
        candidates: BTreeMap::from([("git".to_owned(), "1:2.43.0".to_owned())]),
        provided_by: BTreeMap::from([("libfuse2".to_owned(), "libfuse2t64".to_owned())]),
        missing: vec!["tokei".to_owned()],
    }
}

#[test]
fn role_packages_skips_templated_names() {
    let directory = checkout();
    let names = packages::role_packages(directory.path()).unwrap();
    assert_eq!(names, ["git", "libfuse2", "tokei"]);
}

#[test]
fn newest_evidence_matches_or_reports_drift() {
    let directory = checkout();
    let root = directory.path();
    let names = packages::role_packages(root).unwrap();
    let evidence = packages::assemble("2026-11-23", &names, vec![result("ubuntu:24.04")]).unwrap();
    let older = Evidence {
        date: "2026-11-04".into(),
        ..evidence.clone()
    };
    let evidence_dir = format!("{}/", packages::EVIDENCE_DIR);
    write(
        root,
        &format!("{evidence_dir}platform-packages-2026-11-04.json"),
        &packages::to_json(&older).unwrap(),
    );
    write(
        root,
        &format!("{evidence_dir}platform-packages-2026-11-23.json"),
        &packages::to_json(&evidence).unwrap(),
    );
    write(
        root,
        &format!("{evidence_dir}monthly-template.md"),
        "not evidence\n",
    );

    let newest = packages::newest_evidence(root).unwrap();
    assert!(
        newest.ends_with("platform-packages-2026-11-23.json"),
        "{newest:?}"
    );
    let read: Evidence = packages::read_json(&newest).unwrap();
    assert_eq!(read, evidence);
    assert!(packages::check(&read, &names).is_empty());

    // The role gains a package: the committed evidence is now stale.
    write(
        root,
        packages::PACKAGES_TASK,
        &PACKAGES.replace("      - tokei\n", "      - tokei\n      - usbmuxd\n"),
    );
    let grown = packages::role_packages(root).unwrap();
    let problems = packages::check(&read, &grown);
    assert!(
        problems.iter().any(|problem| problem.contains("usbmuxd")),
        "{problems:?}"
    );
}

#[test]
fn missing_install_task_is_an_error() {
    let directory = checkout();
    write(
        directory.path(),
        packages::PACKAGES_TASK,
        "---\n- name: Something else\n  ansible.builtin.debug: {msg: hi}\n",
    );
    let error = packages::role_packages(directory.path()).unwrap_err();
    assert!(error.to_string().contains("not found"), "{error}");
}
