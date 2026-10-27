#!/usr/bin/env bash
# Record a short clip from a V4L2 device for tools/bench_ai.py (issue #22).
#
#   tools/record_clip.sh --confirm-no-call /dev/video42 canon.mkv                  # Canon, 1056x704 MJPEG
#   tools/record_clip.sh --confirm-no-call /dev/video0 camlink.mkv 1920x1080 30    # Cam Link
#
# USE ONLY WHEN NO CALL IS IN PROGRESS. It opens the camera, so nothing else (the Console, a browser, Teams, Zoom) may be
# using it: stop the call and the Console's video engine first. It refuses to start unless you pass --confirm-no-call and
# refuses a device another process has open. It records video only: no audio, no keystrokes, nothing else is touched.
#
# MJPEG sources are stream-copied (the pixels are exactly what the camera sent); raw sources (YUYV, NV12) are stored
# losslessly (FFV1). Stand in front of the camera as you would on a call: the benchmark measures real faces.
#
# Usage: record_clip.sh --confirm-no-call DEVICE OUTPUT [WIDTHxHEIGHT [FPS [SECONDS]]]
#   WIDTHxHEIGHT  default 1056x704 for the Canon (use 1920x1080 for the Cam Link)
#   FPS           default 30
#   SECONDS       default 20
set -euo pipefail

usage() {
    sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//' >&2
    exit "${1:-2}"
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage 0
fi
if [[ "${1:-}" != "--confirm-no-call" ]]; then
    echo "record_clip: refusing to open a camera without --confirm-no-call (only when no call is in progress)." >&2
    usage 2
fi
shift
if [[ $# -lt 2 || $# -gt 5 ]]; then
    usage 2
fi

device=$1
output=$2
size=${3:-1056x704}
fps=${4:-30}
seconds=${5:-20}

[[ "$device" == /dev/video* ]] || { echo "record_clip: $device is not a /dev/video* device" >&2; exit 2; }
[[ "$size" =~ ^[0-9]{2,5}x[0-9]{2,5}$ ]] || { echo "record_clip: bad size '$size' (WIDTHxHEIGHT)" >&2; exit 2; }
[[ "$fps" =~ ^[0-9]{1,3}$ && "$seconds" =~ ^[0-9]{1,3}$ ]] || { echo "record_clip: FPS and SECONDS must be whole numbers" >&2; exit 2; }
[[ -c "$device" ]] || { echo "record_clip: $device does not exist or is not a character device" >&2; exit 1; }
[[ ! -e "$output" ]] || { echo "record_clip: $output already exists; not overwriting" >&2; exit 1; }
command -v ffmpeg >/dev/null || { echo "record_clip: ffmpeg is not installed" >&2; exit 1; }

if command -v fuser >/dev/null && fuser -s "$device" 2>/dev/null; then
    echo "record_clip: $device is open in another process (a call, the Console or a browser?). Close it and retry." >&2
    exit 1
fi

# Prefer MJPEG (copied as is); fall back to whatever raw format the device offers.
input_format=mjpeg
codec=(-c:v copy)
if command -v v4l2-ctl >/dev/null; then
    formats=$(v4l2-ctl -d "$device" --list-formats 2>/dev/null || true)
    if ! grep -q "MJPG" <<<"$formats"; then
        input_format=""
        codec=(-c:v ffv1 -level 3 -pix_fmt yuv420p)
    fi
fi

args=(-hide_banner -loglevel warning -nostdin -f v4l2)
[[ -n "$input_format" ]] && args+=(-input_format "$input_format")
args+=(-video_size "$size" -framerate "$fps" -i "$device" -t "$seconds" -an "${codec[@]}" "$output")

echo "record_clip: recording ${seconds}s from $device at $size, ${fps} fps to $output" >&2
exec ffmpeg "${args[@]}"
