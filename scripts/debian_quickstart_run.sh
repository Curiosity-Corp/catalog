#!/usr/bin/env bash
# Follow docs/quickstarts/laptop.md as far as a non-root Debian 13 container
# allows, and record what happens at each step (catalog#135).
#
# Runs ONLY in the debian13-quickstart CI job, whose container is discarded
# when the job ends. It targets "localhost," with a local connection, which
# here is the job container itself: never a real host or inventory.
#
# The shared Kubernetes runner is uid 1000 with no capabilities, no sudo and
# kernel.yama.ptrace_scope 2, on purpose. This harness does not emulate root
# (PRoot, fakechroot and user namespaces were tried on this branch and the
# runner refuses them). Instead it:
#   1. prepares the controller as the user, as the guide does, except that apt
#      steps are simulated (see scripts/debian_quickstart_apt.py);
#   2. simulates the role's APT steps for the quickstart tags with user-owned
#      apt state: every package without a candidate on trixie and every vendor
#      repository that does not serve trixie is a finding;
#   3. runs the playbook preview (--syntax-check, --list-tasks, --check --diff)
#      with become switched off, and records exactly where it stops.
# A real apply as root has not run; the evidence file says what a VM run must
# still show.
#
# Every step runs even when an earlier one failed, because the point is to
# record each failure. The script exits non-zero only when it cannot produce
# the report. Output: $OUT/report.md plus one log per step.
set -uo pipefail

OUT="${OUT:-$PWD/debian-quickstart}"
WORK="${WORK:-$PWD/.debian-quickstart-work}"
TAGS="${TAGS:-repos,packages,python,science,data-science,git}"
mkdir -p "$OUT" "$WORK/home"
# The runner uid has no passwd entry and no writable home.
export HOME="$WORK/home"
VARS="$HOME/laptop-vars.yml"
REPORT="$OUT/report.md"
SUMMARY="$OUT/steps.tsv"
: >"$SUMMARY"

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

{
  echo "# Debian quickstart run"
  echo
  echo "- Date (UTC): $(date -u +%Y-%m-%dT%H:%MZ)"
  echo "- Commit: ${CI_COMMIT_SHA:-unknown}"
  echo "- Image: ${CI_JOB_IMAGE:-unknown}"
  # shellcheck disable=SC1091
  echo "- os-release: $(. /etc/os-release && echo "$PRETTY_NAME")"
  echo "- debian_version: $(cat /etc/debian_version 2>/dev/null || echo absent)"
  echo "- uname -r: $(uname -r)"
  echo "- uid: $(id -u)"
  echo "- Capabilities (CapEff): $(awk '/^CapEff/ { print $2 }' /proc/self/status)"
  echo "- kernel.yama.ptrace_scope: $(cat /proc/sys/kernel/yama/ptrace_scope 2>/dev/null || echo absent)"
  echo "- PID 1: $(tr '\0' ' ' </proc/1/cmdline 2>/dev/null || echo unknown)"
  echo "- sudo present: $(command -v sudo >/dev/null && echo yes || echo no)"
  echo "- gpg present: $(command -v gpg >/dev/null && echo yes || echo no)"
  echo "- python: $(command -v python3) $(python3 --version 2>&1)"
  echo "- Tags: $TAGS"
} >"$REPORT"

# Step 1: prepare the controller. The guide's "sudo apt-get install -y git
# python3-venv python3-pip" cannot run here; it is simulated, and the image's
# Python stands in for Debian's python3. The guide clones v0.1.0; here the
# job's own checkout is the code under test.
apt_user=(-o "Dir::State::Lists=$WORK/prereq/lists" -o "Dir::Cache=$WORK/prereq/cache"
          -o Debug::NoLocking=1)
mkdir -p "$WORK/prereq/lists/partial" "$WORK/prereq/cache/archives/partial"
step 1-apt-update apt-get "${apt_user[@]}" update
step 1-apt-prereqs-simulate apt-get "${apt_user[@]}" install --simulate -y git gpg python3-venv python3-pip
step 1-venv python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate || { echo "- Result: venv activation failed" >>"$REPORT"; cat "$REPORT"; exit 1; }
step 1-pip python -m pip install --upgrade pip ansible-core
command -v ansible-playbook >/dev/null \
  || { echo "- Result: ansible-core did not install (see 1-pip.log)" >>"$REPORT"; cat "$REPORT"; exit 1; }
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
echo "- ansible-core: $(ansible --version | head -n 1)" >>"$REPORT"

# Step 2: variables.
step 2-vars cp examples/laptop.yml "$VARS"

# APT: what the packages and repos tags would do on trixie.
python scripts/debian_quickstart_apt.py --work "$WORK/apt" --out "$OUT" >"$OUT/apt-section.md" 2>"$OUT/apt-harness.log"
apt_rc=$?
printf '%s\t%s\t%s\n' "apt-simulation" "$apt_rc" "-" >>"$SUMMARY"
[ "$apt_rc" = 2 ] && { cat "$OUT/apt-section.md" "$OUT/apt-harness.log" >>"$REPORT"; cat "$REPORT"; exit 1; }

# Step 3: preview. The play sets become: true; the runner has no sudo, so
# become is switched off with the ansible_become connection variable, which
# outranks the play keyword. The venv's Python is the target interpreter
# because the image has no /usr/bin/python3. The role's pre-flight checks run
# under any --tags (catalog#165): 3-list-tasks lists them, and 3-check-diff
# skips them by name, since the runner is not root and has no `researcher`
# account; the quickstart-check-preview job proves the root check fires.
export ANSIBLE_ROLES_PATH="$PWD/roles"
nobecome=(-e ansible_become=false -e "ansible_python_interpreter=$(command -v python)")
step 3-syntax ansible-playbook -i localhost, playbooks/site.yml --syntax-check
step 3-list-tasks ansible-playbook -i localhost, -c local playbooks/site.yml \
  -e @"$VARS" --tags "$TAGS" --list-tasks
step 3-check-diff ansible-playbook -i localhost, -c local playbooks/site.yml \
  -e @"$VARS" "${nobecome[@]}" --tags "$TAGS" --skip-tags preflight --check --diff

# Step 4 (apply with --become) is not run: the runner cannot become root.

{
  echo
  echo "| Step | Exit | Seconds |"
  echo "| --- | --- | --- |"
  awk -F'\t' '{ printf "| %s | %s | %s |\n", $1, $2, $3 }' "$SUMMARY"
  cat "$OUT/apt-section.md"
  echo
  echo "## 3-check-diff: where it stops"
  echo
  echo '```'
  grep -E -B2 -A6 '^(fatal|failed|ERROR)' "$OUT/3-check-diff.log" | head -n 80
  grep -E -A3 'PLAY RECAP' "$OUT/3-check-diff.log"
  echo '```'
} >>"$REPORT"
cat "$REPORT"
