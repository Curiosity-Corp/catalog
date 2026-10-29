# Presets

A large, data-driven preset library: looks, platforms, bandwidth tiers and one-click profiles. The data lives in
`src/streamstack/data/presets/*.json`, is validated at load (`streamstack.presets.schema`), and is served to the
Console page at `#/presets` (Alt+8). Change a preset by editing JSON, then run the build check below.

## Catalog

| Kind | Count | File |
| --- | --- | --- |
| Looks | 37 (categories: essentials, film stocks, cinema, creator) | `looks.json` |
| 3D LUTs | 12 generated `.cube` files (17 points) | `luts/*.cube` |
| Look thumbnails | 37 PNG, 128x80, rendered from one synthetic portrait | `data/console/presets/looks/` |
| Platforms | 10 | `platforms.json` |
| Bandwidth tiers | 5 (Ultra, High, Balanced, Constrained, Cellular-safe), AV1/HEVC/H.264 ladders | `tiers.json` |
| Built-in profiles | 8 | `profiles.json` |

A look is a partial set of the engine parameters (brightness, contrast, gamma, saturation, warmth, sharpen, LUT
amount) plus an optional `lut` name. The native engine will load the `.cube`; until then the LUT is metadata
and the thumbnail shows the intended grade.

### LUT generation and licence

The `.cube` files are computed procedurally by `streamstack.presets.luts` (colour maths written for this project, no
third-party LUT data). Each file carries an Apache-2.0 / CC0 header. Regenerate the LUTs and thumbnails with
`python -m streamstack.presets.build`; `python -m streamstack.presets.build --check` (also a unit test) fails when the
shipped files drift from the generator.

## Platforms

A platform has resolution and fps caps, codec order (AV1, HEVC, H.264), rate control, keyframe interval, audio
(codec, bitrate, loudness target), transport order (Enhanced RTMP, RTMP, SRT, WHIP), a rationale, a source URL and a
`verified` grade. Checked 2026-10.

An RTMP platform may add two optional fields: `rtmps` (its ingest takes TLS, so the plan says to use `rtmps://`;
true for YouTube, Twitch, Kick, LinkedIn and Facebook) and `rtmp_audio` (the audio codecs it takes, most preferred
first; AAC only when absent, and `opus` is only valid with the `enhanced-rtmp` transport). The plan carries them as
`rtmp: {tls, audio}`, and the Presets page shows them under Transport.

| Platform | Grade | Source | Uncertain |
| --- | --- | --- | --- |
| YouTube Live | official | support.google.com/youtube/answer/2853702 | AV1/HEVC ingest ranges |
| Twitch | community | help.twitch.tv broadcasting guidelines | Enhanced Broadcasting HEVC/AV1 eligibility |
| Kick | community | Restream Kick guide | bitrate ceiling |
| TikTok Live | uncertain | Restream TikTok guide | 30 vs 60 fps |
| LinkedIn Live | official | linkedin.com/help/learning/answer/128955 | none |
| Facebook Live | community | Facebook Live API encoder guide | codec options |
| X Live | official | help.x.com live studio | none |
| Teams, Zoom, Meet camera | community | Microsoft Teams video resolution | per-app caps |
| Local archive | official | FFmpeg AV1 wiki | none |
| Self-hosted relay | uncertain | RFC 9725 (WHIP) | server limits |

The exact uncertain items are listed per platform in `platforms.json` and shown on the page.

## Tiers and the stream plan

`streamstack.presets.plan.resolve(platform, tier)` combines them: the tier supplies per-codec ladders keyed by short
side and fps, the platform supplies the caps and house rules. The result is one plan (video size, fps, codecs with
bitrates, keyframe interval and GOP, audio, transport, uplink needed).

## The `stream.set` contract (pending)

Choosing a platform and tier, or applying a profile, sends `stream.set` through the rig with
`{"platform": id, "tier": id, "plan": {...resolve output...}}`. The stream backend is not part of this rig yet, so the
rig answers HTTP 501; the controller saves the choice and reports the part as `pending`. When the backend lands it
implements `stream.set` and the same calls report `applied` with no UI change.

With the backend, the platform and tier ids in that call also give the stream its bandwidth ladder: step 0 is this plan
and the later steps are the lower rungs of the same tier and then of the lower tiers
(`presets.plan.ladder_tier`). When the uplink cannot carry the plan the stream steps down along it by itself and back up
when the link is steady; see "Bandwidth adaptation" in [stream](stream.md).

## Profiles

A profile bundles look, platform and tier (together or neither), lighting preset, voice preset and framing mode.
Applying runs `video.looks.apply`, `stream.set`, `lighting.preset`, `audio.preset`, `framing.mode` in order; a failed
step does not stop the others, and the page shows each part as Applied, Pending or Not applied. If every step fails
the action fails with 409.

User profiles live in `~/.config/streamstack/profiles.json` (atomic write: temp file, fsync, rename, mode 600; a
corrupt file is kept as `.corrupt`). Export writes `streamstack.profile/1` (one) or `streamstack.profiles/1` (many);
import validates the schema, rejects built-in names and duplicate names, and saves all or nothing.

Actions: `presets.platform {id}`, `presets.tier {id}`, `presets.clear`, `presets.profile.apply {id|name}`,
`presets.profile.save {profile}`, `presets.profile.delete {name}`, `presets.profile.import {document}`.
Catalog: `GET /api/presets/catalog`. State: the `presets` block of the snapshot.
