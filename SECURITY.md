# Security policy

This role changes operating-system packages, services, trust stores, firewall
rules, and user files. Treat a malicious or compromised release as a serious
incident and pin or verify the collection artifact used in production.

## Reporting a vulnerability

Please do not open a public issue for a vulnerability. Use a private security
advisory on the public repository when that feature is available:

<https://github.com/Curiosity-Corp/catalog/security/advisories/new>

If the advisory route is unavailable, contact a repository maintainer through
the private contact mechanism provided by the hosting platform and include
“Curiosity Workstation security report” in the subject. Do not attach real
credentials, private keys, private CA bundles, or research data; provide a
minimal reproduction and redacted logs instead.

Useful report details include the affected version or commit, operating system,
profile, task tag, impact, reproduction steps, and a proposed mitigation if
known. Reports are acknowledged when a maintainer is available, triaged for
severity, and disclosed with credit only when the reporter agrees.

## Supported versions

Only the latest released collection and the default branch receive routine
security fixes. Downstream users should upgrade through a reviewed, signed
artifact when signing is available and keep a tested rollback path.

## Secure-development expectations

- Never commit secrets or private infrastructure details.
- Verify upstream artifact digests and package-manager signatures.
- Keep optional integrations disabled unless they are configured and tested.
- Use `no_log` for secret-bearing tasks and redact output in bug reports.
- Preserve release checksums, CI provenance, and vulnerability-response notes.
