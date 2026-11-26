#!/usr/bin/env bash
# Follow docs/quickstarts/laptop.md inside a throwaway Debian container and
# record what happens at each step (catalog#135).
#
# Runs ONLY in the debian13-quickstart CI job, whose container is discarded
# when the job ends. It targets "localhost," with a local connection, which
# here is the job container itself: never a real host or inventory.
#
# Every quickstart step runs even when an earlier one failed, because the
# point is to record each failure. The script exits non-zero only when it
# cannot produce the report (for example the container is not root, so no
# step can run). Output: $OUT/report.md plus one log per step.
set -uo pipefail

OUT="${OUT:-$PWD/debian-quickstart}"
TAGS="${TAGS:-repos,packages,python,science,data-science,git}"
VARS="$HOME/laptop-vars.yml"
mkdir -p "$OUT"
REPORT="$OUT/report.md"
SUMMARY="$OUT/steps.tsv"
[ -n "${QUICKSTART_STAGE:-}" ] || : >"$SUMMARY"

# step <name> <command...>: run one quickstart step, keep its log and exit code.
step() {
  local name="$1"; shift
  local log="$OUT/$name.log"
  local start rc
  start=$(date +%s)
  echo "== step $name: $*" | tee "$log"
  "$@" >>"$log" 2>&1
  rc=$?
  printf '%s\t%s\t%s\n' "$name" "$rc" "$(( $(date +%s) - start ))" >>"$SUMMARY"
  echo "== step $name exit $rc ($(( $(date +%s) - start ))s)"
  tail -n 40 "$log"
  return 0
}

[ -n "${QUICKSTART_STAGE:-}" ] || {
  echo "# Debian quickstart run"
  echo
  echo "- Date (UTC): $(date -u +%Y-%m-%dT%H:%MZ)"
  echo "- Commit: ${CI_COMMIT_SHA:-unknown}"
  echo "- Image: ${CI_JOB_IMAGE:-unknown}"
  echo "- os-release: $(. /etc/os-release && echo "$PRETTY_NAME")"
  echo "- uname -r: $(uname -r)"
  echo "- uid: $(id -u)"
  echo "- PID 1: $(tr '\0' ' ' </proc/1/cmdline 2>/dev/null || echo unknown)"
  echo "- sudo present: $(command -v sudo >/dev/null && echo yes || echo no)"
  echo "- Tags: $TAGS"
} >"$REPORT"
[ "${QUICKSTART_STAGE:-}" != inner ] || echo "- Inside the namespace: uid $(id -u), PID 1 $(tr '\0' ' ' </proc/1/cmdline 2>/dev/null)" >>"$REPORT"

