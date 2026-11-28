#!/usr/bin/env bash
# Give a host the root-only apt credentials for the StreamStack apt repository
# (catalog#183), before its inventory sets `streamstack_source: apt`.
#
# Run on the host as an operator who is logged in to Azure (`az login`) and can
# sudo:
#
#   scripts/bootstrap-streamstack-apt.sh --host <gitlab host> --vault <key vault> \
#       --secret <secret name> [--project-id <id>]
#
#   --host        the GitLab host serving the repository (streamstack_apt_gitlab_host)
#   --vault       the Azure Key Vault holding the deploy token
#   --secret      the Key Vault secret: a read-only GitLab deploy token
#                 (read_package_registry on the StreamStack project), value
#                 "<username>:<token>"
#   --project-id  the StreamStack project id (streamstack_apt_project_id, default 2122)
#
# It writes one apt auth.conf entry scoped to the project's Generic Package
# Registry path to /etc/apt/auth.conf.d/streamstack.conf, root:root 0600,
# atomically:
#
#   machine <host>/api/v4/projects/<id>/packages/generic/streamstack-apt login <username> password <token>
#
# apt reads auth.conf.d as root before it drops privileges and sends the entry
# over https only. The value goes from az to the file through a pipe: never
# argv, the environment, the terminal or a log, and this script never prints it.
# Running it again rewrites the same file (a rotated token replaces the old one).
#
# It converges nothing: the next catalog run installs the keyring, the source and
# the package (roles/developer_workstation/tasks/streamstack-apt.yml), and fails
# with a pointer here while this file is missing.
# Environment override (tests): STREAMSTACK_APT_AUTH_FILE.
set -euo pipefail

host=""
vault=""
secret=""
project_id=2122
usage() { echo "usage: bootstrap-streamstack-apt --host <gitlab host> --vault <key vault> --secret <name> [--project-id <id>]" >&2; exit 2; }
while (($#)); do
  case $1 in
    --host|--vault|--secret|--project-id)
      [[ $# -ge 2 && -n ${2:-} ]] || { echo "bootstrap-streamstack-apt: $1 needs a value" >&2; exit 2; }
      case $1 in
        --host) host=$2 ;;
        --vault) vault=$2 ;;
        --secret) secret=$2 ;;
        --project-id) project_id=$2 ;;
      esac
      shift 2 ;;
    -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
    *) echo "bootstrap-streamstack-apt: unknown argument $1" >&2; usage ;;
  esac
done
[[ -n $host && -n $vault && -n $secret ]] || usage

auth_file=${STREAMSTACK_APT_AUTH_FILE:-/etc/apt/auth.conf.d/streamstack.conf}

die() { echo "bootstrap-streamstack-apt: $*" >&2; exit 1; }

[[ $host =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(:[0-9]{1,5})?$ ]] || die "invalid GitLab host: $host"
[[ $vault =~ ^[A-Za-z0-9-]{3,24}$ ]] || die "invalid Key Vault name: $vault"
[[ $secret =~ ^[A-Za-z0-9-]{1,127}$ ]] || die "invalid Key Vault secret name: $secret"
[[ $project_id =~ ^[1-9][0-9]{0,9}$ ]] || die "invalid project id: $project_id"
[[ $EUID -ne 0 ]] || die "run as your own user (az login is per user); the script uses sudo where it needs root"
command -v az >/dev/null || die "az (Azure CLI) is not installed"
command -v python3 >/dev/null || die "python3 is not installed"
sudo -n true 2>/dev/null || sudo -v || die "sudo is required"

# Atomic root-only write of stdin to $1, created under umask 077; an empty
# input leaves the old file in place. The parent directory is created (0755)
# only when it does not exist yet.
write_root_only() {
  local dir
  dir=$(dirname "$1")
  sudo test -d "$dir" || sudo install -d -m 0755 -o root -g root "$dir"
  sudo sh -c '
set -eu
umask 077
tmp=$(mktemp "$1.XXXXXX")
trap "rm -f \"\$tmp\"" EXIT
cat > "$tmp"
test -s "$tmp"
chown root:root "$tmp"
chmod 0600 "$tmp"
mv -f "$tmp" "$1"
trap - EXIT
' sh "$1"
}

echo "Writing $auth_file from Key Vault secret $vault/$secret"
az keyvault secret show --vault-name "$vault" --name "$secret" --query value -o tsv |
  MACHINE="$host/api/v4/projects/$project_id/packages/generic/streamstack-apt" python3 -I -c '
import os, sys
machine = os.environ["MACHINE"]
value = sys.stdin.read().strip()
user, sep, token = value.partition(":")
if not sep or not user or not token or any(c.isspace() for c in user + token):
    sys.exit("secret value must be <username>:<token> without whitespace")
sys.stdout.write("# Managed by scripts/bootstrap-streamstack-apt.sh (catalog#183); root:root 0600.\n")
sys.stdout.write(f"machine {machine} login {user} password {token}\n")
' | write_root_only "$auth_file" ||
  die "could not read $vault/$secret or write $auth_file (check az login and Key Vault access)"

sudo test -s "$auth_file" || die "$auth_file is empty"
echo "Credentials written to $auth_file (root:root 0600)."
echo "Next: set streamstack_source: apt and streamstack_apt_gitlab_host for this host; the next catalog run installs StreamStack from apt."
