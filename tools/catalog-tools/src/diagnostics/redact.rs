//! Redaction of free text for the diagnostic bundle (catalog#41, catalog#25).
//!
//! Everything that is not a validated, structured value (a version, a status,
//! a commit) passes through [`Redactor::redact`] before it can reach the
//! bundle. Values recognised by shape (certificates and keys, credentials,
//! tokens, e-mail and MAC addresses, IP addresses, host names, home and
//! research-data paths) are replaced; values that only this machine knows (its
//! host name, the user's name, words passed with `--redact`) are replaced
//! wherever they appear.
//!
//! Addresses, host names and e-mail addresses become tags such as
//! `ip-1a2b3c4d`: the same value gets the same tag inside one bundle, so a
//! maintainer still sees that two lines name the same host, but the hash key
//! is random per run and never stored, so tags do not link two bundles.

use std::collections::BTreeMap;
use std::hash::{BuildHasher, RandomState};
use std::net::{Ipv4Addr, Ipv6Addr};
use std::sync::LazyLock;

use regex::{Captures, Regex};

/// Values the redactor cannot recognise by shape: who and where it runs.
#[derive(Debug, Clone, Default)]
pub struct Known {
    /// Login names (the current user, the owner of `$HOME`).
    pub users: Vec<String>,
    /// Host names of this machine (short and fully qualified).
    pub hosts: Vec<String>,
    /// Further literal words the researcher asked to hide (`--redact`).
    pub extra: Vec<String>,
}

/// Replaces private values in text and counts what it replaced.
pub struct Redactor {
    key: RandomState,
    literals: Vec<(Regex, Literal)>,
    counts: BTreeMap<&'static str, usize>,
}

#[derive(Clone, Copy)]
enum Literal {
    Host,
    User,
    Extra,
}

fn regex(pattern: &str) -> Regex {
    Regex::new(pattern).expect("built-in redaction pattern is valid")
}

static PEM: LazyLock<Regex> =
    LazyLock::new(|| regex(r"(?s)-----BEGIN [A-Z0-9 ]+-----.*?(?:-----END [A-Z0-9 ]+-----|\z)"));
