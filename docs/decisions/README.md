# Decision records

A decision record is a short file that explains why a default, a public
variable, a tag, a profile, a supported platform or a schema is the way it is.
Future maintainers and downstream users read these to learn whether a change is
safe to undo.

## When to write one

Write a record for any change that:

- changes the default value of a public variable, or adds a default-on behaviour;
- adds, renames or removes a public variable, tag, profile or schema field;
- adds or drops a supported platform;
- weakens or removes a security control listed in the
  [threat model](../threat-model.md).

Routine fixes and documentation changes do not need one.

## How

1. Copy [`0000-template.md`](0000-template.md) to the next free number, such as
   `0003-short-title.md`.
2. Fill in every heading. Keep it under one page.
3. Link it from the merge request. A maintainer other than the author reviews it
   together with the change.
4. Set the status to `accepted` when merged. To change a decision later, write a
   new record and mark the old one `superseded by NNNN`; never delete one.

## Index

| Record | Title | Status |
| --- | --- | --- |
| [0001](0001-public-and-internal-repositories.md) | Keep a public base and a private downstream | accepted |
| [0002](0002-optional-integrations-default-off.md) | Integrations that need private services default to off | accepted |
