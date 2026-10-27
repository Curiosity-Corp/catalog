from __future__ import annotations

import os

import pytest

from streamstack.stream import probe
from streamstack.stream.probe import EncoderProbe, HostProbe

ENCODERS = """Encoders:
 V....D av1_vaapi            AV1 (VAAPI)
 V....D hevc_vaapi           H.265 (VAAPI)
 V....D h264_vaapi           H.264 (VAAPI)
 V....D hevc_nvenc           NVIDIA NVENC hevc
 V....D h264_qsv             H.264 (Intel Quick Sync)
 V....D libx264              libx264 H.264
 V....D libsvtav1            SVT-AV1
"""
PROTOCOLS = "Supported file protocols:\nInput:\n  file\nOutput:\n  file\n  rtmp\n  srt\n  tls\n"
MUXERS = " E mp4             MP4\n E flv             FLV\n E matroska        Matroska\n"
RTMP_HELP = "rtmp AVOptions:\n  -rtmp_enhanced_codecs <string> E.. enhanced\n"


def fake_runner(failures: dict[str, str] | None = None, calls: list[list[str]] | None = None):
    failures = failures or {}

    def run(argv: list[str], timeout: float) -> tuple[int, str]:
        if calls is not None:
            calls.append(argv)
        if "-version" in argv:
            return 0, "ffmpeg version 8.0.1 Copyright"
        if "-encoders" in argv:
            return 0, ENCODERS
        if "-protocols" in argv:
            return 0, PROTOCOLS
        if "-muxers" in argv:
            return 0, MUXERS
        if "protocol=rtmp" in argv:
            return 0, RTMP_HELP
        encoder = argv[argv.index("-c:v") + 1]
        if encoder in failures:
            return 1, failures[encoder]
        return 0, ""

    return run


NO_AIORTC = "aiortc is not installed (install the whip extra: pip install 'streamstack[whip]')"


def host(failures: dict[str, str] | None = None, nodes: list[str] | None = None, whip: str = NO_AIORTC) -> HostProbe:
    return probe.probe_host(
        "ffmpeg",
        fake_runner(failures),
        nodes=["/dev/null"] if nodes is None else nodes,
        cores=16,
        whip_check=lambda: whip,
    )


def test_working_encoders_and_missing_ones_with_reasons() -> None:
    h = host({"hevc_nvenc": "[hevc_nvenc @ 0x5] Cannot load libcuda.so.1"})
    by_name = {e.name: e for e in h.encoders}
    assert by_name["h264_vaapi"].ok and by_name["libx264"].ok
    assert not by_name["hevc_nvenc"].ok and "NVIDIA" in by_name["hevc_nvenc"].reason
    assert "does not include" in by_name["av1_nvenc"].reason
    assert h.available_codecs() == {"av1", "hevc", "h264"}
    assert h.version == "8.0.1"


def test_codec_disappears_when_no_encoder_works() -> None:
    h = host({"av1_vaapi": "No usable encoding profile found", "libsvtav1": "boom"})
    assert "av1" not in h.available_codecs()
    reason = next(e.reason for e in h.encoders if e.name == "av1_vaapi")
    assert "no hardware encoder" in reason


def test_hardware_beats_software_and_software_is_bounded_by_cores() -> None:
    h = host()
    best = h.best("h264")
    assert best is not None and best.backend == "vaapi"
    only_soft = host({"av1_vaapi": "x", "h264_vaapi": "x", "hevc_vaapi": "x", "h264_qsv": "x"})
    assert only_soft.best("av1") is not None
    assert only_soft.best("av1", (3840, 2160), 60) is None
    only_soft.cores = 4
    assert only_soft.best("av1") is None


def test_each_render_node_is_probed_separately_and_pin_prefers_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "access", lambda path, mode: True)
    h = host(nodes=["/dev/dri/renderD128", "/dev/dri/renderD129"])
    devices = [e.device for e in h.encoders if e.name == "av1_vaapi"]
    assert devices == ["/dev/dri/renderD128", "/dev/dri/renderD129"]
    pinned = h.best("av1", pin="/dev/dri/renderD129")
    assert pinned is not None and pinned.device == "/dev/dri/renderD129"


def test_unreadable_render_node_has_a_permission_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "access", lambda path, mode: False)
    h = host(nodes=["/dev/dri/renderD128"])
    vaapi = next(e for e in h.encoders if e.name == "av1_vaapi")
    assert not vaapi.ok and "render group" in vaapi.reason


def test_no_render_nodes_gives_a_reason() -> None:
    h = host(nodes=[])
    assert "no GPU render node" in next(e.reason for e in h.encoders if e.name == "h264_vaapi")
    fallback = h.best("h264")
    assert fallback is not None and fallback.backend == "software"


