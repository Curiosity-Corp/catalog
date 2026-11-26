//! End-to-end: the release SBOM for a small catalog checkout and artifact in a
//! temporary directory.

use std::fs;
use std::path::{Path, PathBuf};

use catalog_tools::sbom;
use flate2::Compression;
use flate2::write::GzEncoder;
use serde_json::Value;

const DIGEST: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

const GALAXY: &str = "---
namespace: curiosity
name: workstation
version: 0.1.0
dependencies:
  community.general: '>=9.0.0'
";

const CI_COLLECTIONS: &str = "---
collections:
  - name: community.general
    version: '14.0.1'
";

const PYTHON_DIRECT: &str = "# direct pins\nansible-core==2.22.0\n";

const PYTHON_LOCK: &str = "# lock
ansible-core==2.22.0 \\
    --hash=sha256:8285d4cc81c3e3eb2d954e75531724fe0ae154d228475638123451f034a74657
    # via -r ci/requirements.in
PyYAML==6.0.4 \\
    --hash=sha256:bf50b4a0cfa985e30585c7ba301897ea7050d224ca49d2816cab10e33a160e1c
    # via ansible-core
";

const CARGO_LOCK: &str = "version = 4

[[package]]
name = \"anyhow\"
version = \"1.0.100\"
source = \"registry+https://github.com/rust-lang/crates.io-index\"
checksum = \"a23eb6b1614318a8071c9b2521f36b424b2c83db5eb3a0fead4a6c0809af6e61\"

[[package]]
name = \"catalog-tools\"
version = \"0.1.0\"
";

const DEFAULTS: &str = "---
tool_release: '1.2.3'
tool_sha256: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
tool_url: >-
  https://downloads.example.net/tool/v{{ tool_release }}/tool.tar.gz
installer_url: https://vendor.example.net/install.sh
docs_url: https://docs.example.net/
";

const TASKS: &str = "---
- name: Vendor repository
  ansible.builtin.apt_repository:
    repo: deb [signed-by=/k.gpg] https://apt.example.net/stable stable main
- name: Packages
  ansible.builtin.apt:
    name: [tool]
- name: Version lookup
  ansible.builtin.uri:
    url: 'https://apt.example.net/stable/Release'
# https://commented.example.net/ is not a reference
";

const SOURCES: &str = "---
sources:
  - name: distribution-apt-archive
    kind: runtime-package-source
    source: apt-repository
    modules: [ansible.builtin.apt]
    declared_in: [roles/r/tasks/main.yml]
    pinned: false
  - name: apt-example
    kind: runtime-package-source
    source: apt-repository
    url: https://apt.example.net/stable
    declared_in: [roles/r/tasks/main.yml]
    pinned: false
  - name: tool
    kind: runtime-package
    source: release-download
    url_var: tool_url
    version_var: tool_release
    sha256_var: tool_sha256
    declared_in: [roles/r/defaults/main.yml]
    pinned: true
  - name: vendor-installer
    kind: external-installer
    source: installer-script
    url_var: installer_url
    declared_in: [roles/r/defaults/main.yml]
    pinned: false
not_dependencies:
  - url: https://docs.example.net/
    reason: documentation link
";

struct Checkout {
    _dir: tempfile::TempDir,
    root: PathBuf,
    archive: PathBuf,
}

fn write(root: &Path, relative: &str, text: &str) {
    let path = root.join(relative);
    fs::create_dir_all(path.parent().expect("has a parent")).expect("create directory");
    fs::write(path, text).expect("write file");
}

fn archive(path: &Path) {
    let file = fs::File::create(path).expect("create archive");
    let mut builder = tar::Builder::new(GzEncoder::new(file, Compression::default()));
    let manifest = r#"{"collection_info": {"namespace": "curiosity", "name": "workstation",
        "version": "0.1.0", "license": ["Apache-2.0"]}}"#;
    for (name, contents) in [
        ("MANIFEST.json", manifest.as_bytes()),
        ("roles/r/tasks/main.yml", TASKS.as_bytes()),
    ] {
        let mut header = tar::Header::new_gnu();
        header.set_size(contents.len() as u64);
        header.set_mode(0o644);
        header.set_cksum();
        builder
            .append_data(&mut header, name, contents)
            .expect("append entry");
    }
    builder
        .into_inner()
        .expect("finish tar")
        .finish()
        .expect("finish gzip");
}

