"""Presets: the shipped data, validation, stream plans, generated LUTs and thumbnails, and the profile store."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from streamstack.console.errors import ActionError
from streamstack.console.presets import PresetsController
from streamstack.presets import PresetError, catalog, resolve, schema
from streamstack.presets import build as presets_build
from streamstack.presets.luts import LUTS, Lut3D, build_cube
from streamstack.presets.plan import frame_size, ladder_tier
from streamstack.presets.store import ProfileStore, export_document
from streamstack.presets.thumbs import HEIGHT, WIDTH, decode_png, grade, png, render, slug

CAT = catalog.load()
PLATFORM_IDS = [p["id"] for p in CAT.platforms]
TIER_IDS = [t["id"] for t in CAT.tiers]


# -- the shipped catalog -------------------------------------------------------------------------------------


def test_catalog_counts() -> None:
    assert len(CAT.looks) >= 30
    assert [p["id"] for p in CAT.platforms][:3] == ["youtube", "twitch", "kick"]
    assert {"youtube", "twitch", "kick", "tiktok", "linkedin", "facebook", "x", "calls", "archive"} <= set(PLATFORM_IDS)
    assert TIER_IDS == ["ultra", "high", "balanced", "constrained", "cellular"]
    assert {p["id"] for p in CAT.profiles} >= {"meeting", "podcast", "gaming", "webinar", "interview", "late-night"}


def test_every_look_is_valid_and_distinct() -> None:
    names = [look["name"] for look in CAT.looks]
    assert len(names) == len({n.casefold() for n in names})
    for look in CAT.looks:
        assert schema.validate_look_values(look["values"]) == look["values"]
        assert look["thumb"].endswith(f"{slug(look['name'])}.png")
    assert {look["category"] for look in CAT.looks} == {c["id"] for c in CAT.categories}


def test_original_looks_keep_their_values() -> None:
    by_name = {look["name"]: look["values"] for look in CAT.looks}
    assert by_name["Natural"] == {}
    assert by_name["Warm Studio"]["warmth"] == 0.25
    assert by_name["Cinematic"]["lut_enabled"] is True


def test_platforms_carry_a_source_and_a_verification_grade() -> None:
    for platform in CAT.platforms:
        assert platform["source"].startswith("https://")
        assert platform["verified"] in schema.VERIFIED
        assert platform["rationale"]
        if platform["verified"] != "official":
            assert platform["uncertain"], f"{platform['id']} is not official, so it must say what is uncertain"


def test_loudness_targets_are_only_where_a_platform_expects_them() -> None:
    youtube = CAT.platform("youtube")["audio"]
    assert youtube["loudness_lufs"] == -14
    twitch = CAT.platform("twitch")["audio"]
    assert twitch["loudness_lufs"] == -16
    assert CAT.platform("archive")["audio"]["loudness_lufs"] is None


def test_every_builtin_profile_references_real_parts() -> None:
    looks = {look["name"] for look in CAT.looks}
    for profile in CAT.profiles:
        assert profile["look"] in looks
        assert profile["platform"] in PLATFORM_IDS and profile["tier"] in TIER_IDS
        assert profile["framing"] in schema.FRAMING_MODES


def test_builtin_profile_parts_exist_in_the_rig_libraries() -> None:
    from streamstack.audiochain import presets as voice_presets
    from streamstack.console.lighting import BUILTIN_PRESETS

    for profile in CAT.profiles:
        assert profile["voice"] in voice_presets.load_all(user=voice_presets.shipped_dir()), profile["name"]
        assert profile["lighting"] in BUILTIN_PRESETS, profile["name"]


def test_framing_modes_match_the_engine() -> None:
    pytest.importorskip("numpy")
    from streamstack.framing.engine import MODES

    assert tuple(MODES) == schema.FRAMING_MODES


def test_look_limits_are_the_ones_the_video_controller_enforces() -> None:
    from streamstack.console import video

    assert video.LIMITS is schema.LOOK_LIMITS and video.NEUTRAL is schema.LOOK_NEUTRAL


# -- schema errors name the path ---------------------------------------------------------------------------


def test_look_values_reject_unknown_and_out_of_range() -> None:
    with pytest.raises(PresetError, match="unknown look setting 'exposure'"):
        schema.validate_look_values({"exposure": 1})
    with pytest.raises(PresetError, match=r"values\.warmth: must be between -1 and 1"):
        schema.validate_look_values({"warmth": 1.5})
    with pytest.raises(PresetError, match="must be true or false"):
        schema.validate_look_values({"lut_enabled": 1})
    with pytest.raises(PresetError, match="must be a number"):
        schema.validate_look_values({"gamma": True})
    with pytest.raises(PresetError, match="must be a number"):
        schema.validate_look_values({"gamma": float("nan")})


def _platform() -> dict:
    return json.loads(json.dumps(CAT.platform("youtube")))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"codecs": ["vp9"]}, "must be one of av1, hevc, h264"),
        ({"transport": []}, "non-empty list"),
        ({"transport": ["carrier-pigeon"]}, "must be one of"),
        ({"source": "http://example.com"}, "https://"),
        ({"bitrate_cap_kbps": 90000}, "must not exceed platform_max_kbps"),
        ({"rate_control": "none"}, "needs a rate control"),
        ({"quality": {"av1": 30}}, "only applies to cqp"),
        ({"surprise": 1}, "unknown field 'surprise'"),
        ({"verified": "trust-me"}, "must be one of official"),
    ],
)
def test_platform_validation(change: dict, message: str) -> None:
    entry = {**_platform(), **change}
    with pytest.raises(PresetError, match=message):
        schema.validate_platform(entry)


def test_platform_audio_validation() -> None:
    entry = _platform()
    entry["audio"]["loudness_lufs"] = 3
    with pytest.raises(PresetError, match=r"audio\.loudness_lufs"):
        schema.validate_platform(entry)


def test_tier_ladders_must_share_rungs_and_run_downward() -> None:
    tier = json.loads(json.dumps(CAT.tier("high")))
    tier["ladders"]["av1"] = tier["ladders"]["av1"][:2]
    with pytest.raises(PresetError, match="same resolution and frame-rate rungs"):
        schema.validate_tier(tier)
    tier = json.loads(json.dumps(CAT.tier("high")))
    tier["ladders"]["h264"].reverse()
    with pytest.raises(PresetError, match="largest to smallest"):
        schema.validate_tier(tier)


def test_every_tier_is_ordered_by_bitrate_per_codec() -> None:
    for codec in schema.CODECS:
        top = [t["ladders"][codec][0]["bitrate_kbps"] for t in CAT.tiers]
        assert top == sorted(top, reverse=True), codec
    for tier in CAT.tiers:  # modern codecs need less bitrate for the same rung
        for rung in range(len(tier["ladders"]["h264"])):
            rates = [
                tier["ladders"][c]["bitrate_kbps"] if False else tier["ladders"][c][rung]["bitrate_kbps"]
                for c in ("av1", "hevc", "h264")
            ]
            assert rates == sorted(rates), (tier["id"], rung)


# -- stream plans ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("platform_id", PLATFORM_IDS)
@pytest.mark.parametrize("tier_id", TIER_IDS)
def test_the_downshift_ladder_starts_at_the_plan_and_every_step_is_a_plan_within_the_limits(
    platform_id: str, tier_id: str
) -> None:
    platform, tier = CAT.platform(platform_id), CAT.tier(tier_id)
    ladder = ladder_tier(platform, CAT.tiers, tier_id)
    assert ladder["steps"], "there is always at least the plan itself"
    plans = [
        resolve(platform, ladder, index=None if platform["tier_policy"] == "ignore" else i)
        for i in range(len(ladder["steps"]))
    ]
    first = resolve(platform, tier)
    assert plans[0]["video"]["width"] == first["video"]["width"] and plans[0]["video"]["fps"] == first["video"]["fps"]
    for plan in plans:
        assert plan["video"]["fps"] <= platform["max_fps"]
        for codec in plan["video"]["codecs"]:
            if platform["bitrate_cap_kbps"] and codec.get("bitrate_kbps"):
                assert codec["bitrate_kbps"] <= platform["bitrate_cap_kbps"]


def test_a_platform_that_ignores_tiers_has_one_step_and_a_bad_index_is_an_error() -> None:
    archive = CAT.platform("archive")
    assert archive["tier_policy"] == "ignore"
    assert len(ladder_tier(archive, CAT.tiers, "high")["steps"]) == 1
    youtube, tier = CAT.platform("youtube"), CAT.tier("high")
    with pytest.raises(PresetError, match="no rung"):
        resolve(youtube, tier, index=99)
    with pytest.raises(PresetError, match="unknown tier"):
        ladder_tier(youtube, CAT.tiers, "nope")


@pytest.mark.parametrize("platform_id", PLATFORM_IDS)
@pytest.mark.parametrize("tier_id", TIER_IDS)
def test_every_platform_and_tier_resolves_within_the_platform_limits(platform_id: str, tier_id: str) -> None:
    platform, tier = CAT.platform(platform_id), CAT.tier(tier_id)
    plan = resolve(platform, tier)
    video = plan["video"]
    short = min(video["width"], video["height"])
    assert short <= platform["max_short_side"] and video["fps"] <= platform["max_fps"]
    assert video["width"] % 2 == 0 and video["height"] % 2 == 0
    assert (video["width"] > video["height"]) == (platform["orientation"] == "landscape")
    assert [c["codec"] for c in video["codecs"]] == platform["codecs"]
    for codec in video["codecs"]:
        rate = codec.get("bitrate_kbps")
        if rate is not None and platform["bitrate_cap_kbps"]:
            assert rate <= platform["bitrate_cap_kbps"]
    assert plan["transport"] == platform["transport"]


def test_youtube_ultra_is_4k60_with_av1_first() -> None:
    plan = resolve(CAT.platform("youtube"), CAT.tier("ultra"))
    assert (plan["video"]["width"], plan["video"]["height"], plan["video"]["fps"]) == (3840, 2160, 60)
    assert [c["codec"] for c in plan["video"]["codecs"]] == ["av1", "hevc", "h264"]
    assert plan["video"]["codecs"][-1]["bitrate_kbps"] == 35000
    assert plan["video"]["gop_frames"] == 120 and plan["audio"]["loudness_lufs"] == -14


def test_twitch_is_capped_at_1080p60_and_6_mbps() -> None:
    plan = resolve(CAT.platform("twitch"), CAT.tier("ultra"))
    assert (plan["video"]["height"], plan["video"]["fps"]) == (1080, 60)
    assert max(c["bitrate_kbps"] for c in plan["video"]["codecs"]) == 6000


def test_tiktok_is_portrait_and_linkedin_30fps() -> None:
    tiktok = resolve(CAT.platform("tiktok"), CAT.tier("balanced"))
    assert (tiktok["video"]["width"], tiktok["video"]["height"], tiktok["video"]["fps"]) == (1080, 1920, 30)
    linkedin = resolve(CAT.platform("linkedin"), CAT.tier("high"))
    assert linkedin["video"]["fps"] == 30 and linkedin["video"]["codecs"][0]["bitrate_kbps"] <= 4500


def test_cellular_tier_is_small() -> None:
    plan = resolve(CAT.platform("youtube"), CAT.tier("cellular"))
    assert plan["video"]["height"] == 540 and plan["video"]["codecs"][0]["bitrate_kbps"] <= 800
    assert plan["uplink_mbps_needed"] < 3


def test_archive_ignores_the_tier_and_uses_quality() -> None:
    low, high = (
        resolve(CAT.platform("archive"), CAT.tier("cellular")),
        resolve(CAT.platform("archive"), CAT.tier("ultra")),
    )
    assert low["video"] == high["video"] and low["tier_applies"] is False
    assert low["video"]["codecs"][0] == {"codec": "av1", "quality": 28} and low["video"]["container"] == "mkv"


def test_call_camera_has_no_codec_or_bitrate() -> None:
    plan = resolve(CAT.platform("calls"), CAT.tier("constrained"))
    assert plan["video"]["codecs"] == [] and plan["transport"] == ["virtualcam"]
    assert plan["video"]["height"] == 720 and plan["uplink_mbps_needed"] is None


def test_frame_size_is_even_and_oriented() -> None:
    assert frame_size(480, "landscape") == (854, 480)
    assert frame_size(1080, "portrait") == (1080, 1920)


# -- generated LUTs and thumbnails -------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(LUTS))
def test_cube_is_well_formed(name: str) -> None:
    text = build_cube(name)
    assert text.splitlines()[2] == "LUT_3D_SIZE 17" and "Apache-2.0" in text
    lut = Lut3D.parse(text)
    assert lut.size == 17 and len(lut.table) == 17**3
    assert all(0.0 <= c <= 1.0 for entry in lut.table for c in entry)
    black, white = lut.sample((0, 0, 0)), lut.sample((1, 1, 1))
    assert sum(white) > sum(black)  # a grade, not an inversion


def test_cube_is_deterministic_and_not_the_identity() -> None:
    assert build_cube("teal-orange") == build_cube("teal-orange")
    teal = Lut3D.parse(build_cube("teal-orange"))
    shadow, highlight = teal.sample((0.2, 0.2, 0.2)), teal.sample((0.85, 0.85, 0.85))
    assert shadow[2] > shadow[0], "shadows lean teal"
    assert highlight[0] > highlight[2], "highlights lean orange"
    noir = Lut3D.parse(build_cube("noir")).sample((0.7, 0.4, 0.2))
    assert noir[0] == noir[1] == noir[2]


def test_lut_sampling_hits_corners_and_interpolates() -> None:
    lut = Lut3D.parse(build_cube("velvia"))
    assert lut.sample((1, 1, 1)) == lut.table[-1]
    mid = lut.sample((0.5, 0.5, 0.5))
    assert all(0.3 < c < 0.7 for c in mid)
    with pytest.raises(ValueError, match="needs 8 entries"):
        Lut3D(2, [(0, 0, 0)])


def test_shipped_assets_are_current() -> None:
    assert presets_build.problems() == []


def test_thumbnails_show_the_look() -> None:
    plain = render(schema.LOOK_NEUTRAL)
    noir = render({**schema.LOOK_NEUTRAL, "saturation": -1.0})
    assert len(plain) == WIDTH * HEIGHT * 3 and plain != noir
    grey = [noir[i : i + 3] for i in range(0, len(noir), 3)]
    assert all(px[0] == px[1] == px[2] for px in grey)
    assert grade((0.5, 0.5, 0.5), {"brightness": 0.1}) > grade((0.5, 0.5, 0.5), {})


def test_png_round_trip() -> None:
    data = render(schema.LOOK_NEUTRAL)
    width, height, pixels = decode_png(png(data))
    assert (width, height, pixels) == (WIDTH, HEIGHT, data)
    with pytest.raises(ValueError, match="does not match"):
        png(b"123")


def test_slug() -> None:
    assert slug("High-Key Beauty") == "high-key-beauty"
    assert slug("Teal and Orange") == "teal-and-orange"
    assert slug("  Kodak  Portra 400! ") == "kodak-portra-400"


# -- profile documents and the store ---------------------------------------------------------------------------


def good_profile(**changes: object) -> dict:
    return {
        "name": "Mine", "blurb": "My setup", "look": "Natural", "platform": "twitch", "tier": "high",
        "lighting": "Soft", "voice": "StreamStack Voice", "framing": "gentle", **changes,
    }  # fmt: skip


def test_profile_validation_normalises_and_rejects() -> None:
    ok = schema.validate_profile(good_profile(), platforms=set(PLATFORM_IDS), tiers=set(TIER_IDS))
    assert ok["name"] == "Mine" and ok["icon"] is None
    for change, message in [
        ({"name": ""}, "must not be empty"),
        ({"name": "x" * 41}, "at most 40"),
        ({"name": "bad\x07"}, "control characters"),
        ({"platform": "myspace"}, "unknown platform"),
        ({"tier": "ludicrous"}, "unknown tier"),
        ({"framing": "wild"}, "must be one of off"),
        ({"tier": None}, "go together"),
        ({"extra": 1}, "unknown field"),
        ({"look": 7}, "must be text"),
    ]:
        with pytest.raises(PresetError, match=message):
            schema.validate_profile(good_profile(**change), platforms=set(PLATFORM_IDS), tiers=set(TIER_IDS))


def test_profile_parts_may_be_left_out() -> None:
    bare = schema.validate_profile({"name": "Only light", "lighting": "Soft"})
    assert bare["look"] is None and bare["platform"] is None and bare["framing"] is None


def test_document_envelopes() -> None:
    single = {"schema": schema.PROFILE_SCHEMA, "profile": good_profile()}
    assert len(schema.validate_profile_document(single)) == 1
    many = {"schema": schema.PROFILES_SCHEMA, "profiles": [good_profile(), good_profile(name="Two")]}
    assert len(schema.validate_profile_document(many)) == 2
    for bad, message in [
        ([], "must be an object"),
        ({"schema": "other"}, "document.schema"),
        ({"schema": schema.PROFILES_SCHEMA, "profiles": []}, "non-empty"),
        ({"schema": schema.PROFILES_SCHEMA, "profiles": [good_profile(tier="nope")]}, r"profiles\[0\]\.tier"),
        ({"schema": schema.PROFILE_SCHEMA}, "'profile' is required"),
        ({"schema": schema.PROFILES_SCHEMA, "profiles": [good_profile(name=f"n{i}") for i in range(51)]}, "at most 50"),
    ]:
        with pytest.raises(PresetError, match=message):
            schema.validate_profile_document(bad, platforms=set(PLATFORM_IDS), tiers=set(TIER_IDS))


def make_store(tmp_path: Path) -> ProfileStore:
    return ProfileStore(tmp_path / "profiles.json", platforms=set(PLATFORM_IDS), tiers=set(TIER_IDS))


def test_store_saves_replaces_deletes(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    assert store.profiles() == {} and store.selection() == {"platform": None, "tier": None}
    store.save_many([good_profile(), good_profile(name="Other", look="Noir")])
    assert set(store.profiles()) == {"Mine", "Other"}
    store.save_many([good_profile(look="Punchy")])
    assert store.profiles()["Mine"]["look"] == "Punchy"
    store.set_selection("kick", "balanced")
    store.delete("Other")
    assert set(store.profiles()) == {"Mine"} and store.selection() == {"platform": "kick", "tier": "balanced"}
    with pytest.raises(PresetError, match="no saved profile"):
        store.delete("Other")


def test_store_is_all_or_nothing(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.save_many([good_profile()])
    with pytest.raises(PresetError):
        store.save_many([good_profile(name="Good"), good_profile(name="Bad", tier="nope")])
    assert set(store.profiles()) == {"Mine"}


def test_store_skips_bad_entries_and_sets_aside_a_corrupt_file(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.path.write_text(json.dumps({"profiles": {"Fine": good_profile(name="Fine"), "Bad": {"name": 5}}}))
    assert set(store.profiles()) == {"Fine"}
    store.path.write_text("{ not json")
    assert store.profiles() == {}
    kept = list(store.path.parent.glob("profiles.json.corrupt-*"))
    assert [p.read_text() for p in kept] == ["{ not json"]
    store.save_many([good_profile()])  # a fresh file; the evidence stays
    assert list(store.path.parent.glob("profiles.json.corrupt-*")) == kept and set(store.profiles()) == {"Mine"}
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_store_drops_a_selection_that_no_longer_exists(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.path.write_text(json.dumps({"selection": {"platform": "myspace", "tier": "high"}}))
    assert store.selection() == {"platform": None, "tier": "high"}


def test_export_round_trips_through_import(tmp_path: Path) -> None:
    exported = export_document([{**good_profile(), "id": "x"}])
    assert "id" not in exported["profiles"][0]
    assert schema.validate_profile_document(exported)[0]["name"] == "Mine"
    assert not os.environ.get("NEVER_SET")


# -- the controller ---------------------------------------------------------------------------------------------


class Recorder:
    """A stand-in for Rig.act: records the calls and fails the ones a test names."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail: dict[str, ActionError] = {}

    def __call__(self, action: str, params: dict) -> str | None:
        self.calls.append((action, params))
        if action in self.fail:
            raise self.fail[action]
        return f"did {action}"


