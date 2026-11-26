# catalog-tools

The catalog's tools, as one Rust binary with a subcommand per tool
([ADR 0001](../docs/adr/0001-rust-tool-crate.md), catalog#160). The Ansible
collection itself does not depend on it and does not ship it.

| Subcommand | What it does | Issue |
| --- | --- | --- |
| `reference` | Writes `docs/reference.md`, the supported variable and tag reference, from the role defaults, the role tasks and `docs/reference-annotations.yml`. `--check` exits 1 when the committed file is out of date. | #27 |
| `packages` | `list` prints the role's apt packages. `probe` runs inside a distro image and records which of them its archives provide, which a virtual name installs through, and which are missing. `assemble` writes the dated evidence file. `check` exits 1 when the newest `docs/evidence/platform-packages-*.json` no longer matches the role's package list. See [docs/platform-differences.md](../docs/platform-differences.md). | #14, #15 |

## Using it

```bash
make reference          # regenerate docs/reference.md
make reference-check    # what CI checks
make packages-check     # evidence still matches the role's package list
make tools              # rustfmt, clippy and the tests
```

The binary finds the checkout from the current directory (`--root` overrides
it). Exit codes: `0` done or up to date, `1` out of date, `2` error (a parse
error, or an annotation that names a variable or tag the roles no longer have).

Without a local Rust toolchain, take the files from the `tools` CI job instead:
its artifacts always hold the static `catalog-tools` binary (musl), `reference.generated.md` (copy it to
`docs/reference.md`), `tools/Cargo.lock`, and `tools-fmt.patch` when
formatting is off (`git apply tools-fmt.patch`).

## The reference

Generated, never edited by hand:

- variables, their type and default come from `roles/<role>/defaults/main.yml`;
  the description is the comment block directly above each variable (after
  the last `# ====` banner line). Dictionaries and long values are summarised,
  and a value named like a credential (`pass`, `password`, `psk`, `token`,
  `secret`) is shown as `(redacted)`, so the reference never copies host data;
- valid values come from `assert` tasks written as `VAR in [...]`;
- tags come from the `tags:` of every task in `roles/<role>/tasks/*.yml`,
  including tags a `block` passes down and `"{{ variable }}"` tags, which
  resolve to that variable's default list;
- stability (`supported`, `experimental`, `internal`, `private-hook`), safety
  flags, value notes and tag descriptions are curated in
  `docs/reference-annotations.yml`. A variable it does not name is
  `supported`. A name that no longer exists, or an unknown word, fails the
  build.

## Layout

- `catalog-tools/src/main.rs`: the command line.
- `catalog-tools/src/lib.rs`: checkout discovery and the modules.
- `catalog-tools/src/yaml.rs`: YAML loading and value helpers.
- `catalog-tools/src/reference/`: defaults, tasks, annotations and rendering.
- `catalog-tools/src/packages/`: the role's package list, the apt probe
  (`apt.rs`, no root: apt's lists and caches go to a private directory), the
  evidence files and dates.
- `catalog-tools/tests/`: end-to-end tests on a temporary checkout.

Add a tool as a new module and subcommand, with unit tests in the module and an
end-to-end test in `tests/`, then list it in the table above.
