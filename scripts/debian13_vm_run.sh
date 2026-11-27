#!/usr/bin/env bash
# shellcheck disable=SC2016  # remote commands are single-quoted on purpose: they expand in the guest
# Follow docs/quickstarts/laptop.md inside a disposable Debian 13 virtual
# machine, apply included, and record what happens at each step (catalog#135).
#
# Runs ONLY in the debian13-vm-run CI job. The VM is a QEMU guest started by
# this script inside the job container, as the runner's ordinary uid: the
# official Debian 13 "generic" cloud image, checked against Debian's
# SHA512SUMS, booted from a throwaway qcow2 overlay with user-mode networking
# (no bridge, no inbound port beyond a loopback SSH forward). Everything,
# image and overlays included, lives in $WORK and is discarded with the job.
# The playbook targets "localhost," inside the guest, exactly as the guide
# does on a laptop: never a real host or inventory.
#
# The runner has no root, so QEMU and its firmware come from Debian's own
# packages, downloaded with user-owned apt state and unpacked with dpkg-deb.
# Without /dev/kvm the guest runs under TCG (software emulation): correct but
# several times slower than a real laptop, so durations are not comparable.
#
# Sequence:
#   0. boot the image, let cloud-init create the `researcher` account (sudo,
#      key-only SSH), shut down, and keep that disk as the clean snapshot;
#   1-4. boot a new overlay on the clean snapshot and run the guide's steps as
#      `researcher`: prepare the controller, copy the variables, preview,
#      apply, and apply again for the changed count;
#   5. collect last-run.json, systemd state, apt sources and package counts;
#   6. restore: boot a fresh overlay on the clean snapshot and check the run
#      left nothing behind (the hypervisor snapshot restore).
#
# Every guide step runs even when an earlier one failed: the point is to
# record each failure. The script exits non-zero only when it cannot produce
# the report (no QEMU, no image, the VM never answers). Output: $OUT.
set -uo pipefail

OUT="${OUT:-$PWD/debian13-vm}"
WORK="${WORK:-${CI_DISK_TMP:-$PWD}/debian13-vm-work}"
TAGS="${TAGS:-repos,packages,python,science,data-science,git}"
IMAGE_DIR_URL="${IMAGE_DIR_URL:-https://cloud.debian.org/images/cloud/trixie/latest}"
IMAGE_NAME="${IMAGE_NAME:-debian-13-generic-amd64.qcow2}"
VM_CPUS="${VM_CPUS:-4}"
VM_MEMORY_MB="${VM_MEMORY_MB:-6144}"
VM_DISK_SIZE="${VM_DISK_SIZE:-40G}"
SSH_PORT="${SSH_PORT:-2222}"
SEED_PORT="${SEED_PORT:-8099}"
MONITOR_PORT="${MONITOR_PORT:-4444}"
# Empty means QEMU's default for the accelerator (qemu64 under TCG).
# SMM off: under TCG the guest sat in SeaBIOS's SMM handler (SMM=1, real mode,
# EIP 0xfe11) after GRUB handed over, and never reached the kernel.
VM_MACHINE="${VM_MACHINE:-q35,smm=off}"
VM_CPU="${VM_CPU:-}"
BOOT_TIMEOUT="${BOOT_TIMEOUT:-1800}"
SRC="$PWD"

mkdir -p "$OUT" "$WORK"
REPORT="$OUT/report.md"
SUMMARY="$OUT/steps.tsv"
: >"$SUMMARY"
QEMU_PID=""
SEED_PID=""

die() {
  echo "- Result: harness stopped: $*" >>"$REPORT"
  echo "debian13_vm_run: $*" >&2
  cat "$REPORT"
  exit 1
}

cleanup() {
  [ -n "$QEMU_PID" ] && kill "$QEMU_PID" 2>/dev/null
  [ -n "$SEED_PID" ] && kill "$SEED_PID" 2>/dev/null
  return 0
}
trap cleanup EXIT