def mine(ctl: PresetsController) -> list[dict]:
    return [p for p in ctl.state()["profiles"] if not p["builtin"]]


@pytest.fixture
def rec() -> Recorder:
    return Recorder()


@pytest.fixture
def ctl(tmp_path: Path, rec: Recorder) -> PresetsController:
    return PresetsController(
        rec, ProfileStore(tmp_path / "profiles.json", platforms=set(PLATFORM_IDS), tiers=set(TIER_IDS))
    )


def test_apply_builtin_profile_calls_every_existing_action(ctl: PresetsController, rec: Recorder) -> None:
    message = ctl.act("presets.profile.apply", {"id": "podcast"})
    names = [name for name, _ in rec.calls]
    assert names == [
        "video.looks.apply",
        "stream.set",
        "lighting.preset",
        "audio.preset",
        "framing.mode",
    ]
    assert dict(rec.calls)["video.looks.apply"] == {"name": "Podcast Studio"}
    assert dict(rec.calls)["lighting.preset"] == {"name": "Soft"}
    assert dict(rec.calls)["audio.preset"] == {"name": "StreamStack Voice Warm"}
    assert "5 of 5" in message


def test_state_lists_builtins_then_the_users_profiles(ctl: PresetsController) -> None:
    ctl.act("presets.profile.save", {"profile": good_profile()})
    listing = ctl.state()["profiles"]
    assert [p["builtin"] for p in listing] == [True] * len(CAT.profiles) + [False]
    assert listing[0]["id"] == "meeting" and listing[-1]["id"] is None


