# 0002: Integrations that need private services default to off

- Status: accepted
- Date: 2026-07-19 (recorded 2026-11-04 from `tests/test_public_contract.py`)
- Deciders: recorded retroactively; a maintainer should confirm
- Affects: `coder_cli_enabled`, `keycloak_sso_enabled`, `ziti_enabled`, `sunshine_enabled`, `profile_cleanup_enabled`, `custom_ca_certificates`

## Context

The role can configure single sign-on, an overlay network, remote streaming,
a workspace CLI and custom certificate authorities. Each needs a service that an
unrelated lab does not have.

## Decision

Each of these defaults to off or empty. An operator turns one on in host or group
variables and supplies the values it needs.

## Safety impact

A default run on a student laptop contacts no private service and trusts no
extra certificate authority. `test_defaults_are_safe_for_unrelated_machines`
asserts the defaults.

## Alternatives considered

Default-on with a no-op when unconfigured: rejected because a half-configured
integration can still change system state.

## Consequences

Turning a default from off to on needs a new decision record and a second
maintainer (see [GOVERNANCE.md](../../GOVERNANCE.md)).
