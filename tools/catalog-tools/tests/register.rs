//! End-to-end: the dependency register against a small catalog checkout in a
//! temporary directory, and against this checkout.

use std::fs;
use std::path::{Path, PathBuf};

use catalog_tools::register::{self, AlertKind};

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

const DEFAULTS: &str = "---
tool_url: https://example.org/tool-{{ tool_release }}.tar.gz
tool_release: '1.2.3'
apt_vendor_repos:
  vendor:
    ubuntu:
      uri: https://apt.example.org/ubuntu
    debian:
      uri: https://apt.example.org/debian
latest_github_repositories:
  thing: example/thing
";

const TASKS: &str = "---
- name: Install the tool
  ansible.builtin.get_url:
    url: \"{{ tool_url }}\"
";

const SOURCES: &str = "---
sources:
  - name: tool
    kind: runtime-package
    source: release-download
    url_var: tool_url
    version_var: tool_release
    declared_in: [roles/demo/tasks/main.yml]
    pinned: true
";

fn entry(name: &str, declarations: &str, extra: &str) -> String {
    format!(
        "  - name: {name}
    upstream: Example project
    source: https://example.org/{name}
    declarations:
{declarations}
    licence: MIT
    licence_source: https://example.org/{name}/LICENSE
    owner: role-maintainer
    update_channel: the latest release on each run
    verification: pinned-checksum
    support:
      status: maintained
      evidence: https://example.org/{name}/releases
      checked: '2026-11-26'
    replacement: the distribution's package
{extra}"
    )
}

fn complete_entries() -> String {
    [
        entry(
            "tool",
            "      source:tool: https://example.org/tool-{{ tool_release }}.tar.gz",
            "",
        ),
        entry(
            "community-general",
            "      collection:community.general: '>=9.0.0'\n      \
             ci-collection:community.general: https://galaxy.ansible.com/ui/repo/published/community/general/",
            "",
        ),
        entry(
            "ansible-core",
            "      ci-python:ansible-core: https://pypi.org/project/ansible-core/",
            "",
        ),
        entry(
            "vendor",
            "      vendor-repo:vendor: https://apt.example.org/ubuntu https://apt.example.org/debian",
            "",
        ),
        entry("thing", "      github-release:thing: example/thing", ""),
    ]
    .join("")
}

fn register_text(entries: &str) -> String {
    format!(
        "---
reviewed: '2026-11-26'
review_interval_days: 31
warn_days: 180
owner_roles:
  role-maintainer: reviews the role
entries:
{entries}"
    )
}

fn checkout(register_entries: &str) -> (tempfile::TempDir, PathBuf) {
    let directory = tempfile::tempdir().expect("temporary directory");
    let root = directory.path().to_path_buf();
    let write = |path: &str, text: &str| {
        let path = root.join(path);
        fs::create_dir_all(path.parent().expect("parent")).expect("mkdir");
        fs::write(path, text).expect("write");
    };
    write("galaxy.yml", GALAXY);
    write("ci/requirements.yml", CI_COLLECTIONS);
    write("ci/requirements.in", "# direct\nansible-core==2.22.0\n");
    write("ci/sbom-sources.yml", SOURCES);
    write("roles/demo/defaults/main.yml", DEFAULTS);
    write("roles/demo/tasks/main.yml", TASKS);
    write(register::REGISTER, &register_text(register_entries));
    (directory, root)
}

fn problems(root: &Path) -> Vec<String> {
    let declared = register::declarations(root).expect("declarations");
    let loaded = register::load(&root.join(register::REGISTER)).expect("register");
    register::check(&loaded, &declared)
}

#[test]
fn a_complete_register_passes() {
    let (_guard, root) = checkout(&complete_entries());
    assert_eq!(problems(&root), Vec::<String>::new());
}