def test_apply_sends_the_resolved_plan_to_stream_set(ctl: PresetsController, rec: Recorder) -> None:
    ctl.act("presets.profile.apply", {"id": "late-night"})
    payload = dict(rec.calls)["stream.set"]
    assert payload["platform"] == "youtube" and payload["tier"] == "ultra"
    assert payload["plan"]["video"]["height"] == 2160
    state = ctl.state()
    assert state["stream"] == {"contract": "stream.set", "status": "applied", "message": "sent to the stream backend"}
    assert state["platform"] == "youtube" and state["profile"] == "Late Night"
    assert [s["status"] for s in state["last_apply"]["steps"]] == ["ok"] * 5


def test_stream_set_pending_is_saved_not_failed(ctl: PresetsController, rec: Recorder) -> None:
    rec.fail["stream.set"] = ActionError("pending", 501)
    message = ctl.act("presets.profile.apply", {"id": "gaming"})
    state = ctl.state()
    assert state["stream"]["status"] == "pending" and state["platform"] == "twitch" and state["tier"] == "high"
    assert "5 of 5" in message
    assert next(s for s in state["last_apply"]["steps"] if s["part"] == "stream")["status"] == "pending"


def test_a_failing_part_does_not_stop_the_others(ctl: PresetsController, rec: Recorder) -> None:
    rec.fail["lighting.preset"] = ActionError("No Litra light is connected.", 409)
    message = ctl.act("presets.profile.apply", {"id": "meeting"})
    assert "4 of 5" in message and "No Litra light is connected." in message
    assert [n for n, _ in rec.calls][-2:] == ["audio.preset", "framing.mode"]
    steps = {s["part"]: s["status"] for s in ctl.state()["last_apply"]["steps"]}
    assert steps["lighting"] == "failed" and steps["voice"] == "ok"