{
  echo "# Debian 13 VM quickstart run"
  echo
  echo "- Date (UTC): $(date -u +%Y-%m-%dT%H:%MZ)"
  echo "- Commit: ${CI_COMMIT_SHA:-$(git rev-parse HEAD 2>/dev/null || echo unknown)}"
  echo "- Job: ${CI_JOB_URL:-local}"
  echo "- Job image: ${CI_JOB_IMAGE:-unknown}"
  echo "- Job kernel: $(uname -r), uid $(id -u), CapEff $(awk '/^CapEff/ { print $2 }' /proc/self/status)"
  echo "- Job CPUs: $(nproc), memory: $(awk '/^MemTotal/ { printf "%d MiB", $2/1024 }' /proc/meminfo)"
  echo "- Tags: $TAGS"
} >"$REPORT"

# --- QEMU from Debian packages, without root --------------------------------
QROOT="$WORK/qemu-root"
apt_user=(-o "Dir::State::Lists=$WORK/apt/lists" -o "Dir::Cache=$WORK/apt/cache"
          -o Debug::NoLocking=1 -o APT::Get::Assume-Yes=true)
mkdir -p "$WORK/apt/lists/partial" "$WORK/apt/cache/archives/partial" "$QROOT"
apt-get "${apt_user[@]}" update >"$OUT/0-qemu-apt.log" 2>&1 \
  || die "apt-get update for QEMU failed (0-qemu-apt.log)"
# --download-only resolves against this container's dpkg status, so only what
# the image lacks is fetched; dpkg-deb unpacks each file into $QROOT.
apt-get "${apt_user[@]}" install --download-only --no-install-recommends \
  qemu-system-x86 qemu-utils seabios ipxe-qemu libnss-wrapper >>"$OUT/0-qemu-apt.log" 2>&1 \
  || die "downloading QEMU packages failed (0-qemu-apt.log)"
