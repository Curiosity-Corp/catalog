"""The YouTube-shaped and Twitch-shaped outputs, pushed at a real RTMP(S) ingest and read back out of it (#82).

The ingest is MediaMTX (pinned, SHA-256 checked, the same release `test_whip_mediamtx.py` uses), on 127.0.0.1, with
an RTMPS listener whose certificate is signed by a CA this test generates. StreamStack trusts that CA through the
destination's `ca_file` option; certificate verification is never switched off (a third test proves a destination
that does not trust the CA never reaches the ingest). The settings come from the shipped Presets catalog
(`presets.plan.resolve` -> `stream.planmap.plan_to_settings`), the same path the Console's Stream page takes:

* YouTube: AV1, enhanced RTMP (FourCC `av01`), RTMPS, CBR, 2 s keyframes, AAC.
* Twitch: H.264 over classic RTMP (an account that is not enrolled in Enhanced Broadcasting) and HEVC over enhanced
  RTMP (one that is), CBR, 2 s keyframes, AAC.

What arrives is measured with ffprobe on a copy taken from the ingest: codec, size, frame rate, keyframe spacing,
audio codec and bitrate, plus the ingest's own connection counters. Encoding is on the CPU (libsvtav1, libx265,
libx264); hardware encode is proven on the host (docs/evidence/streaming/).

Set `STREAMSTACK_REQUIRE_INGEST_E2E=1` (the CI job does) to turn every skip below into a failure, so the gate cannot
pass by not running. Set `STREAMSTACK_INGEST_EVIDENCE_DIR` to keep each preset's ffprobe JSON.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from http_support import HTTP_TIMEOUT

from streamstack.presets import catalog, plan
from streamstack.stream import planmap
from streamstack.stream.ffcmd import AudioSource, Source
from streamstack.stream.service import StreamService

VERSION = "v1.21.2"
URL = f"https://github.com/bluenviron/mediamtx/releases/download/{VERSION}/mediamtx_{VERSION}_linux_amd64.tar.gz"
SHA256 = "121be6e00397e473a95d83c75f4fcd3819a6af3b0e1a50889cba43ab4fab6a27"

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
OPENSSL = shutil.which("openssl")
REQUIRED = os.environ.get("STREAMSTACK_REQUIRE_INGEST_E2E") == "1"
EVIDENCE = os.environ.get("STREAMSTACK_INGEST_EVIDENCE_DIR")
STREAM_KEY = "ingest-test-key"  # not a secret: it names a path on a throwaway server
CAPTURE_SECONDS = 11


MIN_CORES = 8  # the least probe.software_ok accepts for software AV1


def _ffmpeg_text(*args: str) -> str:
    if not FFMPEG:
        return ""
    return subprocess.run([FFMPEG, "-hide_banner", *args], capture_output=True, text=True, check=False).stdout


ENCODERS = _ffmpeg_text("-encoders")
PROTOCOLS = _ffmpeg_text("-protocols")


def _unmet(*needs: tuple[bool, str]) -> str | None:
    for ok, why in needs:
        if not ok:
            return why
    return None


UNMET = _unmet(
    (bool(FFMPEG and FFPROBE), "ffmpeg and ffprobe are needed"),
    (bool(OPENSSL), "the openssl command is needed to make the test CA"),
    (
        all(e in ENCODERS for e in ("libsvtav1", "libx265", "libx264", "libopus")),
        "ffmpeg needs libsvtav1, libx265, libx264 and libopus",
    ),
    ("rtmps" in PROTOCOLS, "ffmpeg has no rtmps protocol"),
    (
        (os.cpu_count() or 1) >= MIN_CORES,
        f"software AV1 needs {MIN_CORES} cores to hold real time (probe.software_ok) and this machine has "
        f"{os.cpu_count()}: StreamStack would send H.264, or the capture would starve (!211, jobs 1935676, 1936044)",
    ),
)


def pytest_skip_or_fail(reason: str) -> None:
    if REQUIRED:
        pytest.fail(f"STREAMSTACK_REQUIRE_INGEST_E2E=1 but: {reason}")
    pytest.skip(reason)


@pytest.fixture(autouse=True)
def _prerequisites() -> None:
    if UNMET:
        pytest_skip_or_fail(UNMET)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(predicate: Callable[[], bool], timeout: float, step: float = 0.2) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def fetch_mediamtx() -> Path:
    """The verified binary, downloaded once per machine (shared with test_whip_mediamtx.py)."""
    root = Path(tempfile.gettempdir()) / f"streamstack-mediamtx-{SHA256[:12]}"
    binary = root / "mediamtx"
    if binary.is_file():
        return binary
    root.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(URL, timeout=60) as reply:
            data = reply.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        pytest_skip_or_fail(f"cannot download MediaMTX {VERSION} ({exc})")
    digest = hashlib.sha256(data).hexdigest()
    if digest != SHA256:
        pytest.fail(f"MediaMTX {VERSION} download has SHA-256 {digest}, expected {SHA256}")
    archive = root / "release.tar.gz"
    archive.write_bytes(data)
    with tarfile.open(archive) as tar:
        member = tar.extractfile("mediamtx")  # one named member, written to a fixed path: nothing else is unpacked
        if member is None:
            pytest.fail(f"MediaMTX {VERSION} archive has no mediamtx binary")
        binary.write_bytes(member.read())
    binary.chmod(0o755)
    return binary


def make_tls(tmp: Path) -> dict[str, Path]:
    """A throwaway CA and a server certificate for localhost / 127.0.0.1 signed by it."""
    assert OPENSSL
    paths = {n: tmp / n for n in ("ca.key", "ca.pem", "server.key", "server.csr", "server.pem", "san.cnf")}
    paths["san.cnf"].write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n")

    def run(*argv: str) -> None:
        subprocess.run([OPENSSL, *argv], check=True, capture_output=True, text=True)

    run(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "2",
        "-subj",
        "/CN=StreamStack test CA",
        "-keyout",
        str(paths["ca.key"]),
        "-out",
        str(paths["ca.pem"]),
    )
    run(
        "req",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-subj",
        "/CN=localhost",
        "-keyout",
        str(paths["server.key"]),
        "-out",
        str(paths["server.csr"]),
    )
    run(
        "x509",
        "-req",
        "-in",
        str(paths["server.csr"]),
        "-CA",
        str(paths["ca.pem"]),
        "-CAkey",
        str(paths["ca.key"]),
        "-CAcreateserial",
        "-days",
        "2",
        "-extfile",
        str(paths["san.cnf"]),
        "-out",
        str(paths["server.pem"]),
    )
    return paths


class Ingest:
    """MediaMTX with RTMP, RTMPS (the test CA's certificate), RTSP for reading back, and the control API."""

    def __init__(self, binary: Path, tmp: Path) -> None:
        tls = make_tls(tmp)
        self.ca = tls["ca.pem"]
        self.rtmp, self.rtmps, self.rtsp, self.api = free_port(), free_port(), free_port(), free_port()
        config = tmp / "mediamtx.yml"
        config.write_text(
            "\n".join(
                [
                    "logLevel: warn",
                    "api: yes",
                    f"apiAddress: 127.0.0.1:{self.api}",
                    "rtmp: yes",
                    "rtmpEncryption: optional",
                    f"rtmpAddress: 127.0.0.1:{self.rtmp}",
                    f"rtmpsAddress: 127.0.0.1:{self.rtmps}",
                    f"rtmpServerKey: {tls['server.key']}",
                    f"rtmpServerCert: {tls['server.pem']}",
                    "rtsp: yes",
                    f"rtspAddress: 127.0.0.1:{self.rtsp}",
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
        self.log = (tmp / "mediamtx.log").open("wb")
        self.proc = subprocess.Popen(
            [str(binary), str(config)],
            stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=tmp,  # MediaMTX writes auto.key / auto.crt into its working directory, never into the repository
        )
        for port in (self.api, self.rtmps):
            if not wait_for(lambda p=port: self._open(p), 10):
                self.stop()
                pytest.fail(f"MediaMTX did not listen on {port}: {Path(self.log.name).read_text()[-400:]}")

    @staticmethod
    def _open(port: int) -> bool:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return True
        except OSError:
            return False

    def get(self, route: str) -> dict[str, Any]:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.api}/v3/{route}", timeout=HTTP_TIMEOUT) as reply:
            return json.loads(reply.read())

    def path_ready(self, name: str) -> bool:
        try:
            return bool(self.get(f"paths/get/{name}").get("ready"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # no publisher has created the path yet
                return False
            raise

    def connections(self, kind: str) -> list[dict[str, Any]]:
        """Publisher connections of `rtmp` or `rtmps`, as MediaMTX counts them."""
        return list(self.get(f"{kind}conns/list").get("items", []))

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.log.close()


@pytest.fixture
def ingest(tmp_path: Path) -> Iterator[Ingest]:
    server = Ingest(fetch_mediamtx(), tmp_path)
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def make_service(tmp_path: Path) -> Iterator[Callable[[int, int, int], StreamService]]:
    made: list[StreamService] = []

    def make(width: int, height: int, fps: int) -> StreamService:
        svc = StreamService(
            {"STREAMSTACK_RECORD_DIR": str(tmp_path / "rec")},
            source_override=Source("lavfi", width=width, height=height, fps=fps),
            audio_override=AudioSource("lavfi"),
        )
        made.append(svc)
        return svc

    yield make
    for svc in made:
        svc.shutdown()


def preset_settings(platform_id: str, tier_id: str, codec: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The stream settings the Console's Stream page would send for this platform and tier, pinned to one codec.

    A plan lists the codecs in preference order; pinning keeps the ones asked for (an account that is not enrolled in
    Enhanced Broadcasting gets H.264, one that is gets HEVC) and leaves everything else as the platform's rules say.
    """
    cat = catalog.load()
    tier = next(t for t in cat.tiers if t["id"] == tier_id)
    resolved = plan.resolve(cat.platform(platform_id), tier)
    resolved["video"]["codecs"] = [c for c in resolved["video"]["codecs"] if c["codec"] == codec]
    assert resolved["video"]["codecs"], f"{platform_id} has no {codec} in its plan"
    return planmap.plan_to_settings(resolved), resolved


def ffprobe_json(target: str, *extra: str) -> dict[str, Any]:
    assert FFPROBE
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", *extra, target],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out)


def keyframe_times(path: Path) -> list[float]:
    """Presentation times of the decoded key frames (the frame header says so; no container flag is trusted)."""
    assert FFPROBE
    out = subprocess.run(
        [
            FFPROBE,
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


def capture_from_ingest(ingest: Ingest, tmp: Path, name: str) -> Path:
    """Copy CAPTURE_SECONDS of what the ingest holds for `name`, without re-encoding, into a Matroska file.

    The reader joins as soon as the path is ready, so the first keyframe it sees is the stream's own first one and
    every keyframe gap that follows is the sender's, not an artefact of joining mid-way."""
    assert FFMPEG
    out = tmp / f"{name.replace('/', '_')}.mkv"
    subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-rtsp_transport",
            "tcp",
            "-i",
            f"rtsp://127.0.0.1:{ingest.rtsp}/{name}",
            "-map",
            "0",
            "-c",
            "copy",
            "-t",
            str(CAPTURE_SECONDS),
            str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=CAPTURE_SECONDS + 30,
    )
    return out


def encoder_stderr(svc: StreamService, encoder: str, codec: str) -> str:
    """What the video encoder says when it is started by hand with the service's own command line (for a failure)."""
    from streamstack.stream.ffcmd import EncoderChoice, video_generation_argv

    argv = video_generation_argv(
        svc.host().ffmpeg,
        svc._source or svc.source(),
        EncoderChoice(encoder, codec, "software"),
        svc._effective,
        time.time(),
    )
    try:
        done = subprocess.run(argv, capture_output=True, timeout=8)
        code, err = done.returncode, done.stderr
    except subprocess.TimeoutExpired as exc:
        code, err = "still running after 8 s (healthy)", exc.stderr or b""
    lines = [x for x in err.decode(errors="replace").splitlines() if "=" not in x.split(" ")[0]]
    return f"{' '.join(argv)}\nexit: {code}\n" + "\n".join(lines[-40:])


def run_preset(
    make_service: Callable[[int, int, int], StreamService],
    ingest: Ingest,
    tmp: Path,
    *,
    label: str,
    platform_id: str,
    codec: str,
    encoder: str,
    destination: dict[str, Any],
    connection_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    settings, resolved = preset_settings(platform_id, "constrained", codec)
    video = resolved["video"]
    monkeypatch.setenv("STREAMSTACK_TEST_KEY", STREAM_KEY)
    svc = make_service(video["width"], video["height"], video["fps"])
    svc.set({**settings, "encoder": encoder, "destinations": [{**destination, "key_ref": "env:STREAMSTACK_TEST_KEY"}]})
    svc.start_stream()
    try:
        live = wait_for(lambda: svc.state()["destinations"][0]["state"] == "live", 60)
        assert live, "\n".join(f"{e['level']}: {e['text']}" for e in svc.state()["events"]) + (
            "\n--- the encoder started by hand ---\n" + encoder_stderr(svc, encoder, codec)
        )
        assert STREAM_KEY not in json.dumps(svc.state())
        path = f"{destination['url'].rsplit('/', 1)[1]}/{STREAM_KEY}"
        assert wait_for(lambda: bool(ingest.connections(connection_kind)), 20), "the ingest saw no publisher"
        assert wait_for(lambda: ingest.path_ready(path), 20), "the path never became ready"
        conn = ingest.connections(connection_kind)[0]
        captured = capture_from_ingest(ingest, tmp, path)
        conn = ingest.connections(connection_kind)[0] | {"at_capture_end": True}
        dest_state = svc.state()["destinations"][0]
        stats = {"path": ingest.get(f"paths/get/{path}"), "connection": conn}
    finally:
        svc.stop_stream()
    probe = ffprobe_json(str(captured))
    keys = keyframe_times(captured)
    # The first packet a reader is handed is the ingest's cached picture, stamped before the live ones (for AV1 it is a
    # keyframe with a 1.8 s hole after it), so the sender's cadence is read from the keyframes after that one.
    live_keys = keys[1:]
    gaps = [round(b - a, 3) for a, b in itertools.pairwise(live_keys)]
    streams = {s["codec_type"]: s for s in probe["streams"]}
    num, den = streams["video"]["avg_frame_rate"].split("/")
    summary = {
        "preset": label,
        "platform": platform_id,
        "transport": connection_kind,
        "video_codec": streams["video"]["codec_name"],
        "width": streams["video"]["width"],
        "height": streams["video"]["height"],
        "fps": round(int(num) / int(den), 2) if int(den) else 0.0,
        "keyframes": len(keys),
        "keyframe_times_s": [round(k, 3) for k in keys],
        "keyframe_gaps_s": gaps,
        "audio_codec": streams["audio"]["codec_name"],
        "audio_channels": streams["audio"]["channels"],
        "audio_sample_rate": int(streams["audio"]["sample_rate"]),
        "destination": {k: dest_state.get(k) for k in ("kind", "state", "codec", "codec_wanted")},
        "plan": {
            "width": video["width"],
            "height": video["height"],
            "fps": video["fps"],
            "bitrate_kbps": video["codecs"][0].get("bitrate_kbps"),
            "keyframe_interval_s": video["keyframe_interval_s"],
            "rate_control": video["rate_control"],
            "audio_kbps": resolved["audio"]["bitrate_kbps"],
        },
    }
    if EVIDENCE:
        target = Path(EVIDENCE)
        target.mkdir(parents=True, exist_ok=True)
        if os.environ.get("STREAMSTACK_INGEST_KEEP_CAPTURE") == "1":
            shutil.copy(captured, target / f"{label}.capture.mkv")
        (target / f"{label}.ffprobe.json").write_text(json.dumps({"summary": summary, "ffprobe": probe}, indent=2))
        (target / f"{label}.ingest-stats.json").write_text(json.dumps(stats, indent=2, default=str))
    return summary


def assert_platform_rules(summary: dict[str, Any]) -> None:
    plan_ = summary["plan"]
    assert (summary["width"], summary["height"]) == (plan_["width"], plan_["height"])
    assert abs(summary["fps"] - plan_["fps"]) <= 1.5
    gaps = summary["keyframe_gaps_s"]
    assert len(gaps) >= 3, summary
    # the platform's rule: a keyframe every 2 s (YouTube's hard limit is 4 s), neither more often nor less
    assert all(abs(g - plan_["keyframe_interval_s"]) <= 0.35 for g in gaps), gaps
    assert summary["audio_codec"] == "aac" and summary["audio_channels"] == 2 and summary["audio_sample_rate"] == 48000
    assert summary["plan"]["rate_control"] == "cbr"


def test_youtube_av1_over_rtmps_with_a_trusted_private_ca(make_service, ingest: Ingest, tmp_path, monkeypatch) -> None:
    summary = run_preset(
        make_service,
        ingest,
        tmp_path,
        label="youtube-av1-rtmps",
        platform_id="youtube",
        codec="av1",
        encoder="libsvtav1",
        connection_kind="rtmps",
        monkeypatch=monkeypatch,
        destination={
            "id": "youtube",
            "kind": "rtmp",
            "url": f"rtmps://localhost:{ingest.rtmps}/live",
            "options": {"mode": "enhanced", "ca_file": str(ingest.ca)},
        },
    )
    assert summary["video_codec"] == "av1"  # it arrived as enhanced RTMP (FourCC av01): classic FLV cannot carry AV1
    assert summary["destination"]["codec"] == "av1"
    assert_platform_rules(summary)


def test_twitch_h264_over_rtmp(make_service, ingest: Ingest, tmp_path, monkeypatch) -> None:
    summary = run_preset(
        make_service,
        ingest,
        tmp_path,
        label="twitch-h264-rtmp",
        platform_id="twitch",
        codec="h264",
        encoder="libx264",
        connection_kind="rtmp",
        monkeypatch=monkeypatch,
        destination={
            "id": "twitch",
            "kind": "rtmp",
            "url": f"rtmp://127.0.0.1:{ingest.rtmp}/live",
            "options": {"mode": "classic"},
        },
    )
    assert summary["video_codec"] == "h264"
    assert_platform_rules(summary)


def test_twitch_hevc_over_enhanced_rtmp(make_service, ingest: Ingest, tmp_path, monkeypatch) -> None:
    summary = run_preset(
        make_service,
        ingest,
        tmp_path,
        label="twitch-hevc-rtmp",
        platform_id="twitch",
        codec="hevc",
        encoder="libx265",
        connection_kind="rtmp",
        monkeypatch=monkeypatch,
        destination={
            "id": "twitch",
            "kind": "rtmp",
            "url": f"rtmp://127.0.0.1:{ingest.rtmp}/live",
            "options": {"mode": "enhanced"},
        },
    )
    assert summary["video_codec"] == "hevc"
    assert_platform_rules(summary)


def test_rtmps_that_does_not_trust_the_ca_never_reaches_the_ingest(make_service, ingest: Ingest, monkeypatch) -> None:
    """Verification stays on: with no `ca_file` the private CA is unknown, so no key and no picture are sent."""
    settings, resolved = preset_settings("youtube", "constrained", "av1")
    video = resolved["video"]
    monkeypatch.setenv("STREAMSTACK_TEST_KEY", STREAM_KEY)
    svc = make_service(video["width"], video["height"], video["fps"])
    svc.set(
        {
            **settings,
            "encoder": "libsvtav1",
            "destinations": [
                {
                    "id": "untrusted",
                    "kind": "rtmp",
                    "url": f"rtmps://localhost:{ingest.rtmps}/live",
                    "key_ref": "env:STREAMSTACK_TEST_KEY",
                    "options": {"mode": "enhanced"},
                }
            ],
        }
    )
    svc.start_stream()
    try:
        went_live = wait_for(lambda: svc.state()["destinations"][0]["state"] == "live", 8)
        assert not went_live
        assert ingest.connections("rtmps") == []
        assert ingest.get("paths/list")["itemCount"] == 0
    finally:
        svc.stop_stream()
