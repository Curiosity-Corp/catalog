#!/bin/sh
# Fetch every Ansible collection CI uses, once per pipeline (catalog#182,
# docs/ci-toolchain.md). Runs ONLY in the galaxy-collections job; every other
# job reads what this writes and never reaches galaxy.ansible.com
# (tests/test_galaxy_fetch_isolation.py fails when one does).
#
#   scripts/fetch-collections.sh
#
# Run from the repository root. It writes three directories, which the job
# passes on as artifacts:
#
#   .ci-collections        ci/requirements.yml installed: the exact pin every
#                          .python_setup job uses (verified offline there).
#   .ci-collections-floor  community.general at the galaxy.yml floor alone,
#                          for the collection-floor job (catalog#171).
#   .ci-collections-user   requirements.yml downloaded as a user resolves it
#                          today (tarballs plus requirements.yml), for the
#                          quickstart jobs, which install it with --offline.
#
# galaxy.ansible.com resets connections in bursts (Errno 104, catalog#174 and
# catalog#182). Each fetch is retried: FETCH_ATTEMPTS tries (default 5), each
# bounded to FETCH_TRY_TIMEOUT seconds (default 150) and 30 s per Galaxy
# request, FETCH_BACKOFF seconds (default 10) before the second try, doubling
# after that (10, 20, 40, 80). The job itself also has GitLab's retry, so a
# longer outage costs one retried job, never a red merge request.
#
# A directory the GitLab cache restored is reused when it verifies offline
# against its pin, so a warm cache makes no Galaxy request for it.
set -eu

attempts="${FETCH_ATTEMPTS:-5}"
try_timeout="${FETCH_TRY_TIMEOUT:-150}"
backoff="${FETCH_BACKOFF:-10}"
pinned_dir="${FETCH_PINNED_DIR:-.ci-collections}"
floor_dir="${FETCH_FLOOR_DIR:-.ci-collections-floor}"
user_dir="${FETCH_USER_DIR:-.ci-collections-user}"

command -v ansible-galaxy >/dev/null 2>&1 || {
  echo "ansible-galaxy not found: install the CI toolchain (pip install --require-hashes -r ci/requirements.txt)" >&2
  exit 2
}

# retry WHAT DIR ARGS...: run `ansible-galaxy ARGS...` with DIR as the only
# collection path until it succeeds, at most $attempts times, each try bounded
# to $try_timeout seconds. DIR as the only collection path means a dependency
# already installed elsewhere on this machine (~/.ansible) is still fetched
# into DIR and travels with the artifact.
retry() {
  what="$1"
  target="$2"
  shift 2
  attempt=1
  wait="$backoff"
  while :; do
    rc=0
    timeout "$try_timeout" env ANSIBLE_COLLECTIONS_PATH="$target" ANSIBLE_COLLECTIONS_SCAN_SYS_PATH=false \
      ansible-galaxy "$@" || rc=$?
    [ "$rc" -eq 0 ] && return 0
    why="exit $rc"
    [ "$rc" -eq 124 ] && why="no answer from galaxy.ansible.com within ${try_timeout} s"
    if [ "$attempt" -ge "$attempts" ]; then
      echo "fetch-collections: $what failed after $attempt attempts ($why)" >&2
      return "$rc"
    fi
    echo "fetch-collections: $what attempt $attempt failed ($why); retrying in ${wait}s" >&2
    sleep "$wait"
    attempt=$((attempt + 1))
    wait=$((wait * 2))
  done
}

# verified DIR REQUIREMENT...: DIR holds exactly these pins, every file
# matching its manifest. Used to accept a cache hit and to prove a fetch.
verified() {
  dir="$1"
  shift
  [ -d "$dir" ] && ANSIBLE_COLLECTIONS_PATH="$dir" ANSIBLE_COLLECTIONS_SCAN_SYS_PATH=false \
    ansible-galaxy collection verify --offline "$@" >/dev/null 2>&1
}

# 1. The exact pin.
if verified "$pinned_dir" -r ci/requirements.yml; then
  echo "fetch-collections: $pinned_dir from cache, verified offline"
else
  rm -rf "$pinned_dir"
  retry "install ci/requirements.yml" \
    "$pinned_dir" collection install --force --timeout 30 -r ci/requirements.yml -p "$pinned_dir"
  verified "$pinned_dir" -r ci/requirements.yml || {
    echo "fetch-collections: $pinned_dir does not verify against ci/requirements.yml after install" >&2
    exit 1
  }
fi

# 2. The galaxy.yml floor (and its dependencies), in its own directory.
floor="$(scripts/check-collection-floor.sh --print-floor)"
if verified "$floor_dir" "community.general:==$floor"; then
  echo "fetch-collections: $floor_dir (community.general $floor) from cache, verified offline"
else
  rm -rf "$floor_dir"
  retry "install community.general $floor" \
    "$floor_dir" collection install --force --timeout 30 -p "$floor_dir" "community.general:==$floor"
  verified "$floor_dir" "community.general:==$floor" || {
    echo "fetch-collections: $floor_dir does not verify as community.general $floor after install" >&2
    exit 1
  }
fi

# 3. What a user following the quickstart resolves today. Never cached: it
# must follow new releases inside requirements.yml's range.
rm -rf "$user_dir"
retry "download requirements.yml" \
  "$user_dir" collection download --timeout 30 -r requirements.yml -p "$user_dir"
[ -f "$user_dir/requirements.yml" ] || {
  echo "fetch-collections: ansible-galaxy collection download wrote no $user_dir/requirements.yml" >&2
  exit 1
}

echo "fetch-collections: $pinned_dir, $floor_dir and $user_dir ready"
