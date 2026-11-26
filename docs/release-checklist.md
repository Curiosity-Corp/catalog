# Release checklist

Use this checklist for every public collection release.

- [ ] Confirm the intended version follows semantic versioning.
- [ ] Review `git diff` for credentials, private hostnames, customer data,
      private CA certificates, mutable installer pipes, and accidental defaults.
- [ ] Run `make validate` on a clean checkout.
- [ ] Run a disposable VM test for each changed profile or integration.
- [ ] Update `CHANGELOG.md`, `docs/compatibility.md`, and migration notes.
- [ ] Build the collection with `ansible-galaxy collection build`.
- [ ] Inspect the tarball contents and compare its checksum with the CI job.
- [ ] Sign the Git tag and collection artifact when the release key is
      available; publish provenance/attestation metadata.
- [ ] Publish the GitHub/GitLab release notes and registry artifact.
- [ ] Link the tag pipeline's `release_assets` artifacts on the release page
      beside the collection: `collection.cdx.json` (the dependency-complete
      SBOM, `docs/sbom.md`), `collection.sha256` and `ci-toolchain.txt`.
      The `sbom` job has already validated the SBOM against the CycloneDX
      schema and checked that it lists every declared dependency.
- [ ] Record the release URL, artifact checksum, CI pipeline, documentation
      URL, and compatibility evidence in the dated evidence log.
- [ ] Copy the `provenance` job's `ci-toolchain.txt` (image digest, Python,
      locked tooling, collections) into the same evidence log
      (`docs/ci-toolchain.md`).
- [ ] Compare the repository description and the release page with
      `docs/project-identity.md`; `galaxy.yml`, README, `SECURITY.md` and that
      page must agree (CI checks the files, not the hosting settings).
- [ ] For a security fix, add the `### Security` changelog line with fixed and
      affected versions and link the published advisory.
- [ ] Announce breaking changes and security fixes clearly to downstream users.
