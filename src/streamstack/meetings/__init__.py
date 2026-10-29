"""Calendar-aware setup: read the next meeting from Microsoft 365 or an ICS link, and get the rig ready for it.

* `model`    the `Meeting` record and the join-link finder shared by every source.
* `ics`      an ICS (iCalendar) reader with time zones and recurring meetings, no dependencies.
* `graph`    Microsoft 365 through Microsoft Graph, signed in with the device-code flow.
* `tokens`   where the sign-in token and private calendar links are kept: the desktop keyring, never a file.

The Console controller that polls the sources and prepares the rig is `streamstack.console.meetings`.
"""