def test_trial_goes_through_the_real_filter_chain() -> None:
    calls: list[list[str]] = []
    probe.probe_host("ffmpeg", fake_runner(calls=calls), nodes=["/dev/null"], cores=4)
    trials = [c for c in calls if "-c:v" in c]
    vaapi = next(c for c in trials if c[c.index("-c:v") + 1] == "h264_vaapi")
    assert "format=nv12,hwupload" in vaapi and "vaapi=va:/dev/null" in vaapi
    assert all("-frames:v" in c and c[c.index("-frames:v") + 1] == "3" for c in trials)


def test_transports() -> None:
    h = host()
    ok = {t.id: t for t in h.transports}
    assert all(ok[i].ok for i in ("record-mkv", "record-mp4", "srt", "rtmp", "rtmp-enhanced", "rtmps"))
    assert not ok["whip"].ok and "WHIP" in ok["whip"].label and "streamstack[whip]" in ok["whip"].reason
    ready = {t.id: t for t in host(whip="").transports}
    assert ready["whip"].ok and ready["whip"].reason == ""
    assert "H.264 + Opus" in ready["whip"].detail and "aiortc" in ready["whip"].detail


def test_whip_does_not_depend_on_an_ffmpeg_muxer() -> None:
    """ffmpeg 8 on Ubuntu has no `whip` muxer; StreamStack publishes WHIP itself, so the muxer list is irrelevant."""
    ts = {t.id: t for t in probe.probe_transports("ff", PROTOCOLS, " E matroska\n", RTMP_HELP, "8.0", lambda: "")}
    assert ts["whip"].ok
    broken = {
        t.id: t for t in probe.probe_transports("ff", PROTOCOLS, " E whip  x\n", RTMP_HELP, "8.0", lambda: "boom")
    }
    assert not broken["whip"].ok and broken["whip"].reason == "boom"


def test_transport_reasons_when_ffmpeg_lacks_things() -> None:
    ts = {t.id: t for t in probe.probe_transports("ff", "Output:\n file\n", " E matroska\n", "", "5.1")}
    assert not ts["srt"].ok and "libsrt" in ts["srt"].reason
    assert not ts["rtmp"].ok and not ts["rtmp-enhanced"].ok and not ts["record-mp4"].ok
    assert ts["record-mkv"].ok
    rtmp_only = {t.id: t for t in probe.probe_transports("ff", "Output:\n rtmp\n", " E flv\n", "", "5.1")}
    assert rtmp_only["rtmp"].ok and "6.1" in rtmp_only["rtmp-enhanced"].reason


def test_missing_ffmpeg_is_a_report_not_an_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe.shutil, "which", lambda name: None)
    h = probe.probe_host()
    assert h.problems and not h.encoders
    ts = {t.id: t for t in h.transports}
    assert ts["record-mkv"].ok and not ts["srt"].ok and "not installed" in ts["srt"].reason


def test_explain_prefers_the_first_problem_line_and_strips_prefixes() -> None:
    out = "libva info: foo\n[hevc_qsv @ 0x55aa] Invalid FrameType:0.\n[x @ 0x1] Error initializing output stream"
    assert probe.explain(out) == "Invalid FrameType:0."
    assert probe.explain("") == "the trial encode failed"
    assert "NVIDIA" in probe.explain("Cannot load libcuda.so.1")


def test_to_dict_is_json_ready() -> None:
    import json

    data = json.loads(json.dumps(host().to_dict()))
    assert data["codecs"] == ["av1", "hevc", "h264"]
    assert {"name", "ok", "reason", "device"} <= set(data["encoders"][0])


def test_cache_reuses_within_ttl_and_refreshes_on_request() -> None:
    count = []

    def make() -> HostProbe:
        count.append(1)
        return HostProbe(at=probe.time.time())

    cache = probe.ProbeCache(make, ttl=60)
    assert cache.peek() is None
    first = cache.get()
    assert cache.get() is first and len(count) == 1
    cache.get(refresh=True)
    assert len(count) == 2


def test_source_info_for_missing_and_real_files(tmp_path) -> None:
    assert probe.source_info(str(tmp_path / "nope"))["exists"] is False
    info = probe.source_info(str(tmp_path))
    assert info["exists"] is True and info["multi_reader"] is False


def test_encoder_probe_builds_a_choice() -> None:
    choice = EncoderProbe("av1_vaapi", "av1", "vaapi", "x", "/dev/dri/renderD128", ok=True).choice()
    assert choice.device == "/dev/dri/renderD128" and choice.codec == "av1"