static URL_CREDENTIALS: LazyLock<Regex> =
    LazyLock::new(|| regex(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@]+@"));
static AUTH_SCHEME: LazyLock<Regex> =
    LazyLock::new(|| regex(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}"));
static KEYED_SECRET: LazyLock<Regex> = LazyLock::new(|| {
    regex(
        r#"(?i)\b([a-z0-9_.-]*(?:token|secret|password|passwd|pass|psk|api[_-]?key|auth|credentials?))("?\s*[:=]\s*"?)([^\s",;]+)"#,
    )
});
static KNOWN_TOKEN: LazyLock<Regex> = LazyLock::new(|| {
    regex(concat!(
        r"\b(?:glpat-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,}",
        r"|xox[abposr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9_-]{20,}",
        r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"
    ))
});
static EMAIL: LazyLock<Regex> =
    LazyLock::new(|| regex(r"(?i)\b[a-z0-9._%+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b"));
static HOME_PATH: LazyLock<Regex> =
    LazyLock::new(|| regex(r#"(/(?:var/)?home/|/Users/)[^/\s"':;,)\]]+"#));
static RESEARCH_PATH: LazyLock<Regex> = LazyLock::new(|| {
    regex(concat!(
        r#"(^|[\s"'=:(,\[])((?:/srv/research-data|/srv/data|/data|/mnt|/media"#,
        r#"|/home/\[user\]/(?:data|datasets|research|Documents|Desktop|Downloads))(?:/[^\s"',;)]*)?)"#
    ))
});
static RESEARCH_FILE: LazyLock<Regex> = LazyLock::new(|| {
    regex(concat!(
        r#"(?i)[^\s/"'=:]+\.(?:csv|tsv|parquet|feather|arrow|h5|hdf5|nc|zarr|fastq|fq|fasta|fa|bam|sam|cram"#,
        r"|vcf|bcf|npy|npz|mat|sav|dta|xlsx?|ods|sqlite3?|nii|dcm|edf|rds|rdata)(?:\.gz|\.bz2|\.xz|\.zst)?\b"
    ))
});
static MAC: LazyLock<Regex> = LazyLock::new(|| regex(r"(?i)\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b"));
static IPV6_CANDIDATE: LazyLock<Regex> = LazyLock::new(|| regex(r"[0-9A-Za-z:.%_-]+"));
static IPV4: LazyLock<Regex> = LazyLock::new(|| regex(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b"));
static FQDN: LazyLock<Regex> = LazyLock::new(|| {
    regex(concat!(
        r"(?i)\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+",
        r"(?:com|org|net|edu|gov|mil|int|io|dev|ai|app|cloud|local|lan|home|internal|intranet|corp",
        r"|localdomain|example|test|invalid|arpa|info|biz|xyz|site|online|tech|science|ac|eu|us|uk",
        r"|de|fr|nl|ch|at|se|no|fi|dk|es|it|pl|cz|be|ie|ca|au|nz|jp|cn|kr|tw|sg|br|ru|za|il)\b"
    ))
});
static GENERIC_TOKEN: LazyLock<Regex> = LazyLock::new(|| regex(r"[A-Za-z0-9_+/=-]{32,}"));

impl Redactor {
    /// A redactor with a fresh random tag key.
    pub fn new(known: &Known) -> Self {
        let mut literals = Vec::new();
        let mut add = |values: &[String], kind: Literal, min_len: usize| {
            for value in values {
                let value = value.trim();
                if value.chars().count() < min_len || value == "root" {
                    continue;
                }
                let pattern = format!(r"(?i)\b{}\b", regex::escape(value));
                let compiled = Regex::new(&pattern).expect("an escaped literal is a valid pattern");
                literals.push((compiled, kind));
            }
        };
        // Host names first and longest first, so a fully qualified name is
        // tagged whole before its short name could split it.
        let mut hosts: Vec<String> = known
            .hosts
            .iter()
            .map(|host| host.trim_end_matches('.').to_string())
            .collect();
        hosts.sort_by_key(|host| std::cmp::Reverse(host.len()));
        add(&hosts, Literal::Host, 2);
        add(&known.users, Literal::User, 2);
        add(&known.extra, Literal::Extra, 1);
        Self {
            key: RandomState::new(),
            literals,
            counts: BTreeMap::new(),
        }
    }

    /// How many values of each kind were replaced so far.
    pub fn counts(&self) -> &BTreeMap<&'static str, usize> {
        &self.counts
    }

    /// `text` with every private value replaced.
    pub fn redact(&mut self, text: &str) -> String {
        let key = &self.key;
        let counts = &mut self.counts;
        let mut bump = |kind: &'static str| *counts.entry(kind).or_insert(0) += 1;
        let tag = |kind: &str, value: &str| {
            let hash = key.hash_one(value.to_ascii_lowercase());
            format!("{kind}-{:08x}", (hash >> 32) as u32)
        };

        let mut text = PEM
            .replace_all(text, |_: &Captures| {
                bump("certificate or key");
                "[certificate or key]".to_string()
            })
            .into_owned();
        text = URL_CREDENTIALS
            .replace_all(&text, |c: &Captures| {
                bump("secret");
                format!("{}[credentials]@", &c[1])
            })
            .into_owned();
        text = AUTH_SCHEME
            .replace_all(&text, |c: &Captures| {
                bump("token");
                format!("{} [token]", &c[1])
            })
            .into_owned();
        text = KEYED_SECRET
            .replace_all(&text, |c: &Captures| {
                if &c[3] == "[token]" || &c[3] == "[redacted]" {
                    return c[0].to_string();
                }
                bump("secret");
                format!("{}{}[redacted]", &c[1], &c[2])
            })
            .into_owned();
        text = KNOWN_TOKEN
            .replace_all(&text, |_: &Captures| {
                bump("token");
                "[token]".to_string()
            })
            .into_owned();
        text = EMAIL
            .replace_all(&text, |c: &Captures| {
                bump("e-mail address");
                tag("email", &c[0])
            })
            .into_owned();
        text = HOME_PATH
            .replace_all(&text, |c: &Captures| {
                bump("user");
                format!("{}[user]", &c[1])
            })
            .into_owned();
        text = RESEARCH_PATH
            .replace_all(&text, |c: &Captures| {
                bump("research data");
                format!("{}[research data path]", &c[1])
            })
            .into_owned();
        text = RESEARCH_FILE
            .replace_all(&text, |_: &Captures| {
                bump("research data");
                "[research data file]".to_string()
            })
            .into_owned();
        text = MAC
            .replace_all(&text, |_: &Captures| {
                bump("MAC address");
                "[mac]".to_string()
            })
            .into_owned();
        text = IPV6_CANDIDATE
            .replace_all(&text, |c: &Captures| {
                let token = &c[0];
                let core = token.trim_end_matches(['.', ',']);
                let address = core.split('%').next().unwrap_or(core);
                if address.contains(':') && address.parse::<Ipv6Addr>().is_ok() {
                    bump("IP address");
                    format!("{}{}", tag("ip", address), &token[core.len()..])
                } else {
                    token.to_string()
                }
            })
            .into_owned();
        text = IPV4
            .replace_all(&text, |c: &Captures| {
                if c[0].parse::<Ipv4Addr>().is_ok() {
                    bump("IP address");
                    tag("ip", &c[0])
                } else {
                    c[0].to_string()
                }
            })
            .into_owned();
        for (pattern, kind) in &self.literals {
            text = pattern
                .replace_all(&text, |c: &Captures| match kind {
                    Literal::Host => {
                        bump("host name");
                        tag("host", &c[0])
                    }
                    Literal::User => {
                        bump("user");
                        "[user]".to_string()
                    }
                    Literal::Extra => {
                        bump("requested word");
                        "[redacted]".to_string()
                    }
                })
                .into_owned();
        }
        text = FQDN
            .replace_all(&text, |c: &Captures| {
                bump("host name");
                tag("host", &c[0])
            })
            .into_owned();
        GENERIC_TOKEN
            .replace_all(&text, |c: &Captures| {
                let candidate = &c[0];
                if looks_like_token(candidate) {
                    bump("token");
                    "[token]".to_string()
                } else {
                    candidate.to_string()
                }
            })
            .into_owned()
    }
}

/// A long run of token characters is a secret unless it is a plain path or a
/// commit or SHA-256 digest (public, and what a maintainer needs).
fn looks_like_token(candidate: &str) -> bool {
    let is_digest = matches!(candidate.len(), 40 | 64)
        && candidate
            .chars()
            .all(|c| c.is_ascii_digit() || ('a'..='f').contains(&c));
    let is_path = candidate.contains('/') && !candidate.chars().any(|c| c.is_ascii_uppercase());
    let has_digit = candidate.chars().any(|c| c.is_ascii_digit());
    let has_letter = candidate.chars().any(|c| c.is_ascii_alphabetic());
    !is_digest && !is_path && has_digit && has_letter
}

#[cfg(test)]
mod tests {
    use super::*;

    fn redactor() -> Redactor {
        Redactor::new(&Known {
            users: vec!["researcher".into()],
            hosts: vec!["lab-ws01".into(), "lab-ws01.example.org".into()],
            extra: vec!["Project Lyra".into()],
        })
    }

    #[test]
    fn ipv4_and_ipv6_become_stable_tags() {
        let mut r = redactor();
        let out = r.redact(
            "from 192.0.2.10:22 via 2001:db8::1/64 and [2001:db8:0:1::5]:443, again 192.0.2.10.",
        );
        assert!(!out.contains("192.0.2.10"), "{out}");
        assert!(!out.contains("2001:db8"), "{out}");
        let tags: Vec<&str> = out
            .split(|c: char| !c.is_ascii_alphanumeric() && c != '-')
            .filter(|w| w.starts_with("ip-"))
            .collect();
        assert_eq!(tags.len(), 4, "{out}");
        assert_eq!(tags[0], tags[3], "the same address gets the same tag");
        assert!(out.contains(":22 "), "the port stays: {out}");
        assert_eq!(r.counts()["IP address"], 4);
    }

    #[test]
    fn versions_times_and_rust_paths_are_not_addresses() {
        let mut r = redactor();
        let text = "ansible [core 2.19.4] at 2026-11-04T12:36:03Z in std::net, sha 1.2.3";
        assert_eq!(r.redact(text), text);
    }

    #[test]
    fn host_names_are_tagged() {
        let mut r = redactor();
        let out =
            r.redact("ssh to lab-ws01.example.org failed; LAB-WS01 down; mirror.example.net up");
        for value in ["lab-ws01", "LAB-WS01", "example.org", "mirror.example.net"] {
            assert!(!out.contains(value), "{value} in {out}");
        }
        assert!(out.contains("host-"), "{out}");
    }

    #[test]
    fn home_paths_and_user_names_are_hidden() {
        let mut r = redactor();
        let out = r
            .redact("config file = /home/researcher/.ansible.cfg; owner researcher; /Users/jdoe/x");
        assert!(!out.contains("researcher"), "{out}");
        assert!(!out.contains("jdoe"), "{out}");
        assert!(out.contains("/home/[user]/.ansible.cfg"), "{out}");
    }

    #[test]
    fn tokens_are_replaced() {
        let mut r = redactor();
        let gitlab = format!("glpat-{}", "Ab3dEf6hIj9kLm2nOp5q");
        let github = format!("ghp_{}", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8");
        let opaque = "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZmdoaWpr";
        let text = format!(
            "clone with {gitlab}; GITHUB_TOKEN={github}; Authorization: Bearer abcDEF123456789xyz; blob {opaque}; https://ci:s3cr3tpw@git.example.org/x"
        );
        let out = r.redact(&text);
        for secret in [
            gitlab.as_str(),
            github.as_str(),
            "abcDEF123456789xyz",
            opaque,
            "s3cr3tpw",
        ] {
            assert!(!out.contains(secret), "{secret} in {out}");
        }
    }

    #[test]
    fn digests_and_plain_paths_are_kept() {
        let mut r = redactor();
        let commit = "0743a9d6c1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6";
        let text =
            format!("commit {commit} in /usr/local/lib/python3/dist-packages/ansible_collections");
        assert_eq!(r.redact(&text), text);
    }

    #[test]
    fn pem_blocks_are_removed_whole() {
        let mut r = redactor();
        let begin = format!("-----BEGIN {}-----", "CERTIFICATE");
        let end = format!("-----END {}-----", "CERTIFICATE");
        let key = format!("-----BEGIN {}-----", "PRIVATE KEY");
        let text = format!(
            "before\n{begin}\nMIIBszCCAVmgAwIBAgIUexample\n{end}\nmid {key}\nMIIEvQIBADANBgkq\n"
        );
        let out = r.redact(&text);
        assert!(!out.contains("MIIB"), "{out}");
        assert!(
            !out.contains("MIIEvQ"),
            "an unterminated key block is removed too: {out}"
        );
        assert!(
            out.starts_with("before\n[certificate or key]\nmid [certificate or key]"),
            "{out}"
        );
        assert_eq!(r.counts()["certificate or key"], 2);
    }

    #[test]
    fn research_data_and_requested_words_are_hidden() {
        let mut r = redactor();
        let out = r.redact(
            "cannot open /srv/research-data/cohort-7/visits.parquet; read /home/researcher/data/run3 and genome_B12.fastq.gz for Project Lyra",
        );
        for value in ["cohort-7", "visits", "run3", "genome_B12", "Lyra"] {
            assert!(!out.contains(value), "{value} in {out}");
        }
        assert!(out.contains("[research data path]"), "{out}");
        assert!(out.contains("[research data file]"), "{out}");
    }

    #[test]
    fn emails_and_macs_are_hidden() {
        let mut r = redactor();
        let out = r.redact("mail ops@example.org about 00:1a:2b:3c:4d:5e");
        assert!(!out.contains("ops@"), "{out}");
        assert!(!out.contains("4d:5e"), "{out}");
    }

    #[test]
    fn tags_do_not_link_two_runs() {
        let a = redactor().redact("192.0.2.10");
        let b = redactor().redact("192.0.2.10");
        assert!(a.starts_with("ip-") && b.starts_with("ip-"));
        assert_ne!(a, b, "each run uses its own key");
    }
}