def test_apply_fails_loudly_when_every_part_fails(ctl: PresetsController, rec: Recorder) -> None:
    for action in ("video.looks.apply", "stream.set", "lighting.preset", "audio.preset", "framing.mode"):
        rec.fail[action] = ActionError("down", 409)
    with pytest.raises(ActionError, match="down"):
        ctl.act("presets.profile.apply", {"id": "meeting"})
    assert ctl.state()["profile"] is None


def test_apply_unknown_profile_and_empty_profile(ctl: PresetsController) -> None:
    with pytest.raises(ActionError, match="No built-in profile"):
        ctl.act("presets.profile.apply", {"id": "nope"})
    with pytest.raises(ActionError, match="No profile called"):
        ctl.act("presets.profile.apply", {"name": "Ghost"})
    ctl.act("presets.profile.save", {"profile": {"name": "Hollow"}})
    with pytest.raises(ActionError, match="no parts to apply"):
        ctl.act("presets.profile.apply", {"name": "Hollow"})


def test_save_apply_delete_a_user_profile(ctl: PresetsController, rec: Recorder) -> None:
    assert ctl.act("presets.profile.save", {"profile": good_profile()}) == "Saved profile Mine"
    assert [p["name"] for p in mine(ctl)] == ["Mine"]
    ctl.act("presets.profile.apply", {"name": "Mine"})
    assert dict(rec.calls)["framing.mode"] == {"mode": "gentle"}
    ctl.act("presets.profile.delete", {"name": "Mine"})
    assert mine(ctl) == [] and ctl.state()["profile"] is None


