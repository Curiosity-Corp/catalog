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
[ -n "${QUICKSTART_INNER:-}" ] || : >"$SUMMARY"

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

[ -n "${QUICKSTART_INNER:-}" ] || {
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
[ -z "${QUICKSTART_INNER:-}" ] || echo "- Inside PRoot: uid $(id -u), PID 1 $(tr '\0' ' ' </proc/1/cmdline 2>/dev/null)" >>"$REPORT"

# The shared Kubernetes runner forces a non-root uid, drops every capability,
# blocks ptrace and ships no sudo, so the quickstart (which needs root) cannot
# run directly. Give it a root it can use, in order of fidelity:
#   1. PRoot -0 (fakes uid 0 through ptrace) when ptrace works;
#   2. fakechroot + fakeroot (LD_PRELOAD, no ptrace or user namespaces), the
#      same tools debootstrap's fakechroot variant uses.
# Both run on a copy of this container's own Debian root filesystem, so every
# library in the "chroot" is the one on the host. Their tools come from
# Debian's signed archive, fetched by apt as the unprivileged user. What this
# emulation cannot show is recorded in the evidence file next to the results.
if [ "$(id -u)" != 0 ] && [ -z "${QUICKSTART_INNER:-}" ]; then
  {
    echo "- kernel.yama.ptrace_scope: $(cat /proc/sys/kernel/yama/ptrace_scope 2>/dev/null || echo absent)"
    echo "- user.max_user_namespaces: $(cat /proc/sys/user/max_user_namespaces 2>/dev/null || echo absent)"
    echo "- Capabilities (CapEff): $(awk '/^CapEff/ { print $2 }' /proc/self/status)"
  } >>"$REPORT"
  base="$PWD/.proot"
  rootfs="$base/rootfs"
  mkdir -p "$base/lists/partial" "$base/cache/archives/partial" "$base/debs" "$rootfs"
  apt_user=(-o "Dir::State::Lists=$base/lists" -o "Dir::Cache=$base/cache"
            -o Debug::NoLocking=true -o "APT::Sandbox::User=$(id -un 2>/dev/null || echo nobody)")
  apt-get "${apt_user[@]}" update >"$OUT/0-tools-fetch.log" 2>&1 \
    && (cd "$base/debs" && apt-get "${apt_user[@]}" download proot libtalloc2 \
          fakeroot libfakeroot fakechroot libfakechroot) >>"$OUT/0-tools-fetch.log" 2>&1 \
    || { echo "- Result: could not download the root emulators from the Debian archive (see 0-tools-fetch.log)" >>"$REPORT"; cat "$REPORT"; exit 1; }
  # A file this uid cannot read (for example /etc/shadow) is skipped; tar exits
  # 2 for those, which is expected here.
  tar --one-file-system -C / --exclude=./proc --exclude=./sys --exclude=./dev \
    --exclude=./builds --exclude=./tmp -cf - . 2>"$OUT/0-rootfs.log" \
    | tar -C "$rootfs" --no-same-owner --no-same-permissions -xf - 2>>"$OUT/0-rootfs.log"
  # The emulators live inside the copy, so their paths are valid both outside
  # and inside the fake root.
  for deb in "$base"/debs/*.deb; do dpkg-deb -x "$deb" "$rootfs"; done
  chmod -R u+rwX "$rootfs"
  mkdir -p "$rootfs/tmp" "$rootfs/root" "$rootfs/src"
  chmod 1777 "$rootfs/tmp"
  cp /etc/resolv.conf /etc/hosts "$rootfs/etc/"
  # Rebuild the shadow files the copy could not read: locked passwords only.
  awk -F: '{ print $1 ":*:20000:0:99999:7:::" }' "$rootfs/etc/passwd" >"$rootfs/etc/shadow"
  awk -F: '{ print $1 ":*::" }' "$rootfs/etc/group" >"$rootfs/etc/gshadow"
  lib="$rootfs/usr/lib/x86_64-linux-gnu"
  inner_env=(QUICKSTART_INNER=1 HOME=/root LANG=C.UTF-8 TAGS="$TAGS"
             PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
             CI_COMMIT_SHA="${CI_COMMIT_SHA:-}" CI_JOB_IMAGE="${CI_JOB_IMAGE:-}")
  if LD_LIBRARY_PATH="$lib" "$rootfs/usr/bin/proot" -0 -r "$rootfs" /bin/true >"$OUT/0-proot-probe.log" 2>&1; then
    echo "- Root: emulated with PRoot -0 (runner uid $(id -u))" >>"$REPORT"
    LD_LIBRARY_PATH="$lib" "$rootfs/usr/bin/proot" -0 -r "$rootfs" -b /proc -b /sys -b /dev \
      -b "$PWD:/src" -w /src \
      /usr/bin/env -i "${inner_env[@]}" OUT=/src/debian-quickstart \
      /bin/bash /src/scripts/debian_quickstart_run.sh
    exit $?
  fi
  echo "- PRoot unusable here (see 0-proot-probe.log): $(head -n 1 "$OUT/0-proot-probe.log")" >>"$REPORT"
  echo "- Root: emulated with fakechroot + fakeroot (runner uid $(id -u))" >>"$REPORT"
  # fakechroot cannot bind-mount, so the checkout is copied in and the step
  # logs are copied back out afterwards.
  tar -C "$PWD" --exclude=./.proot --exclude=./.git -cf - . | tar -C "$rootfs/src" -xf -
  fakeroot_lib="$lib/libfakeroot/libfakeroot-sysv.so"
  fakechroot_lib="$lib/fakechroot/libfakechroot.so"
  key_pid=$("$rootfs/usr/bin/faked-sysv") \
    || { echo "- Result: faked-sysv did not start" >>"$REPORT"; cat "$REPORT"; exit 1; }
  ldconfig_subst=/sbin/ldconfig=/bin/true:/usr/sbin/ldconfig=/bin/true:/usr/bin/ldd=/usr/bin/ldd.fakechroot
  env -u LD_LIBRARY_PATH "${inner_env[@]}" OUT=/src/debian-quickstart \
    FAKEROOTKEY="${key_pid%:*}" FAKECHROOT=true \
    FAKECHROOT_VERSION="$(dpkg-deb -f "$base"/debs/fakechroot_*.deb Version | sed 's/-.*//')" \
    FAKECHROOT_EXCLUDE_PATH=/dev:/proc:/sys FAKECHROOT_CMD_SUBST="$ldconfig_subst" \
    LD_LIBRARY_PATH="$lib/fakechroot:$lib/libfakeroot" \
    LD_PRELOAD="$fakechroot_lib:$fakeroot_lib" \
    /usr/sbin/chroot "$rootfs" /bin/bash /src/scripts/debian_quickstart_run.sh
  rc=$?
  kill "${key_pid#*:}" 2>/dev/null || true
  cp -a "$rootfs/src/debian-quickstart/." "$OUT/"
  cat "$OUT/report.md"
  exit "$rc"
fi
if [ "$(id -u)" != 0 ]; then
  echo "- Result: still not root inside the emulator, so the quickstart cannot run." >>"$REPORT"
  cat "$REPORT"
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
# chroot starts in /; the steps use paths relative to the checkout.
[ -z "${QUICKSTART_INNER:-}" ] || cd /src

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
