# Configuration guide

The role defaults are deliberately useful for a generic Linux workstation and
conservative for optional integrations. Put site-specific values in inventory,
`group_vars`, `host_vars`, Ansible Vault, or a secret manager. The tracked
`playbooks/host_vars/localhost.yml` is a documentation-safe example, not a
machine inventory.

## Minimal local variables

```yaml
---
dev_user: researcher
dev_user_home: /home/researcher
workstation_profile: desktop
hardware_profile: generic

# Optional: permanently mask obsolete user-scoped systemd units. Keep this in
# protected host/group inventory when the names identify private applications.
workstation_user_systemd_masked_units:
  - obsolete-helper.service
  - obsolete-helper.timer
```

For a VM or an existing user, pass the actual account and home directory. The
role asserts that the account exists and that its home is a directory before it
changes anything else.

## Optional private services

Keep these values out of the repository:

```yaml
---
coder_cli_enabled: true
coder_deployment_url: https://coder.example.edu

keycloak_sso_enabled: true
keycloak_host: login.example.edu
keycloak_realm: research
keycloak_desktop_client_id: workstation-login

ziti_enabled: true
ziti_oidc_provider: lab-oidc
ziti_intercept_domains:
  - registry.lab.example.edu
  - notebooks.lab.example.edu
ziti_etc_hosts_pins:
  - registry.lab.example.edu
```

Use `ziti_service_name` and `ziti_manage_service: false` when a separate
operator-managed tunnel unit owns the interface. Pins are refreshed only for
names explicitly supplied by the operator.

## User-scoped systemd masks

`workstation_user_systemd_masked_units` is an opt-in list of bare user-unit
names. For each entry the role stops and disables a running unit when the
managed user's systemd session is available, removes stale `default.target`
and `timers.target` links, and converges the unit path to systemd's native
`/dev/null` mask. It also clears any stale failed state recorded for the unit,
so a retired timer cannot continue to appear as a failed user service after it
is masked. A mask prevents a superseded timer or service from being started
again by an installer or helper. The default is empty so unrelated workstations
are unchanged; keep organization-specific unit names in protected inventory
rather than in this public collection.

## Custom CA certificates

The source path is local to the Ansible controller. With `ansible-pull`, that
means a root-readable path on the managed machine, outside the checkout:

```yaml
custom_ca_certificates:
  - name: research-registry
    src: /etc/ansible/private/research-registry.crt
    nss_name: Research registry
```

The role updates the system trust store and, when `nss_name` is set, imports
the certificate into the managed user's NSS database. Do not commit a private
CA certificate merely because it is not a private key: certificate subjects,
names, and topology can still disclose sensitive infrastructure.

## Safety-sensitive switches

`sunshine_enabled`, `profile_cleanup_enabled`, `ziti_enabled`, and all
identity-provider integrations default to `false`. If enabling Sunshine,
provide a non-default credential from a protected variable or configure it
interactively. If enabling cleanup, test the retention window on a disposable
shared machine. If enabling automatic updates, choose an update ring and keep
the local rollback state on a persistent filesystem.