for deb in "$WORK"/apt/cache/archives/*.deb; do
  dpkg-deb -x "$deb" "$QROOT" || die "dpkg-deb could not unpack $deb"
done
export PATH="$QROOT/usr/bin:$PATH"
export LD_LIBRARY_PATH="$QROOT/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export QEMU_MODULE_DIR="$QROOT/usr/lib/x86_64-linux-gnu/qemu"
# The runner uid has no passwd entry, and ssh and ssh-keygen refuse to run
# without one ("No user exists for uid 1000"). nss_wrapper (Debian's
# libnss-wrapper) gives this process tree one, without root.
printf 'runner:x:%s:%s:runner:%s:/bin/bash\n' "$(id -u)" "$(id -g)" "$WORK" >"$WORK/passwd"
printf 'runner:x:%s:\n' "$(id -g)" >"$WORK/group"
export NSS_WRAPPER_PASSWD="$WORK/passwd" NSS_WRAPPER_GROUP="$WORK/group"
export LD_PRELOAD="$QROOT/usr/lib/x86_64-linux-gnu/libnss_wrapper.so"
[ -r "$LD_PRELOAD" ] || die "libnss_wrapper.so is missing from the unpacked packages"
qemu-system-x86_64 --version >"$OUT/0-qemu-version.log" 2>&1 \
  || die "qemu-system-x86_64 does not start ($(head -n 3 "$OUT/0-qemu-version.log"))"
if [ -r /dev/kvm ] && [ -w /dev/kvm ]; then
  ACCEL=(-accel kvm -cpu host)
  echo "- Accelerator: KVM" >>"$REPORT"
else
  # QEMU's default CPU model (qemu64): Debian 13 needs only baseline x86-64.
  # With -cpu max the guest kernel printed nothing after GRUB for 18 minutes.
  ACCEL=(-accel "tcg,thread=multi")
  [ -n "$VM_CPU" ] && ACCEL+=(-cpu "$VM_CPU")
  echo "- Accelerator: TCG (no /dev/kvm in the job; software emulation)" >>"$REPORT"
fi
echo "- QEMU: $(head -n 1 "$OUT/0-qemu-version.log")" >>"$REPORT"

# --- Image, checked against Debian's published SHA-512 ----------------------
curl -fsSL --retry 3 -o "$WORK/SHA512SUMS" "$IMAGE_DIR_URL/SHA512SUMS" \
  || die "could not fetch $IMAGE_DIR_URL/SHA512SUMS"
curl -fsSL --retry 3 -o "$WORK/$IMAGE_NAME" "$IMAGE_DIR_URL/$IMAGE_NAME" \
  || die "could not fetch $IMAGE_DIR_URL/$IMAGE_NAME"
expected=$(awk -v n="$IMAGE_NAME" '$2 == n { print $1 }' "$WORK/SHA512SUMS")
[ -n "$expected" ] || die "$IMAGE_NAME is not listed in SHA512SUMS"
echo "$expected  $WORK/$IMAGE_NAME" | sha512sum -c - >/dev/null \
  || die "$IMAGE_NAME does not match SHA512SUMS"
{
  echo "- Image: $IMAGE_DIR_URL/$IMAGE_NAME"
  echo "- Image SHA-512 (matches SHA512SUMS): $expected"
  echo "- Image build: $(curl -fsSIL "$IMAGE_DIR_URL/$IMAGE_NAME" | awk 'tolower($1) == "last-modified:" { $1 = ""; print substr($0, 2) }' | tr -d '\r' | tail -n 1)"
  echo "- VM: $VM_CPUS vCPU, $VM_MEMORY_MB MiB RAM, $VM_DISK_SIZE virtual disk, machine $VM_MACHINE, CPU ${VM_CPU:-default}, user-mode network"
} >>"$REPORT"

# --- cloud-init seed: researcher, key-only SSH, passwordless sudo -----------
SEED="$WORK/seed"
mkdir -p "$SEED"
ssh-keygen -q -t ed25519 -N '' -C debian13-vm-run -f "$WORK/id_ed25519" \
  || die "ssh-keygen failed"
cat >"$SEED/user-data" <<EOF
#cloud-config
users:
  - name: researcher
    gecos: Researcher
    shell: /bin/bash
    groups: [sudo]
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    lock_passwd: true
    ssh_authorized_keys:
      - $(cat "$WORK/id_ed25519.pub")
ssh_pwauth: false
disable_root: true
EOF
printf 'instance-id: debian13-vm-run\nlocal-hostname: debian13-laptop\n' >"$SEED/meta-data"
: >"$SEED/vendor-data"
python3 -m http.server --bind 127.0.0.1 --directory "$SEED" "$SEED_PORT" \
  >"$OUT/0-seed-http.log" 2>&1 &
SEED_PID=$!

SSH=(ssh -i "$WORK/id_ed25519" -p "$SSH_PORT" -o BatchMode=yes -o ConnectTimeout=10
     -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR
     -o ServerAliveInterval=30 -o ServerAliveCountMax=10 researcher@127.0.0.1)

# boot <disk> <serial-log>: start the guest in the background.
boot() {
  qemu-system-x86_64 -machine "$VM_MACHINE" "${ACCEL[@]}" -smp "$VM_CPUS" -m "$VM_MEMORY_MB" \
    -L "$QROOT/usr/share/seabios" -L "$QROOT/usr/share/qemu" -L "$QROOT/usr/lib/ipxe/qemu" \
    -nodefaults -display none -serial "file:$2" \
    -drive "file=$1,if=virtio,format=qcow2,cache=unsafe,discard=unmap" \
    -netdev "user,id=n0,hostfwd=tcp:127.0.0.1:$SSH_PORT-:22" \
    -device virtio-net-pci,netdev=n0,romfile= \
    -device virtio-rng-pci \
    -smbios "type=1,serial=ds=nocloud;s=http://10.0.2.2:$SEED_PORT/" \
    -monitor "tcp:127.0.0.1:$MONITOR_PORT,server,nowait" \
    >>"$OUT/qemu.log" 2>&1 &
  QEMU_PID=$!
}

# monitor_sample: append the guest's state and registers to monitor.log, so a
# guest that never answers SSH shows whether it is running, halted or looping.
monitor_sample() {
  local fd
  { exec {fd}<>"/dev/tcp/127.0.0.1/$MONITOR_PORT"; } 2>/dev/null || return 0
  printf 'info status\ninfo registers\n' >&"$fd"
  { echo "== $(date -u +%H:%M:%S)"; timeout 3 cat <&"$fd"; } >>"$OUT/monitor.log" 2>&1
  exec {fd}>&-
}

# wait_ssh: wait until the guest answers SSH and cloud-init has finished.
wait_ssh() {
  local start=$SECONDS
  until "${SSH[@]}" true 2>/dev/null; do
    kill -0 "$QEMU_PID" 2>/dev/null || return 1
    [ $(( (SECONDS - start) % 300 )) -lt 10 ] && monitor_sample
    [ $((SECONDS - start)) -ge "$BOOT_TIMEOUT" ] && return 1
    sleep 10
  done
  "${SSH[@]}" cloud-init status --wait >/dev/null 2>&1
  echo $((SECONDS - start))
}

# shutdown_vm: power the guest off from inside and wait for QEMU to exit.
shutdown_vm() {
  "${SSH[@]}" sudo systemctl poweroff >/dev/null 2>&1
  local _
  for _ in $(seq 1 60); do
    kill -0 "$QEMU_PID" 2>/dev/null || { QEMU_PID=""; return 0; }
    sleep 5
  done
  kill "$QEMU_PID" 2>/dev/null
  QEMU_PID=""
}

# step <name> <remote shell command>: one guide step inside the guest, as
# researcher, from the checkout. Keeps the log, exit code and duration.
step() {
  local name="$1" cmd="$2" log="$OUT/$1.log" start rc
  start=$SECONDS
  echo "== step $name: $cmd" | tee "$log"
  "${SSH[@]}" "cd ~/catalog 2>/dev/null || cd ~; $cmd" </dev/null >>"$log" 2>&1
  rc=$?
  printf '%s\t%s\t%s\n' "$name" "$rc" "$((SECONDS - start))" >>"$SUMMARY"
  echo "== step $name exit $rc ($((SECONDS - start))s)"
  tail -n 30 "$log"
  return 0
}

# --- 0. First boot: cloud-init, then keep the disk as the clean snapshot ----
qemu-img create -q -f qcow2 -F qcow2 -b "$WORK/$IMAGE_NAME" "$WORK/clean.qcow2" "$VM_DISK_SIZE" \
  || die "qemu-img could not create the clean overlay"
boot "$WORK/clean.qcow2" "$OUT/serial-0-first-boot.log"
first_boot=$(wait_ssh) || { tail -n 60 "$OUT/serial-0-first-boot.log"; die "the guest did not answer SSH within ${BOOT_TIMEOUT}s (serial-0-first-boot.log)"; }
{
  echo "- First boot to SSH and cloud-init done: ${first_boot}s"
  echo "- Guest os-release: $("${SSH[@]}" '. /etc/os-release; echo "$PRETTY_NAME"')"
  echo "- Guest debian_version: $("${SSH[@]}" cat /etc/debian_version)"
  echo "- Guest kernel before the run: $("${SSH[@]}" uname -r)"
  echo "- Guest PID 1: $("${SSH[@]}" 'ps -p 1 -o comm=')"
  echo "- Guest packages before the run: $("${SSH[@]}" "dpkg-query -W -f='\${db:Status-Abbrev}\n' | grep -c '^ii'")"
  echo "- Guest python3: $("${SSH[@]}" 'python3 --version; dpkg-query -W -f="python3-apt \${Version}\n" python3-apt 2>/dev/null || echo "python3-apt not installed"' | tr '\n' ' ')"
} >>"$REPORT"
"${SSH[@]}" "dpkg-query -W -f='\${Package}\n' | sort" >"$OUT/packages-before.txt" 2>&1
shutdown_vm

# --- 1-4. The guide, on a new overlay over the clean snapshot ---------------
qemu-img create -q -f qcow2 -F qcow2 -b "$WORK/clean.qcow2" "$WORK/run.qcow2" \
  || die "qemu-img could not create the run overlay"
boot "$WORK/run.qcow2" "$OUT/serial-1-run.log"
wait_ssh >/dev/null || die "the guest did not come back for the run (serial-1-run.log)"

# Step 1. The guide clones tag v0.1.0 from GitHub; the code under test here is
# this job's commit, copied in with git archive (same files, no .git).
step 1-apt-update "sudo apt-get update"
step 1-apt-prereqs "sudo apt-get install -y git python3-venv python3-pip"
start=$SECONDS
git -C "$SRC" archive --format=tar HEAD \
  | "${SSH[@]}" 'rm -rf ~/catalog && mkdir ~/catalog && tar -x -C ~/catalog' >"$OUT/1-checkout.log" 2>&1
printf '%s\t%s\t%s\n' "1-checkout" "$?" "$((SECONDS - start))" >>"$SUMMARY"
step 1-venv "python3 -m venv .venv"
step 1-pip ". .venv/bin/activate && python -m pip install --upgrade pip ansible-core"
step 1-galaxy ". .venv/bin/activate && for a in 1 2 3; do ansible-galaxy collection install -r requirements.yml && exit 0; sleep 10; done; exit 1"
echo "- ansible-core in the guest: $("${SSH[@]}" '"$HOME"/catalog/.venv/bin/ansible --version | head -n 1')" >>"$REPORT"

# Step 2.
step 2-vars "cp examples/laptop.yml ~/laptop-vars.yml"

# Steps 3 and 4. The guide adds --ask-become-pass; the VM account has
# passwordless sudo, so the prompt is left out and nothing else changes.
play=". .venv/bin/activate && ansible-playbook -i localhost, -c local playbooks/site.yml -e @\$HOME/laptop-vars.yml --tags '$TAGS'"
step 3-syntax ". .venv/bin/activate && ansible-playbook -i localhost, playbooks/site.yml --syntax-check"
step 3-list-tasks "$play --list-tasks"
step 3-check-diff "$play --check --diff --become"
step 4-apply "$play --become"
step 4-apply-again "$play --become"

# --- 5. What changed ---------------------------------------------------------
step 5-last-run "sudo cat /var/lib/curiosity/workstation-updates/last-run.json"
step 5-state-dir "sudo ls -la /var/lib/curiosity/workstation-updates /var/lib/curiosity/workstation-updates/snapshots"
step 5-systemd-failed "systemctl --failed --no-pager; systemctl --user --failed --no-pager"
step 5-systemd-timers "systemctl list-timers --all --no-pager"
step 5-systemd-enabled "systemctl list-unit-files --state=enabled --no-pager"
step 5-apt-sources "ls -l /etc/apt/sources.list.d /etc/apt/keyrings /usr/share/keyrings; grep -rh '^deb\\|^URIs\\|^Suites' /etc/apt/sources.list.d"
step 5-disk "df -h / ; sudo du -sh /opt /var/cache/apt /var/lib/curiosity /var/cache/curiosity 2>/dev/null"
step 5-science "/opt/science-venv/bin/python -c 'import numpy, pandas, scipy, matplotlib; print(numpy.__version__, pandas.__version__, scipy.__version__, matplotlib.__version__)'; ls -l /usr/local/bin/jupyter /usr/local/bin/ipython; sudo -u researcher -i pipx list --short"
"${SSH[@]}" "dpkg-query -W -f='\${Package}\n' | sort" >"$OUT/packages-after.txt" 2>&1
{
  echo "- Guest packages after the run: $(wc -l <"$OUT/packages-after.txt")"
  echo "- Packages added: $(comm -13 "$OUT/packages-before.txt" "$OUT/packages-after.txt" | wc -l) (packages-added.txt)"
} >>"$REPORT"
comm -13 "$OUT/packages-before.txt" "$OUT/packages-after.txt" >"$OUT/packages-added.txt"
shutdown_vm

# --- 6. Hypervisor snapshot restore -----------------------------------------
qemu-img create -q -f qcow2 -F qcow2 -b "$WORK/clean.qcow2" "$WORK/restore.qcow2" \
  || die "qemu-img could not create the restore overlay"
boot "$WORK/restore.qcow2" "$OUT/serial-2-restore.log"
if wait_ssh >/dev/null; then
  step 6-restore-check "test ! -e /var/lib/curiosity && test ! -e /opt/science-venv && echo 'clean: no /var/lib/curiosity, no /opt/science-venv'; dpkg-query -W -f='\${Package}\n' | wc -l"
  shutdown_vm
else
  printf '%s\t%s\t%s\n' "6-restore-check" "boot-failed" "-" >>"$SUMMARY"
fi

{
  echo
  echo "| Step | Exit | Seconds |"
  echo "| --- | --- | --- |"
  awk -F'\t' '{ printf "| %s | %s | %s |\n", $1, $2, $3 }' "$SUMMARY"
  for s in 3-check-diff 4-apply 4-apply-again; do
    echo
    echo "## $s: failures and recap"
    echo
    echo '```'
    grep -E -B2 -A8 '^(fatal|failed|ERROR)' "$OUT/$s.log" | head -n 120
    grep -E -A3 'PLAY RECAP' "$OUT/$s.log"
    echo '```'
  done
} >>"$REPORT"
cat "$REPORT"
