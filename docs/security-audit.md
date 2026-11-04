# Privilege, secret handling and no_log audit

This is the first reviewed pass for catalog #24. It is backed by
`scripts/audit_tasks.py` (a read-only static audit of every task file) and by
`tests/test_security_audit.py`, which runs in CI. Re-run it any time:

```
python3 scripts/audit_tasks.py            # summary and every finding needing a decision
python3 scripts/audit_tasks.py --json
python3 scripts/audit_tasks.py --update-baseline   # only after reviewing a new finding
```

The audit prints a rule, a file and a task name. It never prints a value.

## What was reviewed

At the time of writing, 117 task files and 1,550 tasks (the vendored StreamStack
subtree is Python, not Ansible, and is out of scope):

| Inventory | Count | Notes |
|---|---|---|
| `command`, `shell`, `raw`, `script` tasks | 183 | Listed by the tool; pattern checks below apply to each. |
| `uri`, `get_url` tasks | 76 | Same. |

## Rules, and what the tool enforces

| Rule | Enforced as | Meaning |
|---|---|---|
| `curl-pipe` | failure | `curl` or `wget` piped into a shell. None exist today. |
| `insecure-tls` | failure | `validate_certs: false`, `curl -k`, `--no-check-certificate`. None exist today. |
| `default-secret` | failure | A role default that is a non-empty literal for a secret-named variable. One reviewed exception, below. |
| `secret-nolog` | reviewed baseline | A task that passes a secret-looking Jinja variable to a command, network or file module without `no_log`. |
| `secret-mode` | reviewed baseline | A secret-looking destination (`.env`, token, secret, credential, password, `.key`) without a restrictive mode. |
| `no-checksum` | reviewed baseline | `get_url` without `checksum`. |
| `unpinned` | reviewed baseline | A git clone of a moving ref (`main`, `master`, `HEAD`, `latest`), or `pip install` without a version. |

A hard rule fails CI. A baseline rule fails CI only when a finding appears that is
not in `docs/security-audit-baseline.json`, and also when a baseline entry is
fixed but left in the list, so the list can only shrink without a decision.

## Findings and classification

Secret handling that is already right (checked, and now asserted by tests):

- The Qwen provider key is fetched by an operator-supplied command with
  `no_log: true`, written with `no_log: true` to `~/.qwen/.env` mode `0600`, in a
  `0700` directory. The role holds no key and no default for the command.
- Role defaults contain no passwords: `sunshine_default_pass` and
  `qwen_provider_secret_command` default to empty.
- The Git credential helper mints a short-lived token per request and writes
  nothing to disk (docs/workspace-providers.md).
- `/etc/curiosity/samsung-tv.env` holds the NAMES of a vault and a secret, never a
  value; `~/.keys/age.key` is `0600`.

Reviewed baseline, with the reason each was accepted for now:

| Group | Count | Reason | Follow-up |
|---|---|---|---|
| `secret-mode` for `/etc/curiosity/g604.env`, `/etc/curiosity/samsung-tv.env`, `/etc/streamstack/streamstack.env` (mode `0644`) | 3 | Settings and names only; no secret is rendered into them. | Keep it that way; if a secret is ever added, the file must move to `0600`. |
| `secret-mode` for `/usr/local/bin/git-credential-coder-gitlab` | 1 | An executable script that mints tokens; it holds none. | None. |
| `secret-nolog` for the Samsung TV settings | 1 | The templated variable is a vault secret NAME. | See `samsung_tv_key_vault_secret` below. |
| `no-checksum` AWS CLI, Codex, Claude Code, Coder CLI installers | 5 | Each is verified afterwards by a different check (the AWS CLI by a pinned signing-key fingerprint and signature; Codex and Claude Code by a launcher check; Coder by comparing against the live deployment version). A checksum is not available from those vendors in a stable form. | Prefer a pinned digest where a vendor publishes one. |
| `no-checksum` APT signing keys (`apt-keyring`, `authd-repo`) | 2 | `apt_keyring_fingerprint` pins the key when set; the authd key is not pinned. | Pin the authd key fingerprint. |
| `no-checksum` PIA installer ("latest") and the WeeChat `go.py` script | 2 | Mutable download with no verification step. | Real gap. Pin a version and digest, or drop the "latest" installer. |
| `unpinned` git clones: nvm, pyenv (3 tasks), SDKMAN, Oh My Zsh, zsh-autosuggestions, zsh-syntax-highlighting | 8 | Moving refs; each update runs whatever upstream's default branch holds. | Real gap. Pin to tags or commits with a documented refresh. |

Default that remains by design, for now: `samsung_tv_key_vault_secret` defaults to
a private vault-secret NAME (not a value). It is allowed in
`scripts/audit_tasks.py` with that reason. The right fix is an empty default plus
a requirement that it is set when a vault is set; that changes behaviour for
labs that set only the vault, so it needs a release note and is left for a
maintainer.

## Smaller changes made in this pass

- The warning shown when the Qwen provider key cannot be fetched no longer tells
  the reader to run `az login` (a vendor-specific hint); it says to check the
  configured secret command.

## Privilege

The role runs as root by design (a pre-flight asserts it). Per-user work uses
`become_user` or `owner`/`group` on the managed account, and the task files contain about
180 explicit `become` or `become_user` lines. A task-by-task least-privilege review of
the `shell` tasks that could use a purpose-built module is not done.

## Remaining (not done in this pass)

1. Pin or remove the two unverified downloads (PIA "latest", WeeChat `go.py`).
2. Pin the eight moving-ref git clones.
3. Decide the `samsung_tv_key_vault_secret` default.
4. Pin the authd archive key fingerprint.
5. Review `shell` tasks that have no shell syntax and could be `command`, and each
   `become` for the least privilege that works.
6. Add a mutable-installer check for `pip install` inside templates and scripts
   under `files/` (the audit reads task files only).