def test_builtin_names_are_reserved(ctl: PresetsController) -> None:
    for name in ("Meeting", "meeting", "Late Night", "late-night"):
        with pytest.raises(ActionError, match="built-in profile"):
            ctl.act("presets.profile.save", {"profile": good_profile(name=name)})


def test_save_rejects_an_invalid_profile_with_a_400(ctl: PresetsController) -> None:
    with pytest.raises(ActionError, match="unknown platform"):
        ctl.act("presets.profile.save", {"profile": good_profile(platform="myspace")})


def test_import(ctl: PresetsController) -> None:
    doc = {"schema": schema.PROFILES_SCHEMA, "profiles": [good_profile(name="A"), good_profile(name="B")]}
    assert ctl.act("presets.profile.import", {"document": doc}) == "Imported 2 profiles"
    assert ctl.act("presets.profile.import", {"document": doc}) == "Imported 2 profiles (replaced A, B)"
    dup = {"schema": schema.PROFILES_SCHEMA, "profiles": [good_profile(name="C"), good_profile(name="c")]}
    with pytest.raises(ActionError, match="two profiles with the same name"):
        ctl.act("presets.profile.import", {"document": dup})
    clash = {"schema": schema.PROFILE_SCHEMA, "profile": good_profile(name="Podcast")}
    with pytest.raises(ActionError, match="built-in profile"):
        ctl.act("presets.profile.import", {"document": clash})
    with pytest.raises(ActionError, match=r"document\.schema") as caught:
        ctl.act("presets.profile.import", {"document": {"hello": 1}})
    assert caught.value.status == 400
    assert {p["name"] for p in mine(ctl)} == {"A", "B"}