fn checkout() -> Checkout {
    let dir = tempfile::tempdir().expect("temporary directory");
    let root = dir.path().to_path_buf();
    write(&root, "galaxy.yml", GALAXY);
    write(&root, "ci/requirements.yml", CI_COLLECTIONS);
    write(&root, "ci/requirements.in", PYTHON_DIRECT);
    write(&root, "ci/requirements.txt", PYTHON_LOCK);
    write(&root, "ci/sbom-sources.yml", SOURCES);
    write(
        &root,
        ".gitlab-ci.yml",
        &format!("image: public.ecr.aws/docker/library/python:3.13-slim@sha256:{DIGEST}\n"),
    );
    write(&root, "tools/Cargo.lock", CARGO_LOCK);
    write(&root, "roles/r/defaults/main.yml", DEFAULTS);
    write(&root, "roles/r/tasks/main.yml", TASKS);
    let archive_path = root.join("curiosity-workstation-0.1.0.tar.gz");
    archive(&archive_path);
    Checkout {
        _dir: dir,
        root,
        archive: archive_path,
    }
}

fn component<'a>(bom: &'a Value, reference: &str) -> &'a Value {
    bom["components"]
        .as_array()
        .expect("components")
        .iter()
        .find(|component| component["bom-ref"] == reference)
        .unwrap_or_else(|| panic!("{reference} is in the SBOM"))
}

fn kind(component: &Value) -> &str {
    component["properties"][0]["value"].as_str().expect("kind")
}

fn error(result: anyhow::Result<Value>) -> String {
    format!("{:#}", result.expect_err("generation fails"))
}

#[test]
fn every_declared_dependency_is_listed_with_its_kind() {
    let checkout = checkout();
    let bom = sbom::generate(&checkout.root, &checkout.archive).expect("SBOM");
    assert_eq!(bom["bomFormat"], "CycloneDX");
    assert_eq!(bom["specVersion"], "1.6");
    assert_eq!(bom["metadata"]["component"]["name"], "workstation");

    let file = component(&bom, "file:roles/r/tasks/main.yml");
    assert_eq!(kind(file), "source-file");
    assert_eq!(file["type"], "file");

    let dependency = component(&bom, "collection:community.general");
    assert_eq!(kind(dependency), "collection-dependency");
    assert_eq!(dependency["version"], "14.0.1");
    assert_eq!(dependency["scope"], "required");

    let python = component(&bom, "ci-python:pyyaml");
    assert_eq!(kind(python), "ci-tool");
    assert_eq!(python["purl"], "pkg:pypi/pyyaml@6.0.4");
    assert_eq!(python["scope"], "excluded");
    assert_eq!(
        kind(component(&bom, "ci-collection:community.general")),
        "ci-tool"
    );
    assert_eq!(kind(component(&bom, "ci-cargo:anyhow@1.0.100")), "ci-tool");
    let image = format!("ci-image:public.ecr.aws/docker/library/python:3.13-slim@sha256:{DIGEST}");
    assert_eq!(component(&bom, &image)["type"], "container");

    assert_eq!(
        kind(component(&bom, "source:apt-example")),
        "runtime-package-source"
    );
    let tool = component(&bom, "source:tool");
    assert_eq!(kind(tool), "runtime-package");
    assert_eq!(tool["version"], "1.2.3");
    assert_eq!(
        tool["externalReferences"][0]["url"],
        "https://downloads.example.net/tool/v1.2.3/tool.tar.gz"
    );
    assert_eq!(
        tool["hashes"][0]["content"].as_str().map(str::len),
        Some(64)
    );
    assert_eq!(
        kind(component(&bom, "source:vendor-installer")),
        "external-installer"
    );

    let depends: Vec<&str> = bom["dependencies"][0]["dependsOn"]
        .as_array()
        .expect("dependsOn")
        .iter()
        .filter_map(Value::as_str)
        .collect();
    assert!(depends.contains(&"collection:community.general"));
    assert!(depends.contains(&"source:tool"));
    assert!(!depends.iter().any(|reference| reference.starts_with("ci-")));
}

#[test]
fn the_same_inputs_give_the_same_document() {
    let checkout = checkout();
    let first = sbom::generate(&checkout.root, &checkout.archive).expect("SBOM");
    let second = sbom::generate(&checkout.root, &checkout.archive).expect("SBOM");
    assert_eq!(first["serialNumber"], second["serialNumber"]);
    assert_eq!(first["components"], second["components"]);
}

