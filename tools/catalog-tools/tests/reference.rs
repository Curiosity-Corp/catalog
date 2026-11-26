//! End-to-end: a small catalog checkout in a temporary directory.

use std::fs;
use std::path::Path;

use catalog_tools::{find_root, reference};

const DEFAULTS: &str = "---
# Which profile to converge.
# desktop = Full workstation.
# workspace = Container-safe tooling.
workstation_profile: desktop

# Hardware selection.
hardware_profile: ''

# Known hardware.
hardware_profiles:
  laptop-a:
    description: Small laptop | with a pipe
    ssh_key: ssh-ed25519 AAAA-not-copied
    swap: 4

# Wi-Fi network key.
wifi_psk: correct-horse

# Path the role writes its marker to.
build_marker: /var/lib/x

# Tags of the update transaction.
update_tags: [updates, always]

feature_mode: fast
";

const STORAGE_DEFAULTS: &str = "---
# Turn storage on.
workstation_storage_enabled: false
";

const TASKS: &str = "---
- name: Validate
  ansible.builtin.assert:
    that:
      - workstation_profile in ['desktop', 'workspace', 'byod-kiosk']
      - feature_mode in ['fast', 'safe']
  tags: always

- name: Desktop block
  when: workstation_profile == 'desktop'
  tags: [desktop]
  block:
    - name: Inner
      ansible.builtin.debug: {msg: hi}

- name: Transaction
  ansible.builtin.include_tasks: update.yml
  tags: '{{ update_tags }}'
";

const ANNOTATIONS: &str = "---
patterns:
  - match: '_marker$'
    stability: internal
variables:
  feature_mode: {stability: experimental, note: Picks the speed.}
profile_notes:
  byod-kiosk: Kiosk browser.
tags:
  desktop: {description: Desktop pieces.}
";

fn write(root: &Path, path: &str, text: &str) {
    let path = root.join(path);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, text).unwrap();
}

fn checkout() -> tempfile::TempDir {
    let directory = tempfile::tempdir().unwrap();
    let root = directory.path();
    write(root, "galaxy.yml", "namespace: curiosity\n");
    write(
        root,
        "roles/developer_workstation/defaults/main.yml",
        DEFAULTS,
    );
    write(root, "roles/developer_workstation/tasks/main.yml", TASKS);
    write(
        root,
        "roles/workstation_storage/defaults/main.yml",
        STORAGE_DEFAULTS,
    );
    write(root, "roles/not_a_role/README.md", "no defaults\n");
    write(root, reference::ANNOTATIONS, ANNOTATIONS);
    directory
}

#[test]
fn generates_the_reference() {
    let directory = checkout();
    let text = reference::generate(directory.path()).unwrap();

    // Variables, in file order, with types, defaults and classification.
    assert!(text.contains("| `feature_mode` | string | `\"fast\"` | `\"fast\"`, `\"safe\"` | experimental |  | Picks the speed. |"), "{text}");
    assert!(
        text.contains(
            "| `wifi_psk` | string | (redacted) |  | supported | secret | Wi-Fi network key. |"
        ),
        "{text}"
    );
    assert!(
        text.contains("| `hardware_profiles` | dict | mapping, 1 key |"),
        "{text}"
    );
    assert!(
        text.contains("| `hardware_profile` | string | `\"\"` | `\"laptop-a\"` |"),
        "{text}"
    );
    assert!(
        text.contains("| `build_marker` | `developer_workstation` | internal |"),
        "{text}"
    );
    assert!(
        text.contains("| `feature_mode` | `developer_workstation` | experimental |"),
        "{text}"
    );
    let developer = text.find("### Role `developer_workstation`").unwrap();
    let storage = text.find("### Role `workstation_storage`").unwrap();
    assert!(developer < storage);
    assert!(!text.contains("not_a_role"));

    // The hardware profile's values are never copied, only its setting names.
    assert!(!text.contains("AAAA-not-copied"));
    assert!(!text.contains("correct-horse"));
    assert!(
        text.contains("| `laptop-a` | Small laptop \\| with a pipe | `ssh_key`, `swap` |"),
        "{text}"
    );

    // Profiles: described by the defaults comment, then by the annotations.
    assert!(text.contains("  - `desktop`: Full workstation."), "{text}");
    assert!(text.contains("  - `byod-kiosk`: Kiosk browser."), "{text}");

    // Tags: block inheritance, variable-resolved tags and profile gating.
    assert!(
        text.contains("| `desktop` | supported | 2 | `main.yml` | `desktop` | Desktop pieces. |"),
        "{text}"
    );
    assert!(
        text.contains("| `updates` | supported | 1 | `main.yml` |  |  |"),
        "{text}"
    );
    assert!(text.contains("| `always` | supported | 2 |"), "{text}");
    assert!(text.contains("This role defines no tags"), "{text}");
    assert!(text.ends_with("|\n"));
}

#[test]
fn check_reports_a_stale_reference() {
    let directory = checkout();
    let root = directory.path();
    let text = reference::generate(root).unwrap();
    assert!(reference::first_difference(&text, &text).is_none());

    let defaults = root.join("roles/developer_workstation/defaults/main.yml");
    fs::write(
        &defaults,
        DEFAULTS.replace("feature_mode: fast", "feature_mode: safe"),
    )
    .unwrap();
    let changed = reference::generate(root).unwrap();
    let difference = reference::first_difference(&text, &changed).unwrap();
    assert!(difference.contains("feature_mode"), "{difference}");
    assert!(
        reference::first_difference("", &changed)
            .unwrap()
            .contains("line 1")
    );
}

#[test]
fn stale_annotations_fail_the_build() {
    let directory = checkout();
    let root = directory.path();
    write(
        root,
        reference::ANNOTATIONS,
        "variables:\n  gone_variable: {stability: internal}\n",
    );
    let error = reference::generate(root).unwrap_err();
    assert!(error.to_string().contains("gone_variable"), "{error}");

    write(
        root,
        reference::ANNOTATIONS,
        "tags:\n  gone-tag: {description: x}\n",
    );
    let error = reference::generate(root).unwrap_err();
    assert!(error.to_string().contains("gone-tag"), "{error}");
}

#[test]
fn the_root_is_found_from_a_subdirectory() {
    let directory = checkout();
    let nested = directory.path().join("roles/developer_workstation/tasks");
    assert_eq!(find_root(&nested).unwrap(), directory.path());
    let elsewhere = tempfile::tempdir().unwrap();
    assert!(find_root(elsewhere.path()).is_err());
}