def test_choose_platform_and_tier(ctl: PresetsController, rec: Recorder) -> None:
    assert ctl.act("presets.tier", {"id": "constrained"}) == "Tier: Constrained"
    assert rec.calls == []  # no platform yet: nothing to resolve
    message = ctl.act("presets.platform", {"id": "youtube"})
    assert message == "YouTube Live, Constrained"
    assert rec.calls[-1][0] == "stream.set" and rec.calls[-1][1]["plan"]["video"]["height"] == 720
    ctl.act("presets.tier", {"id": "ultra"})
    assert ctl.state()["plan"]["video"]["height"] == 2160
    with pytest.raises(ActionError):
        ctl.act("presets.platform", {"id": "myspace"})
    with pytest.raises(ActionError, match="id is required"):
        ctl.act("presets.tier", {})


def test_clear_forgets_the_choice(ctl: PresetsController) -> None:
    ctl.act("presets.platform", {"id": "kick"})
    assert ctl.act("presets.clear", {}) == "Platform and tier cleared"
    state = ctl.state()
    assert (state["platform"], state["tier"], state["plan"], state["last_apply"]) == (None, None, None, None)


def test_choose_reports_a_backend_failure(ctl: PresetsController, rec: Recorder) -> None:
    rec.fail["stream.set"] = ActionError("encoder busy", 409)
    with pytest.raises(ActionError, match="encoder busy"):
        ctl.act("presets.platform", {"id": "kick"})
    assert ctl.state()["stream"]["status"] == "failed"


