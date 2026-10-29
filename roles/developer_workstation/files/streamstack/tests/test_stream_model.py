from __future__ import annotations

from typing import ClassVar

import pytest

from streamstack.stream import model
from streamstack.stream.model import (
    AssignError,
    Destination,
    EncoderSettings,
    SecretError,
    SettingsError,
    assign_codec,
    classify_change,
    destinations_from,
    plan_chains,
    redact,
    resolve_secret,
)


def dest(kind: str, ident: str | None = None, **extra: object) -> Destination:
    data: dict[str, object] = {"id": ident or kind, "kind": kind}
    if kind in ("srt", "rtmp", "whip"):
        data["url"] = {"srt": "srt://example.test:9000", "rtmp": "rtmp://live.test/app", "whip": "https://w.test/x"}[
            kind
        ]
    data.update(extra)
    return Destination.from_dict(data)


class TestSettings:
    def test_defaults_and_roundtrip(self) -> None:
        s = EncoderSettings()
        assert EncoderSettings.from_dict(s.to_dict()) == s

    def test_partial_update_keeps_the_rest(self) -> None:
        base = EncoderSettings(bitrate_kbps=6000)
        new = EncoderSettings.from_dict({"keyint_s": 1}, base)
        assert new.bitrate_kbps == 6000 and new.keyint_s == 1.0

    @pytest.mark.parametrize(
        "data",
        [
            {"nope": 1},
            {"codec": "vp9"},
            {"encoder": "Bad Name"},
            {"tier": "8k"},
            {"fps": 2},
            {"fps": True},
            {"bitrate_kbps": 50},
            {"bitrate_kbps": 10**9},
            {"rate_control": "magic"},
            {"keyint_s": 0.1},
            {"quality": 99},
            {"audio_bitrate_kbps": 8},
        ],
    )
    def test_invalid_values_are_rejected(self, data: dict[str, object]) -> None:
        with pytest.raises(SettingsError):
            EncoderSettings.from_dict(data)

    def test_size_follows_tier_and_aspect(self) -> None:
        assert EncoderSettings().size((1920, 1080)) == (1920, 1080)
        assert EncoderSettings(tier="720p").size((1920, 1080)) == (1280, 720)
        wide, high = EncoderSettings(tier="720p").size((1920, 800))
        assert wide == 1280 and high % 2 == 0 and abs(high - 533) <= 2
        wide, high = EncoderSettings(tier="720p").size((1080, 1920))
        assert high == 720 and wide % 2 == 0

    def test_bitrate_scales_and_explicit_wins(self) -> None:
        s = EncoderSettings()
        assert s.bitrate_for("h264", (1920, 1080), 30) > s.bitrate_for("h264", (1280, 720), 30)
        assert s.bitrate_for("av1", (1920, 1080), 30) < s.bitrate_for("h264", (1920, 1080), 30)
        assert s.bitrate_for("h264", (1920, 1080), 60) > s.bitrate_for("h264", (1920, 1080), 30)
        assert EncoderSettings(bitrate_kbps=1234).bitrate_for("av1", (640, 360), 30) == 1234

    @pytest.mark.parametrize(
        ("change", "expected"),
        [
            ({}, "none"),
            ({"bitrate_kbps": 5000}, "seamless"),
            ({"rate_control": "vbr"}, "seamless"),
            ({"keyint_s": 1}, "seamless"),
            ({"encoder": "libx264"}, "seamless"),
            ({"tier": "720p"}, "geometry"),
            ({"fps": 60}, "geometry"),
            ({"codec": "hevc"}, "codec"),
            ({"audio_bitrate_kbps": 96}, "audio"),
            ({"codec": "hevc", "bitrate_kbps": 5000, "tier": "720p"}, "codec"),
            ({"tier": "720p", "bitrate_kbps": 5000}, "geometry"),
        ],
    )
    def test_classify_change(self, change: dict[str, object], expected: str) -> None:
        old = EncoderSettings()
        assert classify_change(old, EncoderSettings.from_dict(change, old)) == expected


