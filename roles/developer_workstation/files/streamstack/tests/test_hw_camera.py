"""Cameras and virtual cameras by the hardware profiles: USB ids, loopback devices, the modprobe line."""

from __future__ import annotations

from pathlib import Path

import pytest
from hardware_support import HOSTS, FakeMachine, inventory_text

from streamstack import config, system
from streamstack.hardware import camera, inventory, loopback, schema
from streamstack.hardware.plan import resolve
from streamstack.hardware.schema import LoopbackSpec

CATALOGUE = schema.packaged()


def recorded(host: str, overrides: dict[str, str] | None = None):
    return resolve(inventory.snapshot_from_inventory(inventory_text(host)), CATALOGUE, overrides)


# -- USB ids ------------------------------------------------------------------------------------------


def test_every_cam_link_revision_is_matched() -> None:
    ids = camera.capture_card_ids()
    assert ("0fd9", "0067") in ids and ("0fd9", "0066") in ids
    assert ("0fd9", "0067") in system.capture_card_ids() and ("0fd9", "0066") in system.capture_card_ids()


@pytest.mark.parametrize("host", HOSTS)
def test_the_recorded_cam_link_and_canon_match_the_profile_ids(host: str) -> None:
    snap = inventory.snapshot_from_inventory(inventory_text(host))
    usb = {tuple(u.usb_id.split(":")) for u in snap.usb}
    assert usb & set(camera.capture_card_ids()), "the Cam Link on this host is matched by a profile id"
    if host == "dm313red":
        assert usb & set(camera.canon_ids()), "the Canon EOS 50D is matched by a profile id"


def test_the_ids_come_from_the_catalogue_not_from_code(tmp_path: Path) -> None:
    (tmp_path / "rigs.toml").write_text('schema = 1\n[[rig]]\nid = "r"\nlabel = "R"\nfallback = true\n')
    (tmp_path / "capture.toml").write_text(
        'schema = 1\n[[device]]\nid = "card-x"\nclass = "capture-card"\nlabel = "Card X"\n'
        '[device.match]\nusb = ["1234:abcd"]\n'
    )
    assert camera.capture_card_ids(schema.load(tmp_path)) == (("1234", "abcd"),)
    assert camera.usb_ids_of("nothing", schema.load(tmp_path)) == ()


def test_find_camlink_finds_a_card_by_each_known_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system, "CAMLINK_LINK", tmp_path / "no-such-link")
    for usb_id in ("0fd9:0066", "0fd9:0067"):
        machine = FakeMachine(tmp_path / usb_id.replace(":", "_"))
        node = machine.add_v4l2(0, "Cam Link 4K: Cam Link 4K", usb_id)
        (machine.sys / "class" / "video4linux" / "video0" / "index").write_text("0\n")
        found = system.video_by_usb_id(system.capture_card_ids(), machine.sys / "class" / "video4linux")
        assert found == [node.replace("/dev/", "/dev/")] or found == ["/dev/video0"]


def test_canon_products_match_by_profile_id_then_by_vendor(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[tuple[str, str], ...]] = []
    monkeypatch.setattr(system, "usb_products_by_id", lambda ids: (seen.append(ids), ["Canon EOS 50D"])[1])
    assert system.canon_products() == ["Canon EOS 50D"]
    assert seen == [(("04a9", "319b"),)]
    monkeypatch.setattr(system, "usb_products_by_id", lambda ids: [])
    monkeypatch.setattr(system, "usb_products", lambda vendor: [f"vendor {vendor}"])
    assert system.canon_products() == ["vendor 04a9"]


# -- which /dev/video the cameras are --------------------------------------------------------------------


@pytest.mark.parametrize("host", HOSTS)
def test_the_virtual_cameras_are_found_by_label_on_each_host(host: str) -> None:
    plan = recorded(host)
    assert camera.device_for("camera", {}, plan) == "/dev/video42"
    assert camera.device_for("program", {}, plan) == "/dev/video43"