def test_unknown_action(ctl: PresetsController) -> None:
    with pytest.raises(ActionError, match="unknown action"):
        ctl.act("presets.nope", {})


def test_a_save_in_flight_never_holds_up_a_read_and_the_new_selection_shows_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #161: the selection was read under the lock a save held through two fsyncs, so on a starved disk every
    state push waited and the Console showed the old tier for tens of seconds."""
    import threading

    from streamstack import store as store_module

    prof = make_store(tmp_path)
    prof.set_selection("youtube", "standard")
    inside, release = threading.Event(), threading.Event()
    real = store_module.write_json

    def slow(path: Path | str, data: object, **kw: object) -> Path:
        inside.set()
        assert release.wait(30)
        return real(path, data, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(store_module, "write_json", slow)
    saver = threading.Thread(target=prof.set_selection, args=("youtube", "cellular"), daemon=True)
    saver.start()
    try:
        assert inside.wait(10)
        seen: dict[str, object] = {}
        reader = threading.Thread(target=lambda: seen.update(prof.selection()), daemon=True)
        reader.start()
        reader.join(5)
        assert not reader.is_alive(), "a read waited behind a save in flight"
        assert seen == {"platform": "youtube", "tier": "cellular"}
    finally:
        release.set()
        saver.join(10)
    assert prof.selection() == {"platform": "youtube", "tier": "cellular"}
