# /etc/profile.d/coder-gitlab.sh
# -----------------------------------------------------------------------------
# System-wide login-shell defaults for Coder workspaces talking to the
# git.developerdojo.org GitLab instance. Sourced by every login shell (bash,
# zsh, sh) — Coder web terminal, ssh login, code-server, etc.
#
# Rationale:
#  - GITLAB_HOST: glab CLI defaults to gitlab.com without this. Pin it.
#  - unset GITLAB_TOKEN: Coder injects a `GITLAB_TOKEN` via its external-auth
#    integration. That token is an OAuth access token; glab v1.92 sends it
#    as a `PRIVATE-TOKEN` header (no Bearer support upstream), which the
#    GitLab API rejects with 401. We unset it so glab CLI falls through to
#    the per-host PAT in `~/.config/glab-cli/config.yml` (or whatever the
#    user has configured via `glab auth login`). Git operations themselves
#    do NOT need the env var — they go through the credential helper at
#    /usr/local/bin/git-credential-coder-gitlab, which DOES use Bearer.
#
# Owned by Curiosity-Corp/catalog (roles/developer-workstation).
# -----------------------------------------------------------------------------

export GITLAB_HOST="git.developerdojo.org"
unset GITLAB_TOKEN
