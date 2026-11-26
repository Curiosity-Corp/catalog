//! End-to-end: `catalog-tools diagnostics` against a fixture machine in a
//! temporary directory (fake `ansible` on PATH, a fixture update manifest, a
//! private HOME). Never against a real host. Every fixture value is a
//! documentation value (192.0.2.0/24, 2001:db8::/32, example.org); token and
//! key shapes are assembled at run time so no scanner mistakes them for real.

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};

use serde_json::Value;

struct Machine {
    _temp: tempfile::TempDir,
    root: PathBuf,
    manifest: PathBuf,
    secrets: Vec<String>,
}

fn token() -> String {
    format!("glpat-{}", "Q7xK2mP9vL4nR8sT1wY6")
}

fn opaque_token() -> String {
    ["c2VjcmV0", "LXRva2Vu", "LWZvci10", "ZXN0czEy", "MzQ1Ng"].concat()
}

fn pem() -> String {
    let label = ["PRIVATE", " KEY"].concat();
    format!(
        "-----BEGIN {label}-----\nMIIEvgIBADANBgkqhkiG9w0BAQEFAASC\nBKgwggSkAgEAAoIBAQC7exampleonly\n-----END {label}-----"
    )
}

fn machine() -> Machine {
    let temp = tempfile::tempdir().expect("temporary directory");
    let root = temp.path().to_path_buf();
    let bin = root.join("bin");
    fs::create_dir_all(&bin).unwrap();
    fs::create_dir_all(root.join("home/researcher")).unwrap();
    let script = |name: &str, body: &str| {
        let path = bin.join(name);
        fs::write(&path, format!("#!/bin/sh\n{body}\n")).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
    };
    script(
        "ansible",
        "echo 'ansible [core 2.19.4]'\necho '  config file = /home/researcher/.ansible.cfg'",
    );
    script(
        "ansible-community",
        "echo 'Ansible community version 12.2.0'",
    );

    let failure = format!(
        "Download from https://ws07.example.org/api failed: connect to 192.0.2.44 and \
         2001:db8:4::44 refused; cache /home/researcher/.cache/streamstack; \
         header PRIVATE-TOKEN {} ; key {}",
        token(),
        pem().replace('\n', "\\n")
    );
    let manifest_value = serde_json::json!({
        "schema": "curiosity.workstation.update/v1",
        "run_id": "1764000000-a1b2c3d4e5f6",
        "status": "rolled_back",
        "started_at": "2026-11-24T08:00:00Z",
        "finished_at": "2026-11-24T08:20:11Z",
        "host": "ws07.example.org",
        "profile": "desktop",
        "update_ring": "stable",
        "quarantine_active": false,
        "install_allowed": true,
        "catalog_commit": "0743a9d6",
        "catalog_commit_verified": true,
        "failure": {"task": "Install StreamStack", "message": failure},
        "healthcheck_failures": [
            "streamstack",
            format!("jupyter: cannot read /srv/research-data/cohort-7/visits.parquet as researcher ({})", opaque_token()),
        ],
        "package_inventory": {"dpkg": "private-lab-package 1.0"},
        "before": [{"path": "/home/researcher/.local/bin/tool", "checksum": "abc"}],
    });
    let manifest = root.join("last-run.json");
    fs::write(
        &manifest,
        serde_json::to_string_pretty(&manifest_value).unwrap(),
    )
    .unwrap();

    let secrets = vec![
        "192.0.2.44".to_string(),
        "2001:db8:4::44".to_string(),
        "ws07".to_string(),
        "example.org".to_string(),
        "/home/researcher".to_string(),
        "researcher".to_string(),
        token(),
        opaque_token(),
        "MIIEvgIBADANBgkqhkiG9w0BAQEFAASC".to_string(),
        "BEGIN PRIVATE".to_string(),
        "cohort-7".to_string(),
        "visits.parquet".to_string(),
        "private-lab-package".to_string(),
        "1764000000".to_string(),
    ];
    Machine {
        _temp: temp,
        root,
        manifest,
        secrets,
    }
}

