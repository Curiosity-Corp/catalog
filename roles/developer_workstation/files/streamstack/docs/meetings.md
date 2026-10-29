# Calendar-aware setup (Meetings)

StreamStack reads your calendar and, a couple of minutes before the next online meeting, gets the rig ready: the studio
is started, the camera is turned on (privacy off) and the health checks run. The Meetings card is at the top of the right
column of the Auto page.

## Calendars

Two kinds of source, both read-only:

- **Microsoft 365 / Outlook** through Microsoft Graph. Press "Sign in to Microsoft 365": the card shows a short code and
  a link (device-code sign-in, the same flow as Teams on this machine). Open the link on any device, enter the code and
  approve. The permission asked for is `Calendars.Read`, plus `offline_access` and `User.Read`. The default client id is
  the one the workstation Teams tooling uses; `meetings.m365.connect` accepts `client_id` and `tenant` to use your own
  app registration.
- **Calendar links (ICS).** Paste a published or "secret address" calendar link (Outlook web, Google Calendar, Apple,
  Nextcloud). `https`, `http` and `webcal` links are accepted; addresses on a local or private network are refused. Up to
  8 links. Recurring meetings (RRULE with EXDATE and moved instances) are expanded in the calendar's own time zone, so a
  meeting stays at the same wall-clock time across a daylight-saving change.

Sources are read every 5 minutes; `meetings.sync` reads them now. A source that fails shows
its reason on its row and the others keep working.

## Secrets

Nothing secret is written to `~/.config/streamstack/meetings.json` (mode 600). The Microsoft refresh token and the full
calendar links (which can carry a private key) live in the desktop keyring (Secret Service) under the service name
`streamstack`; the settings file keeps only a name and the host of each link. Install the extra with
`pip install "streamstack[calendar]"` (it brings `keyring`). Without a working keyring the Console refuses to store a
secret and the card says why; it never falls back to a plain file.

## What happens before a meeting

When the next meeting is `lead_minutes` (0 to 30, default 2) away, the Console runs the steps you picked, once per
meeting (the list of prepared meetings is remembered, so a restart does not repeat it):

| Step | Action it runs |
|---|---|
| Start the studio | `studio.start` (skipped when it already runs) |
| Camera on (privacy off) | `video.privacy` with `on: false` |
| Unmute the microphone | `audio.mute` with `on: false` (off by default) |
| Run the health checks | `health.refresh` |

Rules that keep it from getting in the way:

- Only meetings that carry a call link (Teams, Zoom, Meet, Webex ...) count, unless "Only meetings with a call link" is
  switched off.
- If a call is already running (call awareness, `docs/call-awareness.md`), nothing is changed. The card says so.
- "Prepare now" runs the steps for the next meeting immediately, even during a call, because a person asked.
- Each step reports its own result on the card; one failing step does not stop the others, and the toast says how many
  did not work.

## Actions

| Action | Params |
|---|---|
| `meetings.enable` | `on` |
| `meetings.settings` | `lead_minutes`, `only_online`, `steps` (any of `studio`, `camera`, `mic`, `health`) |
| `meetings.sync` / `meetings.prepare` | `prepare` takes an optional `id` |
| `meetings.m365.connect` | optional `client_id`, `tenant` |
| `meetings.m365.cancel` / `meetings.m365.disconnect` | none |
| `meetings.ics.add` | `link`, `name` |
| `meetings.ics.remove` | `id` |

The snapshot section `meetings` carries `enabled`, the settings, `sources`, `next`, `upcoming` (up to 6, each with
`prep` waiting, due or done), `prep` (the last preparation and each step's result), `in_call` and `events`.

The demo rig (`streamstack console --demo`) adds `meetings.demo.*` actions (a scripted Microsoft sign-in, calendar feeds
and a clock tick) so the whole flow can be driven by the e2e tests without a network.
