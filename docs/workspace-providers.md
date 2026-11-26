# Remote workspaces: plain SSH or a workspace service

The `workspace` profile (`workstation_profile: workspace`) sets up a headless
machine people reach remotely. It works the same whether that machine is a
plain server you SSH into or a workspace that a workspace service (Coder, or
a service like it with its own Git provider) creates for each person.

The role names no deployment, host or provider. With the defaults it installs
nothing that talks to a workspace service. The credential helper below is
opt-in, and every value it needs comes from your host or group variables.

## A plain SSH workspace

This needs nothing beyond the profile:

```yaml
workstation_profile: workspace
```

With the defaults:

- No git credential helper is installed, and no `/etc/gitconfig` credential
  entry is written. A helper or entry the role installed on an earlier run is
  removed.
- No login-shell environment file is written.
- `coder_cli_enabled` stays `false`, so no workspace service client is
  installed and no deployment is contacted.
- Login shells still read `~/.bashrc`, through `/etc/skel/.bash_profile` and a
  `~/.bash_profile` for the primary account (an existing one is never
  overwritten). This is not provider specific.

Git then works with ordinary SSH credentials. Use one of these:

- **Agent forwarding.** Connect with `ssh -A workspace.example.org`. Your
  laptop's SSH agent answers for the workspace, and no key is stored on it.
  Forward the agent only to machines you trust: their administrators can use
  it while you are connected.
- **A key on the workspace.** Create a key on the workspace
  (`ssh-keygen -t ed25519`) and register its public half with your Git server,
  as a personal key or as a deploy key limited to one project.

Clone over SSH, for example `git clone git@git.example.org:group/project.git`.

## A workspace service with external auth

A workspace service can link each person's Git account once (Coder calls this
external auth) and hand out short-lived tokens on request. The credential
helper uses that: on every git request to your Git server over HTTPS it asks
the service for a token, so nobody hands out or stores a personal access
token.

```yaml
workstation_profile: workspace
workspace_git_auth_helper_enabled: true
workspace_git_host: git.example.org                 # host or host:port, no scheme
workspace_external_auth_provider: primary-git       # the service's id for that Git provider
# Optional:
workspace_external_auth_binary: coder               # a name on PATH or an absolute path
workspace_git_auth_username: oauth2
workspace_login_env:
  GITLAB_HOST: git.example.org                      # for example, the GitLab CLI's default host
workspace_login_env_unset: [GITLAB_TOKEN]
```

The role then:

1. Installs `/usr/local/bin/git-credential-workspace`.
2. Wires it in `/etc/gitconfig` as `credential.https://<workspace_git_host>.helper`,
   for that host only. Other hosts keep their own credentials.
3. Writes `/etc/profile.d/curiosity-workspace-env.sh` while
   `workspace_login_env` or `workspace_login_env_unset` is set, and removes it
   when both are empty.

The helper runs
`<workspace_external_auth_binary> external-auth access-token <workspace_external_auth_provider>`,
the Coder CLI's form. Another service works when its client takes the same
arguments, or through a small wrapper script that does and is named in
`workspace_external_auth_binary`. The client itself is not installed by this
setting; for Coder, `coder_cli_enabled` with `coder_deployment_url` installs
the client that matches your deployment.

### When no token can be had

The helper never prints the token, and never shows anything the token command
wrote: the command's error output is dropped and its standard output is kept
in a variable. When it gets no token it writes one line to standard error and
exits 1. Git then falls back to its next credential source, or stops when there
is none:

```text
git-credential-workspace: cannot get a token for git.example.org: 'coder external-auth access-token primary-git' failed (exit status 1). Sign in to the workspace service and connect your primary-git account there, then try again.
```

| Message ends with | What to do |
| --- | --- |
| `coder is not installed or not on PATH` | Install the service's client (`coder_cli_enabled`), set `workspace_external_auth_binary` to its absolute path, or clone over SSH. |
| `failed (exit status N)` | Sign in to the workspace service from the workspace and connect your account for that provider. Running the command yourself shows the service's own message (and, when it works, the token, so do it only in your own terminal). |
| `printed no token` | The account is not connected yet: connect it in the workspace service. |
| `printed something other than a single token` | The client printed more than one line or word. Point `workspace_external_auth_binary` at a client or wrapper that prints only the token. |

`store` and `erase` do nothing: tokens are fetched fresh for every request, so
nothing is kept on disk.

### Checks before the run

`tasks/validate-config.yml` stops the run before any change
([validation](configuration.md#validation)) when, on the `workspace` profile,
`workspace_git_auth_helper_enabled` is true and `workspace_git_host`,
`workspace_external_auth_provider`, `workspace_external_auth_binary` or
`workspace_git_auth_username` is empty. On any profile it also
refuses values that would not be safe in a shell script or `/etc/gitconfig`: a
host with a scheme, path or login part; a provider or user name with
characters other than letters, digits, `.`, `-` and `_`; a command with spaces
or shell characters; an environment variable name that is not one; and a
multi-line environment value.

## Moving from the earlier GitLab-named settings

Earlier releases always installed `/usr/local/bin/git-credential-coder-gitlab`
and `/etc/profile.d/coder-gitlab.sh` on the `workspace` profile, for
`workspace_gitlab_host` (default `gitlab.com`). Those two variables are
retired: the role ignores them, prints a warning while a host still sets one,
and removes the old files and their `/etc/gitconfig` entry. To keep a helper,
set `workspace_git_auth_helper_enabled: true`, `workspace_git_host` and
`workspace_external_auth_provider`, and move any `GITLAB_HOST` into
`workspace_login_env`.

## Tests

`tests/test_workspace_credential_helper.py` installs the helper with the role's
own tasks into a temporary directory and has real git clone from a loopback
server that demands a token. A stub client asks a stand-in workspace service
on the same loopback server for the token. The tests cover success, the
service refusing (401 and 500), the service unreachable, the client missing,
an empty token and output that is not a single token. Each failure must give
its message and must not show the token. They also run the `/etc/gitconfig`
and file tasks against a throwaway system git configuration. The
`workspace-helper` CI job runs them.