def test_the_profile_number_is_used_when_the_node_does_not_exist_yet() -> None:
    plan = recorded("dm313red")
    gone = type(plan.loopback)(**{**plan.loopback.__dict__, "camera_path": None, "program_path": None})
    plan = type(plan)(**{**plan.__dict__, "loopback": gone})
    assert camera.device_for("program", {}, plan) == "/dev/video43"
    assert camera.device_for("camera", {}, plan) == "/dev/video42"


def test_a_renumbered_loopback_is_followed_by_its_label(tmp_path: Path) -> None:
    machine = FakeMachine(tmp_path)
    machine.add_loopback_module("devices=2 video_nr=10,11")
    machine.add_v4l2(10, "StreamStack Camera", loopback=True)
    machine.add_v4l2(11, "Canon EOS 50D", loopback=True)
    plan = resolve(machine.snapshot(), CATALOGUE)
    assert camera.device_for("program", {}, plan) == "/dev/video10"
    assert camera.device_for("camera", {}, plan) == "/dev/video11"


def test_an_explicit_device_beats_the_plan_and_the_default_is_the_last_resort() -> None:
    plan = recorded("dm313red")
    assert camera.device_for("program", {"STREAMSTACK_PROGRAM_DEVICE": "/dev/video99"}, plan) == "/dev/video99"
    assert camera.device_for("program", dict(config.DEFAULTS), plan) == "/dev/video43"
    assert camera.device_for("program", {}, None) == config.DEFAULTS["STREAMSTACK_PROGRAM_DEVICE"]
    assert camera.device_for("camera", {"STREAMSTACK_CAMERA_DEVICE": "/dev/video7"}, None) == "/dev/video7"


# -- the loopback spec ---------------------------------------------------------------------------------


def packaged_conf_options() -> str:
    text = (Path(__file__).parent.parent / "packaging" / "modprobe.d" / "streamstack-v4l2loopback.conf").read_text()
    lines = [ln for ln in text.splitlines() if ln.startswith("options v4l2loopback ")]
    assert len(lines) == 1
    return lines[0].removeprefix("options v4l2loopback ")


@pytest.mark.parametrize("rig", ["studio-wave-xlr", "studio-deck", "generic-linux"])
def test_the_shipped_modprobe_file_is_what_every_rig_profile_produces(rig: str) -> None:
    spec = CATALOGUE.rig(rig).loopback
    assert spec is not None
    assert spec.modprobe_options() == packaged_conf_options()


def test_the_spec_names_the_devices_by_role() -> None:
    spec = CATALOGUE.rig("studio-deck").loopback
    assert (spec.device("camera").video_nr, spec.device("camera").card_label) == (42, "Canon EOS 50D")
    assert (spec.device("program").video_nr, spec.device("program").card_label) == (43, "StreamStack Camera")
    assert spec.device("other") is None
    assert all(d.exclusive_caps for d in spec.devices)


def test_a_profile_with_other_numbers_and_labels_changes_the_options() -> None:
    from streamstack.hardware.schema import LoopbackDevice

    spec = LoopbackSpec((LoopbackDevice("program", 20, "Studio Cam", False),), 4)
    assert spec.modprobe_options() == 'devices=1 video_nr=20 card_label="Studio Cam" exclusive_caps=0 max_buffers=4'


def rigs_text(devices: str, extra: str = "") -> dict[str, str]:
    return {"rigs": f'[[rig]]\nid = "r"\nlabel = "R"\nfallback = true\n[rig.loopback]\n{extra}{devices}'}


def load(tmp_path: Path, devices: str, extra: str = ""):
    (tmp_path / "rigs.toml").write_text("schema = 1\n" + rigs_text(devices, extra)["rigs"])
    return schema.load(tmp_path)


