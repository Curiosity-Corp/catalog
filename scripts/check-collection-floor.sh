#!/bin/sh
# Check the roles against the OLDEST community.general a user may resolve
# (catalog#171, docs/ci-toolchain.md).
#
#   scripts/check-collection-floor.sh COLLECTIONS_DIR
#
# galaxy.yml's dependency range lets users install any community.general from
# its floor upward, while every other CI job uses the exact pin in
# ci/requirements.yml. This script:
#
# 1. reads the floor from galaxy.yml (`community.general: ">=X.Y.Z"`) and fails
#    when requirements.yml (what users install) states a different floor;
# 2. installs exactly that version into COLLECTIONS_DIR, which must be empty
#    or absent, and makes it the ONLY collection path (no ~/.ansible, no
#    site-packages), then checks that ansible-doc reports that version;
# 3. resolves every `community.general.<name>` the roles and playbooks name,
#    as a module, filter, test or lookup, with ansible-doc against that path,
#    and fails on a name the floor lacks or marks deprecated.
#
# ansible-lint and --syntax-check cannot do step 3: neither isolates the
# collection path, and syntax-check does not load dynamically included tasks.
set -eu

if [ "$#" -ne 1 ]; then
  echo "usage: $0 COLLECTIONS_DIR" >&2
  exit 2
fi
dest="$1"
for tool in ansible-galaxy ansible-doc; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "$tool not found: install the CI toolchain (pip install --require-hashes -r ci/requirements.txt)" >&2
    exit 2
  }
done
if [ -e "$dest" ] && [ -n "$(ls -A "$dest")" ]; then
  echo "$dest is not empty; give a fresh directory so no other collection is on the path" >&2
  exit 2
fi

floor="$(sed -n 's/^  community\.general: ">=\([0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)"$/\1/p' galaxy.yml)"
if [ -z "$floor" ]; then
  echo 'galaxy.yml: no dependency line of the form  community.general: ">=X.Y.Z"' >&2
  exit 1
fi
user_floor="$(sed -n 's/^    version: ">=\([0-9][0-9.]*\)"$/\1/p' requirements.yml)"
if [ "$user_floor" != "$floor" ]; then
  echo "requirements.yml asks for community.general >=${user_floor:-?}, galaxy.yml for >=$floor; keep them equal" >&2
  exit 1
fi

# galaxy.ansible.com occasionally resets the connection mid-download.
attempt=1
until ansible-galaxy collection install --force -p "$dest" "community.general:==$floor"; do
  if [ "$attempt" -ge 3 ]; then
    echo "could not install community.general $floor after $attempt attempts" >&2
    exit 1
  fi
  attempt=$((attempt + 1))
  sleep 10
done

ANSIBLE_COLLECTIONS_PATH="$(cd "$dest" && pwd)"
ANSIBLE_COLLECTIONS_SCAN_SYS_PATH=false
ANSIBLE_NOCOLOR=1
export ANSIBLE_COLLECTIONS_PATH ANSIBLE_COLLECTIONS_SCAN_SYS_PATH ANSIBLE_NOCOLOR

installed="$(ansible-galaxy collection list community.general 2>/dev/null \
  | awk '$1 == "community.general" { print $2 }')"
if [ "$installed" != "$floor" ]; then
  echo "expected only community.general $floor on the collection path, found: ${installed:-none}" >&2
  exit 1
fi

names="$(grep -rhoE 'community\.general\.[a-z0-9_]+' roles playbooks \
  --include='*.yml' --include='*.yaml' --include='*.j2' \
  --exclude-dir=streamstack | sort -u)"
if [ -z "$names" ]; then
  echo "no community.general names found in roles/ or playbooks/; is this the repository root?" >&2
  exit 1
fi

listing="$(mktemp)"
trap 'rm -f "$listing"' EXIT
for type in module filter test lookup; do
  ansible-doc -t "$type" -l community.general 2>/dev/null \
    | awk -v t="$type" '$1 ~ /^community\.general\./ { print t, $1 }' >> "$listing"
done

failures=0
for name in $names; do
  type="$(awk -v n="$name" '$2 == n { print $1; exit }' "$listing")"
  if [ -z "$type" ]; then
    echo "MISSING    $name: not in community.general $floor"
    failures=$((failures + 1))
  elif ansible-doc -t "$type" "$name" 2>/dev/null | grep -q '^DEPRECATED'; then
    echo "DEPRECATED $name: $type deprecated in community.general $floor"
    failures=$((failures + 1))
  else
    echo "ok         $name ($type)"
  fi
done

if [ "$failures" -gt 0 ]; then
  echo "$failures community.general name(s) do not work with the galaxy.yml floor $floor: raise the floor or change the role" >&2
  exit 1
fi
echo "every community.general name the roles use exists, not deprecated, in $floor"