# The shared Kubernetes runner forces a non-root uid, drops every capability
# and ships no sudo, so the quickstart (which needs root) cannot run directly.
# Measured on the first runs: kernel.yama.ptrace_scope is 2, so PRoot cannot
# trace; fakechroot started but could not exec any command. Unprivileged user
# namespaces are open, so:
#   - copy this container's own Debian root filesystem into a writable
#     directory (every file then belongs to the runner uid);
#   - unshare a user and mount namespace that maps the runner uid to 0, which
#     makes that copy root-owned and lets us bind /proc, /sys, /dev and chroot;
#   - run under fakeroot inside it, because only uid 0 is mapped and dpkg must
#     chown files to other users and groups (_apt, shadow, ...).
# fakeroot comes from Debian's signed archive, fetched by apt as the runner
# user. What this emulation cannot show is recorded in the evidence file.
if [ "$(id -u)" != 0 ] && [ -z "${QUICKSTART_STAGE:-}" ]; then
  {
    echo "- kernel.yama.ptrace_scope: $(cat /proc/sys/kernel/yama/ptrace_scope 2>/dev/null || echo absent)"
    echo "- user.max_user_namespaces: $(cat /proc/sys/user/max_user_namespaces 2>/dev/null || echo absent)"
    echo "- Capabilities (CapEff): $(awk '/^CapEff/ { print $2 }' /proc/self/status)"
    echo "- Root: emulated with a user namespace (runner uid $(id -u) mapped to 0), chroot and fakeroot"
  } >>"$REPORT"
  base="$PWD/.rootless"
  rootfs="$base/rootfs"
  mkdir -p "$base/lists/partial" "$base/cache/archives/partial" "$base/debs" "$rootfs"
  apt_user=(-o "Dir::State::Lists=$base/lists" -o "Dir::Cache=$base/cache"
            -o Debug::NoLocking=true -o "APT::Sandbox::User=$(id -un 2>/dev/null || echo nobody)")
  apt-get "${apt_user[@]}" update >"$OUT/0-tools-fetch.log" 2>&1 \
    && (cd "$base/debs" && apt-get "${apt_user[@]}" download fakeroot libfakeroot) >>"$OUT/0-tools-fetch.log" 2>&1 \
    || { echo "- Result: could not download fakeroot from the Debian archive (see 0-tools-fetch.log)" >>"$REPORT"; cat "$REPORT"; exit 1; }
  # A file this uid cannot read (for example /etc/shadow) is skipped; tar exits
  # 2 for those, which is expected here.
  tar --one-file-system -C / --exclude=./proc --exclude=./sys --exclude=./dev \
    --exclude=./builds --exclude=./tmp -cf - . 2>"$OUT/0-rootfs.log" \
    | tar -C "$rootfs" --no-same-owner --no-same-permissions -xf - 2>>"$OUT/0-rootfs.log"
  for deb in "$base"/debs/*.deb; do dpkg-deb -x "$deb" "$rootfs"; done
  chmod -R u+rwX "$rootfs"
  mkdir -p "$rootfs/tmp" "$rootfs/root" "$rootfs/src" "$rootfs/proc" "$rootfs/sys" "$rootfs/dev"
  chmod 1777 "$rootfs/tmp"
  cp /etc/resolv.conf /etc/hosts "$rootfs/etc/"
  # Rebuild the shadow files the copy could not read: locked passwords only.
  awk -F: '{ print $1 ":*:20000:0:99999:7:::" }' "$rootfs/etc/passwd" >"$rootfs/etc/shadow"
  awk -F: '{ print $1 ":*::" }' "$rootfs/etc/group" >"$rootfs/etc/gshadow"
  QUICKSTART_STAGE=namespace ROOTFS="$rootfs" SRC="$PWD" \
    unshare --user --map-root-user --mount /bin/bash "$0" 2>"$OUT/0-namespace.log"
  rc=$?
  if [ "$rc" != 0 ] && ! grep -q '^| 4-apply' "$REPORT"; then
    echo "- Result: the namespace stage failed (exit $rc): $(head -n 3 "$OUT/0-namespace.log" | tr '\n' ' ')" >>"$REPORT"
  fi
  cat "$REPORT"
  exit "$rc"
fi
if [ "${QUICKSTART_STAGE:-}" = namespace ]; then
  set -e
  for fs in proc sys dev; do mount --rbind "/$fs" "$ROOTFS/$fs"; done
  mount --bind "$SRC" "$ROOTFS/src"
  key_pid=$(chroot "$ROOTFS" /usr/bin/faked-sysv)
  set +e
  chroot "$ROOTFS" /usr/bin/env -i QUICKSTART_STAGE=inner HOME=/root LANG=C.UTF-8 TAGS="$TAGS" \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    OUT=/src/debian-quickstart CI_COMMIT_SHA="${CI_COMMIT_SHA:-}" CI_JOB_IMAGE="${CI_JOB_IMAGE:-}" \
    FAKEROOTKEY="${key_pid%:*}" \
    LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu/libfakeroot \
    LD_PRELOAD=libfakeroot-sysv.so \
    /bin/bash /src/scripts/debian_quickstart_run.sh
  rc=$?
  kill "${key_pid#*:}" 2>/dev/null || true
  exit "$rc"
fi
if [ "$(id -u)" != 0 ]; then
  echo "- Result: not root even inside the namespace, so the quickstart cannot run." >>"$REPORT"
  cat "$REPORT"
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
# chroot starts in /; the steps use paths relative to the checkout.
[ -z "${QUICKSTART_STAGE:-}" ] || cd /src

# Step 1: prepare the controller. The guide uses sudo; the container is root
# and ships no sudo, so the commands run without it. The guide clones v0.1.0;
# here the job's own checkout is the code under test.
step 1-apt-update apt-get update
step 1-apt-prereqs apt-get install -y git python3-venv python3-pip
step 1-venv python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate || { echo "venv activation failed" >>"$REPORT"; cat "$REPORT"; exit 1; }
step 1-pip python -m pip install --upgrade pip ansible-core
galaxy() {
  local attempt
  for attempt in 1 2 3; do
    ansible-galaxy collection install -r requirements.yml && return 0
    echo "ansible-galaxy attempt $attempt failed; retrying in 10s" >&2
    sleep 10
  done
  return 1
}
step 1-galaxy galaxy

# The guide assumes the laptop owner's account exists. A fresh container has
# none, so create the example's dev_user.
step 1-dev-user useradd --create-home --shell /bin/bash researcher

# Step 2: variables.
step 2-vars cp examples/laptop.yml "$VARS"

# Step 3: preview.
export ANSIBLE_ROLES_PATH="$PWD/roles"
step 3-syntax ansible-playbook -i localhost, playbooks/site.yml --syntax-check
step 3-list-tasks ansible-playbook -i localhost, -c local playbooks/site.yml \
  -e @"$VARS" --tags "$TAGS" --list-tasks
step 3-check-diff ansible-playbook -i localhost, -c local playbooks/site.yml \
  -e @"$VARS" --tags "$TAGS" --check --diff

# Step 4: apply. The container is already root, so --become needs no password
# and --ask-become-pass is dropped (there is no terminal to type into).
step 4-apply ansible-playbook -i localhost, -c local playbooks/site.yml \
  -e @"$VARS" --tags "$TAGS" --become

# Second run: the template expects no changes apart from update checks.
step 4-apply-again ansible-playbook -i localhost, -c local playbooks/site.yml \
  -e @"$VARS" --tags "$TAGS" --become

# Step 5: what changed.
STATE=/var/lib/curiosity/workstation-updates
step 5-state ls -lR "$STATE"
cp "$STATE/last-run.json" "$OUT/" 2>/dev/null || true
cp "$STATE/quarantine.json" "$OUT/" 2>/dev/null || true
step 5-dpkg-count sh -c 'dpkg-query -W -f "\${Package}\n" | wc -l'
step 5-apt-sources sh -c 'ls -l /etc/apt/sources.list.d /etc/apt/keyrings 2>&1; cat /etc/apt/sources.list.d/* 2>&1'

{
  echo
  echo "| Step | Exit | Seconds |"
  echo "| --- | --- | --- |"
  awk -F'\t' '{ printf "| %s | %s | %s |\n", $1, $2, $3 }' "$SUMMARY"
  for f in 3-check-diff 4-apply 4-apply-again; do
    echo
    echo "## $f: recap and failed tasks"
    echo
    echo '```'
    grep -E '^(fatal|failed|ERROR|TASK \[)|PLAY RECAP|localhost +:' "$OUT/$f.log" \
      | grep -E -B1 '^(fatal|failed|ERROR)|PLAY RECAP|localhost +:' | head -n 120
    echo '```'
  done
} >>"$REPORT"
cat "$REPORT"