DEV = '[[rig.loopback.device]]\nrole = "{role}"\nvideo_nr = {nr}\ncard_label = "{label}"\n'


def test_a_valid_loopback_block_loads(tmp_path: Path) -> None:
    text = DEV.format(role="camera", nr=1, label="A") + DEV.format(role="program", nr=2, label="B")
    spec = load(tmp_path, text, "max_buffers = 3\n").rig("r").loopback
    assert [d.video_nr for d in spec.devices] == [1, 2] and spec.max_buffers == 3


@pytest.mark.parametrize(
    ("devices", "message"),
    [
        (DEV.format(role="camera", nr=1, label="A") + DEV.format(role="camera", nr=2, label="B"), "repeated role"),
        (DEV.format(role="camera", nr=1, label="A") + DEV.format(role="program", nr=1, label="B"), "repeated video_nr"),
        (
            DEV.format(role="camera", nr=1, label="A") + DEV.format(role="program", nr=2, label="A"),
            "repeated card_label",
        ),
        (DEV.format(role="camera", nr=1, label="A,B"), "comma"),
        (DEV.format(role="mic", nr=1, label="A"), "not one of"),
        ('[[rig.loopback.device]]\nrole = "camera"\ncard_label = "A"\n', "video_nr"),
        (DEV.format(role="camera", nr=999, label="A"), "video_nr"),
        ("", "at least one device"),
    ],
)
def test_a_bad_loopback_block_is_refused_with_the_reason(tmp_path: Path, devices: str, message: str) -> None:
    with pytest.raises(schema.ProfileError, match=message):
        load(tmp_path, devices)


def test_max_buffers_stays_in_range(tmp_path: Path) -> None:
    with pytest.raises(schema.ProfileError, match="max_buffers"):
        load(tmp_path, DEV.format(role="camera", nr=1, label="A"), "max_buffers = 1\n")


# -- plan: module options ------------------------------------------------------------------------------


def test_the_plan_compares_the_loaded_options_with_the_profile(tmp_path: Path) -> None:
    machine = FakeMachine(tmp_path)
    machine.add_loopback_module(CATALOGUE.rig("generic-linux").loopback.modprobe_options())
    assert resolve(machine.snapshot(), CATALOGUE).loopback.options_match is True
    other = FakeMachine(tmp_path / "other")
    other.add_loopback_module("devices=1 video_nr=9")
    plan = resolve(other.snapshot(), CATALOGUE)
    assert plan.loopback.options_match is False and plan.loopback.module_loaded


# -- streamstack loopback status / apply ----------------------------------------------------------------


def rig_and_plan(host: str = "dm313red"):
    plan = recorded(host)
    return plan, CATALOGUE.rig(plan.rig.id)


def test_status_reports_ok_when_the_nodes_exist_under_their_labels() -> None:
    plan, rig = rig_and_plan()
    status = loopback.status(plan, rig)
    assert [(r.role, r.state, r.path) for r in status.roles] == [
        ("camera", "ok", "/dev/video42"),
        ("program", "ok", "/dev/video43"),
    ]
    assert status.ok and any("wanted options" in ln for ln in status.lines())


def test_status_tells_missing_from_mislabelled(tmp_path: Path) -> None:
    machine = FakeMachine(tmp_path)
    machine.add_loopback_module("devices=1 video_nr=43")
    plan = resolve(machine.snapshot(), CATALOGUE)
    rig = CATALOGUE.rig(plan.rig.id)
    missing = loopback.status(plan, rig)
    assert {r.state for r in missing.roles} == {"missing"} and not missing.ok
    odd = loopback.status(plan, rig, {43: "Dummy video device"})
    assert {r.role: r.state for r in odd.roles} == {"camera": "missing", "program": "mislabelled"}


