# Thin client and shared lab examples

Two layouts that university labs ask about:

- a **thin client** or kiosk: a small appliance that holds a browser and access
  tools and reaches a **remote research VM**;
- a **shared lab machine**: one desktop used by several people.

Example variables: [`thin-client.yml`](../../examples/thin-client.yml) and
[`shared-lab.yml`](../../examples/shared-lab.yml). Every service name in them is
synthetic; replace the account names, interface name and certificate path with
your own.

## Security boundary between the access appliance and the remote VM

```text
 person --> [ thin client / kiosk ] ==network==> [ remote research VM ]
            browser, ssh, sshfs                  compilers, Python, data,
            no research data                     notebooks, results
            no cloud credentials                 its own users and updates
```

What the `thin-client` profile installs is a small set (`firefox`,
`openssh-client`, `sshfs`, `rsync`, `git`, shell and network diagnostics).
Compilers, Python science stacks, language runtimes, Kubernetes and DevOps
tools are installed only for the `desktop` and `workspace` profiles, so heavy
tooling and research data belong in the remote VM, not on the appliance.

- **The appliance trusts the VM only through the tools you configure**: SSH
  keys, a browser session, or an optional overlay network. Nothing in the role
  copies credentials from one to the other.
- **The VM does not trust the appliance more than any other client.** Give each
  person their own VM account. Do not give the appliance a shared long-lived
  key with access to everything.
- **The appliance is rebuildable.** Keep no research data on it; treat what is
  on it as disposable, and mount remote data with `sshfs` when needed.
- The `thin-client` and `byod-kiosk` profiles turn the UFW firewall on with
  default-deny inbound; SSH to the appliance is open on every interface unless
  `firewall_ssh_interfaces` names the trusted ones.
- `byod-kiosk` goes further: a full-screen Chromium session pointed at
  `kiosk_target_url`, no shell, no `sudo` for the kiosk user, and virtual
  terminals 2 to 6 disabled. Use it only on borrowed or shared devices.

## Settings for the thin client

| Need | Variable | Example value and notes |
| --- | --- | --- |
| Local identity | `keycloak_sso_enabled: false`, `ziti_enabled: false` | Both are off by default. Users sign in to the remote VM with its own SSH keys or web login; no identity provider is contacted |
| Proxy | environment of the run | The role has no proxy variables. See [restricted networks](restricted-networks.md) for a wrapper playbook |
| Browser | `thin_client_playwright_enabled: false`; `kiosk_target_url` for the kiosk | Firefox is installed by the profile. Playwright tooling is opt-in. Point `kiosk_target_url` at your own URL; the default `https://example.org` is a placeholder |
| Storage | none on the appliance | No data is kept on it; the optional `workstation_storage` role is for a physical second drive and is off by default |
| Network boundary | `firewall_enabled`, `firewall_ssh_interfaces`, `firewall_mosh` | Default-deny inbound; list only the trusted interface names |
| Private web CA | `custom_ca_certificates` | Controller-local certificate files, applied for `thin-client` and `byod-kiosk`. Keep the files out of Git |
| Keyboard layout | `keyboard_layout`, `keyboard_variant`, `keyboard_options` | The profile writes a US layout unless you set these (for example `keyboard_layout: de`). Wrong layouts are the commonest login-prompt problem on an appliance, so check the physical keyboard. See [accessibility and localization](../accessibility-and-localization.md) |

## Settings for a shared lab machine

| Need | Variable | Notes |
| --- | --- | --- |
| Several accounts | `dev_users` (list of `{name, home}`) and `dev_user` | `dev_user` must also be in `dev_users` and stays the primary account for user-scoped tools; every listed account gets per-user desktop and systemd convergence |
| Temporary file hygiene | `tmp_hygiene_enabled`, `tmp_hygiene_max_age` | Age-based `/tmp` cleanup and pressure relief ([guide](../tmp-hygiene.md)). Test the retention window on a disposable machine |
| Stale profiles | `profile_cleanup_enabled`, `profile_cleanup_days` | Weekly timer that removes `/home/<user>@<domain>` directories of directory-joined accounts that have not logged in for the given days. Local accounts without an `@` are never touched |
| Update timing | `workstation_update_ring: general` | A 24-hour observation window and a random start delay up to 24 hours ([update rings](../update-rings.md)) |

## Disabling cleanup, streaming and private integrations

All of these default to `false`, and the examples set them explicitly so a
reader sees the switch:

| Switch | What it controls |
| --- | --- |
| `profile_cleanup_enabled` | Stale directory-user home cleanup timer |
| `tmp_hygiene_enabled` | `/tmp` age-based cleanup and pressure relief |
| `sunshine_enabled` | Moonlight-compatible remote streaming (never enable with a default password) |
| `streamstack_enabled` | Camera, capture and voice studio, derived from the hardware profile; `generic` has none |
| `keycloak_sso_enabled`, `ziti_enabled` | Single sign-on and the overlay network |
| `coder_cli_enabled`, `missionctl_enabled` | Coder deployment CLI and the optional TUI |
| `cloud_clis_enabled` | Azure, AWS and GitLab CLIs (default `true` for `desktop` and `workspace`) |

To turn an integration off on a machine that already ran it, set the switch to
`false`, run again and then check the unit with `systemctl list-timers` or
`systemctl status <unit>`; the role stops managing the integration but does not
promise to remove every file it created earlier.

## Try it with synthetic names

```bash
ansible-playbook -i localhost, -c local playbooks/site.yml \
  -e @examples/thin-client.yml --tags firewall,keyboard --list-tasks
ansible-playbook -i localhost, -c local playbooks/site.yml \
  -e @examples/shared-lab.yml --tags tmp-hygiene,profile-cleanup --list-tasks
```

`tests/test_quickstarts.py` runs exactly these previews, so a renamed variable
or tag fails CI instead of failing a lab.

