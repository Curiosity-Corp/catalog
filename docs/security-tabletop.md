# Security tabletop exercise

A tabletop exercise walks the [vulnerability response runbook](vulnerability-response.md)
with a made-up report, so the team finds gaps before a real report arrives. It
uses no real vulnerability, no real reporter and no real credentials.

Allow about 45 minutes. At least two maintainers take part: one plays the
handling maintainer and one the second maintainer. A third person, who does not
have to be a maintainer, plays the reporter.

## Scenario (synthetic)

> A reporter writes through the private advisory form. They say the
> `custom_ca_certificates` task copies certificates with mode `0644` and has
> no check that the file is a certificate authority, so a controller user who
> can edit the variable file can make every managed machine trust any
> certificate. They have not tested it beyond reading the task. They ask when
> it will be fixed and whether they will be credited. The report contains a
> made-up certificate file name, `example-lab-ca.crt`, and no real secrets.

The scenario is invented for the exercise. Do not file it as an issue.

## Run sheet

1. Intake (5 minutes). The reporter posts the message into a private channel.
   The handling maintainer drafts the acknowledgement. Check: is the reply inside
   3 days, does it name the handling maintainer, and does it say the report is
   private?
2. Backup (5 minutes). Pretend the handling maintainer is on leave. The second
   maintainer finds the report, takes it over, and tells the reporter. Check:
   could they find it without asking the first maintainer?
3. Triage (10 minutes). Using the threat model, decide severity, affected
   versions and profiles. Check: does the model's private CA row cover this?
   Does the runbook's severity list give a clear answer, or does it need an edit?
4. Fix and review (10 minutes). Outline the fix and the test that would fail
   without it. The second maintainer reviews the outline and the advisory text.
5. Release and disclosure (10 minutes). Walk the emergency release steps for a
   made-up version number. Draft the changelog `Security` line and the advisory
   text that names affected and fixed versions. Agree the credit line with the
   reporter.
6. Debrief (5 minutes). Write down every point where someone did not know what
   to do, who they asked, and which document should change.

## Record

Copy [`evidence/tabletop-template.md`](evidence/tabletop-template.md) to
`docs/evidence/tabletop-YYYY-MM-DD.md`, fill it in from what happened, and open
a merge request for each gap found. A tabletop that finds no gap is suspicious;
say what was tested.
