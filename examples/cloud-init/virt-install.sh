#!/usr/bin/env bash
# Create a research VM on a local libvirt/QEMU host from a public cloud image
# and the cloud-init example next to this script
# (docs/quickstarts/cloud-init-libvirt.md). No vendor account is involved.
#
# The script only creates new files in the work directory and defines one new
# libvirt domain. It refuses to reuse an existing domain or disk, never deletes
# anything, and never applies the role: that stays a reviewed, manual step.
#
# Usage:
#   examples/cloud-init/virt-install.sh --image noble-server-cloudimg-amd64.img \
#     --ssh-key ~/.ssh/id_ed25519.pub [--name research-vm] [--workdir DIR] \
#     [--os-size 40G] [--data-size 100G] [--memory 4096] [--vcpus 2] \
#     [--network network=default] [--dry-run]
#
# Requires: qemu-img, virt-install, and cloud-localds (cloud-image-utils) or
# genisoimage. Download the cloud image yourself and check it against the
# publisher's SHA256SUMS before you pass it here.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
name="research-vm"
image=""
ssh_key=""
workdir=""
os_size="40G"
data_size="100G"
memory="4096"
vcpus="2"
network="network=default"
os_variant="ubuntu24.04"
dry_run=0

die() {
  echo "virt-install.sh: $*" >&2
  exit 1
}

usage() {
  sed -n '2,19p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --name) name="${2:?--name needs a value}"; shift 2 ;;
    --image) image="${2:?--image needs a path}"; shift 2 ;;
    --ssh-key) ssh_key="${2:?--ssh-key needs a path}"; shift 2 ;;
    --workdir) workdir="${2:?--workdir needs a path}"; shift 2 ;;
    --os-size) os_size="${2:?--os-size needs a size}"; shift 2 ;;
    --data-size) data_size="${2:?--data-size needs a size}"; shift 2 ;;
    --memory) memory="${2:?--memory needs MiB}"; shift 2 ;;
    --vcpus) vcpus="${2:?--vcpus needs a count}"; shift 2 ;;
    --network) network="${2:?--network needs a libvirt network spec}"; shift 2 ;;
    --os-variant) os_variant="${2:?--os-variant needs a value}"; shift 2 ;;
    --dry-run) dry_run=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument '$1' (see --help)" ;;
  esac
done

[ -n "$image" ] || die "--image is required: a downloaded, checksum-verified cloud image"
[ -n "$ssh_key" ] || die "--ssh-key is required: the public key that may log in as 'researcher'"
[ -r "$image" ] || die "cannot read cloud image '$image'"
[ -r "$ssh_key" ] || die "cannot read public key '$ssh_key'"
case "$name" in
  *[!A-Za-z0-9._-]*|"") die "--name '$name' may only use letters, digits, '.', '_' and '-'" ;;
esac

key="$(head -n 1 "$ssh_key")"
case "$key" in
  ssh-ed25519\ *|ecdsa-sha2-*\ *|sk-ssh-ed25519@openssh.com\ *|sk-ecdsa-sha2-*\ *|ssh-rsa\ *) ;;
  *) die "'$ssh_key' does not look like an OpenSSH public key (did you pass the private key?)" ;;
esac
case "$key" in
  *EXAMPLE*) die "'$ssh_key' is the documentation placeholder; pass your own public key" ;;
esac

workdir="${workdir:-$PWD/$name}"
os_disk="$workdir/$name.qcow2"
data_disk="$workdir/$name-data.qcow2"
seed="$workdir/$name-seed.iso"

run() {
  printf '+'
  printf ' %q' "$@"
  printf '\n'
  if [ "$dry_run" -eq 0 ]; then
    "$@"
  fi
}

if [ "$dry_run" -eq 0 ]; then
  for tool in qemu-img virt-install virsh; do
    command -v "$tool" >/dev/null 2>&1 || die "'$tool' is not installed"
  done
  if virsh dominfo "$name" >/dev/null 2>&1; then
    die "a libvirt domain named '$name' already exists; pick another --name"
  fi
  for path in "$os_disk" "$data_disk" "$seed"; do
    [ ! -e "$path" ] || die "'$path' already exists; this script never overwrites a disk"
  done
  mkdir -p "$workdir"
fi

# The seed carries the example user-data with the placeholder key swapped for
# yours, and a meta-data whose instance-id and hostname match --name.
user_data="$workdir/user-data"
meta_data="$workdir/meta-data"
if [ "$dry_run" -eq 0 ]; then
  KEY="$key" awk '
    /ssh-ed25519 AAAA[A-Za-z0-9]*EXAMPLE/ { sub(/ssh-ed25519 .*/, ENVIRON["KEY"]) }
    { print }
  ' "$here/user-data" > "$user_data"
  grep -q 'EXAMPLE' "$user_data" && die "placeholder key still present in $user_data"
  sed -e "s/^hostname: .*/hostname: $name/" -i "$user_data"
  printf 'instance-id: %s-%s\nlocal-hostname: %s\n' "$name" "$(date -u +%Y%m%d%H%M%S)" "$name" > "$meta_data"
fi

if [ "$dry_run" -eq 1 ] || command -v cloud-localds >/dev/null 2>&1; then
  run cloud-localds "$seed" "$user_data" "$meta_data"
elif command -v genisoimage >/dev/null 2>&1; then
  run genisoimage -quiet -output "$seed" -volid cidata -joliet -rock "$user_data" "$meta_data"
else
  die "install cloud-image-utils (cloud-localds) or genisoimage to build the seed"
fi

# OS disk: a copy-on-write overlay, so the base image stays untouched and one
# image serves many VMs. Data disk: separate, so the OS disk can be rebuilt
# without touching research data (cloud-init formats it only when it is empty).
run qemu-img create -f qcow2 -F qcow2 -b "$(realpath "$image")" "$os_disk" "$os_size"
run qemu-img create -f qcow2 "$data_disk" "$data_size"

run virt-install \
  --name "$name" \
  --memory "$memory" \
  --vcpus "$vcpus" \
  --os-variant "$os_variant" \
  --import \
  --disk "path=$os_disk,format=qcow2,bus=virtio" \
  --disk "path=$data_disk,format=qcow2,bus=virtio" \
  --disk "path=$seed,device=cdrom" \
  --network "$network,model=virtio" \
  --graphics none \
  --noautoconsole

cat <<EOF

Next steps:
  virsh console $name            # watch cloud-init finish (Ctrl+] to leave)
  virsh domifaddr $name          # the address to SSH to
  ssh researcher@<address> cloud-init status --wait
  virsh snapshot-create-as $name before-catalog --disk-only --atomic --diskspec vdb,snapshot=no
                                 # OS disk only: a revert never rolls back research data
Then follow docs/quickstarts/research-vm.md to review and apply the role.
EOF
