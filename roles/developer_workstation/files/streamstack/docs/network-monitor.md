# Network quality monitor

StreamStack watches the connection a call depends on, and tells you early when it is going bad and why. It sends a
few tiny probes, never any media, and never contacts anything that is not on its target list.

## What it measures

Every 5 seconds it sends one probe to each target and to the default gateway:

- **Targets.** By default Microsoft Teams (`worldaz.tr.teams.microsoft.com:3478`, STUN), Google Meet
  (`stun.l.google.com:19302`, STUN) and Zoom (`zoom.us:443`, a TCP connect). A STUN probe is one UDP binding request,
  which is the same path real call media takes. You can edit the list on the System page (up to 8 targets).
- **Gateway.** One ping to the default route, to tell a bad Wi-Fi or LAN from a bad internet connection.
- **Wi-Fi.** Signal and retry rate from `/proc/net/wireless` and `iw`, when present.

The numbers are the median over the live targets across the last 20 probes: round trip time, jitter and loss.

| Rating | When |
| --- | --- |
| `good` | none of the below |
| `degraded` | rtt 150 ms or more, jitter 30 ms or more, or loss 2 % or more |
| `poor` | rtt 400 ms or more, jitter 250 ms or more, or loss 10 % or more |
| `offline` | every probe fails |

## The cause

A bad rating names the likely cause from where the probes fail: weak Wi-Fi, the local network (the gateway is bad),
the internet connection (the gateway is fine, every target is bad), a single service (one target only) or the
provider (a route that is up but slow).

## Warnings

One warning per episode: a toast and a top-bar chip (`NETWORK DEGRADED`, `POOR` or `OFFLINE`, linking to the System
page). A warning needs 2 bad evaluations in a row, clears after 3 good ones, and the same warning waits at least 120 s
before it repeats.

## Bandwidth test

The System page has a **Test bandwidth** button. It downloads about 10 MB and uploads about 1.5 MB against Cloudflare's
speed endpoints and rates the uplink against the 3 Mbit/s a 1080p30 call needs. It is limited to one run a minute.
During a call it asks first (it uses your uplink and can hurt the call); the API needs `confirm: true` then, and
answers 409 without it.

## Pre-flight

`rig.network.facts()` returns rtt, jitter, loss and the last bandwidth result for the Pre-flight network check.

## Privacy and limits

Only the target list is contacted, and the bandwidth test only when you press the button. Settings are saved in
`console-network.json`. Turn the monitor off with `network.set {"enabled": false}`. Probes use the host's network, so a
VPN changes what is measured.
