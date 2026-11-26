# Configuration validation examples (catalog#31)

Inputs for `scripts/config_validation_check.sh`, which the `config-validation`
CI job runs in its throwaway container (never on a real host or inventory).
docs/configuration.md#validation describes the checks they exercise.

- `valid/*.yml`: extra-vars files that must pass `tasks/validate-config.yml`.
  The job also runs every vars file in `examples/`, and every hardware profile
  with every workstation profile.
- `invalid/*.yml`: extra-vars files that must stop the role. The job runs the
  whole role in check mode and requires that it fails, that no task outside
  the validation tasks ran first, that every `# expect: <text>` line of the
  file appears in the output, and that no value containing `SENTIN` (the
  marker that starts every secret of these files) appears, even at `-vvv`.
- `invalid/*.yml` with a `# role: <name>` line run that role instead of
  developer_workstation.
- `play-role.yml` runs a whole role; `play-validate.yml` runs only
  `validate-config.yml`.

Add a case when you add a rule: one invalid file whose `# expect:` line is the
new message, and, when the rule has a gate, a valid file that passes it.
