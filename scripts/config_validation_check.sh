#!/usr/bin/env bash
# Test the role's public configuration validation (catalog#31,
# docs/configuration.md#validation) with the examples in tests/config_validation/.
#
# Runs ONLY in the config-validation CI job: "localhost" is that job's
# throwaway container. Valid inputs run tasks/validate-config.yml alone, which
# reads variables and changes nothing. Invalid inputs run the whole role in
# check mode, and the run must stop inside the validation tasks: if validation
# let one through, the next task is the root check, which fails on the non-root
# runner, so even a broken validation applies nothing. It never targets a real
# host or inventory.
#
# Checks:
#   1. Every vars file in examples/ and tests/config_validation/valid/, and
#      every hardware profile with every workstation profile, passes.
#   2. Every tests/config_validation/invalid/ file fails, only validation tasks
#      ran, every `# expect:` line appears in the output and no SENTIN marker
#      (a secret value) appears, at -vvv.
# A report of every case goes to $OUT/report.md, with each run's output.
set -euo pipefail

if [ "${CI:-}" != "true" ] || [ "${CI_JOB_NAME:-}" != "config-validation" ]; then
  echo "refusing to run outside the config-validation CI job: it runs the role against localhost" >&2
  exit 2
fi

ROOT="$PWD"
CASES="$ROOT/tests/config_validation"
OUT="${OUT:-$ROOT/config-validation}"
mkdir -p "$OUT/runs"
export ANSIBLE_ROLES_PATH="$ROOT/roles" ANSIBLE_NOCOLOR=1 ANSIBLE_FORCE_COLOR=0
export ANSIBLE_LOCAL_TMP="$OUT/.ansible-local" ANSIBLE_REMOTE_TMP="$OUT/.ansible-remote"

REPORT="$OUT/report.md"
failures=0
{
  echo "# Configuration validation report"
  echo
  echo "| Case | Expected | Result |"
  echo "| --- | --- | --- |"
} > "$REPORT"

record() { # case expected result
  echo "| \`$1\` | $2 | $3 |" >> "$REPORT"
  if [ "$3" != "ok" ]; then
    failures=$((failures + 1))
    echo "FAIL $1: $3" >&2
  else
    echo "ok   $1"
  fi
}

# Tasks a failing run may reach: the role's local-vars loading, which must
# precede validation, and the validation tasks themselves.
allowed_task() {
  case "$1" in
    "Run validate-config.yml" | \
    "Validating arguments against arg spec "* | \
    "Check for local workstation vars file" | "Load local workstation vars" | \
    "Derive dev_user_home when only dev_user was locally overridden" | \
    "Validate the public configuration" | \
    "Load the public variable specification" | \
    "Validate the types and allowed values of the public variables" | \
    "Find list, mapping, URL, checksum and path values of the wrong shape" | \
    "Find option combinations that cannot work" | \
    "Find secret-bearing settings that cannot work, without printing them" | \
    "Stop before any change when the public configuration is invalid") return 0 ;;
    *) return 1 ;;
  esac
}

run_valid() { # name, then extra ansible-playbook arguments
  local name="$1" log
  shift
  log="$OUT/runs/valid-$(echo "$name" | tr '/ =' '---').log"
  if ansible-playbook -i localhost, "$CASES/play-validate.yml" "$@" > "$log" 2>&1; then
    record "$name" "passes" "ok"
  else
    record "$name" "passes" "failed, see runs/$(basename "$log")"
  fi
}

# 1. Valid configurations.
for file in "$ROOT"/examples/*.yml "$CASES"/valid/*.yml; do
  # examples/ also holds playbooks (lists); only mappings are vars files.
  if [ "$(head -c 4096 "$file" | grep -cE '^- (name|hosts|import_playbook):')" -gt 0 ]; then
    continue
  fi
  run_valid "${file#"$ROOT"/}" -e "@$file"
done
mapfile -t hardware_profiles < <(
  ansible localhost -i localhost, -c local -m ansible.builtin.debug \
    -a "msg={{ hardware_profiles | list | join(' ') }}" \
    -e "@$ROOT/roles/developer_workstation/defaults/main.yml" 2>/dev/null \
    | sed -n 's/.*"msg": "\(.*\)".*/\1/p' | tr ' ' '\n' | sed '/^$/d'
)
if [ "${#hardware_profiles[@]}" -lt 2 ]; then
  record "hardware profile list" "read from defaults" "found ${#hardware_profiles[@]} profiles"
fi
for hardware in "${hardware_profiles[@]}"; do
  for profile in desktop workspace thin-client byod-kiosk; do
    run_valid "profiles $hardware/$profile" -e "hardware_profile=$hardware" -e "workstation_profile=$profile"
  done
done

# 2. Invalid configurations.
for file in "$CASES"/invalid/*.yml; do
  name="${file#"$ROOT"/}"
  log="$OUT/runs/invalid-$(basename "$file" .yml).log"
  role="$(sed -n 's/^# role: *//p' "$file" | head -n 1)"
  role="${role:-developer_workstation}"
  if ansible-playbook -i localhost, --check -vvv "$CASES/play-role.yml" \
      -e "config_validation_role=$role" -e "@$file" > "$log" 2>&1; then
    record "$name" "fails in validation" "the role run succeeded"
    continue
  fi
  problems=()
  while IFS= read -r task; do
    task="${task#"$role : "}"
    allowed_task "$task" || problems+=("task ran before validation stopped: $task")
  done < <(sed -n 's/^TASK \[\(.*\)\] \**$/\1/p' "$log")
  grep -q "^localhost *: .*changed=0 " "$log" || problems+=("a task reported a change")
  grep -qE "^fatal: \[localhost\]: FAILED!" "$log" || problems+=("no failed task")
  while IFS= read -r expected; do
    grep -qF -- "$expected" "$log" || problems+=("missing message: $expected")
  done < <(sed -n 's/^# expect: //p' "$file")
  if grep -q "SENTIN" "$log"; then
    problems+=("a secret value (SENTIN marker) appears in the output")
  fi
  if [ "${#problems[@]}" -eq 0 ]; then
    record "$name" "fails in validation" "ok"
  else
    record "$name" "fails in validation" "$(IFS='; '; echo "${problems[*]}"), see runs/$(basename "$log")"
  fi
done

echo
echo "$failures failing case(s); report: $REPORT"
[ "$failures" -eq 0 ]
