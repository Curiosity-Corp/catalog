# Release SBOM

Every pipeline builds `collection.cdx.json`, a CycloneDX 1.6 SBOM for the
collection artifact it builds (catalog#21). It answers the question a lab
has to answer before it adopts the collection: what does it pull in?

## What it lists

Each component carries the property `curiosity:component-kind`:

| Kind | What | From | CycloneDX scope |
| --- | --- | --- | --- |
| `source-file` | every file in the artifact, with its SHA-256 | the built `.tar.gz` | required |
| `collection-dependency` | each `galaxy.yml` dependency, with the declared range (`curiosity:declared-range`) and the version CI resolves and tests | `galaxy.yml`, `ci/requirements.yml` | required |
| `runtime-package-source` | a repository or registry the roles install packages from: the distribution archive, vendor APT repositories, Snap Store, npm, PyPI, Flathub | `ci/sbom-sources.yml` | optional |
| `runtime-package` | one artifact the roles download directly: a release asset, wheel, model or git checkout, with version and SHA-256 when the role pins them | `ci/sbom-sources.yml` and the role defaults | optional |
| `external-installer` | a vendor script or installer the roles run | `ci/sbom-sources.yml` | optional |
| `ci-tool` | the locked CI toolchain: every Python package of `ci/requirements.txt` (with its hashes and `curiosity:direct`), the CI collections, the digest-pinned job images and the crates of `tools/Cargo.lock` | the lock files, `.gitlab-ci.yml` | excluded |

`optional` means a host only uses the source when the feature that needs it is
enabled; `excluded` means it never reaches a host. Runtime components also
carry `curiosity:source` (apt-repository, release-download, installer-script
...), `curiosity:pinned` (does the role check a version or checksum), and
`curiosity:declared-in`. The metadata component is the artifact itself, with
its SHA-256, and its `dependencies` entry lists every collection dependency and
runtime component.

`jq` answers the usual questions:

```bash
jq -r '.components[] | select(.properties[0].value == "external-installer") | .name' collection.cdx.json
jq -r '.components[] | select(.properties[] | .name == "curiosity:pinned" and .value == "false") | .name' collection.cdx.json
```

## How CI builds and checks it

The `sbom` job runs `catalog-tools sbom` (the binary from the `tools` job,
`tools/README.md`), then:

1. `check-jsonschema` validates the file against the CycloneDX 1.6 schema
   vendored in `ci/schemas/cyclonedx-1.6/` (CycloneDX/specification tag
   1.6.1, unmodified). `--base-uri` makes the schema's `$ref`s resolve to the
   vendored files, so validation never reaches the network.
2. `catalog-tools sbom --check` recomputes every declared component and exits 1
   when the file lacks one, describes one with another version, checksum,
   purl or kind, lists one that is not declared, or describes another artifact.

`make sbom` runs the same three steps locally.

Generation itself fails when `ci/sbom-sources.yml` and the roles disagree:

- an http(s) URL in `roles/*/{defaults,tasks,vars,handlers}` that no source
  (`url`, the value of `url_var`, `covers`, `keys`) and no `not_dependencies`
  entry accounts for;
- a task calling a package-manager module (`ansible.builtin.apt`,
  `community.general.npm`, `snap`, `pipx`, `flatpak`, ...) that no source names
  in `modules`;
- a source or `not_dependencies` entry that no longer matches anything, or a
  `url_var`, `version_var` or `sha256_var` that is not a role default;
- a `galaxy.yml` dependency that `ci/requirements.yml` does not pin (its
  resolved version would be unknown).

So a change that adds a download, repository or installer to a role fails CI
until the same MR declares it, and the SBOM cannot fall behind the roles.

## Adding or changing a source

Add an entry to `ci/sbom-sources.yml` with its `kind`, `source`, the URL as
written in the role (`url`) or the default that holds it (`url_var`), and
`declared_in`. When the role pins the artifact, point `version_var` and
`sha256_var` at the defaults so a bump in the defaults reaches the SBOM by
itself. A URL nothing is installed from (an API endpoint, a documentation
link, an XML namespace) goes under `not_dependencies` with its reason.

A source whose URL is set per host has no URL prefix to declare. It is
declared by the module it installs with and its pinned version instead:
`apt-streamstack`, StreamStack's signed apt repository (catalog#183), is built
from `streamstack_apt_gitlab_host` in the inventory, pinned to
`streamstack_apt_release`, and its signing key ships in the role
(`roles/developer_workstation/files/streamstack-apt-public.gpg`, fingerprint
`F3AA4E82E815D695089DB4D2456BDF83CAB0C1E8`, listed as key material in
`REUSE.toml`), so the SBOM records it with no distribution URL and the key in
its note.

## Limits

- The URL scan covers role defaults, tasks, vars and handlers. Scripts shipped
  in `files/` and `templates/` that fetch something on the host are declared
  by hand; the scan does not see them.
- Packages installed from a registry are covered by their source (the APT
  archive, npm, PyPI ...), not one component per package name: the package
  lists are host and profile dependent and the versions are resolved on the
  host at install time. The update transaction records what a host actually
  installed in its own SBOM (`workstation_update_sbom_path`).
- The collection dependency's resolved version is the one CI tests with;
  users who install with `requirements.yml` get whatever satisfies the range.
- The `research-profile` image tag is a CI variable without a digest
  (`docs/ci-toolchain.md`) and is not listed.

## Publishing

The `sbom` and `provenance` jobs keep `collection.cdx.json` next to the
artifact, and `collection.sha256` covers both. On a release tag the
`release_assets` job checks those checksums and keeps the artifact, SBOM,
`ci-toolchain.txt` and `collection.sha256` without expiry, for the release
page to link (`docs/release-checklist.md`).