#[test]
fn check_passes_for_a_complete_sbom() {
    let checkout = checkout();
    let bom = sbom::generate(&checkout.root, &checkout.archive).expect("SBOM");
    let problems = sbom::check(&checkout.root, &checkout.archive, &bom).expect("check runs");
    assert!(problems.is_empty(), "{problems:?}");
}

#[test]
fn check_fails_when_a_declared_dependency_is_missing() {
    let checkout = checkout();
    let mut bom = sbom::generate(&checkout.root, &checkout.archive).expect("SBOM");
    for removed in [
        "collection:community.general",
        "ci-python:ansible-core",
        "source:apt-example",
        "source:vendor-installer",
    ] {
        bom["components"]
            .as_array_mut()
            .expect("components")
            .retain(|component| component["bom-ref"] != removed);
    }
    let problems = sbom::check(&checkout.root, &checkout.archive, &bom).expect("check runs");
    assert_eq!(
        problems,
        vec![
            "missing: ci-python:ansible-core",
            "missing: collection:community.general",
            "missing: source:apt-example",
            "missing: source:vendor-installer",
        ]
    );
}

#[test]
fn check_fails_when_a_version_changed_or_a_component_is_undeclared() {
    let checkout = checkout();
    let mut bom = sbom::generate(&checkout.root, &checkout.archive).expect("SBOM");
    let components = bom["components"].as_array_mut().expect("components");
    for component in components.iter_mut() {
        if component["bom-ref"] == "collection:community.general" {
            component["version"] = Value::from("9.0.0");
        }
    }
    let mut extra = components[0].clone();
    extra["bom-ref"] = Value::from("file:planted");
    components.push(extra);
    let problems = sbom::check(&checkout.root, &checkout.archive, &bom).expect("check runs");
    assert_eq!(problems.len(), 2, "{problems:?}");
    assert!(problems[0].starts_with("differs: collection:community.general"));
    assert_eq!(problems[1], "not declared: file:planted");
}

#[test]
fn check_fails_for_an_sbom_of_another_artifact() {
    let checkout = checkout();
    let mut bom = sbom::generate(&checkout.root, &checkout.archive).expect("SBOM");
    bom["metadata"]["component"]["hashes"][0]["content"] = Value::from(DIGEST);
    let problems = sbom::check(&checkout.root, &checkout.archive, &bom).expect("check runs");
    assert_eq!(problems.len(), 1);
    assert!(problems[0].contains("describes another artifact"));
}

#[test]
fn an_undeclared_url_in_the_roles_fails() {
    let checkout = checkout();
    write(
        &checkout.root,
        "roles/r/tasks/extra.yml",
        "---\n- name: New download\n  ansible.builtin.get_url:\n    url: https://new.example.net/x.tar.gz\n",
    );
    let message = error(sbom::generate(&checkout.root, &checkout.archive));
    assert!(
        message.contains(
            "undeclared URL https://new.example.net/x.tar.gz (roles/r/tasks/extra.yml:4)"
        ),
        "{message}"
    );
}

#[test]
fn an_undeclared_package_module_fails() {
    let checkout = checkout();
    write(
        &checkout.root,
        "roles/r/tasks/npm.yml",
        "---\n- name: Node tool\n  community.general.npm:\n    name: x\n    global: true\n",
    );
    let message = error(sbom::generate(&checkout.root, &checkout.archive));
    assert!(
        message.contains("package module community.general.npm (roles/r/tasks/npm.yml:3)"),
        "{message}"
    );
}

#[test]
fn a_stale_source_fails() {
    let checkout = checkout();
    write(
        &checkout.root,
        "roles/r/defaults/main.yml",
        &DEFAULTS.replace("docs_url: https://docs.example.net/\n", ""),
    );
    let message = error(sbom::generate(&checkout.root, &checkout.archive));
    assert!(
        message.contains("stale not_dependencies entry https://docs.example.net/"),
        "{message}"
    );
}

#[test]
fn a_collection_dependency_without_a_resolved_version_fails() {
    let checkout = checkout();
    write(
        &checkout.root,
        "galaxy.yml",
        &format!("{GALAXY}  ansible.posix: '>=1.0.0'\n"),
    );
    let message = error(sbom::generate(&checkout.root, &checkout.archive));
    assert!(message.contains("ansible.posix"), "{message}");
}