#[test]
fn a_missing_declaration_fails() {
    let entries = complete_entries().replace("      github-release:thing: example/thing\n", "");
    let entries = entries.replace(
        "  - name: thing\n",
        "  - name: thing\n    alert_tracked_in: https://example.org/issues/1\n",
    );
    let (_guard, root) = checkout(&entries);
    let found = problems(&root);
    assert!(
        found
            .iter()
            .any(|problem| problem.contains("github-release:thing (example/thing) is declared")),
        "{found:?}"
    );
    assert!(
        found
            .iter()
            .any(|problem| problem.contains("thing: covers no declaration")),
        "{found:?}"
    );
}

#[test]
fn a_changed_or_stale_declaration_fails() {
    let entries = complete_entries()
        .replace(
            "github-release:thing: example/thing",
            "github-release:thing: example/old-thing",
        )
        .replace(
            "      ci-python:ansible-core: https://pypi.org/project/ansible-core/",
            "      ci-python:ansible-core: https://pypi.org/project/ansible-core/\n      \
             ci-python:removed: https://pypi.org/project/removed/",
        );
    let (_guard, root) = checkout(&entries);
    let found = problems(&root);
    assert!(
        found.iter().any(|problem| problem.contains(
            "github-release:thing now declares \"example/thing\", the register says \"example/old-thing\""
        )),
        "{found:?}"
    );
    assert!(
        found
            .iter()
            .any(|problem| problem.contains("ci-python:removed is no longer declared")),
        "{found:?}"
    );
}

#[test]
fn malformed_entries_fail() {
    let entries = complete_entries()
        .replacen("owner: role-maintainer", "owner: a-person", 1)
        .replacen(
            "evidence: https://example.org/tool/releases",
            "evidence: example.org",
            1,
        );
    let (_guard, root) = checkout(&entries);
    let found = problems(&root);
    assert!(
        found
            .iter()
            .any(|problem| problem.contains("owner \"a-person\" is not one of owner_roles")),
        "{found:?}"
    );
    assert!(
        found
            .iter()
            .any(|problem| problem.contains("support.evidence is not an https:// URL")),
        "{found:?}"
    );
}

#[test]
fn alerts_cover_status_dates_verification_and_review() {
    let mut entries = complete_entries();
    entries = entries.replacen(
        "      status: maintained\n      evidence: https://example.org/thing/releases",
        "      status: archived\n      evidence: https://example.org/thing/releases",
        1,
    );
    entries = entries.replacen(
        "      checked: '2026-11-26'\n    replacement: the distribution's package\n  - name: community-general",
        "      checked: '2026-11-26'\n      ends:\n        - scope: tool 1.x\n          date: 2027-03\n    \
         replacement: the distribution's package\n    security_critical: true\n  - name: community-general",
        1,
    );
    entries = entries.replacen(
        "verification: pinned-checksum\n    support:\n      status: maintained\n      evidence: https://example.org/tool",
        "verification: tls-only\n    support:\n      status: maintained\n      evidence: https://example.org/tool",
        1,
    );
    let (_guard, root) = checkout(&entries);
    assert_eq!(problems(&root), Vec::<String>::new());
    let loaded = register::load(&root.join(register::REGISTER)).expect("register");

    let today = register::alerts(&loaded, "2026-11-26").expect("alerts");
    let kinds: Vec<(AlertKind, &str)> = today
        .iter()
        .map(|alert| (alert.kind, alert.entry.as_str()))
        .collect();
    assert_eq!(
        kinds,
        vec![
            (AlertKind::Abandoned, "thing"),
            (AlertKind::SupportEnding, "tool"),
            (AlertKind::Unverified, "tool"),
        ]
    );
    assert!(today.iter().all(|alert| alert.tracked_in.is_none()));

    let later = register::alerts(&loaded, "2027-01-15").expect("alerts");
    assert_eq!(later[0].kind, AlertKind::ReviewOverdue);
    assert!(later[0].tracked_in.is_none());
}

#[test]
fn this_checkout_has_a_complete_register() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let declared = register::declarations(&root).expect("declarations");
    let loaded = register::load(&root.join(register::REGISTER)).expect("register");
    assert_eq!(register::check(&loaded, &declared), Vec::<String>::new());
}
