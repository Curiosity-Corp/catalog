# Roadmap

The roadmap follows the project's mission: reduce technical debt for students
and researchers while making the automation safe and useful to unrelated OSS
users.

1. Public foundation: sanitize defaults, document the role, add license,
   security/contributor policy, collection metadata, and reproducible CI.
2. First collection release: validate the supported matrix, publish a signed
   `0.1.0` artifact, and document an upgrade path.
3. Researcher experience: provide small examples for Python/data-science,
   remote VM, thin-client, and shared-lab workflows; improve dry-run and
   change summaries.
4. Reliability: finish Molecule-based disposable VM tests, failure injection,
   idempotence checks, rollback drills, and update-ring observability.
5. Security and supply chain: lock CI dependencies, secret scanning, SBOM and
   provenance attestations, signed releases, and a vulnerability response
   process.
6. Community: publish good-first issues, respond to users, credit substantive
   contributors, and accept compatible downstream roles or examples.
7. Adoption and governance: publish dated aggregate evidence, document
   maintainership, and ask real downstream labs what should become stable.

Milestones and issue-level acceptance criteria are tracked in the Curiosity
`catalog` GitLab project. The public repository is the source of truth for
code; private deployment overlays remain outside the collection.
