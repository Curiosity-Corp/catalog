"""StreamStack low-latency tuning: PipeWire, realtime limits, USB power, Intel media.

Everything is hardware-gated by id or node name, so these tests render the
role's templates and run the one shell script against a fake sysfs; nothing
touches the machine running the tests.
"""

import ast
import re
import shutil
import subprocess
from pathlib import Path

import jinja2
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
TASKS = yaml.safe_load((ROLE / "tasks/streamstack-low-latency.yml").read_text())
MAIN = yaml.safe_load((ROLE / "tasks/main.yml").read_text())


def _environment() -> jinja2.Environment:
    from ansible.plugins.filter.core import FilterModule
    from ansible.plugins.filter.mathstuff import FilterModule as MathFilterModule
    from ansible.plugins.test.core import TestModule

    env = jinja2.Environment()
    env.filters.update(FilterModule().filters())
    env.filters.update(MathFilterModule().filters())
    env.tests.update(TestModule().tests())
    return env


def _task(name: str) -> dict:
    return next(t for t in _flatten(TASKS) if t["name"] == name)


def _flatten(tasks: list[dict]) -> list[dict]:
    flat = []
    for task in tasks:
        flat.append(task)
        flat.extend(_flatten(task.get("block", [])))
    return flat


def _render(text: str, **extra) -> str:
    context = {k: v for k, v in DEFAULTS.items() if k.startswith("streamstack_")}
    context.update(extra)
    return _environment().from_string(text).render(**context)


