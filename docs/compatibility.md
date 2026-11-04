# Compatibility matrix

This is the current support contract for the public collection. A platform is
not promoted to supported until CI and a disposable-machine run produce dated
evidence.

| Platform | Intended status | Notes |
| --- | --- | --- |
| Ubuntu 22.04 (amd64) | supported target | Core role and desktop profile are the primary compatibility path |
| Ubuntu 24.04 (amd64) | supported target | Recommended starting point for new deployments |
| Ubuntu 25.10+ (amd64) | experimental | Some third-party repositories may use `apt_fallback_release` |
| Ubuntu 26.04 (amd64) | experimental target | Used by the T460 BYOD image; validate package availability and hardware behavior before fleet rollout |
| Debian 12/13 (amd64) | experimental | Test profile-specific package names before fleet rollout |
| ARM64 Debian-family devices | experimental | Use thin-client or generic hardware profile first |
| Other distributions | unsupported for now | Contributions welcome with CI and VM evidence |

Architecture-specific binaries are selected by the role's installers, but not
every optional upstream tool publishes every architecture. A failed optional
asset assertion should be reported with the upstream release URL and target
architecture.

The matrix is intentionally conservative. “Syntax-check passed” means the
playbook parsed; it does not mean a real package, firmware device, display
server, identity provider, or tunnel was exercised.

## Dated evidence

* 2026-11-04, package availability only (no converge): see
  [platform-differences.md](platform-differences.md). `tokei` is absent from the Ubuntu 22.04,
  Ubuntu 24.04 and Debian 12 archives and `eza` from 22.04 and Debian 12, so the role's single
  package task is unverified on those releases; Debian 13 differs in `software-properties-common`.
  No platform was promoted or demoted by this result.
* 2026-11-04, update rollback drill on the task files (no disposable machine): see
  [rollback-drill.md](rollback-drill.md).
* 2026-11-04, ARM64 without hardware: apt architecture names fixed and tested, see [arm64.md](arm64.md). No ARM64 machine was run.

