# Adoption and evidence

The project exists for real users, not activity metrics. Record external use
only when a downstream maintainer has actually chosen the collection or role.
Do not manufacture contributors, split one change into many pull requests, or
count bots as people.

For each monthly snapshot, preserve:

- collection version and artifact checksum;
- supported-platform test results and dates;
- registry downloads and dependent repositories/packages, when the registry
  exposes those measurements;
- public release, documentation, security-policy, and support links;
- anonymized downstream project names or links when permission allows;
- merged external contributor links and the type of substantive contribution;
- incidents, rollback events, and follow-up improvements.

Use [`evidence/monthly-template.md`](evidence/monthly-template.md) and keep
private or personal data out of the public repository. Store the authoritative
export in an access-controlled location, then publish only aggregate facts
that downstream users need.