fn run(machine: &Machine, args: &[&str], state: Option<&str>) -> Output {
    let path = format!("{}:/usr/bin:/bin", machine.root.join("bin").display());
    let mut command = Command::new(env!("CARGO_BIN_EXE_catalog-tools"));
    command.args(["diagnostics"]).args(args);
    // Never the real machine's check list: a test that wants one passes it.
    let builds_a_bundle = matches!(args.first(), Some(&("preview" | "write" | "summary")));
    if builds_a_bundle && !args.contains(&"--checks") {
        command
            .arg("--checks")
            .arg(machine.root.join("no-check-list.json"));
    }
    command
        .env_clear()
        .env("PATH", path)
        .env("HOME", machine.root.join("home/researcher"))
        .env("USER", "researcher")
        .stdin(Stdio::null());
    if let Some(state) = state {
        command.env("XDG_STATE_HOME", state);
    }
    command.output().expect("run catalog-tools")
}

fn state(machine: &Machine) -> String {
    machine.root.join("state").display().to_string()
}

fn bundle_dir(machine: &Machine) -> PathBuf {
    machine.root.join("state/curiosity/diagnostics")
}

fn assert_redacted(machine: &Machine, text: &str) {
    for secret in &machine.secrets {
        assert!(
            !text.contains(secret.as_str()),
            "{secret} leaked into:\n{text}"
        );
    }
}

fn bundle_json(stdout: &[u8]) -> Value {
    let text = String::from_utf8_lossy(stdout);
    let end = text.rfind("}\n").expect("a JSON bundle") + 1;
    serde_json::from_str(&text[..end]).expect("the preview is the JSON bundle")
}

fn manifest_arg(machine: &Machine) -> String {
    machine.manifest.display().to_string()
}

#[test]
fn preview_collects_the_fields_redacts_and_writes_nothing() {
    let m = machine();
    let manifest = manifest_arg(&m);
    let out = run(
        &m,
        &[
            "preview",
            "--manifest",
            &manifest,
            "--tags",
            "packages,update",
        ],
        Some(&state(&m)),
    );
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let stdout = String::from_utf8_lossy(&out.stdout);
    assert_redacted(&m, &stdout);
    assert_redacted(&m, &String::from_utf8_lossy(&out.stderr));

    let bundle = bundle_json(&out.stdout);
    assert_eq!(bundle["schema"], "curiosity.workstation.diagnostics/v1");
    assert_eq!(bundle["ansible"]["ansible_core"], "2.19.4");
    assert_eq!(bundle["ansible"]["ansible"], "12.2.0");
    assert_eq!(bundle["profile"], "desktop");
    assert!(
        bundle["architecture"]
            .as_str()
            .is_some_and(|a| !a.is_empty())
    );
    assert_eq!(
        bundle["selected_tags"],
        serde_json::json!(["packages", "update"])
    );
    assert_eq!(bundle["last_run"]["status"], "rolled_back");
    assert_eq!(bundle["last_run"]["catalog_commit"], "0743a9d6");
    let message = bundle["last_run"]["failure_message"].as_str().unwrap();
    assert!(message.contains("[token]"), "{message}");
    assert!(message.contains("[certificate or key]"), "{message}");
    assert!(message.contains("/home/[user]/.cache"), "{message}");
    assert!(message.contains("ip-"), "{message}");
    assert!(message.contains("host-"), "{message}");
    for kind in [
        "IP address",
        "host name",
        "token",
        "certificate or key",
        "user",
        "research data",
    ] {
        assert!(
            bundle["redacted"][kind].as_u64().unwrap_or(0) >= 1,
            "{kind}: {bundle}"
        );
    }
    assert!(String::from_utf8_lossy(&out.stderr).contains("nothing was written"));
    assert!(
        !m.root.join("state").exists(),
        "preview must not create anything"
    );
}

#[test]
fn write_refuses_without_a_terminal_or_yes() {
    let m = machine();
    let manifest = manifest_arg(&m);
    let out = run(&m, &["write", "--manifest", &manifest], Some(&state(&m)));
    assert_eq!(out.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&out.stderr).contains("pass --yes"));
    assert!(!bundle_dir(&m).exists());
}