class TestSecrets:
    def test_env_cfg_file_and_cmd(self, tmp_path) -> None:
        keyfile = tmp_path / "key"
        keyfile.write_text("filekey\n")
        assert resolve_secret("env:K", environ={"K": "envkey"}) == "envkey"
        assert resolve_secret("cfg:K", cfg={"K": "cfgkey"}) == "cfgkey"
        assert resolve_secret(f"file:{keyfile}") == "filekey"
        assert resolve_secret("cmd:vault get x", run=lambda argv: f"{argv[0]}-{argv[2]}\n") == "vault-x"

    @pytest.mark.parametrize("ref", ["plain", "env:", "http://x"])
    def test_malformed_reference(self, ref: str) -> None:
        with pytest.raises(SecretError):
            resolve_secret(ref, environ={})

    def test_missing_values_name_the_reference_not_a_secret(self, tmp_path) -> None:
        with pytest.raises(SecretError, match="MISSING"):
            resolve_secret("env:MISSING", environ={})
        with pytest.raises(SecretError, match="Cannot read"):
            resolve_secret(f"file:{tmp_path}/absent")

    def test_failing_command_does_not_leak_output(self) -> None:
        def boom(argv: list[str]) -> str:
            raise OSError("secret-in-message")

        with pytest.raises(SecretError) as err:
            resolve_secret("cmd:tool arg", run=boom)
        assert "secret-in-message" not in str(err.value)

    def test_redact_covers_quoted_forms_and_longest_first(self) -> None:
        text = "rtmp://h/app/abc%2Fdef and abc/def and abc"
        out = redact(text, ["abc/def", "abc", ""])
        assert "abc" not in out and out.count("***") == 3


class TestDestinations:
    def test_valid_kinds(self) -> None:
        for kind in ("record", "srt", "rtmp", "whip"):
            assert dest(kind).kind == kind

    @pytest.mark.parametrize(
        "data",
        [
            {"id": "Bad", "kind": "record"},
            {"id": "a", "kind": "udp"},
            {"id": "a", "kind": "srt", "url": "srt://host"},
            {"id": "a", "kind": "srt", "url": "rtmp://host:1"},
            {"id": "a", "kind": "srt", "url": "srt://u:p@host:1"},
            {"id": "a", "kind": "srt", "url": "srt://h:1", "options": {"mode": "x"}},
            {"id": "a", "kind": "rtmp", "url": "rtmp://h/app?key=1"},
            {"id": "a", "kind": "rtmp", "url": "rtmp://h/app", "key_ref": "literal-key"},
            {"id": "a", "kind": "rtmp", "url": "rtmp://h/app#frag"},
            {"id": "a", "kind": "rtmp", "url": "rtmp://h:99999/app"},
            {"id": "a", "kind": "rtmp", "url": "rtmp://h/app", "options": {"mode": "fast"}},
            {"id": "a", "kind": "rtmp", "url": "rtmp://h/app", "options": {"latency_ms": 5}},
            {"id": "a", "kind": "rtmp", "url": "rtmp://h/app", "options": {"audio": "mp3"}},
            {"id": "a", "kind": "rtmp", "url": "rtmp://h/app", "options": {"audio": "opus", "mode": "classic"}},
            {"id": "a", "kind": "rtmp", "url": "rtmp://h/app", "options": {"tls_verify": False}},
            {"id": "a", "kind": "rtmp", "url": "rtmp://h/app", "options": {"ca_file": "/x.pem"}},
            {"id": "a", "kind": "rtmp", "url": "rtmps://h/app", "options": {"tls_verify": "no"}},
            {"id": "a", "kind": "rtmp", "url": "rtmps://h/app", "options": {"ca_file": 5}},
            {"id": "a", "kind": "rtmp", "url": "rtmps://h/app", "options": {"ca_file": "/x.pem", "tls_verify": False}},
            {"id": "a", "kind": "rtmp", "url": "rtmps://u:p@h/app"},
            {"id": "a", "kind": "record", "options": {"format": "avi"}},
            {"id": "a", "kind": "whip", "url": "ftp://h/x"},
            {"id": "a", "kind": "whip", "url": "https://u:p@h/x"},
            {"id": "a", "kind": "record", "surprise": 1},
        ],
    )
    def test_invalid(self, data: dict[str, object]) -> None:
        with pytest.raises(SettingsError):
            Destination.from_dict(data)

    def test_rtmp_options_and_defaults(self) -> None:
        plain = dest("rtmp")
        assert plain.rtmp_audio() == "aac" and not plain.is_tls() and plain.tls_verify()
        opus = dest("rtmp", options={"audio": "opus", "mode": "enhanced"})
        assert opus.rtmp_audio() == "opus" and opus.allowed_codecs() == ("av1", "hevc", "h264")
        secure = dest("rtmp", url="rtmps://live.test:443/app", options={"tls_verify": False})
        assert secure.is_tls() and not secure.tls_verify()
        with_ca = dest("rtmp", url="rtmps://live.test/app", options={"ca_file": "/etc/ssl/ingest.pem"})
        assert with_ca.is_tls() and with_ca.tls_verify() and Destination.from_dict(with_ca.to_dict()) == with_ca

    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("RTMPS://Live.Example.COM/app/", "rtmps://live.example.com/app"),
            ("rtmp://Live.test:1935/app/sub//", "rtmp://live.test:1935/app/sub"),
            ("  rtmps://[::1]:443/live  ", "rtmps://[::1]:443/live"),
            ("rtmp://live.test/a%20b", "rtmp://live.test/a%20b"),
        ],
    )
    def test_rtmp_urls_are_normalised(self, given: str, expected: str) -> None:
        assert dest("rtmp", url=given).url == expected

    def test_normalising_leaves_what_validation_must_reject(self) -> None:
        from streamstack.stream.model import normalize_rtmp_url

        for url in ("rtmps://u:p@h/app", "rtmp://h:99999/app", "srt://h:1", "", "not a url"):
            assert normalize_rtmp_url(url) == url

    def test_allowed_codecs(self) -> None:
        assert dest("rtmp").allowed_codecs()[0] == "av1"
        assert dest("rtmp", options={"mode": "classic"}).allowed_codecs() == ("h264",)
        assert "av1" not in dest("srt").allowed_codecs()
        assert dest("srt", codec="av1").allowed_codecs()[0] == "av1"
        assert dest("whip").allowed_codecs() == ("h264",)
        # WebRTC receivers rarely take HEVC or AV1, so they are used only when the destination names them
        assert dest("whip", codec="av1").allowed_codecs() == ("av1",)
        assert dest("whip", codec="hevc").allowed_codecs() == ("hevc",)
        assert dest("whip", codec="av1").to_dict()["codec"] == "av1"

    def test_list_rejects_duplicates(self) -> None:
        with pytest.raises(SettingsError):
            destinations_from([{"id": "a", "kind": "record"}, {"id": "a", "kind": "record"}])
        with pytest.raises(SettingsError):
            destinations_from({"id": "a"})

    def test_roundtrip(self) -> None:
        d = dest("srt", options={"latency_ms": 200}, key_ref="env:PASS")
        assert Destination.from_dict(d.to_dict()) == d


