"""Hardware profile schema: the packaged catalogue loads, and every kind of mistake is rejected with its place."""

from __future__ import annotations

from pathlib import Path

import pytest

from streamstack.hardware import schema
from streamstack.hardware.schema import ProfileError

DEVICE = """
[[device]]
id = "mic"
class = "audio-interface"
label = "A Mic"
[device.match]
usb = ["1234:5678"]
[device.audio]
driver = "wave-xlr"
playback_rates = [48000]
capture_rates = [48000, 96000]
formats = ["S24_3LE"]
preferred_rate = 48000
"""
GPU = """
[[device]]
id = "gpu-a"
class = "gpu"
label = "GPU A"
[device.match]
pci = ["8086:56a5"]
[device.gpu]
kind = "discrete"
roles = ["grade", "encode"]
[[device.gpu.encoder]]
codec = "av1"
ffmpeg = "av1_vaapi"
va_profile = "VAProfileAV1Profile0"
entrypoint = "VAEntrypointEncSliceLP"
"""
RIG = """
[[rig]]
id = "studio"
label = "Studio"
[[rig.expects]]
id = "mic"
required = true
[rig.preferences]
audio_interface = ["mic"]
[rig.preferences.gpu]
encode = ["gpu-a"]
[[rig]]
id = "plain"
label = "Plain"
fallback = true
"""


def load(tmp_path: Path, **files: str) -> schema.Catalogue:
    for name, text in files.items():
        (tmp_path / f"{name}.toml").write_text("schema = 1\n" + text)
    return schema.load(tmp_path)


def test_the_packaged_catalogue_is_valid() -> None:
    catalogue = schema.packaged()
    assert {r.id for r in catalogue.rigs} == {"studio-wave-xlr", "studio-deck", "generic-linux"}
    assert catalogue.fallback_rig().id == "generic-linux"
    assert {"wave-xlr", "scarlett-2i2-gen4", "scarlett-2i2-gen3", "arc-a380", "uhd-770", "cam-link-4k"} <= {
        d.id for d in catalogue.devices
    }


def test_the_packaged_wave_xlr_profile_holds_what_the_probe_recorded() -> None:
    wave = schema.packaged().device("wave-xlr")
    assert wave.usb == ("0fd9:007d",)
    assert wave.audio.playback_rates == wave.audio.capture_rates == (48000, 96000)
    assert wave.audio.formats == ("S24_3LE",) and wave.audio.preferred_rate == 48000


def test_a_minimal_catalogue_loads(tmp_path: Path) -> None:
    catalogue = load(tmp_path, a=DEVICE + GPU + RIG)
    assert [d.id for d in catalogue.devices] == ["mic", "gpu-a"]
    assert catalogue.rig("studio").gpu_candidates("encode") == ("gpu-a",)
    assert catalogue.rig("studio").gpu_candidates("grade") == ()


def test_files_are_merged_in_name_order(tmp_path: Path) -> None:
    catalogue = load(tmp_path, a=DEVICE, b=GPU + RIG)
    assert [d.id for d in catalogue.devices] == ["mic", "gpu-a"]


def test_the_schema_version_must_match(tmp_path: Path) -> None:
    (tmp_path / "x.toml").write_text("schema = 2\n" + DEVICE)
    with pytest.raises(ProfileError, match=r"x\.toml: schema must be 1, found 2"):
        schema.load(tmp_path)


def test_a_missing_schema_version_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "x.toml").write_text(DEVICE)
    with pytest.raises(ProfileError, match="found None"):
        schema.load(tmp_path)


def test_invalid_toml_is_reported_with_the_file(tmp_path: Path) -> None:
    (tmp_path / "x.toml").write_text("schema = 1\n[[device\n")
    with pytest.raises(ProfileError, match=r"x\.toml: not valid TOML"):
        schema.load(tmp_path)


def test_an_empty_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="no hardware profiles"):
        schema.load(tmp_path)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('label = "A Mic"', 'label = "A Mic"\nbogus = 1', r"device\[0\]\.bogus: is not a known key"),
        ('driver = "wave-xlr"', 'driver = "wave-xlr"\nbogus = 1', r"audio\.bogus: is not a known key"),
        ('usb = ["1234:5678"]', 'usb = ["1234:5678"]\nbogus = 1', r"match\.bogus: is not a known key"),
        ('usb = ["1234:5678"]', 'usb = ["12345678"]', r"is not in the expected form"),
        ('id = "mic"', 'id = "Mic"', r"must be lowercase words"),
        ('class = "audio-interface"', 'class = "toaster"', r"is not one of"),
        ('label = "A Mic"', "", r"label: is required"),
        ("capture_rates = [48000, 96000]", 'capture_rates = [48000, "x"]', r"must be a list of whole numbers"),
        ("capture_rates = [48000, 96000]", "capture_rates = [48000, 100]", r"between 8000"),
        ("preferred_rate = 48000", "preferred_rate = 44100", r"not one of the interface's rates"),
        ('formats = ["S24_3LE"]', 'formats = ["s24le"]', r"not in the expected form"),
        ('formats = ["S24_3LE"]', 'formats = ["S24_3LE", "S24_3LE"]', r"repeated entry"),
        ('usb = ["1234:5678"]', "", r"at least one of usb, pci or names"),
        ('driver = "wave-xlr"', "driver = 7", r"driver: must be a string"),
        ('class = "audio-interface"', 'class = "light"', r"audio: does not apply to a light"),
    ],
)
def test_a_bad_device_is_rejected_at_the_key(tmp_path: Path, old: str, new: str, message: str) -> None:
    text = DEVICE.replace(old, new)
    assert text != DEVICE
    with pytest.raises(ProfileError, match=message):
        load(tmp_path, a=text + RIG.split("[[rig]]")[0] + "[[rig]]\nid = 'plain'\nlabel = 'P'\nfallback = true\n")


