# Pilot kit for unrelated research projects

For the day a maintainer invites other student, lab or research projects to try
a **released** artifact and say what happened. Nothing here is sent by an
automated process; a person writes to each project.

## Before inviting anyone

- A release exists and its checksum is published (the
  [release checklist](../release-checklist.md) covers this). Pilots use the
  released artifact, not a branch.
- The [researcher quickstarts](../quickstarts/README.md) match that release.
- You can answer within the 7-day target in [GOVERNANCE.md](../../GOVERNANCE.md).
- Your own deployments stay out of the pilot record. Keep them in a separate
  file; public adoption evidence lists only projects that chose to try the
  collection and agreed to be listed.

## Who to invite

Aim for at least five unrelated projects: a student lab on laptops, a research
VM, a shared teaching machine, a thin-client setup, and one behind a proxy or
slow link, so the pilots exercise different quickstarts. "Unrelated" means no
shared funding, employer or maintainer with this project.

## The invitation (edit before sending)

> Hello. We maintain an open source Ansible collection that keeps research
> workstations and VMs up to date. We would like to know whether it solves a
> real maintenance problem for you. If you are willing, try version X.Y.Z on a
> disposable VM following <quickstart link>, then tell us what worked and what
> did not. You do not need to adopt it. We will only publish what you allow,
> and you can withdraw at any time. Questions go to <issue tracker link>.

## What a pilot participant is asked to do

1. Follow one quickstart on a disposable machine, with `--check --diff` first.
2. Run `curiosity-update-summary` after the first update and send what they are
   comfortable sharing ([maintenance summary](../maintenance-summary.md) is
   free of host names and secrets by design).
3. Say what they would change.

## Recording feedback

Use [`docs/evidence/pilot-feedback-template.md`](../evidence/pilot-feedback-template.md),
one file per project, **after** the participant has said what may be published:

- *Public*: project name or a description such as "a university chemistry
  group", feedback, issues opened.
- *Private*: contact details and anything they asked to keep private. These go
  in an access-controlled location, never in this repository.

Open an issue for each defect or request, link it from the feedback file, and
credit the reporter if they agree.

## Prioritizing

A request that two or more pilots make outranks one that a single project
makes, and a fix for a safety problem outranks both. Record the order and the
reason in the monthly evidence file. Do not change a stable default for one
project: that needs a [decision record](../decisions/README.md).

## What this kit does not do

It does not find the projects, send the invitation or get anyone's permission.
Those are person-to-person steps for a maintainer.
