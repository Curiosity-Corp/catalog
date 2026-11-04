# Streaming outputs against a self-hosted ingest (#82)

CEO decision, 2026-11-03: the YouTube-shaped and Twitch-shaped outputs are proven against our own ingest server, with
no public accounts. The platform-specific remainder, a real YouTube channel with its stream key and a real Twitch
account, is **out of scope by that decision**; nothing here claims a platform accepted a stream.

What is proven: StreamStack's installed stream code, with the Presets catalog's settings for each platform, pushes
over the real wire protocols (enhanced RTMP over RTMPS with certificate verification on, and classic and enhanced
RTMP), and an ingest server receives exactly the codec, size, frame rate, keyframe interval and audio the preset asks for.

## The ingest

MediaMTX v1.21.2 (the release `tests/test_whip_mediamtx.py` pins, SHA-256 checked), on 127.0.0.1, with RTMP and an
RTMPS listener. The RTMPS certificate is signed by a CA generated for the run; the destination trusts it through its
`ca_file` option (`ffmpeg -tls_verify 1 -verifyhost localhost -ca_file ...`). Verification is never switched off: a
destination that does not trust the CA never reaches the ingest (a test proves it). The stream key is a throwaway
made up for the run (redacted to `hostrun-REDACTED` in the files here) and supplied the way the Console's Stream page does (stored 0600, never printed or committed).
The ingest side is measured by reading the stream back out of MediaMTX (RTSP, TCP) and probing it with ffprobe,
plus MediaMTX's own API counters (`ingest-stats.json`).

## Presets

| Preset | Platform shape | Codec / encoder (host run) | Transport |
|---|---|---|---|
| `youtube-av1-rtmps` | YouTube | AV1 (`av1_vaapi`), enhanced RTMP FourCC `av01` | RTMPS |
| `twitch-hevc-rtmp` | Twitch, Enhanced Broadcasting account | HEVC (`hevc_vaapi`), enhanced RTMP `hvc1` | RTMP |
| `twitch-h264-rtmp` | Twitch, ordinary account | H.264 (`h264_vaapi`), classic FLV | RTMP |

All three use the `constrained` tier of the catalog (1280x720 at 30 fps, CBR, 2 s keyframes, AAC 48 kHz stereo).

## 1. CI end-to-end test (merge request !202)

`tests/test_stream_ingest_platforms.py`, job `ingest-e2e` (source-security stage; a pinned, SHA-256 checked ffmpeg with
libsvtav1, libx264, libx265, libopus and rtmps; the CI run's captured evidence is the job's `ingest-evidence/`
artifact). It asserts on the ingest side with ffprobe: codec, resolution, frame rate, keyframe interval (decoded
keyframe flags, first join keyframe dropped, at least three gaps within 0.35 s of 2 s) and audio; plus the
untrusted-CA negative case.

## 2. Live host run on dm313red

Host: Intel Arc A380 (VA-API), StreamStack 0.0.26 installed. No call was live (`fuser -v /dev/video42 /dev/video43`,
`pactl list source-outputs`, checked first; the script checks again before each preset). 75 s per preset.

```
python3 -I docs/evidence/streaming/host_ingest_run.py --mediamtx <scratch>/mediamtx --work <scratch>/work \
    --out docs/evidence/streaming --seconds 75 --source synthetic
```

Measured at the ingest (`<preset>.ffprobe.json` has the full ffprobe output and the keyframe gaps):

| Preset | Codec | Size | fps | Keyframe gaps (35 measured) | Audio | Stream bit rate (plan, video) | Ingest bytes |
|---|---|---|---|---|---|---|---|
| `youtube-av1-rtmps` | av1 | 1280x720 | 30 | 1.999 to 2.0 s | aac, 2 ch, 48 kHz | 1648 kbps (1500) | 17,105,780 |
| `twitch-hevc-rtmp` | hevc | 1280x720 | 30 | 1.999 to 2.0 s | aac, 2 ch, 48 kHz | 2017 kbps (1900) | 20,963,415 |
| `twitch-h264-rtmp` | h264 | 1280x720 | 30 | 2.0 s | aac, 2 ch, 48 kHz | 2611 kbps (2500) | 27,108,441 |

The stream bit rate is the whole file (video plus 128 kbps audio plus container), so it sits slightly above the video plan.

### Picture source, and a product bug found on the way

The run used `--source synthetic`: the installed package's own stream code and hardware encoders in the runner's
process, on a generated 1280x720 picture. The intended source, the program feed read only through the running
Console service, could not be used: while the Console window is open its preview holds `/dev/video43`, and
v4l2loopback 0.15.3 refuses a second reader's `VIDIOC_S_FMT` with EBUSY, so the encoder cannot open the device
(strace shows the ioctl). That is a product bug, filed as #183 and fixed forward by #181 (!207: the Console preview no longer opens the program
device, it reads a JPEG the engine writes) and #184 (!208: recording and streaming read the engine's frame socket). It
is not in the installed 0.0.26; the live check of the program feed comes with 0.0.27 on dm313red. Nothing here wrote to `/dev/video42` or
`/dev/video43`, restarted a service, or stopped a process that was not started by the run.

## Files

- `host_ingest_run.py`: the runner (commands above).
- `summary.json`: the three summaries.
- `<preset>.ffprobe.json`, `<preset>.ingest-stats.json`: ffprobe output and MediaMTX API counters per preset.
- `mediamtx.log`: the ingest's log (connections, codecs, TLS).
