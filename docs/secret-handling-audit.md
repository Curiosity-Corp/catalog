# Secret handling, no_log and privilege audit

Audit for catalog#24, taken 2026-11-26 against `main`. It answers three
questions for a lab adopting the collection: which tasks handle a secret,
whether that secret can reach Ansible's output, and where the role runs as
root and why.

The rule is enforced by `tests/test_no_log_policy.py` (CI job `policy`,
`make test`). It fails when a task renders a secret-like variable (token,
password, passphrase, secret, PSK, API or private key, LUKS key, credential,
shadow entry) in its arguments, name, loop or vars without `no_log: true`, or
`slurp`s a credential-like file without it. Conditions (`when`,
`failed_when`, `changed_when`, `until`, `assert`'s `that`) are exempt because
Ansible prints them as written, not with values substituted. Reviewed false
positives live in the test's `ALLOWED` table with their reasons; an entry that
stops matching fails the test.

## How the role gets secrets

The role ships no secret and no default password: every secret-bearing
variable defaults to `""` and its tasks are skipped until the operator sets it
from an inventory vault or a protected `vars` file. The one vendor-specific
lookup left is `samsung_tv_key_vault_secret`, which is an Azure Key Vault
entry *name* that the user's own `curiosity-tv` CLI resolves at run time; the
role never fetches it.

## Tasks that handle a secret

All paths are under `roles/developer_workstation/tasks/` unless noted.

| Task file | Task | Secret | no_log | Notes |
| --- | --- | --- | --- | --- |
| `latest-releases.yml` | Resolve an available GitHub API token | `github_api_token`, `GITHUB_TOKEN`, `GH_TOKEN` | yes | |
| `latest-releases.yml` | Resolve GitHub CLI token for release discovery | `gh auth token` output | yes | Runs as `dev_user`; `failed_when: false`, an absent token means anonymous queries |
| `latest-releases.yml` | Prefer the authenticated GitHub CLI token | `_github_api_token` | yes | |
| `latest-releases.yml` | Query latest / recent / tag-prefixed GitHub releases (3 tasks) | `Authorization: Bearer` header | yes | `failed_when: false`; a failed query shows up as missing release metadata in the installers that follow |
| `maintenance.yml` | Attach Ubuntu Pro | `ubuntu_pro_token` | yes | Failure is now reported by **Require a successful Ubuntu Pro attach** with the exit code, not the token |
| `ai-assistants.yml` | Fetch optional Qwen provider API key | output of `qwen_provider_secret_command` | yes | `failed_when: false`; the next task warns without printing stderr |
| `ai-assistants.yml` | Write NVIDIA API key to Qwen Code env file | `_nvidia_api_key.stdout` | yes | File mode `0600`, owned by `dev_user` |
| `fleetd.yml` | Write the fleetd enroll secret file | `fleetd_enroll_secret` | yes | `/etc/fleetd/enroll-secret.env`, `0600` root |
| `fleetd.yml` | Check / require / load the enroll secret file (4 tasks) | file path only | no (allow-listed) | The value is checked with `grep` on the host and never read into the play |
| `pia-vpn.yml` | Check PIA login state (`slurp` of `/opt/piavpn/etc/account.json`) | PIA daemon auth token | **yes, added here** | Was printed base64-encoded at `-v` |
| `pia-vpn.yml` | Log in to PIA | `pia_login_file` (credentials file) | yes | Failure is now reported by **Require a successful PIA login** with the exit code |
| `streaming.yml` | Set default Sunshine credentials | `sunshine_default_user`, `sunshine_default_pass` | yes | Skipped unless both are set and the password is not `sunshine` |
| `wifi-netplan.yml` | Validate managed WiFi inputs | `wifi_psk` (in `that` only) | **no, removed here** | The assert prints its expressions, never the PSK; `no_log` was hiding the `fail_msg` a user needs |
| `wifi-netplan.yml` | Render / install the WiFi netplan file (2 tasks) | `wifi_psk` via template | yes | `0600` root; `no_log` also hides `--diff` output |
| `minimal-desktop-locking-user.yml` | Read the shadow entry, decide if the user has a password (2 tasks) | `/etc/shadow` hash | yes | |
| `kiosk-cleanup.yml` | Read saved kiosk user groups | group list | yes | Not a secret; kept quiet as account metadata |
| `user-git-timer-entry.yml` | Check / require / use the credential file (3 tasks) | file path only | no (allow-listed) | git reads the root-only `credential-store` file itself |
| `samsung-tv-cli.yml` | Render the Samsung TV Key Vault settings | vault and entry names | no (allow-listed) | Names only, no value |
| `security.yml` | Generate PQ age keypair | private key written by `age-keygen -o` | not needed | The private key goes straight to a `0600` file; only the public key is printed |
| `roles/workstation_storage/tasks/main.yml` | Provision the data volume | `workstation_storage_luks_key_file` path | no (allow-listed) | The provisioning script reads the key file; the play sees only the path |

The remaining `no_log` tasks (`user-systemd.yml`, the manager probe in
`user-git-timer-entry.yml`) run `systemctl --user show-environment`, whose
output is the user manager's whole environment and can carry a token the user
exported. They are quiet for that reason; nothing in them is a role secret.

## Known exposures outside Ansible's output

These are not fixed here because fixing them changes what a run does; each is
a follow-up for a maintainer.

- `pro attach {{ ubuntu_pro_token }}` and `sunshine --creds USER PASS` put the
  secret on the command line, where any local user can read it from
  `/proc/<pid>/cmdline` while the command runs. `pro attach --attach-config`
  and a Sunshine config file would avoid it.
- The Sunshine credentials task has `failed_when: false`, so a failure is
  silent.
- Earlier review items still open (catalog#24 note of 2026-11-04): the PIA
  "latest" installer and WeeChat `go.py` have no checksum, eight git clones
  follow moving refs, the authd archive key is fetched by fingerprint from a
  keyserver, and scripts under `files/` are not scanned by this test.

## Where the role escalates privilege

`playbooks/site.yml` runs the whole play with `become: true`: the role is a
system converge (apt, systemd units, `/etc`, udev, sudoers, netplan) and is
meant to be run as root by `ansible-pull`. It drops privilege where the work
belongs to a person:

- `become_user: "{{ dev_user }}"` (or the looped user) for everything written
  into a home directory or run in a user context: language toolchains, pipx
  and npm packages, VS Code extensions, dotfiles, git config, user systemd
  units, `gh auth token`, the age keypair, Sunshine credentials and the
  StreamStack user services (31 task files).
- `ansible-pull-timer.yml` runs the converge itself from a root systemd timer.

The role also grants root to unprivileged users in two places, both
`validate`d with `visudo`, mode `0440`:

| File | Who | What | Why |
| --- | --- | --- | --- |
| `/etc/sudoers.d/ziti-oidc` (`ziti.yml`) | every user | `ziti-oidc-login.sh`, `-logout.sh`, `-reactivate.sh` as root, no password | The tunnel's identity files are root-owned; a desktop user must be able to renew their own OIDC session. The scripts take no arguments from the caller. |
| `/etc/sudoers.d/curiosity-pia-tunnel` (`pia-tunnel.yml`) | users in `pia_tunnel_users` | `curiosity-pia-run --exec *` as root, no password | Moves one command into the PIA network namespace (`docs/pia-tunnel.md`); the wrapper drops back to the caller's uid before exec. |

`kiosk-cleanup.yml` restores or removes a per-user `/etc/sudoers.d/<dev_user>`
saved by the kiosk profile; it never widens it.