#[test]
fn write_saves_a_private_file_and_delete_removes_only_bundles() {
    let m = machine();
    let manifest = manifest_arg(&m);
    let out = run(
        &m,
        &["write", "--yes", "--manifest", &manifest],
        Some(&state(&m)),
    );
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("catalog-tools diagnostics delete"),
        "{stderr}"
    );

    let dir = bundle_dir(&m);
    let files: Vec<PathBuf> = fs::read_dir(&dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .collect();
    assert_eq!(files.len(), 1, "{files:?}");
    let file = &files[0];
    let name = file.file_name().unwrap().to_string_lossy().into_owned();
    assert!(
        name.starts_with("diagnostics-") && name.ends_with(".json"),
        "{name}"
    );
    assert_eq!(mode(file), 0o600);
    assert_eq!(mode(&dir), 0o700);
    let saved = fs::read_to_string(file).unwrap();
    assert_redacted(&m, &saved);
    let bundle: Value = serde_json::from_str(&saved).unwrap();
    assert_eq!(
        bundle,
        bundle_json(&out.stdout),
        "the file is exactly the preview"
    );

    fs::write(dir.join("notes.txt"), "mine").unwrap();
    let out = run(&m, &["delete"], Some(&state(&m)));
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(!file.exists());
    assert!(
        dir.join("notes.txt").exists(),
        "delete touches only bundle files"
    );
    assert!(String::from_utf8_lossy(&out.stdout).contains("1 bundle(s) deleted"));
}

#[test]
fn bundle_folder_falls_back_to_home_and_a_missing_manifest_is_reported() {
    let m = machine();
    let out = run(&m, &["path"], Some("relative/state"));
    assert!(out.status.success());
    let expected = m
        .root
        .join("home/researcher/.local/state/curiosity/diagnostics");
    assert_eq!(
        String::from_utf8_lossy(&out.stdout).trim(),
        expected.display().to_string()
    );

    let missing = m.root.join("absent.json").display().to_string();
    let out = run(&m, &["preview", "--manifest", &missing], None);
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let bundle = bundle_json(&out.stdout);
    assert_eq!(bundle["last_run"]["status"], "unknown");
    assert!(
        bundle["last_run"]["error"]
            .as_str()
            .unwrap()
            .contains("no update manifest")
    );
}

fn mode(path: &Path) -> u32 {
    fs::metadata(path).unwrap().permissions().mode() & 0o777
}

/// The health-check fixture: a check list, fake `systemctl` and `df` that log
/// every call, a managed tool that is present, one that is gone and one whose
/// file changed since the update. Returns (manifest, check list, call log).
fn health_fixture(m: &Machine) -> (String, String, PathBuf) {
    let calls = m.root.join("calls.log");
    let bin = m.root.join("bin");
    let script = |path: &Path, body: &str| {
        fs::write(path, format!("#!/bin/sh\n{body}\n")).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
    };
    script(
        &bin.join("systemctl"),
        &format!(
            r#"echo "systemctl $*" >> '{calls}'
[ "$1" = show ] || exit 64
for unit; do :; done
case "$unit" in
  ansible-pull.service) printf 'LoadState=loaded\nActiveState=failed\nSubState=failed\nResult=exit-code\n' ;;
  *) printf 'LoadState=loaded\nActiveState=active\nSubState=waiting\nResult=success\n' ;;
esac"#,
            calls = calls.display()
        ),
    );
    script(
        &bin.join("df"),
        &format!(
            r#"echo "df $*" >> '{calls}'
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 10240000 9728000 512000 95%% /\n'"#,
            calls = calls.display()
        ),
    );
    let tools = m.root.join("opt/bin");
    fs::create_dir_all(&tools).unwrap();
    let present = tools.join("goodtool");
    script(
        &present,
        &format!(r#"echo "goodtool $*" >> '{}'; echo 'goodtool 1.2.3'"#, calls.display()),
    );
    let stale = tools.join("staletool");
    script(&stale, "echo 'staletool 0.9.0'");
    let sha = |bytes: &[u8]| -> String {
        use sha2::Digest as _;
        sha2::Sha256::digest(bytes)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect()
    };
    let present_sum = sha(&fs::read(&present).unwrap());
    let stale_sum = sha(b"the file the update installed");
    let gone = tools.join("gonetool");
    let manifest = serde_json::json!({
        "status": "success",
        "finished_at": "2026-01-02T03:04:05Z",
        "profile": "desktop",
        "application_contracts": {
            "goodtool": {"verification": "github-asset-digest", "healthcheck": [present, "--version"]},
            "gonetool": {"healthcheck": [gone, "--version"]},
            "staletool": {"healthcheck": [stale, "--version"]},
            "streamstack": {"healthcheck": [present, "--version"]}
        },
        "upstream_releases": {
            "goodtool": {"repository": "example/goodtool", "tag_name": "v1.2.3"}
        },
        "after": [
            {"path": present, "exists": true, "checksum": present_sum},
            {"path": stale, "exists": true, "checksum": stale_sum}
        ]
    });
    let manifest_path = m.root.join("health-run.json");
    fs::write(&manifest_path, manifest.to_string()).unwrap();
    let checks = m.root.join("checks.json");
    fs::write(
        &checks,
        serde_json::json!({
            "schema": "curiosity.workstation.diagnostics-checks/v1",
            "services": ["ansible-pull.timer", "ansible-pull.service", "streamstack-console.service"],
            "disk_paths": ["/"],
            "disk_min_free_mib": 2048,
            "stale_after_days": 14
        })
        .to_string(),
    )
    .unwrap();
    (
        manifest_path.display().to_string(),
        checks.display().to_string(),
        calls,
    )
}

