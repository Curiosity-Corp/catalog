#!/usr/bin/env python3
"""Push the YouTube-shaped and Twitch-shaped outputs of the INSTALLED StreamStack at a local MediaMTX ingest (#82).

Run on the streaming desk (dm313red) when no call is live. The program feed is read only through the running
StreamStack Console service (its own output path, hardware encoders); nothing here opens /dev/video42 or /dev/video43,
and no service is restarted. The ingest is MediaMTX from its pinned release, started from a scratch directory on
127.0.0.1 with an RTMPS certificate signed by a CA made for the run; StreamStack trusts that CA through the
destination's `ca_file` (verification stays on). The stream key is a throwaway made up for the run, supplied the way
the Console's Stream page does (stored 0600 by the service, never printed).

    python3 -I host_ingest_run.py --mediamtx /path/to/mediamtx --work /path/to/scratch --out docs/evidence/streaming \
        --seconds 75

Only the processes this script starts are stopped, and the Console's stream destinations are returned to what they
were (none).
"""

from __future__ import annotations

import argparse
import itertools
import json
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, "/usr/local/lib/streamstack")  # the installed package: the presets catalog and plan resolver

from streamstack.presets import catalog, plan

TOKEN_HEADER = "X-StreamStack-Token"  # noqa: S105 - a header name


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def call_is_live() -> str:
    """Why the desk is busy, or an empty string. StreamStack's own ffmpeg and parec do not count."""
    out = subprocess.run(["pactl", "list", "source-outputs"], capture_output=True, text=True, check=False).stdout
    for name in ("msedge", "teams", "zoom", "chrome", "brave"):
        if f'application.process.binary = "{name}' in out.lower() or f'application.name = "{name}' in out.lower():
            return f"{name} is capturing the microphone"
    return ""


class Console:
    def __init__(self) -> None:
        url = subprocess.run(
            ["streamstack", "console", "--url"], capture_output=True, text=True, check=True
        ).stdout.strip()
        self.base, _, query = url.partition("?token=")
        self.token = query
        if not self.token:
            raise SystemExit("the Console URL carries no token")

    def _call(self, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base.rstrip("/") + path, data=data, headers={TOKEN_HEADER: self.token})
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as reply:
                return json.loads(reply.read())
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"{path}: HTTP {exc.code}: {exc.read().decode()[:300]}") from None

    def act(self, action: str, params: dict[str, Any]) -> Any:
        return self._call("/api/action", {"action": action, **params})

    def stream(self) -> dict[str, Any]:
        return self._call("/api/state")["stream"]


class InProcess:
    """The Console's own stream controller (`console.stream.StreamController`) in this process, on a synthetic picture.

    Same actions, same settings translation, same encoders and sinks as the service behind the Console window; only the
    picture is a generated test pattern instead of the program feed, and the state lives in a scratch directory."""

    def __init__(self, work: Path, width: int, height: int, fps: int) -> None:
        from streamstack.console.stream import StreamController
        from streamstack.stream.ffcmd import AudioSource, Source
        from streamstack.stream.service import StreamService

        cfg = {"STREAMSTACK_RECORD_DIR": str(work / "rec")}
        service = StreamService(
            cfg,
            state_path=work / "stream.json",
            source_override=Source("lavfi", width=width, height=height, fps=fps),
            audio_override=AudioSource("lavfi"),
        )
        self.controller = StreamController(cfg, service=service)

    def act(self, action: str, params: dict[str, Any]) -> Any:
        return self.controller.act(action, params)

    def stream(self) -> dict[str, Any]:
        return self.controller.state()

    def close(self) -> None:
        self.controller.service.shutdown()


def make_tls(work: Path) -> dict[str, Path]:
    p = {n: work / n for n in ("ca.key", "ca.pem", "server.key", "server.csr", "server.pem", "san.cnf")}
    p["san.cnf"].write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n")

    def ssl(*argv: str) -> None:
        subprocess.run(["openssl", *argv], check=True, capture_output=True, text=True)

    ssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "2",
        "-subj",
        "/CN=StreamStack host-run CA",
        "-keyout",
        str(p["ca.key"]),
        "-out",
        str(p["ca.pem"]),
    )
    ssl(
        "req",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-subj",
        "/CN=localhost",
        "-keyout",
        str(p["server.key"]),
        "-out",
        str(p["server.csr"]),
    )
    ssl(
        "x509",
        "-req",
        "-in",
        str(p["server.csr"]),
        "-CA",
        str(p["ca.pem"]),
        "-CAkey",
        str(p["ca.key"]),
        "-CAcreateserial",
        "-days",
        "2",
        "-extfile",
        str(p["san.cnf"]),
        "-out",
        str(p["server.pem"]),
    )
    return p


