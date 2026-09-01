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
