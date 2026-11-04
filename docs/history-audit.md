# Public Git history audit (catalog #4)

Purpose: decide whether the GitLab repository can become public without
exposing historical private infrastructure, certificates, personal data or
credentials. This report records what was scanned, what was found, a recommended
strategy, and the decision record that a repository owner must complete.

Nothing here changes repository visibility. That is a human decision (see the
end of this file). The report contains rule names, file paths and commit ids
only. It never contains matched values, and it does not name private
organisations or hosts; the private name list is supplied at run time to the
script and is kept out of this repository.

## What was scanned

A fresh `git clone --mirror` taken on 2026-11-04, so every branch, tag and
GitLab-internal ref was included.

| Item | Count |
|---|---|
| Reachable commits (all refs) | 767 (285 merge commits, 482 others) |
| Refs | 201 (20 branches, 1 tag, and GitLab's own `refs/merge-requests/*` and `refs/pipelines/*`) |
| Blobs ever committed | 3,357 (about 52 MB) |
| Issue and merge request text | 291 texts (issues, merge requests, their notes) |

Tools: `gitleaks` 8.30.1 over all history, and `scripts/history_audit.py` (added
here, so the sweep can be repeated) for every text blob, using generic
detectors plus a private list of names. Reproduce with:

```
git clone --mirror <url> history.git
python3 scripts/history_audit.py --repo history.git --markers /path/outside/repo/private-names.txt --paths
```

## Findings

### Credential scan (gitleaks, all history)

480 commits with a diff were scanned (the other 285 are merge commits with no
diff of their own). 10 findings, all rule `generic-api-key`, all classified
**false positive** by reading the context (test fixtures, example values and
placeholders; no working credential):

| File | Commit | Line |
|---|---|---|
| `tests/test_preview_vars.py` | `068bf10e` | 20, 25 |
| `tests/e2e/test_page_telemetry.py` | `8b6af3b2` | 105 |
| `tests/test_console_stream.py` | `8b6af3b2` | 204 |
| `tests/test_crash.py` | `8b6af3b2` | 16 |
| `tests/test_stream_integration.py` | `8b6af3b2` | 195 |
| `tests/test_whip_e2e.py` | `8b6af3b2` | 27 |
| `roles/developer_workstation/tasks/repos.yml` | `a95b9529` | 137 |
| `roles/developer-workstation/defaults/main.yml` | `803f9fbf` | 287, 288 |

A separate private-key-block pattern hit one file, `tests/test_preview_vars.py`,
which holds a synthetic base64 test string, not a key.

### Sweep of all blobs

| Category | Paths | Blobs | Classification |
|---|---|---|---|
| Private key block | 1 | 1 | Synthetic test string (above). |
| Certificate block | 3 | n/a | Public certificates (below). |
| Provider tokens (AWS, GitHub, GitLab, JWT) | 0 | 0 | None found. |
| RFC 1918 addresses | 17 | 76 | Private-network addressing in docs, tests and defaults. Infrastructure detail, not a credential. Needs sanitising before publishing. |
| Email addresses | 17 | n/a | Personal data. Needs review path by path. |
| Private organisation, host or project names | about 13 distinct names across 4 to 17 paths each | 15 to 66 per name | Infrastructure and organisational detail; the largest group is the private deployment domain (17 paths, 66 blobs). Needs sanitising. |

Sensitive-looking file names ever committed (4):

- `roles/developer-workstation/files/harbor-internal-ca.crt`
- `roles/developer-workstation/files/ziti-controller-root-ca.crt`
- `roles/developer_workstation/files/streamstack/packaging/apt-public.asc`
- `roles/developer_workstation/files/streamstack/packaging/signing-cert.pem`

All four are public material (CA certificates and public signing keys). They are
not secrets, but the two CA certificates identify private infrastructure and are
removed together with the names above.

### Issue and merge request text

291 texts scanned for counts only: private keys 0, certificates 0, tokens 0,
JWTs 0. RFC 1918 addresses appear in 6 texts and email addresses in 4. These need
a manual clean-up (edit or delete the notes) before visibility changes, because
issue history is separate from the Git history.

### Not scanned in this pass

- CI job artifacts, the package registry and the container registry. They need
  project-maintainer API access. One real finding came from a related check: the
  collection tarball published by the pipeline on the main branch included a
  secret-detection report file (fixed in the supply-chain change). Old artifacts
  built before that fix may still hold it; expire them.
- Wiki pages, snippets and CI/CD variables (settings, not history).
- Forks and local clones that already exist.

## Rotation

No credential was found, so no rotation is required by this scan. Rotate
anything that a person recognises from the false-positive list (the commits above
are small enough to inspect) before publishing; the cost is low and the scan
cannot prove a test value was never real.

## Strategy options

| Option | What it does | Fits when |
|---|---|---|
| A. Sanitised public mirror (recommended) | Create a new public repository whose first history is a cleaned tree (or a filtered copy of history), and keep the current repository private as the working copy. | The history holds infrastructure names and addresses (it does) and cleaning in place is risky. GitLab's hidden `refs/merge-requests/*` and `refs/pipelines/*` refs, issues and merge request notes never leave the private project. |
| B. Squash to one public initial commit | Same as A, with one commit and no history. | History has no value to outsiders. Loses blame and the audit trail. |
| C. Rewrite history in place (`git filter-repo`), then make public | Remove the files and strings, force-push every branch. | Only if the project must stay at this URL. Hidden refs, MR notes and every clone and fork still hold the old history; GitLab may keep the old objects until garbage collection. Highest risk. |
| D. Publish as is | Switch visibility. | Not recommended: the sweep finds private addressing, names and personal data. |

Recommendation: **A**, with the mirror produced from a filtered copy so authorship
is kept, then re-scanned with the same tools until the sweep reports zero in
every category except the documented public certificates.

## Sanitising checklist for option A

1. Decide the exclusion list: the 4 files above, anything the sweep lists by `--paths`, and the issue and merge request texts that hold addresses (6) or emails (4; some overlap).
2. Build the filtered copy with `git filter-repo` in a scratch clone (never the working repository).
3. Re-run `scripts/history_audit.py` and `gitleaks` on the copy. Target: no private names, no RFC 1918 addresses, no email addresses other than a documented maintainer address, no key blocks.
4. Review a diff of the tree against the private repository for anything the detectors cannot see (screenshots, PDFs, images are not scanned).
5. Publish the copy to a new project, with Issues disabled until the clean-up in the next step is done.
6. Keep the old project private. Do not make it public later.

## Decision record (to be completed by the owner)

| Field | Value |
|---|---|
| Chosen strategy | (A, B, C or D) |
| Repository owner approving | (name, date) |
| Rotation completed or not needed | (who, date) |
| Re-scan after sanitising: gitleaks findings | (number) |
| Re-scan after sanitising: sweep categories at zero | (yes/no) |
| New public repository URL | (URL) |

The owner (a Maintainer of this project) records the decision above and performs
the visibility or publishing step. An agent must not change visibility.