#[test]
fn health_checks_find_a_missing_binary_a_failed_service_and_a_stale_artifact() {
    let m = machine();
    let (manifest, checks, calls) = health_fixture(&m);
    let out = run(
        &m,
        &["preview", "--manifest", &manifest, "--checks", &checks],
        Some(&state(&m)),
    );
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let bundle = bundle_json(&out.stdout);
    let health = &bundle["health"];
    let command = |name: &str| {
        health["commands"]
            .as_array()
            .unwrap()
            .iter()
            .find(|c| c["name"] == name)
            .cloned()
            .unwrap_or(Value::Null)
    };
    assert_eq!(command("goodtool")["version"], "1.2.3");
    assert_eq!(command("goodtool")["matches_release"], true);
    assert_eq!(command("goodtool")["repository"], "example/goodtool");
    assert_eq!(command("gonetool")["status"], "missing");
    assert_eq!(command("streamstack"), Value::Null, "StreamStack is left out");
    assert_eq!(health["services"].as_array().unwrap().len(), 2);
    assert_eq!(health["services"][1]["active"], "failed");
    assert_eq!(health["disks"][0]["available_mib"], 500);
    assert_eq!(health["artifacts"]["unchanged"], 1);
    assert_eq!(health["artifacts"]["problems"][0]["state"], "changed");

    let findings: Vec<String> = bundle["findings"]
        .as_array()
        .unwrap()
        .iter()
        .map(|f| f.as_str().unwrap().to_string())
        .collect();
    let has = |needle: &str| findings.iter().any(|f| f.contains(needle));
    assert!(has("Command `gonetool` is missing"), "{findings:?}");
    assert!(
        has("Service `ansible-pull.service` has failed (result: exit-code)."),
        "{findings:?}"
    );
    assert!(has("Low disk space on /: 500 MiB free"), "{findings:?}");
    assert!(
        has("staletool differs from the file the last update installed"),
        "{findings:?}"
    );
    assert!(has("The last update finished"), "{findings:?}");

    // Read-only and bounded: services only `show`n, disks only `df`'d, a
    // managed tool only asked for its version, nothing about StreamStack.
    let log = fs::read_to_string(&calls).unwrap();
    for line in log.lines() {
        assert!(
            line.starts_with("systemctl show --no-pager --property=")
                || line.starts_with("df -P -k -- ")
                || line == "goodtool --version",
            "unexpected call: {line}"
        );
        assert!(!line.to_ascii_lowercase().contains("streamstack"), "{line}");
    }
    assert_eq!(log.lines().count(), 4, "{log}");
    assert!(
        !m.root.join("state").exists(),
        "preview must not create anything"
    );
}

#[test]
fn summary_is_short_redacted_text_for_an_issue() {
    let m = machine();
    let (_, checks, _) = health_fixture(&m);
    let manifest = manifest_arg(&m);
    let out = run(
        &m,
        &["summary", "--manifest", &manifest, "--checks", &checks],
        Some(&state(&m)),
    );
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let text = String::from_utf8_lossy(&out.stdout);
    assert_redacted(&m, &text);
    assert_redacted(&m, &String::from_utf8_lossy(&out.stderr));
    assert!(text.starts_with("### Workstation diagnostics"), "{text}");
    assert!(text.contains("- Last update: `rolled_back`"), "{text}");
    assert!(
        text.contains("- The last update run ended with status `rolled_back`."),
        "{text}"
    );
    assert!(
        text.contains("- Service `ansible-pull.service` has failed (result: exit-code)."),
        "{text}"
    );
    assert!(text.contains("Redacted before printing:"), "{text}");
    assert!(String::from_utf8_lossy(&out.stderr).contains("Nothing was written"));
    assert!(!m.root.join("state").exists(), "summary writes nothing");
}