class TestPlanning:
    ALL: ClassVar[set[str]] = {"av1", "hevc", "h264"}

    def test_each_destination_gets_its_best_codec(self) -> None:
        plan = plan_chains([dest("record"), dest("srt"), dest("rtmp"), dest("whip")], self.ALL)
        assert plan.codec_of("record") == "av1"
        assert plan.codec_of("rtmp") == "av1"
        assert plan.codec_of("whip") == "h264"
        assert plan.unserved == {}
        assert len(plan.chains) <= 2

    def test_max_chains_folds_the_smaller_chain(self) -> None:
        dests = [dest("record"), dest("srt"), dest("whip")]
        plan = plan_chains(dests, self.ALL, max_chains=2)
        assert set(plan.chains) == {"av1", "h264"} or len(plan.chains) == 2
        one = plan_chains(dests, self.ALL, max_chains=1)
        assert list(one.chains) == ["h264"] and sorted(one.chains["h264"]) == ["record", "srt", "whip"]

    def test_unavailable_codecs_are_skipped_with_a_reason(self) -> None:
        plan = plan_chains([dest("record"), dest("whip")], {"hevc"})
        assert plan.codec_of("record") == "hevc"
        assert "whip" in plan.unserved

    def test_wanted_codec_applies_only_to_auto_destinations(self) -> None:
        plan = plan_chains([dest("record"), dest("rtmp", codec="h264")], self.ALL, wanted="hevc")
        assert plan.codec_of("record") == "hevc" and plan.codec_of("rtmp") == "h264"

    def test_assign_codec_reuses_chain_at_the_cap(self) -> None:
        assert assign_codec(dest("record"), self.ALL, set()) == "av1"
        assert assign_codec(dest("record"), self.ALL, {"h264", "hevc"}, max_chains=2) == "hevc"
        assert assign_codec(dest("record"), self.ALL, {"h264"}, max_chains=2) == "av1"
        with pytest.raises(AssignError):
            assign_codec(dest("whip"), self.ALL, {"av1", "hevc"}, max_chains=2)
        with pytest.raises(AssignError):
            assign_codec(dest("whip"), {"av1"}, set())


def test_constants_are_consistent() -> None:
    assert set(model.CODEC_RANK) == set(model.CODECS)
    assert all(codecs for codecs in model.KIND_CODECS.values())