def _active(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def test_main_includes_low_latency_only_for_opted_in_streamstack_desktops() -> None:
    (task,) = [
        t for t in MAIN
        if isinstance(t.get("ansible.builtin.include_tasks"), dict)
        and t["ansible.builtin.include_tasks"]["file"] == "streamstack-low-latency.yml"
    ]
    assert "streamstack-low-latency" in task["tags"]
    assert "streamstack-low-latency" in task["ansible.builtin.include_tasks"]["apply"]["tags"]
    for condition in ("workstation_profile == 'desktop'", "streamstack_enabled | bool", "streamstack_low_latency | bool"):
        assert condition in task["when"]
    assert DEFAULTS["streamstack_low_latency"] is True


def test_pipewire_clock_is_48k_quantum_256_with_128_allowed() -> None:
    conf = _render(_task("Set the PipeWire graph clock for low latency")["ansible.builtin.copy"]["content"])
    values = dict(re.findall(r"(default\.clock\.[\w-]+)\s*=\s*(.+)", conf))
    assert values["default.clock.rate"] == "48000"
    assert values["default.clock.allowed-rates"] == "[ 48000 ]"
    assert values["default.clock.quantum"] == "256"
    assert values["default.clock.min-quantum"] == "128"
    assert values["default.clock.max-quantum"] == "1024"
    pulse = _render(_task("Match the PulseAudio clients (browsers, Teams, Zoom) to the graph clock")["ansible.builtin.copy"]["content"])
    assert "pulse.default.req = 256/48000" in pulse
    assert "pulse.min.req     = 128/48000" in pulse


_SPA_ESCAPES = set('"\\/bfnrtu')


def _assert_valid_spa_json(text: str) -> None:
    """Fail on what WirePlumber's SPA-JSON parser rejects in these drop-ins.

    WirePlumber drops a whole file ("section 'monitor.alsa.rules' has no value")
    when a string holds an escape JSON does not define, such as a regex `\\.`.
    The real parser (`spa-json-dump`, shipped with PipeWire) runs too when the
    machine has it; the string/bracket check below always runs.
    """
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    depth = 0
    in_string = False
    index = 0
    while index < len(body):
        char = body[index]
        if in_string:
            if char == "\\":
                assert body[index + 1] in _SPA_ESCAPES, f"invalid escape \\{body[index + 1]} in SPA-JSON string"
                index += 1
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            assert depth >= 0, "unbalanced closing bracket"
        index += 1
    assert not in_string and depth == 0, "unterminated string or bracket"
    dump = shutil.which("spa-json-dump")
    if dump:
        result = subprocess.run([dump], input=text, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr or result.stdout


def test_alsa_rules_match_by_node_name_and_never_suspend() -> None:
    conf = _render(_task("Pin the audio interface ALSA period and keep it awake")["ansible.builtin.copy"]["content"])
    _assert_valid_spa_json(conf)
    nodes = re.findall(r'node\.name = "~([^"]+)"', conf)
    assert len(nodes) == 2
    # No backslash at all: a `\.` escape is a parse error in WirePlumber's SPA-JSON.
    assert not any("\\" in n for n in nodes)
    scarlett = re.compile(nodes[0])
    assert scarlett.match("alsa_input.usb-Focusrite_Scarlett_2i2_USB_Y8ABCDE-00.HiFi__Mic1__source")
    assert not scarlett.match("alsa_output.pci-0000_03_00.1.hdmi-stereo")
    wave = re.compile(nodes[1])
    assert wave.match("alsa_output.usb-Elgato_Systems_Elgato_Wave_XLR_DS23M2A00049-00.analog-stereo")
    assert not wave.match("alsa_output_usb-Elgato_Systems_Elgato_Wave_XLR")
    for key, value in (
        ("session.suspend-timeout-seconds", "0"),
        ("node.pause-on-idle", "false"),
        ("api.alsa.period-size", "128"),
        ("api.alsa.headroom", "64"),
    ):
        assert len(re.findall(rf"{re.escape(key)} = {value}\b", conf)) == 2, key
    assert conf.count("{") == conf.count("}")


def test_spa_json_check_rejects_the_incident_file() -> None:
    broken = 'monitor.alsa.rules = [ { matches = [ { node.name = "~alsa_(input|output)\\.usb-X_.*" } ] } ]'
    with pytest.raises(AssertionError):
        _assert_valid_spa_json(broken)


def test_wireplumber_patterns_are_escape_free_and_validated() -> None:
    patterns = DEFAULTS["streamstack_audio_node_patterns"] + DEFAULTS["streamstack_audio_card_patterns"]
    assert patterns and not any("\\" in p or '"' in p for p in patterns)
    assert any("streamstack_audio_card_patterns" in str(c) for c in _task("Validate the StreamStack low-latency settings")["ansible.builtin.assert"]["that"])


def _users(*present: str, absent: tuple[str, ...] = ()) -> list[dict]:
    results = [
        {"item": {"name": n}, "ansible_facts": {"getent_passwd": {n: ["x", str(1000 + i), str(1000 + i), "", f"/home/{n}", "/bin/bash"]}}}
        for i, n in enumerate(present)
    ]
    results += [{"item": {"name": n}, "ansible_facts": {"getent_passwd": {n: None}}} for n in absent]
    return results


def _excluded(users: list[dict], **extra) -> list[str]:
    ctx = {"_streamstack_rt_users": {"results": users}}
    ctx.update({k: v for k, v in DEFAULTS.items() if k.startswith("streamstack_")})
    ctx.update(extra)
    env = _environment()
    for name in ("Decide which users must not manage the studio audio cards", "Choose the accounts that get the studio audio exclusion"):
        for key, template in _task(name)["ansible.builtin.set_fact"].items():
            ctx[key] = ast.literal_eval(env.from_string(template).render(**ctx).strip())
    return sorted(ctx["_streamstack_audio_excluded"])


def test_only_the_streamstack_owner_keeps_the_studio_audio_cards() -> None:
    assert DEFAULTS["streamstack_audio_owner_users"] == ["mrh"]
    assert _excluded(_users("mrh", "seantech")) == ["seantech"]
    # A user that does not exist on the host is never touched.
    assert _excluded(_users("mrh", absent=("seantech",))) == []
    # An owner name that is not on the host must not silence everyone.
    assert _excluded(_users("alice", "bob")) == []
    assert _excluded(_users("mrh", "seantech"), streamstack_audio_exclusive=False) == []
    assert _excluded(_users("mrh", "seantech", "guest"), streamstack_audio_owner_users=["mrh", "seantech"]) == ["guest"]


def test_exclusion_drop_in_disables_only_the_studio_cards_and_parses() -> None:
    task = _task("Keep the studio audio cards out of the other users' WirePlumber")["ansible.builtin.copy"]
    conf = _render(task["content"])
    _assert_valid_spa_json(conf)
    assert task["dest"].endswith("/.config/wireplumber/wireplumber.conf.d/50-curiosity-streamstack-exclusive-audio.conf")
    cards = [re.compile(c) for c in re.findall(r'device\.name = "~([^"]+)"', conf)]
    assert len(cards) == 2 and conf.count("device.disabled = true") == 2
    assert any(c.match("alsa_card.usb-Elgato_Systems_Elgato_Wave_XLR_DS23M2A00049-00") for c in cards)
    assert any(c.match("alsa_card.usb-Elgato_Cam_Link_4K_00066EBB11000-03") for c in cards)
    assert any(c.match("alsa_card.usb-Focusrite_Scarlett_2i2_USB_Y8ABCDE-00") for c in cards)
    assert not any(c.match("alsa_card.pci-0000_00_1f.3") for c in cards)


def test_overrides_flow_into_the_rendered_files() -> None:
    conf = _render(
        _task("Set the PipeWire graph clock for low latency")["ansible.builtin.copy"]["content"],
        streamstack_audio_quantum=128, streamstack_audio_min_quantum=64,
    )
    assert "default.clock.quantum       = 128" in conf
    assert "default.clock.min-quantum   = 64" in conf


def test_settings_validation_rejects_bad_quantum_order_and_usb_ids() -> None:
    checks = _task("Validate the StreamStack low-latency settings")["ansible.builtin.assert"]["that"]
    env = _environment()

    def passes(**extra) -> bool:
        context = {k: v for k, v in DEFAULTS.items() if k.startswith("streamstack_")}
        context.update(extra)
        return all(env.compile_expression(c)(**context) for c in checks)

    assert passes()
    assert not passes(streamstack_audio_min_quantum=512)
    assert not passes(streamstack_audio_quantum=2048)
    assert not passes(streamstack_usb_no_autosuspend_ids=["1235"])
    assert not passes(streamstack_usb_no_autosuspend_ids=["1235:*; rm"])
    assert not passes(streamstack_media_package_state="absent")
    assert not passes(streamstack_kernel_args=["quiet splash"])
    assert not passes(streamstack_realtime_rtprio=0)
    assert passes(streamstack_kernel_args=["threadirqs", "preempt=full"])


def test_realtime_limits_cover_the_audio_group_and_users_are_never_created() -> None:
    limits = _render(_task("Grant the audio group realtime priority and locked memory")["ansible.builtin.copy"]["content"])
    active = [" ".join(line.split()) for line in _active(limits)]
    assert active == ["@audio - rtprio 95", "@audio - memlock unlimited", "@audio - nice -19"]
    assert _task("Install rtkit (PipeWire's realtime fallback)")["ansible.builtin.apt"]["name"] == "rtkit"
    lookup = _task("Look up the managed users")
    assert lookup["ansible.builtin.getent"]["fail_key"] is False
    add = _task("Put the managed users in the realtime group")
    assert add["ansible.builtin.user"]["append"] is True
    assert "getent_passwd" in add["when"], "a missing account must be skipped, not created"


def test_usb_rules_cover_audio_and_capture_devices_and_sort_before_uaccess() -> None:
    task = _task("Keep the StreamStack USB devices awake")
    dest = task["ansible.builtin.copy"]["dest"]
    assert int(re.search(r"/(\d+)-", dest).group(1)) <= 70
    rules = _active(_render(task["ansible.builtin.copy"]["content"]))
    assert len(rules) == 4
    by_vendor = {vendor: [r for r in rules if f'ATTR{{idVendor}}=="{vendor}"' in r] for vendor in ("1235", "0fd9", "04a9")}
    assert len(by_vendor["1235"]) == 1 and "idProduct" not in by_vendor["1235"][0]
    assert len(by_vendor["04a9"]) == 1 and "idProduct" not in by_vendor["04a9"][0]
    assert sorted(re.search(r'idProduct}=="(\w+)"', r).group(1) for r in by_vendor["0fd9"]) == ["0066", "007d"]
    for rule in rules:
        assert 'ATTR{power/control}="on"' in rule and 'TEST=="power/control"' in rule
        assert 'SUBSYSTEM=="usb"' in rule and 'DEVTYPE}=="usb_device"' in rule


def test_live_autosuspend_pass_only_touches_matching_devices(tmp_path: Path) -> None:
    sysfs = tmp_path / "usb"

    def device(name: str, vendor: str, product: str, control: str) -> Path:
        d = sysfs / name
        (d / "power").mkdir(parents=True)
        (d / "idVendor").write_text(vendor + "\n")
        (d / "idProduct").write_text(product + "\n")
        (d / "power/control").write_text(control + "\n")
        return d / "power/control"

    scarlett = device("1-1", "1235", "8210", "auto")
    wave = device("1-2", "0fd9", "007d", "auto")
    camlink = device("1-3", "0fd9", "0066", "on")
    other_elgato = device("1-4", "0fd9", "0060", "auto")
    canon = device("1-5", "04a9", "319b", "auto")
    mouse = device("1-6", "046d", "c539", "auto")
    cmd = _render(_task("Disable autosuspend on the StreamStack USB devices that are present")["ansible.builtin.shell"]["cmd"])
    cmd = cmd.replace("/sys/bus/usb/devices", str(sysfs))

    def run() -> str:
        return subprocess.run(["bash", "-c", cmd], check=True, capture_output=True, text=True).stdout

    first = run()
    assert sorted(first.splitlines()) == ["changed 04a9:319b", "changed 0fd9:007d", "changed 1235:8210"]
    assert [p.read_text().strip() for p in (scarlett, wave, camlink, canon)] == ["on"] * 4
    assert other_elgato.read_text().strip() == "auto"
    assert mouse.read_text().strip() == "auto"
    assert run() == "", "second pass must be idempotent"
    # A host with none of the devices is a clean no-op.
    for p in (scarlett, wave, camlink, canon):
        p.parent.parent.joinpath("idVendor").write_text("1d6b\n")
    assert run() == ""


def test_media_stack_is_gated_on_an_intel_gpu_and_keeps_one_va_driver() -> None:
    detect = _task("Detect an Intel GPU")
    assert detect["changed_when"] is False and detect["failed_when"] is False
    install = _task("Install the current Intel media stack")
    assert install["when"] == "_streamstack_intel_gpu.rc == 0"
    names = re.findall(r"[\w.+-]+", _render(install["ansible.builtin.apt"]["name"]))
    assert "intel-media-va-driver" in names and "intel-media-va-driver-non-free" not in names
    for needed in ("linux-firmware", "libvpl2", "libmfx-gen1.2", "vainfo", "intel-gpu-tools"):
        assert needed in names
    nonfree = re.findall(r"[\w.+-]+", _render(install["ansible.builtin.apt"]["name"], streamstack_intel_media_non_free=True))
    assert "intel-media-va-driver-non-free" in nonfree and "intel-media-va-driver" not in nonfree
    assert install["ansible.builtin.apt"]["state"] == "{{ streamstack_media_package_state }}"


def test_media_stack_never_switches_i915_or_xe() -> None:
    text = (ROLE / "tasks/streamstack-low-latency.yml").read_text()
    assert "modprobe" not in text and "blacklist" not in text and "force_probe" not in text


def test_kernel_arguments_are_opt_in_append_only_and_reversible() -> None:
    assert DEFAULTS["streamstack_kernel_args"] == []
    install = _task("Install the StreamStack kernel-argument drop-in")
    assert "streamstack_kernel_args | length > 0" in install["when"]
    content = _render(
        _task("Write the StreamStack kernel-argument drop-in")["ansible.builtin.copy"]["content"],
        streamstack_kernel_args=["threadirqs", "preempt=full"],
    )
    (line,) = _active(content)
    assert line == 'GRUB_CMDLINE_LINUX_DEFAULT="${GRUB_CMDLINE_LINUX_DEFAULT:+$GRUB_CMDLINE_LINUX_DEFAULT }threadirqs preempt=full"'
    removal = _task("Remove the StreamStack kernel-argument drop-in when no arguments are set")
    assert removal["ansible.builtin.file"]["state"] == "absent"
    assert removal["when"] == "streamstack_kernel_args | length == 0"


def test_loopback_options_are_unchanged_by_the_tuning() -> None:
    streamstack = yaml.safe_load((ROLE / "tasks/streamstack.yml").read_text())
    task = next(t for t in streamstack if t["name"] == "Configure the StreamStack loopback cameras")
    assert "devices=2 video_nr=" in task["ansible.builtin.copy"]["content"]
    assert "exclusive_caps=1,1" in task["ansible.builtin.copy"]["content"]
    assert "v4l2loopback" not in (ROLE / "tasks/streamstack-low-latency.yml").read_text()


def test_canon_bridge_unit_gets_an_unbounded_backoff_restart_policy() -> None:
    assert DEFAULTS["streamstack_canonctl_webcam_unit"] == "canonctl-ptp-webcam.service"
    task = _task("Write the Canon bridge restart policy drop-in")["ansible.builtin.copy"]
    conf = _render(task["content"])
    assert task["dest"].endswith("/.config/systemd/user/{{ streamstack_canonctl_webcam_unit }}.d/10-curiosity-resilience.conf")
    assert "StartLimitIntervalSec=0" in conf
    # on-failure, never always: an intentional stop or unplugged camera is not forced back up.
    assert "Restart=on-failure" in conf and "Restart=always" not in conf
    assert "RestartSteps=" in conf and "RestartMaxDelaySec=60" in conf
