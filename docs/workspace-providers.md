# Workspace providers and plain SSH workspaces

The `workspace` profile makes a fresh remote workspace usable without handing
out a personal access token. Nothing about it is tied to one Coder or GitLab
deployment: the host, the external-auth provider name, the helper command and
the username are variables, and the whole credential helper can be switched off.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `workspace_gitlab_host` | `gitlab.com` | Git host the credential helper and `GITLAB_HOST` apply to. Empty means "no provider". |
| `workspace_gitlab_external_auth_provider` | `gitlab` | Provider name passed to the token command. |
| `workspace_external_auth_binary` | `coder` | Command that mints tokens: `<binary> external-auth access-token <provider>`. |
| `workspace_credential_helper_username` | `oauth2` | The HTTPS Basic-auth user label sent with the token. |
| `workspace_credential_helper_enabled` | `true` | `false` installs no helper, no `/etc/gitconfig` entry and no `profile.d` file. |
| `coder_deployment_url` | empty | The lab's own Coder deployment; required only when `coder_cli_enabled` is true. |

Set them in host or group variables, not in the role.

## What happens when no token is available

The helper is deliberately quiet and safe. It prints nothing and exits 0 when
the binary is missing, when the token call fails (deployment unreachable, not
signed in, provider not configured) and when the token comes back empty. Git then
falls back to its normal behaviour (SSH keys, the next credential helper or a
prompt). `store` and `erase` do nothing, and no token is ever written to disk.
`tests/test_workspace_credential_helper.py` runs the real template against stub
binaries for each of these cases.

## Using a plain SSH workspace

A plain SSH workspace has no Coder and no external-auth provider. Use
`workspace_gitlab_host: ""` or `workspace_credential_helper_enabled: false`:

```yaml
workstation_profile: workspace
workspace_credential_helper_enabled: false   # or: workspace_gitlab_host: ""
coder_cli_enabled: false                     # the default
```

Then:

1. The helper script, `/etc/profile.d/coder-gitlab.sh` and the `/etc/gitconfig`
   credential entry are not installed. Existing files from an earlier run are
   left alone; remove them by hand if you want a clean host.
2. The login-shell fix (`/etc/skel/.bash_profile` and the backfill) still runs,
   because it is not provider specific.
3. Git works with the SSH keys or agent forwarding you already use
   (`ssh -A host`, or a deploy key under the account's `~/.ssh`). Clone with
   `git@host:group/project.git`.

## What is not covered yet

The installer for the Coder CLI itself stays opt-in (`coder_cli_enabled`) and
needs `coder_deployment_url`. Other external-auth brokers can reuse the helper by
setting `workspace_external_auth_binary` to a command that accepts the same
`external-auth access-token <provider>` arguments; a broker with different
arguments needs a small wrapper script on the host. A generic hook for arbitrary
token commands is left for a later change.