def test_apply_writes_both_files_once_and_nothing_the_second_time(tmp_path: Path) -> None:
    plan, rig = rig_and_plan()
    first = loopback.apply(plan, rig, root=tmp_path, create_nodes=False)
    assert len(first) == 2 and all(line.startswith("wrote ") for line in first)
    conf = (tmp_path / loopback.MODPROBE).read_text()
    assert f"options v4l2loopback {rig.loopback.modprobe_options()}\n" in conf
    assert (tmp_path / loopback.MODULES_LOAD).read_text() == "v4l2loopback\n"
    again = loopback.apply(plan, rig, root=tmp_path, create_nodes=False)
    assert again == ["nothing to do: the files and the nodes already match the profile"]


def test_apply_with_dry_run_changes_nothing(tmp_path: Path) -> None:
    plan, rig = rig_and_plan()
    lines = loopback.apply(plan, rig, root=tmp_path, dry_run=True)
    assert all(line.startswith("would ") for line in lines)
    assert [p for p in tmp_path.rglob("*") if "etc" in p.parts] == []


def test_apply_replaces_a_stale_file(tmp_path: Path) -> None:
    plan, rig = rig_and_plan()
    path = tmp_path / loopback.MODPROBE
    path.parent.mkdir(parents=True)
    path.write_text("options v4l2loopback devices=1\n")
    assert loopback.apply(plan, rig, root=tmp_path, create_nodes=False)[0].startswith("wrote ")
    assert path.read_text() == loopback.modprobe_text(rig.loopback)


def test_apply_explains_an_unwritable_root(tmp_path: Path) -> None:
    plan, rig = rig_and_plan()
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(loopback.LoopbackError, match="cannot"):
        loopback.apply(plan, rig, root=blocker, create_nodes=False)


def loaded_module_without_nodes(tmp_path: Path):
    machine = FakeMachine(tmp_path)
    machine.add_loopback_module("devices=1 video_nr=9")
    plan = resolve(machine.snapshot(), CATALOGUE)
    return plan, CATALOGUE.rig(plan.rig.id)


def test_apply_creates_a_missing_node_on_a_loaded_module(tmp_path: Path) -> None:
    plan, rig = loaded_module_without_nodes(tmp_path / "m")
    calls: list[list[str]] = []
    lines = loopback.apply(
        plan,
        rig,
        root=tmp_path / "root",
        run=lambda argv: (calls.append(argv), (0, ""))[1],
        which=lambda name: "/usr/bin/" + name,
    )
    assert ["v4l2loopback-ctl", "add", "-n", "StreamStack Camera", "-x", "1", "/dev/video43"] in calls
    assert ["v4l2loopback-ctl", "add", "-n", "Canon EOS 50D", "-x", "1", "/dev/video42"] in calls
    assert sum(line.startswith("created ") for line in lines) == 2


def test_apply_says_so_when_the_tool_is_missing_and_surfaces_a_failure(tmp_path: Path) -> None:
    plan, rig = loaded_module_without_nodes(tmp_path / "m")
    lines = loopback.apply(plan, rig, root=tmp_path / "r1", which=lambda name: None)
    assert any("v4l2loopback-ctl is not installed" in line for line in lines)
    with pytest.raises(loopback.LoopbackError, match="failed: busy"):
        loopback.apply(
            plan, rig, root=tmp_path / "r2", run=lambda argv: (1, "busy"), which=lambda name: "/usr/bin/" + name
        )


def test_apply_never_creates_nodes_for_ones_that_exist() -> None:
    plan, rig = rig_and_plan()
    calls: list[list[str]] = []
    loopback.apply(plan, rig, root=Path("/nonexistent-root-for-test"), dry_run=True, run=calls.append)
    assert calls == []


def test_a_rig_without_loopback_cannot_be_applied() -> None:
    import dataclasses

    plan, rig = rig_and_plan()
    with pytest.raises(loopback.LoopbackError, match="declares no virtual cameras"):
        loopback.apply(plan, dataclasses.replace(rig, loopback=None), dry_run=True)
