//! What the diagnostics read from the machine, behind one trait so that tests
//! inject a fake machine and never touch a real host (catalog#18).
//!
//! [`Local`] is the only implementation that reaches the machine. It runs a
//! program only through [`run_bounded`] (stdin closed, output capped, killed
//! after [`PROBE_TIMEOUT`]) and reads files without writing, so every check
//! built on it is bounded and non-destructive.

use std::fs::{self, File};
use std::io::{ErrorKind, Read};
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use sha2::{Digest as _, Sha256};

/// How long one program may run before it is stopped.
pub const PROBE_TIMEOUT: Duration = Duration::from_secs(5);
/// The most program output read; the rest is discarded.
pub const OUTPUT_LIMIT: u64 = 64 * 1024;
/// Files larger than this are not hashed.
pub const HASH_LIMIT: u64 = 1024 * 1024 * 1024;

/// A file as an executable check sees it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Presence {
    Executable,
    NotExecutable,
    NotAFile,
    Missing,
}

/// The SHA-256 of a file now, or why there is none.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FileDigest {
    Sha256(String),
    Missing,
    TooLarge,
    Unreadable(String),
}

/// Read-only access to the machine.
pub trait Host {
    /// The output of `program args` (stdout, at most [`OUTPUT_LIMIT`] bytes),
    /// or why it is unavailable. `program` is a bare name looked up on `PATH`
    /// or an absolute path.
    fn run(&self, program: &str, args: &[&str]) -> Result<String, String>;
    /// Whether `path` (following links) is an executable regular file.
    fn presence(&self, path: &Path) -> Presence;
    /// The SHA-256 of `path` (following links), hex.
    fn sha256(&self, path: &Path) -> FileDigest;
}

/// The machine this runs on.
pub struct Local;

impl Host for Local {
    fn run(&self, program: &str, args: &[&str]) -> Result<String, String> {
        run_bounded(program, args)
    }

    fn presence(&self, path: &Path) -> Presence {
        match fs::metadata(path) {
            Ok(meta) if !meta.is_file() => Presence::NotAFile,
            Ok(meta) if meta.permissions().mode() & 0o111 == 0 => Presence::NotExecutable,
            Ok(_) => Presence::Executable,
            Err(_) => Presence::Missing,
        }
    }

    fn sha256(&self, path: &Path) -> FileDigest {
        let mut file = match File::open(path) {
            Ok(file) => file,
            Err(error) if error.kind() == ErrorKind::NotFound => return FileDigest::Missing,
            Err(error) => return FileDigest::Unreadable(error.kind().to_string()),
        };
        match file.metadata() {
            Ok(meta) if !meta.is_file() => {
                return FileDigest::Unreadable("not a regular file".to_string());
            }
            Ok(meta) if meta.len() > HASH_LIMIT => return FileDigest::TooLarge,
            Ok(_) => {}
            Err(error) => return FileDigest::Unreadable(error.kind().to_string()),
        }
        let mut hasher = Sha256::new();
        let mut buffer = vec![0_u8; 64 * 1024];
        loop {
            match file.read(&mut buffer) {
                Ok(0) => break,
                Ok(read) => hasher.update(&buffer[..read]),
                Err(error) if error.kind() == ErrorKind::Interrupted => {}
                Err(error) => return FileDigest::Unreadable(error.kind().to_string()),
            }
        }
        FileDigest::Sha256(
            hasher
                .finalize()
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect(),
        )
    }
}

/// Runs `program args` with stdin closed and stderr discarded, reads at most
/// [`OUTPUT_LIMIT`] bytes of stdout and stops it after [`PROBE_TIMEOUT`].
/// The only place the diagnostics start a process.
pub fn run_bounded(program: &str, args: &[&str]) -> Result<String, String> {
    let mut child = match Command::new(program)
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
    {
        Ok(child) => child,
        Err(error) if error.kind() == ErrorKind::NotFound => {
            return Err("not installed".to_string());
        }
        Err(error) if error.kind() == ErrorKind::PermissionDenied => {
            return Err("not executable".to_string());
        }
        Err(error) => return Err(format!("cannot run: {}", error.kind())),
    };
    let Some(stdout) = child.stdout.take() else {
        return Err("no output pipe".to_string());
    };
    let reader = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let mut limited = stdout.take(OUTPUT_LIMIT);
        limited.read_to_end(&mut bytes).map(|_| bytes)
    });
    let deadline = Instant::now() + PROBE_TIMEOUT;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                let bytes = reader
                    .join()
                    .map_err(|_| "reading the output failed".to_string())?
                    .map_err(|error| format!("reading the output failed: {}", error.kind()))?;
                if !status.success() {
                    return Err(match status.code() {
                        Some(code) => format!("exited with status {code}"),
                        None => "stopped by a signal".to_string(),
                    });
                }
                return Ok(String::from_utf8_lossy(&bytes).into_owned());
            }
            Ok(None) if Instant::now() >= deadline => {
                // Best effort: the child is abandoned either way and the result says so.
                if let Err(error) = child.kill() {
                    return Err(format!(
                        "timed out and could not be stopped: {}",
                        error.kind()
                    ));
                }
                // Reaping a killed child cannot fail in a way the bundle could report.
                child.wait().ok();
                return Err(format!(
                    "did not answer within {} s",
                    PROBE_TIMEOUT.as_secs()
                ));
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(20)),
            Err(error) => return Err(format!("waiting failed: {}", error.kind())),
        }
    }
}

/// The first non-empty line of `text`, trimmed.
pub fn first_line(text: &str) -> Option<&str> {
    text.lines().map(str::trim).find(|line| !line.is_empty())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_missing_program_is_reported_not_fatal() {
        assert_eq!(
            run_bounded("/nonexistent/catalog-tools-probe", &["--version"]),
            Err("not installed".to_string())
        );
    }

    #[test]
    fn hashes_a_file_and_reports_a_missing_one() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("tool");
        fs::write(&path, b"abc").unwrap();
        assert_eq!(
            Local.sha256(&path),
            FileDigest::Sha256(
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad".to_string()
            )
        );
        assert_eq!(Local.sha256(&dir.path().join("gone")), FileDigest::Missing);
        assert_eq!(Local.presence(&path), Presence::NotExecutable);
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
        assert_eq!(Local.presence(&path), Presence::Executable);
        assert_eq!(Local.presence(dir.path()), Presence::NotAFile);
        assert_eq!(Local.presence(&dir.path().join("gone")), Presence::Missing);
    }

    #[test]
    fn first_line_skips_blank_lines() {
        assert_eq!(first_line("\n  \n v1.2 \nx"), Some("v1.2"));
        assert_eq!(first_line("  \n"), None);
    }
}