def test_an_audio_interface_needs_its_audio_table(tmp_path: Path) -> None:
    without = DEVICE.split("[device.audio]")[0]
    with pytest.raises(ProfileError, match="audio: is required for a audio-interface"):
        load(tmp_path, a=without + RIG.split("[[rig]]")[0] + "[[rig]]\nid = 'plain'\nlabel = 'P'\nfallback = true\n")


def test_unknown_keys_are_rejected_at_the_top_level(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="bogus: is not a known key"):
        load(tmp_path, a=DEVICE + RIG + "\nbogus = 1\n")


def test_unknown_keys_are_rejected_in_a_rig(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match=r"rig\[0\]\.bogus"):
        load(tmp_path, a=DEVICE + GPU + RIG.replace('label = "Studio"', 'label = "Studio"\nbogus = true'))


def test_unknown_keys_are_rejected_in_an_encoder(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match=r"encoder\[0\]\.bogus"):
        load(tmp_path, a=DEVICE + GPU.replace('codec = "av1"', 'codec = "av1"\nbogus = 1') + RIG)


def test_duplicate_ids_are_rejected_within_and_across_files(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="'mic' is already used by a device"):
        load(tmp_path, a=DEVICE + DEVICE + RIG)
    with pytest.raises(ProfileError, match="'mic' is already used"):
        load(tmp_path, a=DEVICE, b=DEVICE + RIG)


def test_a_rig_may_not_share_an_id_with_a_device(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="'mic' is already used by a device"):
        load(tmp_path, a=DEVICE + RIG.replace('id = "studio"', 'id = "mic"'))


def test_a_rig_must_name_real_devices(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="expects names 'mic', which is not a device profile"):
        load(tmp_path, a=RIG)


def test_a_rig_preference_must_have_the_right_class(tmp_path: Path) -> None:
    text = RIG.replace('audio_interface = ["mic"]', 'audio_interface = ["gpu-a"]')
    with pytest.raises(ProfileError, match="a gpu, not a audio-interface"):
        load(tmp_path, a=DEVICE + GPU + text)


def test_a_gpu_preference_must_fit_the_role(tmp_path: Path) -> None:
    text = RIG.replace('encode = ["gpu-a"]', 'decode = ["gpu-a"]')
    with pytest.raises(ProfileError, match="not fit for decode"):
        load(tmp_path, a=DEVICE + GPU + text)


def test_exactly_one_fallback_rig(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="exactly one rig must be the fallback, found 0"):
        load(tmp_path, a=DEVICE + GPU + RIG.replace("fallback = true", ""))
    two = RIG + '[[rig]]\nid = "plain2"\nlabel = "P2"\nfallback = true\n'
    with pytest.raises(ProfileError, match="found 2"):
        load(tmp_path, a=DEVICE + GPU + two)


def test_the_fallback_rig_expects_nothing(tmp_path: Path) -> None:
    text = RIG.replace(
        'id = "plain"\nlabel = "Plain"', 'id = "plain"\nlabel = "Plain"\nmachine_ids = ["' + "0" * 32 + '"]'
    )
    with pytest.raises(ProfileError, match="fallback rig expects nothing"):
        load(tmp_path, a=DEVICE + text)


def test_machine_ids_must_be_hashes(tmp_path: Path) -> None:
    text = RIG.replace('label = "Studio"', 'label = "Studio"\nmachine_ids = ["my-hostname"]')
    with pytest.raises(ProfileError, match="machine_ids: 'my-hostname' is not in the expected form"):
        load(tmp_path, a=DEVICE + text)


def test_gpu_encoder_rules(tmp_path: Path) -> None:
    for old, new, message in (
        ('entrypoint = "VAEntrypointEncSliceLP"', 'entrypoint = "VAEntrypointVLD"', "must be an encode entrypoint"),
        ('va_profile = "VAProfileAV1Profile0"', 'va_profile = "AV1"', "must be a VAProfile name"),
        ('codec = "av1"', 'codec = "divx"', "is not one of"),
        ('roles = ["grade", "encode"]', "roles = []", "must name at least one role"),
        ('roles = ["grade", "encode"]', 'roles = ["paint"]', "is not one of"),
        ('kind = "discrete"', 'kind = "quantum"', "is not one of"),
    ):
        with pytest.raises(ProfileError, match=message):
            load(tmp_path, a=DEVICE + GPU.replace(old, new) + RIG)


def test_a_file_that_cannot_be_read_is_reported(tmp_path: Path) -> None:
    (tmp_path / "bad.toml").write_bytes(b"\xff\xfe\x00 not utf8")
    with pytest.raises(ProfileError, match=r"bad\.toml: cannot be read"):
        schema.load(tmp_path)


def test_a_toplevel_table_must_be_a_table(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="expected a table"):
        load(tmp_path, a='device = ["x"]\n' + RIG)


def test_rigs_never_mention_a_host_name() -> None:
    from streamstack.config import data_dir

    for path in (data_dir() / "hardware").glob("*.toml"):
        text = path.read_text().lower()
        # comments may explain; no key or value may name a host
        for line in text.splitlines():
            stripped = line.split("#", 1)[0]
            assert "dm313red" not in stripped and "deskmeet1" not in stripped, f"{path.name}: {line}"
