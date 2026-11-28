#!/usr/bin/env bash
# Prove roles/developer_workstation/files/aide/zz_workstation_excludes with the
# distribution's own AIDE (catalog#185), as security.yml's comments require:
# the rules are checked by aide itself, never assumed from generic docs.
#
# 1. Fetch aide and aide-common from the image's distribution archive without
#    root (apt lists and cache in a private directory) and unpack them.
# 2. Point the packaged /etc/aide/aide.conf at the unpacked tree, add the
#    role's zz_workstation_excludes to its aide.conf.d, and run
#    aide --config-check: any syntax error fails the job.
# 3. aide --path-check every pruned tree and every path that must stay
#    monitored. Exit 0 means the entry would be added to the database, 1 that
#    it would not (aide(1), --path-check).
#
# Runs in the aide-config-check CI job; locally it needs only apt-get and
# dpkg-deb (any Debian or Ubuntu with AIDE 0.19 or later in its archive).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXCLUDES="$REPO/roles/developer_workstation/files/aide/zz_workstation_excludes"
WORK="${AIDE_CHECK_WORK:-$REPO/.aide-check}"
OUT="${AIDE_CHECK_OUT:-$REPO/aide-config-check}"

die() { echo "aide_config_check: $*" >&2; exit 1; }

[ -r "$EXCLUDES" ] || die "missing $EXCLUDES"
rm -rf "$WORK"
mkdir -p "$WORK/apt/lists/partial" "$WORK/apt/cache/archives/partial" "$WORK/root" "$OUT"

# --- AIDE from the distribution archive, without root ------------------------
apt_user=(-o "Dir::State::Lists=$WORK/apt/lists" -o "Dir::Cache=$WORK/apt/cache"
          -o Debug::NoLocking=1 -o APT::Get::Assume-Yes=true -o Acquire::Retries=3)
apt-get "${apt_user[@]}" update >"$OUT/apt.log" 2>&1 \
  || die "apt-get update failed: $(tail -n 5 "$OUT/apt.log")"
# --download-only resolves against this container's dpkg status, so only what
# the image lacks is fetched; dpkg-deb unpacks each package into $WORK/root.
apt-get "${apt_user[@]}" install --download-only --no-install-recommends aide aide-common \
  >>"$OUT/apt.log" 2>&1 || die "downloading aide failed: $(tail -n 5 "$OUT/apt.log")"
shopt -s nullglob
debs=("$WORK"/apt/cache/archives/*.deb)
[ "${#debs[@]}" -gt 0 ] || die "apt-get downloaded no packages"
for deb in "${debs[@]}"; do
  dpkg-deb -x "$deb" "$WORK/root" || die "dpkg-deb could not unpack $deb"
done
ROOT="$WORK/root"
AIDE="$ROOT/usr/bin/aide"
[ -x "$AIDE" ] || die "the aide package has no /usr/bin/aide"
export LD_LIBRARY_PATH="$ROOT/usr/lib/x86_64-linux-gnu:$ROOT/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
"$AIDE" --version >"$OUT/aide-version.txt" 2>&1 || die "aide does not start: $(head -n 3 "$OUT/aide-version.txt")"
echo "AIDE under test: $(head -n 1 "$OUT/aide-version.txt")"

# --- The packaged configuration, with the role's rules added -----------------
CONF="$ROOT/etc/aide/aide.conf"
# Debian's and Ubuntu's aide-common ship the stock configuration under
# /usr/share/aide/config and its maintainer script copies it to /etc (ucf);
# dpkg-deb -x runs no maintainer script, so do that copy here.
SHIPPED="$ROOT/usr/share/aide/config/aide"
if [ ! -r "$CONF" ] && [ -r "$SHIPPED/aide.conf" ]; then
  mkdir -p "$ROOT/etc"
  cp -a "$SHIPPED" "$ROOT/etc/aide"
fi
[ -r "$CONF" ] || die "aide-common ships neither /etc/aide/aide.conf nor /usr/share/aide/config/aide/aide.conf"
ls "$ROOT/etc/aide/aide.conf.d" >"$OUT/aide.conf.d.txt"
grep -q '/etc/aide/aide.conf.d' "$CONF" || die "aide.conf does not include /etc/aide/aide.conf.d"
# x_include runs executable snippets only from a directory owned by the
# current user and not group- or world-writable (aide.conf(5)).
chmod -R go-w "$ROOT/etc/aide"
install -m 0644 "$EXCLUDES" "$ROOT/etc/aide/aide.conf.d/zz_workstation_excludes"
# Read the unpacked tree instead of the container's (absent) /etc/aide.
sed -i "s#/etc/aide/#$ROOT/etc/aide/#g" "$CONF"

if ! "$AIDE" --config "$CONF" --config-check >"$OUT/config-check.log" 2>&1; then
  cat "$OUT/config-check.log" >&2
  die "aide --config-check rejected the configuration"
fi
if grep -qiE 'error|warning' "$OUT/config-check.log"; then
  cat "$OUT/config-check.log" >&2
  die "aide --config-check reported problems"
fi
echo "aide --config-check: ok"

# --- What the rules keep out, and what they still monitor --------------------
failures=0
expect() {  # expect added|skipped TYPE:PATH
  local want="$1" spec="$2" rc=0 got
  "$AIDE" --config "$CONF" --path-check="$spec" >>"$OUT/path-check.log" 2>&1 || rc=$?
  case "$rc" in
    0) got=added ;;
    1) got=skipped ;;
    *) echo "FAIL $spec: aide --path-check exited $rc"; failures=$((failures + 1)); return ;;
  esac
  if [ "$got" = "$want" ]; then
    echo "ok   $spec $got"
  else
    echo "FAIL $spec: $got, expected $want"
    failures=$((failures + 1))
  fi
}
: >"$OUT/path-check.log"
# RAM-backed and volatile trees (catalog#185): neither the directory nor
# anything below it.
for tree in /tmp /var/tmp /dev/shm /run/user; do
  expect skipped "d:$tree"
  expect skipped "d:$tree/agent-scratch"
  expect skipped "f:$tree/agent-scratch/repo/.git/objects/pack/big.pack"
done
expect skipped "f:/run/user/1000/bus.log"
# The earlier prunes.
expect skipped "f:/home/someone/projects/node_modules/x/index.js"
expect skipped "f:/var/lib/docker/overlay2/layer/file"
expect skipped "f:/swap.img"
# System integrity is still monitored, and so are the re-included home files.
expect added "f:/etc/passwd"
expect added "f:/etc/ssh/sshd_config"
expect added "f:/usr/bin/sudo"
expect added "f:/home/someone/.ssh/authorized_keys"
expect added "f:/home/someone/.bashrc"
# Only the exact volatile directories are pruned, not look-alikes.
expect added "f:/var/tmpfiles-not-volatile/config"
expect added "f:/usr/lib/tmpfiles.d/tmp.conf"

if [ "$failures" -ne 0 ]; then
  echo "--- aide --path-check output ---" >&2
  cat "$OUT/path-check.log" >&2
  die "$failures path check(s) failed"
fi
echo "All AIDE path checks passed."