def keyframe_times(path: Path) -> list[float]:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "frame=pts_time,key_frame",
            "-print_format",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [float(f["pts_time"]) for f in json.loads(out)["frames"] if f.get("key_frame") == 1 and "pts_time" in f]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mediamtx", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seconds", type=int, default=75)
    ap.add_argument(
        "--source",
        choices=("program", "synthetic"),
        default="program",
        help="program: the running Console service and the program feed; synthetic: its stream code, in this process, "
        "on a generated 1280x720 picture (when the program device cannot be opened, see README)",
    )
    args = ap.parse_args()
    work, out = Path(args.work), Path(args.out)
    work.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    busy = call_is_live()
    if busy:
        print(f"desk busy: {busy}")
        return 3

    console: Console | InProcess = Console() if args.source == "program" else InProcess(work, 1280, 720, 30)
    before = console.stream()
    if before["destinations"]:
        print("the Console already has stream destinations or a live stream; not touching it")
        return 4

    tls = make_tls(work)
    rtmp, rtmps, rtsp, api = free_port(), free_port(), free_port(), free_port()
    cfg = work / "mediamtx.yml"
    cfg.write_text(
        "\n".join(
            [
                "logLevel: info",
                "api: yes",
                f"apiAddress: 127.0.0.1:{api}",
                "rtmp: yes",
                "rtmpEncryption: optional",
                f"rtmpAddress: 127.0.0.1:{rtmp}",
                f"rtmpsAddress: 127.0.0.1:{rtmps}",
                f"rtmpServerKey: {tls['server.key']}",
                f"rtmpServerCert: {tls['server.pem']}",
                "rtsp: yes",
                f"rtspAddress: 127.0.0.1:{rtsp}",
                "rtspTransports: [tcp]",
                "hls: no",
                "webrtc: no",
                "srt: no",
                "paths:",
                "  all_others:",
                "",
            ]
        )
    )
    log = (out / "mediamtx.log").open("wb")
    ingest = subprocess.Popen(  # MediaMTX writes auto.key / auto.crt into its working directory: the scratch dir
        [args.mediamtx, str(cfg)], stdout=log, stderr=subprocess.STDOUT, start_new_session=True, cwd=work
    )
    key = "hostrun-" + secrets.token_hex(6)  # throwaway: names a path on a server that only exists for this run
    cat = catalog.load()
    runs = [
        ("youtube-av1-rtmps", "youtube", "av1", "av1_vaapi", "enhanced", f"rtmps://localhost:{rtmps}/live", "rtmps"),
        ("twitch-hevc-rtmp", "twitch", "hevc", "hevc_vaapi", "enhanced", f"rtmp://127.0.0.1:{rtmp}/live", "rtmp"),
        ("twitch-h264-rtmp", "twitch", "h264", "h264_vaapi", "classic", f"rtmp://127.0.0.1:{rtmp}/live", "rtmp"),
    ]
    summaries: list[dict[str, Any]] = []
    try:
        time.sleep(1.5)

        def get(route: str) -> Any:
            with urllib.request.urlopen(f"http://127.0.0.1:{api}/v3/{route}", timeout=5) as r:
                return json.loads(r.read())

        for label, platform_id, codec, encoder, mode, url, kind in runs:
            busy = call_is_live()
            if busy:
                print(f"desk busy before {label}: {busy}")
                break
            tier = next(t for t in cat.tiers if t["id"] == "constrained")
            resolved = plan.resolve(cat.platform(platform_id), tier)
            resolved["video"]["codecs"] = [c for c in resolved["video"]["codecs"] if c["codec"] == codec]
            dest = {
                "id": label,
                "kind": "rtmp",
                "name": label,
                "url": url,
                "key": key,
                "enabled": True,
                "options": {"mode": mode, **({"ca_file": str(tls["ca.pem"])} if url.startswith("rtmps://") else {})},
            }
            console.act(
                "stream.set",
                {
                    "platform": platform_id,
                    "tier": "constrained",
                    "plan": resolved,
                    "encoder": encoder,
                    "adapt": False,
                    "destinations": [dest],
                },
            )
            console.act("stream.start", {})
            path = f"live/{key}"
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                try:
                    if get(f"paths/get/{path}").get("ready"):
                        break
                except urllib.error.HTTPError:
                    pass
                time.sleep(0.5)
            else:
                state = console.stream()
                console.act("stream.stop", {})
                raise SystemExit(f"{label}: the ingest never got the stream; Console says {state['destinations']}")
            captured = work / f"{label}.mkv"
            reader = subprocess.Popen(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-nostdin",
                    "-y",
                    "-rtsp_transport",
                    "tcp",
                    "-i",
                    f"rtsp://127.0.0.1:{rtsp}/{path}",
                    "-map",
                    "0",
                    "-c",
                    "copy",
                    "-t",
                    str(args.seconds),
                    str(captured),
                ],
                stderr=subprocess.PIPE,
                text=True,
            )
            series: list[dict[str, Any]] = []
            console_series: list[dict[str, Any]] = []
            t0 = time.monotonic()
            while reader.poll() is None and time.monotonic() - t0 < args.seconds + 30:
                time.sleep(5)
                conn = get(f"{kind}conns/list").get("items", [])
                series.append({"t": round(time.monotonic() - t0, 1), "connections": conn})
                st = console.stream()
                d = st["destinations"][0] if st["destinations"] else {}
                console_series.append(
                    {
                        "t": round(time.monotonic() - t0, 1),
                        "state": d.get("state"),
                        "codec": d.get("codec"),
                        "bitrate_kbps": st.get("bitrate_kbps"),
                        "dropped_frames": st.get("dropped_frames"),
                        "encoder": st.get("encoder"),
                        "latency_ms": st.get("latency_ms"),
                        "cpu_percent": st.get("cpu_percent"),
                        "gpu_percent": st.get("gpu_percent"),
                    }
                )
            reader_err = reader.stderr.read() if reader.stderr else ""
            final_path = get(f"paths/get/{path}")
            console.act("stream.stop", {})
            console.act("stream.set", {"destinations": []})
            probe = json.loads(
                subprocess.run(
                    ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(captured)],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
            )
            keys = keyframe_times(captured)[1:]  # the first picture a late reader gets is the ingest's cached one
            gaps = [round(y - x, 3) for x, y in itertools.pairwise(keys)]
            streams = {s["codec_type"]: s for s in probe["streams"]}
            num, den = streams["video"]["avg_frame_rate"].split("/")
            v, a = streams["video"], streams["audio"]
            summary = {
                "preset": label,
                "platform": platform_id,
                "ingest_transport": kind,
                "encoder": encoder,
                "video_codec": v["codec_name"],
                "width": v["width"],
                "height": v["height"],
                "fps": round(int(num) / int(den), 2) if int(den) else 0.0,
                "stream_bit_rate_bps": int(probe["format"].get("bit_rate", 0)) or None,
                "keyframe_gaps_s": gaps,
                "keyframe_gap_median_s": sorted(gaps)[len(gaps) // 2] if gaps else None,
                "audio_codec": a["codec_name"],
                "audio_channels": a["channels"],
                "audio_sample_rate": int(a["sample_rate"]),
                "duration_s": float(probe["format"]["duration"]),
                "ingest_bytes_received": final_path.get("bytesReceived"),
                "reader_errors": reader_err.strip()[-300:],
                "plan": {
                    "width": resolved["video"]["width"],
                    "height": resolved["video"]["height"],
                    "fps": resolved["video"]["fps"],
                    "bitrate_kbps": resolved["video"]["codecs"][0]["bitrate_kbps"],
                    "keyframe_interval_s": resolved["video"]["keyframe_interval_s"],
                },
            }
            summaries.append(summary)
            (out / f"{label}.ffprobe.json").write_text(json.dumps({"summary": summary, "ffprobe": probe}, indent=2))
            (out / f"{label}.ingest-stats.json").write_text(
                json.dumps(
                    {"path": final_path, "connections_every_5s": series, "console_every_5s": console_series},
                    indent=2,
                    default=str,
                )
            )
            print(json.dumps(summary))
            time.sleep(3)
    finally:
        try:
            console.act("stream.stop", {})
            console.act("stream.set", {"destinations": []})
        except SystemExit as exc:
            print(f"cleanup: {exc}")
        ingest.terminate()
        try:
            ingest.wait(timeout=5)
        except subprocess.TimeoutExpired:
            ingest.kill()
        log.close()
        (work / "ca.key").unlink(missing_ok=True)
    (out / "summary.json").write_text(json.dumps(summaries, indent=2))
    return 0 if len(summaries) == len(runs) else 5


if __name__ == "__main__":
    raise SystemExit(main())
