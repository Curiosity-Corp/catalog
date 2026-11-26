#!/usr/bin/env bash
# Run step 3 of docs/quickstarts/laptop.md (--check --diff) on a clean machine
# and check how far the preview gets (catalog#164).
#
# Runs ONLY in the quickstart-check-preview CI job: "localhost" is that job's
# throwaway container, which the role has never run on. It previews (--check)
# and applies nothing; it never targets a real host or inventory.
#
# The shared runner is uid 1000 with no sudo, so become is switched off with
# the ansible_become connection variable (it outranks the play keyword). On a
# laptop the user previews with --become instead; check mode takes the same
# paths either way on a machine the role has not run on.
#
# Ubuntu ships python3-apt and gpg; this Debian image has gpg but no
# python3-apt, which apt_repository needs in check mode. The runner cannot
# apt-get install, so the trixie python3-apt package (built for the image's
# Python 3.13) is unpacked into the job's venv. That stands in for the
# preinstalled package; it changes nothing the role does.
#
# Checks:
#   1. `--tags repos --check --diff` succeeds: every vendor keyring is
#      reported as "would change" and the whole repos task group previews.
#   2. With the guide's own tag set the preview runs past the repos group, and
#      the task it stops at, if any, is one laptop.md step 3 names.
set -euo pipefail

if [ "${CI:-}" != "true" ] || [ "${CI_JOB_NAME:-}" != "quickstart-check-preview" ]; then
  echo "refusing to run outside the quickstart-check-preview CI job: it previews against localhost" >&2
  exit 2
fi

OUT="${OUT:-$PWD/quickstart-check-preview}"
WORK="${WORK:-$PWD/.quickstart-check-preview}"
GUIDE=docs/quickstarts/laptop.md
mkdir -p "$OUT" "$WORK/home"
# The runner uid has no passwd entry and no writable home.
export HOME="$WORK/home"
export ANSIBLE_LOCAL_TMP="$WORK/ansible-local" ANSIBLE_REMOTE_TMP="$WORK/ansible-remote"
export ANSIBLE_ROLES_PATH="$PWD/roles" ANSIBLE_NOCOLOR=1 ANSIBLE_FORCE_COLOR=0

python -m venv "$WORK/venv"
# shellcheck disable=SC1091
. "$WORK/venv/bin/activate"
python -m pip install --quiet --upgrade pip ansible-core
for attempt in 1 2 3; do
  ansible-galaxy collection install -r requirements.yml && break
  [ "$attempt" = 3 ] && { echo "ansible-galaxy failed after 3 attempts" >&2; exit 1; }
  echo "ansible-galaxy attempt $attempt failed; retrying in 10s" >&2
  sleep 10
done

apt_dir="$WORK/apt"
apt_opts=(-o "Dir::State::Lists=$apt_dir/lists" -o "Dir::Cache=$apt_dir/cache" -o Debug::NoLocking=1)
mkdir -p "$apt_dir/lists/partial" "$apt_dir/cache/archives/partial" "$apt_dir/deb"
apt-get "${apt_opts[@]}" update
(cd "$apt_dir/deb" && apt-get "${apt_opts[@]}" download python3-apt)
dpkg-deb -x "$apt_dir"/deb/python3-apt_*.deb "$apt_dir/root"
site_packages=$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
echo "$apt_dir/root/usr/lib/python3/dist-packages" >"$site_packages/python3-apt.pth"
python -c 'import apt, apt_pkg' || { echo "python3-apt did not import in the venv" >&2; exit 1; }

tags=$(grep -m1 -oP '^TAGS=\K\S+' "$GUIDE") || { echo "$GUIDE no longer sets TAGS=" >&2; exit 1; }
cp examples/laptop.yml "$HOME/laptop-vars.yml"

# preview <log> <tags>: the guide's step 3 preview, become switched off.
preview() {
  local rc=0
  ansible-playbook -i localhost, -c local playbooks/site.yml \
    -e @"$HOME/laptop-vars.yml" -e ansible_become=false \
    -e "ansible_python_interpreter=$(command -v python)" \
    --tags "$2" --check --diff >"$OUT/$1" 2>&1 || rc=$?
  echo "== preview --tags $2: exit $rc"
  grep -A1 'PLAY RECAP' "$OUT/$1" | tail -n 1
  return "$rc"
}

failures=0

# 1. The repos task group previews end to end.
if preview repos.log repos; then
  keyrings=$(grep -c 'Would download ' "$OUT/repos.log" || true)
  if [ "$keyrings" -gt 0 ]; then
    echo "ok: repos previews to the end; $keyrings vendor keyrings reported as would change"
  else
    echo "FAIL: repos previewed but no keyring was reported as would change" >&2
    failures=$((failures + 1))
  fi
else
  echo "FAIL: --tags repos --check --diff failed:" >&2
  grep -B2 -A6 '^fatal' "$OUT/repos.log" >&2 || tail -n 40 "$OUT/repos.log" >&2
  failures=$((failures + 1))
fi

# 2. The guide's tag set gets past the repos group, and stops only where the
#    guide says it does.
if preview guide-tags.log "$tags"; then
  echo "ok: the guide's tag set ($tags) previews to the end"
else
  stop=$(awk '/^TASK \[/ { task = $0 } /^fatal:/ { print task; exit }' "$OUT/guide-tags.log" \
    | sed -E 's/^TASK \[([^]:]+ : )?([^]]+)\].*/\2/')
  if [ -z "$stop" ]; then
    echo "FAIL: the guide's tag set failed without a failing task:" >&2
    tail -n 40 "$OUT/guide-tags.log" >&2
    failures=$((failures + 1))
  elif ! grep -q 'Include APT package installation (desktop)' "$OUT/guide-tags.log"; then
    echo "FAIL: the guide's tag set stopped at '$stop', before the end of the repos task group" >&2
    grep -B2 -A6 '^fatal' "$OUT/guide-tags.log" >&2
    failures=$((failures + 1))
  elif ! grep -qF "**$stop**" "$GUIDE"; then
    echo "FAIL: the guide's tag set stops at '$stop', which $GUIDE step 3 does not name" >&2
    grep -B2 -A6 '^fatal' "$OUT/guide-tags.log" >&2
    failures=$((failures + 1))
  else
    echo "ok: the guide's tag set runs past the repos group and stops at '$stop', as $GUIDE says"
  fi
fi

exit "$failures"
