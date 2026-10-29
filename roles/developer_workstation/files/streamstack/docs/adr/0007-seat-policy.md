# ADR 0007: Two users on one desk: the seat user owns audio, the other user's PipeWire is kept off the hardware

Status: provisional (2026-10-27), pending the A/B replug trial in `tools/seat_replug_trial.py`. Issue #127, milestone
"Hardware-native".

## Context

dm313red has two accounts. `mrh` has the seat (session on seat0, the screen, the Wave XLR and the Canon). `seantech`
has no session on the seat but is lingering, so systemd keeps his user manager, and with it his own `pipewire`,
`wireplumber`, `pipewire-pulse` and a filter-chain, running for days. Both users are in `audio` and `video`, and
`/dev/snd/*` is `root:audio 0660`, so a second WirePlumber can open every card. We measured this on the live desk:
the second user's WirePlumber is up for 2 d 20 h and is a plausible contender for the Wave XLR's profile after a
replug (#125 fault B: the card returns with output profiles only and the microphone is gone).

What does not help: the `org.freedesktop.ReserveDevice1` handshake is per session bus, so it cannot arbitrate between
two users. Seat ACLs (logind) already give the seat user the device; the group grants are what let the other user
in as well.

## Decision

1. **Primary: a WirePlumber profile drop-in for the other user** (`~/.config/wireplumber/wireplumber.conf.d/
   90-streamstack-seat-guest.conf`) that disables the `hardware.audio` and `hardware.video-capture` profiles. His
   PipeWire still runs (his filter-chain and anything not on hardware keep working) but his session manager never
   opens a card or a camera. Reversible by deleting one file.
2. **Hardening: remove him from `audio` and `video`**, so the kernel refuses even if the drop-in is lost. This changes
   what he can do on the machine, so it is offered, not default.
3. **Last resort: stop his lingering manager's audio units** (or `loginctl disable-linger`).

StreamStack never changes another user's files itself. It diagnoses (`streamstack seat-policy status`, the
`hw-seat` doctor row, the Console health panel) and prints the exact script for the owner to run as root
(`streamstack seat-policy script --user seantech --option dropin|groups|stop`). It refuses the seat user and unknown
users. `STREAMSTACK_SEAT_GUARDED=user[,user]` records which users the owner guarded, so the doctor can report
"guarded" instead of "unverified" when it cannot read their home.

## Why provisional

Whether the second WirePlumber really causes the lost-input faults is not yet shown. The trial replugs the Wave XLR N
times in three arms (other WirePlumber running, stopped for the run, policy applied) and counts cycles ending
`duplex`, `no-input` or `no-card`. If the arms do not differ, the policy is still correct hygiene (two session
managers should not both own one card) but is not the cure, and this ADR will say so.

## Consequences

- Verdicts: `contends` (another user's audio process runs and can open the cards), `unverified` (cannot see his
  config), `lingering`, `guarded`, `idle`. Only `contends` and `lingering` make the CLI exit 1.
- The doctor is read-only; the trial is dry-run unless `--execute`, refuses during a call and never changes the default
  source.
