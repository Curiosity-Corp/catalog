"""Which render node does each video job use: chosen from the Plan by PCI id and role, on both recorded hosts.

dm313red has the Arc alone as renderD128. deskmeet1 enumerates the UHD 770 first (renderD128) and the Arc second
(renderD129), so a rule like "first node" or "node 128" is right on one host and wrong on the other. Every case here
runs against the recorded inventories, never against the host the tests happen to run on.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest
from hardware_support import ARC_VA, HOSTS, IGPU_VA, FakeMachine, inventory_text

from streamstack import config
from streamstack.canon import bridge
from streamstack.engine import encode as engine_encode
from streamstack.engine import probe as engine_probe
from streamstack.engine import segment
from streamstack.engine.graph import Source, Spec, Tap, build_argv
from streamstack.hardware import inventory, schema
from streamstack.hardware.plan import Plan, resolve
from streamstack.hardware.roles import LivePlan, choose, generic_node
from streamstack.stream import probe as stream_probe
from streamstack.stream.ffcmd import EncoderChoice
from streamstack.stream.ffcmd import Source as StreamSource
from streamstack.stream.model import EncoderSettings
from streamstack.stream.probe import EncoderProbe, HostProbe, ProbeCache
from streamstack.stream.service import StreamService

CATALOGUE = schema.packaged()
ARC = "8086:56a5"
IGPU = "8086:a780"
EXPECTED = {
    "dm313red": {
        "engine": ("/dev/dri/renderD128", ARC),
        "blur": ("/dev/dri/renderD128", ARC),
        "stream": ("/dev/dri/renderD128", ARC),
        "canon": ("/dev/dri/renderD128", ARC),
    },
    "deskmeet1": {
        "engine": ("/dev/dri/renderD129", ARC),
        "blur": ("/dev/dri/renderD129", ARC),
        "stream": ("/dev/dri/renderD129", ARC),
        "canon": ("/dev/dri/renderD128", IGPU),
    },
}


def recorded(host: str, overrides: dict[str, str] | None = None) -> Plan:
    return resolve(inventory.snapshot_from_inventory(inventory_text(host)), CATALOGUE, overrides)


def pick(consumer: str, plan: Plan | None, configured: str = "", **kw) -> tuple[str, str | None]:
    choice = choose(consumer, plan, configured, **kw)
    return choice.path, choice.pci_id


@pytest.fixture
def machine(tmp_path: Path) -> FakeMachine:
    return FakeMachine(tmp_path)


def two_gpu_machine(machine: FakeMachine, *, arc: int = 129, igpu: int = 128) -> FakeMachine:
    machine.add_gpu(igpu, "0000:00:02.0", IGPU, "Intel Corporation Raptor Lake-S GT1 [UHD Graphics 770]", IGPU_VA)
    machine.add_gpu(arc, "0000:03:00.0", ARC, "Intel Corporation DG2 [Arc A380]", ARC_VA)
    return machine


# -- the Plan decides, per host ------------------------------------------------------------------------


@pytest.mark.parametrize("host", HOSTS)
def test_every_job_takes_the_node_of_its_role_on_each_recorded_host(host: str) -> None:
    plan = recorded(host)
    for consumer, expected in EXPECTED[host].items():
        assert pick(consumer, plan) == expected, consumer


@pytest.mark.parametrize("host", HOSTS)
def test_the_choice_names_the_profile_as_its_reason(host: str) -> None:
    for consumer in EXPECTED[host]:
        assert choose(consumer, recorded(host)).via == "rig"


def test_the_arc_only_av1_encode_is_chosen_even_when_the_igpu_enumerates_first(machine: FakeMachine) -> None:
    plan = resolve(two_gpu_machine(machine).snapshot(), CATALOGUE)
    assert pick("stream", plan) == ("/dev/dri/renderD129", ARC)
    assert pick("engine", plan) == ("/dev/dri/renderD129", ARC)
    assert plan.encoder.ffmpeg == "av1_vaapi"


def test_the_igpu_takes_the_canon_decode(machine: FakeMachine) -> None:
    plan = resolve(two_gpu_machine(machine).snapshot(), CATALOGUE, {"STREAMSTACK_RIG": "studio-deck"})
    assert pick("canon", plan) == ("/dev/dri/renderD128", IGPU)


def test_without_the_arc_the_igpu_enumerating_first_does_not_get_the_grade_or_the_encode(machine: FakeMachine) -> None:
    machine.add_gpu(128, "0000:00:02.0", IGPU, "Intel Corporation Raptor Lake-S GT1 [UHD Graphics 770]", IGPU_VA)
    plan = resolve(machine.snapshot(), CATALOGUE, {"STREAMSTACK_RIG": "studio-deck"})
    assert plan.gpu("encode") is None and plan.gpu("grade") is None
    assert plan.encoder.hardware is False
    # the encoder has no preferred node, so the stream's own trials decide (and find the CPU)
    assert choose("stream", plan).via == "none" and choose("stream", plan).path == ""
    # the engine falls back to the generic choice, as it did before profiles
    fake = machine.sys / "class" / "drm"
    assert choose("engine", plan, sysfs=fake, dev=Path("/dev/dri")).path == "/dev/dri/renderD128"
    assert pick("canon", plan) == ("/dev/dri/renderD128", IGPU)


def test_no_plan_gives_the_generic_choice_and_the_stream_no_preference(tmp_path: Path) -> None:
    assert pick("stream", None) == ("", None)
    assert choose("engine", None, sysfs=tmp_path / "none").path == "/dev/dri/renderD128"


@pytest.mark.parametrize("host", HOSTS)
def test_an_explicit_node_always_wins(host: str) -> None:
    plan = recorded(host)
    for consumer in EXPECTED[host]:
        choice = choose(consumer, plan, "/dev/dri/renderD77")
        assert (choice.path, choice.via) == ("/dev/dri/renderD77", "override")
    assert choose("canon", plan, "/dev/dri/renderD129").pci_id == ARC if host == "deskmeet1" else True


@pytest.mark.parametrize("host", HOSTS)
def test_the_plan_itself_honours_the_explicit_keys(host: str) -> None:
    node = "/dev/dri/renderD128"
    plan = recorded(host, {"STREAMSTACK_STREAM_RENDER_NODE": node, "STREAMSTACK_ENGINE_RENDER_NODE": node})
    assert plan.gpu("encode").node_path == node and plan.gpu("encode").via == "override"
    assert plan.gpu("grade").node_path == node and plan.gpu("blur").node_path == node


# -- the generic fallback ------------------------------------------------------------------------------


def sysfs_with(tmp_path: Path, nodes: dict[int, tuple[str, str]]) -> Path:
    """A /sys/class/drm with renderD<n> -> device -> a PCI directory named by its slot, vendor from the table."""
    root = tmp_path / "sys"
    for number, (slot, vendor) in nodes.items():
        pci = tmp_path / "pci" / slot
        pci.mkdir(parents=True, exist_ok=True)
        (pci / "vendor").write_text(vendor + "\n")
        entry = root / "class" / "drm" / f"renderD{number}"
        entry.mkdir(parents=True)
        (entry / "device").symlink_to(pci)
    return root / "class" / "drm"


def test_the_generic_choice_takes_a_discrete_intel_gpu_before_the_integrated_one(tmp_path: Path) -> None:
    drm = sysfs_with(tmp_path, {128: ("0000:00:02.0", "0x8086"), 129: ("0000:03:00.0", "0x8086")})
    assert generic_node(drm, Path("/dev/dri")) == "/dev/dri/renderD129"


def test_the_generic_choice_takes_the_integrated_gpu_when_it_is_the_only_intel_one(tmp_path: Path) -> None:
    drm = sysfs_with(tmp_path, {128: ("0000:01:00.0", "0x10de"), 129: ("0000:00:02.0", "0x8086")})
    assert generic_node(drm, Path("/dev/dri")) == "/dev/dri/renderD129"


def test_the_generic_choice_takes_the_first_node_without_an_intel_gpu(tmp_path: Path) -> None:
    drm = sysfs_with(tmp_path, {130: ("0000:02:00.0", "0x1002"), 128: ("0000:01:00.0", "0x10de")})
    assert generic_node(drm, Path("/dev/dri")) == "/dev/dri/renderD128"
    assert generic_node(tmp_path / "missing", Path("/dev/dri")) == "/dev/dri/renderD128"


def test_the_engine_probe_function_delegates_to_the_same_choice(tmp_path: Path) -> None:
    drm = sysfs_with(tmp_path, {128: ("0000:00:02.0", "0x8086"), 129: ("0000:03:00.0", "0x8086")})
    assert engine_probe.render_node("", drm, Path("/dev/dri")) == "/dev/dri/renderD129"
    assert engine_probe.render_node("/dev/dri/renderD200", drm) == "/dev/dri/renderD200"
    plan = recorded("deskmeet1")
    assert engine_probe.render_node("", drm, Path("/dev/dri"), plan) == "/dev/dri/renderD129"


def test_the_canon_fallback_lists_the_discrete_gpu_first(tmp_path: Path) -> None:
    from streamstack.canon import encode as canon_encode

    drm = sysfs_with(tmp_path, {128: ("0000:00:02.0", "0x8086"), 129: ("0000:03:00.0", "0x8086")})
    assert [n for n, _ in canon_encode.render_nodes(drm, Path("/dev/dri"))] == [
        "/dev/dri/renderD129",
        "/dev/dri/renderD128",
    ]


# -- hot-plug: the node number moves, the role follows the PCI id ---------------------------------------


def live(machine: FakeMachine, **kw) -> LivePlan:
    return LivePlan(
        {},
        take=machine.snapshot,
        catalogue=CATALOGUE,
        sysfs=machine.sys / "class" / "drm",
        usb=machine.sys / "bus" / "usb" / "devices",
        video=machine.sys / "class" / "video4linux",
        **kw,
    )


def test_a_renumbered_arc_is_resolved_again(machine: FakeMachine) -> None:
    machine.add_gpu(128, "0000:03:00.0", ARC, "Intel Corporation DG2 [Arc A380]", ARC_VA)
    plan = live(machine)
    assert pick("engine", plan.get()) == ("/dev/dri/renderD128", ARC)
    machine.remove_gpu(128)
    machine.add_gpu(128, "0000:00:02.0", IGPU, "Intel Corporation Raptor Lake-S GT1 [UHD Graphics 770]", IGPU_VA)
    machine.add_gpu(129, "0000:03:00.0", ARC, "Intel Corporation DG2 [Arc A380]", ARC_VA)
    assert pick("engine", plan.get()) == ("/dev/dri/renderD129", ARC)
    assert pick("stream", plan.get()) == ("/dev/dri/renderD129", ARC)
    assert plan.probes == 2


def test_an_unchanged_machine_is_not_probed_again(machine: FakeMachine) -> None:
    two_gpu_machine(machine)
    plan = live(machine)
    first = plan.get()
    assert plan.get() is first and plan.probes == 1


def test_the_plan_is_refreshed_after_its_ttl(machine: FakeMachine) -> None:
    two_gpu_machine(machine)
    now = [0.0]
    plan = live(machine, ttl=60.0, clock=lambda: now[0])
    plan.get()
    now[0] = 61.0
    plan.get()
    assert plan.probes == 2


def test_a_failed_probe_keeps_the_last_plan_and_never_raises(machine: FakeMachine) -> None:
    two_gpu_machine(machine)
    calls = [0]

    def take():
        calls[0] += 1
        if calls[0] > 1:
            raise OSError("udev went away")
        return machine.snapshot()

    plan = LivePlan({}, take=take, catalogue=CATALOGUE, sysfs=machine.sys / "class" / "drm", ttl=0.0)
    first = plan.get()
    assert plan.get() is first
    broken = LivePlan({}, take=lambda: (_ for _ in ()).throw(OSError("no")), catalogue=CATALOGUE, ttl=0.0)
    assert broken.get() is None


# -- the video engine -----------------------------------------------------------------------------------


def test_the_engine_graph_is_built_on_the_chosen_node_and_pins_the_quick_sync_taps_to_it() -> None:
    source = Source("cam", "/dev/video42", "YUYV", 1280, 720, 30.0)
    tap = Tap("pipe:1", "av1_qsv")
    spec = Spec(source, "/dev/video43", 1280, 720, backend="vulkan", render_node="/dev/dri/renderD129", taps=(tap,))
    argv = build_argv(spec, {})
    assert "vaapi=va:/dev/dri/renderD129" in argv
    assert "qsv=qs:hw,child_device=/dev/dri/renderD129,child_device_type=vaapi" in argv
    plain = build_argv(dataclasses.replace(spec, taps=()), {})
    assert not any(a.startswith("qsv=") for a in plain)


def test_the_encoder_trial_can_name_the_node() -> None:
    assert engine_encode.qsv_device("/dev/dri/renderD129") == (
        "qsv=q:hw,child_device=/dev/dri/renderD129,child_device_type=vaapi"
    )
    assert engine_encode.qsv_device() == "qsv=q:hw"


@pytest.mark.parametrize("host", HOSTS)
def test_native_video_builds_its_graph_on_the_grade_node_of_each_host(tmp_path: Path, host: str) -> None:
    from test_console_native import CANON, CANON_SRC, FakeEngine, wait_for

    from streamstack.console.arcblur import ArcBlur
    from streamstack.console.native import NativeVideo

    plan = [recorded(host)]
    made: list[FakeEngine] = []
    probed: list[str] = []

    def factory(spec: Spec, look: dict | None = None, **kw) -> FakeEngine:
        made.append(FakeEngine(spec, look, **kw))
        return made[-1]

    cfg = {**config.DEFAULTS}
    video = NativeVideo(
        cfg,
        arc=ArcBlur(cfg, probe=lambda wanted: segment.Arc(False, "no openvino here")),
        state_file=tmp_path / "video.json",
        discover=lambda c: [CANON],
        resolve=lambda camera, c: CANON_SRC,
        engine_factory=factory,
        backend=lambda node: (probed.append(node), ("vulkan", ""))[1],
        plan=lambda: plan[0],
    )
    video.start()
    try:
        assert wait_for(lambda: made and made[0].running)
        assert made[0].spec.render_node == EXPECTED[host]["engine"][0]
        assert probed == [EXPECTED[host]["engine"][0]]
        if host == "deskmeet1":  # the Arc moves to another node: the running engine is rebuilt on it
            plan[0] = recorded_renumbered()
            video._tick()
            assert made[0].spec.render_node == "/dev/dri/renderD130"
            assert probed[-1] == "/dev/dri/renderD130"
    finally:
        video.stop()


def recorded_renumbered() -> Plan:
    """deskmeet1 with the Arc enumerated as renderD130 (a hot-plug renumbering)."""
    snap = inventory.snapshot_from_inventory(inventory_text("deskmeet1"))
    nodes = tuple(
        dataclasses.replace(n, path="/dev/dri/renderD130") if n.pci_id == ARC else n for n in snap.render_nodes
    )
    return resolve(dataclasses.replace(snap, render_nodes=nodes), CATALOGUE)


def test_a_setting_for_the_engine_node_beats_the_plan(tmp_path: Path) -> None:
    from test_console_native import CANON, CANON_SRC, FakeEngine, wait_for

    from streamstack.console.arcblur import ArcBlur
    from streamstack.console.native import NativeVideo

    made: list[FakeEngine] = []
    cfg = {**config.DEFAULTS, "STREAMSTACK_ENGINE_RENDER_NODE": "/dev/dri/renderD128"}
    video = NativeVideo(
        cfg,
        arc=ArcBlur(cfg, probe=lambda wanted: segment.Arc(False, "none")),
        state_file=tmp_path / "video.json",
        discover=lambda c: [CANON],
        resolve=lambda camera, c: CANON_SRC,
        engine_factory=lambda spec, look=None, **kw: made.append(FakeEngine(spec, look, **kw)) or made[-1],
        backend=lambda node: ("vulkan", ""),
        plan=lambda: recorded("deskmeet1"),
    )
    video.start()
    try:
        assert wait_for(lambda: made and made[0].running)
        assert made[0].spec.render_node == "/dev/dri/renderD128"
    finally:
        video.stop()


# -- blur -----------------------------------------------------------------------------------------------


class Core:
    def __init__(self, devices: dict[str, tuple[str, str, str]]) -> None:
        self.available_devices = ["CPU", *devices]
        self.devices = devices  # name -> (full name, type, GPU_DEVICE_ID)

    def get_property(self, device: str, key: str) -> str:
        name, kind, pci = self.devices[device]
        return {"FULL_DEVICE_NAME": name, "DEVICE_TYPE": kind, "GPU_DEVICE_ID": pci}[key]


def two_openvino_gpus() -> Core:
    return Core(
        {
            "GPU.0": ("Intel(R) UHD Graphics 770 (iGPU)", "Type.INTEGRATED", "0xa780"),
            "GPU.1": ("Intel(R) Arc(TM) A380 Graphics (dGPU)", "Type.DISCRETE", "0x56a5"),
        }
    )


@pytest.fixture
def openvino_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(segment.importlib.util, "find_spec", lambda name: object())


def test_blur_follows_the_blur_role_by_pci_id_not_by_gpu_index(openvino_present: None) -> None:
    arc = segment.probe(lambda: two_openvino_gpus(), pci_id=ARC)
    assert (arc.ok, arc.device) == (True, "GPU.1")
    # the same machine with the Arc enumerated first by OpenVINO
    swapped = Core(
        {
            "GPU.0": ("Intel(R) Arc(TM) A380 Graphics (dGPU)", "Type.DISCRETE", "0x56a5"),
            "GPU.1": ("Intel(R) UHD Graphics 770 (iGPU)", "Type.INTEGRATED", "0xa780"),
        }
    )
    assert segment.probe(lambda: swapped, pci_id=ARC).device == "GPU.0"


def test_blur_falls_back_to_the_discrete_gpu_when_openvino_cannot_say_which_is_which(openvino_present: None) -> None:
    class Bare(Core):
        def get_property(self, device: str, key: str) -> str:
            if key == "GPU_DEVICE_ID":
                raise RuntimeError("unsupported property")
            return super().get_property(device, key)

    assert segment.probe(lambda: Bare(two_openvino_gpus().devices), pci_id=ARC).device == "GPU.1"
    assert segment.probe(lambda: two_openvino_gpus()).device == "GPU.1"  # no role: discrete first, as before


def test_an_explicit_blur_device_beats_the_plan(openvino_present: None) -> None:
    assert segment.probe(lambda: two_openvino_gpus(), wanted="GPU.0", pci_id=ARC).device == "GPU.0"


def test_arcblur_asks_the_plan_for_the_blur_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    from streamstack.console.arcblur import ArcBlur

    seen: list[str] = []
    monkeypatch.setattr(
        segment,
        "probe",
        lambda core_factory=None, wanted="auto", pci_id="": seen.append(pci_id) or segment.Arc(False, "x"),
    )
    blur = ArcBlur(dict(config.DEFAULTS), plan=lambda: recorded("deskmeet1"))
    assert blur.wait_probed(5)
    assert seen == [ARC]
    seen.clear()
    manual = ArcBlur(
        {**config.DEFAULTS, "STREAMSTACK_ENGINE_RENDER_NODE": "/dev/dri/renderD128"}, plan=lambda: recorded("deskmeet1")
    )
    assert manual.wait_probed(5)
    assert seen == [""]


# -- the stream encoders --------------------------------------------------------------------------------

ENCODER_LIST = """Encoders:
 V....D av1_vaapi            AV1 (VAAPI)
 V....D hevc_vaapi           H.265 (VAAPI)
 V....D h264_vaapi           H.264 (VAAPI)
 V....D h264_qsv             H.264 (Intel Quick Sync)
 V....D libx264              libx264 H.264
