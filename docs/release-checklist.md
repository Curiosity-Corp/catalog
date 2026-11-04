# Release checklist

Use this checklist for every public collection release.

- [ ] Confirm the intended version follows semantic versioning.
- [ ] Review `git diff` for credentials, private hostnames, customer data,
      private CA certificates, mutable installer pipes, and accidental defaults.
- [ ] Run `make validate` on a clean checkout.
- [ ] Review `THIRD_PARTY_INVENTORY.yml` and `NOTICE`: every vendored, derived or pinned
      item has its source, version, licence and redistribution conditions, resolve or
      keep excluding `needs-review`/`unresolved` items, and run `make licenses`
      against the built tarball.
- [ ] Run a disposable VM test for each changed profile or integration.
- [ ] Update `CHANGELOG.md`, `docs/compatibility.md`, and migration notes.
- [ ] Build the collection with `ansible-galaxy collection build`.
- [ ] Inspect the tarball contents and compare its checksum with the CI job.
- [ ] Sign the Git tag and collection artifact when the release key is
      available; publish provenance/attestation metadata.
- [ ] Publish the GitHub/GitLab release notes and registry artifact.
- [ ] Record the release URL, artifact checksum, CI pipeline, documentation
      URL, and compatibility evidence in the dated evidence log.
- [ ] Announce breaking changes and security fixes clearly to downstream users.