"""


def trial_runner(igpu_cannot: tuple[str, ...] = ("av1_vaapi",)):
    """The UHD 770 cannot encode AV1; everything else works on both GPUs."""

    def run(argv: list[str], timeout: float) -> tuple[int, str]:
        if "-version" in argv:
            return 0, "ffmpeg version 8.0.1"
        if "-encoders" in argv:
            return 0, ENCODER_LIST
        if "-protocols" in argv or "-muxers" in argv or "protocol=rtmp" in argv:
            return 0, ""
        encoder = argv[argv.index("-c:v") + 1]
        if encoder in igpu_cannot and any("renderD128" in a for a in argv):
            return 1, "No usable encoding profile found"
        return 0, ""

    return run


PCI_OF = {"/dev/dri/renderD128": IGPU, "/dev/dri/renderD129": ARC}


def deskmeet_host(
    monkeypatch: pytest.MonkeyPatch, pci_of: dict[str, str] | None = None, cannot: tuple[str, ...] = ("av1_vaapi",)
) -> HostProbe:
    table = PCI_OF if pci_of is None else pci_of
    monkeypatch.setattr(os, "access", lambda path, mode: True)
    return stream_probe.probe_host(
        "ffmpeg", trial_runner(cannot), nodes=list(table), cores=16, pci_of=lambda node: table.get(node, "")
    )


def test_every_trial_remembers_the_gpu_it_ran_on(monkeypatch: pytest.MonkeyPatch) -> None:
    host = deskmeet_host(monkeypatch)
    assert {(e.device, e.pci_id) for e in host.encoders if e.backend == "vaapi"} == set(PCI_OF.items())
    assert all(e.pci_id == "" for e in host.encoders if e.backend == "software")
    assert next(e for e in host.encoders if e.name == "hevc_vaapi").to_dict()["pci_id"] == IGPU


def test_without_a_plan_the_first_node_that_works_encodes_h264_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    host = deskmeet_host(monkeypatch)
    assert host.best("h264").device == "/dev/dri/renderD128"
    assert host.best("av1").device == "/dev/dri/renderD129"  # only the Arc could do it


def test_the_planned_gpu_encodes_every_codec_when_it_can(monkeypatch: pytest.MonkeyPatch) -> None:
    host = deskmeet_host(monkeypatch)
    for codec in ("av1", "hevc", "h264"):
        assert host.best(codec, pci_id=ARC).device == "/dev/dri/renderD129"
    assert host.best("h264", pin="/dev/dri/renderD129").device == "/dev/dri/renderD129"


def test_the_planned_gpu_is_found_by_pci_id_even_after_the_node_moved(monkeypatch: pytest.MonkeyPatch) -> None:
    host = deskmeet_host(monkeypatch)
    assert host.best("h264", pin="/dev/dri/renderD77", pci_id=ARC).pci_id == ARC


def test_when_the_planned_gpu_cannot_do_it_the_first_working_encoder_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "access", lambda path, mode: True)
    host = stream_probe.probe_host(
        "ffmpeg",
        trial_runner(("hevc_vaapi",)),
        nodes=list(PCI_OF),
        cores=16,
        pci_of=lambda n: PCI_OF[n],
    )
    host.encoders = [e for e in host.encoders if not (e.name == "hevc_vaapi" and e.device.endswith("D129"))]
    assert host.best("hevc", pci_id=ARC) is None or host.best("hevc", pci_id=ARC).device != "/dev/dri/renderD129"


def service(tmp_path: Path, host: HostProbe, plan: Plan | None, **cfg: str) -> StreamService:
    return StreamService(
        {"STREAMSTACK_RECORD_DIR": str(tmp_path / "rec"), **cfg},
        probe=ProbeCache(lambda: host),
        state_path=tmp_path / "stream.json",
        source_override=StreamSource(kind="lavfi", width=1280, height=720, fps=30),
        plan=(lambda: plan) if plan is not None else None,
    )


@pytest.mark.parametrize(("host", "node"), [("dm313red", "/dev/dri/renderD128"), ("deskmeet1", "/dev/dri/renderD129")])
def test_the_stream_chooses_hevc_and_h264_on_the_planned_gpu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str, node: str
) -> None:
    table = PCI_OF if host == "deskmeet1" else {"/dev/dri/renderD128": ARC}
    svc = service(
        tmp_path, deskmeet_host(monkeypatch, table, () if host == "dm313red" else ("av1_vaapi",)), recorded(host)
    )
    for codec in ("h264", "hevc", "av1"):
        choice = svc._choose(codec, EncoderSettings())
        assert isinstance(choice, EncoderChoice) and choice.device == node, codec
    assert svc.encode_node().path == node


def test_a_stream_without_a_plan_keeps_the_old_first_node_behaviour(tmp_path: Path, monkeypatch) -> None:
    svc = service(tmp_path, deskmeet_host(monkeypatch), None)
    assert svc._choose("h264", EncoderSettings()).device == "/dev/dri/renderD128"
    assert svc._choose("av1", EncoderSettings()).device == "/dev/dri/renderD129"


def test_an_explicit_stream_node_beats_the_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    svc = service(
        tmp_path,
        deskmeet_host(monkeypatch),
        recorded("deskmeet1"),
        STREAMSTACK_STREAM_RENDER_NODE="/dev/dri/renderD128",
    )
    assert svc._choose("h264", EncoderSettings()).device == "/dev/dri/renderD128"


def test_a_pinned_encoder_runs_on_the_planned_gpu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    svc = service(tmp_path, deskmeet_host(monkeypatch), recorded("deskmeet1"))
    settings = EncoderSettings(encoder="h264_vaapi")
    assert svc._choose("h264", settings).device == "/dev/dri/renderD129"


def test_the_probe_cache_refreshes_when_the_gpus_change() -> None:
    calls = [0]
    key = [(("/dev/dri/renderD128", IGPU),)]
    cache = ProbeCache(
        lambda: (calls.__setitem__(0, calls[0] + 1), HostProbe(at=__import__("time").time()))[1], key=lambda: key[0]
    )
    cache.get()
    cache.get()
    assert calls[0] == 1
    key[0] = (("/dev/dri/renderD128", IGPU), ("/dev/dri/renderD129", ARC))
    cache.get()
    assert calls[0] == 2


def test_render_signature_pairs_each_node_with_its_gpu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    drm = tmp_path / "drm"
    for node, (vendor, device) in {"renderD128": ("0x8086", "0xa780"), "renderD129": ("0x8086", "0x56a5")}.items():
        (drm / node / "device").mkdir(parents=True)
        (drm / node / "device" / "vendor").write_text(vendor + "\n")
        (drm / node / "device" / "device").write_text(device + "\n")
    assert stream_probe.node_pci_id("/dev/dri/renderD129", drm) == ARC
    assert stream_probe.node_pci_id("/dev/dri/renderD99", drm) == ""
    monkeypatch.setattr(stream_probe, "render_nodes", lambda: ["/dev/dri/renderD128", "/dev/dri/renderD129"])
    assert stream_probe.render_signature(drm) == (("/dev/dri/renderD128", IGPU), ("/dev/dri/renderD129", ARC))


def test_an_encoder_probe_matches_by_node_or_pci_id() -> None:
    probe = EncoderProbe("h264_vaapi", "h264", "vaapi", "x", "/dev/dri/renderD129", ok=True, pci_id=ARC)
    assert probe.on("/dev/dri/renderD129") and probe.on("", ARC) and probe.on("/dev/dri/renderD9", ARC)
    assert not probe.on("/dev/dri/renderD128", IGPU) and not probe.on("", "")
    assert not EncoderProbe("libx264", "h264", "software", "x").on("/dev/dri/renderD128", ARC)


# -- the Canon bridge's decode node ---------------------------------------------------------------------


def canon_bridge(plan: Plan | None, **settings) -> bridge.Bridge:
    s = bridge.Settings(device="/dev/video42", canvas=(1920, 1080), **settings)
    return bridge.Bridge(
        s,
        make_session=lambda: None,
        nodes=lambda: [("/dev/dri/renderD128", "0x8086"), ("/dev/dri/renderD129", "0x8086")],
        plan=(lambda: plan) if plan is not None else None,
    )


@pytest.mark.parametrize("host", HOSTS)
def test_the_canon_decode_runs_on_the_decode_role_of_each_host(host: str) -> None:
    node, vendor = canon_bridge(recorded(host))._render_node()
    assert node == EXPECTED[host]["canon"][0] and vendor == "0x8086"


def test_the_canon_decode_without_a_plan_keeps_the_first_listed_node() -> None:
    assert canon_bridge(None)._render_node()[0] == "/dev/dri/renderD128"


def test_the_canon_render_node_setting_beats_the_plan() -> None:
    assert canon_bridge(recorded("deskmeet1"), render_node="/dev/dri/renderD129")._render_node()[0] == (
        "/dev/dri/renderD129"
    )


def test_the_canon_bridge_settings_take_the_camera_node_from_the_plan() -> None:
    plan = recorded("deskmeet1")
    assert bridge.Settings.from_config(dict(config.DEFAULTS), plan).device == "/dev/video42"
