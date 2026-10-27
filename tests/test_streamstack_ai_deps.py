"""StreamStack AI helper dependencies: OpenVINO, face model, DeepFilterNet LADSPA library.

Static checks of the pinned artifacts and of the hardware gating; nothing is
downloaded or installed (catalog#100, catalog#101, catalog#102).
"""

from __future__ import annotations

import re
from pathlib import Path

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
MAIN = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
FRAMING = yaml.safe_load((ROLE / "tasks/streamstack-framing.yml").read_text())
DEEPFILTER = yaml.safe_load((ROLE / "tasks/streamstack-deepfilter.yml").read_text())
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _render(text: str) -> str:
    return jinja2.Environment().from_string(text).render(**DEFAULTS)


def _walk(tasks: list[dict]):
    for task in tasks:
        yield task
        for key in ("block", "rescue", "always"):
            yield from _walk(task.get(key, []))


def _named(tasks: list[dict], prefix: str) -> dict:
    return next(t for t in _walk(tasks) if t["name"].startswith(prefix))


def test_every_pinned_artifact_carries_a_sha256() -> None:
    for key in (
        "streamstack_openvino_wheel_sha256",
        "streamstack_openvino_telemetry_wheel_sha256",
        "streamstack_framing_model_sha256",
        "streamstack_deepfilter_sha256",
    ):
        assert SHA256.match(DEFAULTS[key]), key


def test_pinned_urls_name_the_pinned_files() -> None:
    for wheel, url in (("streamstack_openvino_wheel", "streamstack_openvino_wheel_url"),
                       ("streamstack_openvino_telemetry_wheel", "streamstack_openvino_telemetry_wheel_url")):
        assert _render(DEFAULTS[url]).startswith("https://files.pythonhosted.org/packages/")
        assert _render(DEFAULTS[url]).endswith("/" + DEFAULTS[wheel])
    wheel = DEFAULTS["streamstack_openvino_wheel"]
    assert wheel.startswith(f"openvino-{DEFAULTS['streamstack_openvino_release']}-")
    assert f"-{DEFAULTS['streamstack_openvino_python_tag']}-" in wheel
    assert DEFAULTS["streamstack_openvino_telemetry_wheel"].startswith(
        f"openvino_telemetry-{DEFAULTS['streamstack_openvino_telemetry_release']}-")
    model = _render(DEFAULTS["streamstack_framing_model_url"])
    assert model.startswith("https://github.com/opencv/opencv_zoo/raw/")
    assert re.search(r"/raw/[0-9a-f]{40}/", model), "the model must be pinned to a commit"
    assert model.endswith("/" + DEFAULTS["streamstack_framing_model_file"])


def test_every_download_is_checksummed_and_never_forced() -> None:
    for tasks in (FRAMING, DEEPFILTER):
        for task in _walk(tasks):
            module = task.get("ansible.builtin.get_url")
            if module:
                assert str(module["checksum"]).startswith("sha256:"), task["name"]
                assert module["force"] is False, task["name"]


def test_the_obs_background_plugin_is_gone() -> None:
    # Background blur runs inside the native engine through OpenVINO; the OBS plugin is retired (streamstack#75).
    assert not (ROLE / "tasks/streamstack-background-removal.yml").exists()
    assert not any(key.startswith(("streamstack_backgroundremoval", "streamstack_obs_")) for key in DEFAULTS)
    assert "streamstack_background_removal_enabled" not in DEFAULTS
    assert "obs-backgroundremoval" in DEFAULTS["streamstack_retired_packages"]
    assert "streamstack-background-removal" not in str(MAIN)


def test_openvino_goes_where_the_isolated_launcher_looks() -> None:
    assert DEFAULTS["streamstack_openvino_target"] == "/usr/local/lib/streamstack"
    pip = _named(FRAMING, "Install the OpenVINO wheels with pip")["ansible.builtin.command"]["argv"]
    assert pip[:3] == ["/usr/bin/python3", "-I", "-m"]
    for flag in ("--no-deps", "--no-index", "--target"):
        assert flag in pip
    assert "pip" in DEFAULTS["streamstack_framing_packages"][0]
    assert {"python3-numpy", "python3-opencv", "python3-packaging"} <= set(DEFAULTS["streamstack_framing_packages"])


def test_the_gpu_runtime_is_installed_only_with_an_intel_gpu() -> None:
    gpu = _named(FRAMING, "Install the Intel GPU compute runtime")
    assert "_streamstack_framing_intel_gpu.rc == 0" in gpu["when"]
    assert {"intel-opencl-icd", "libze-intel-gpu1", "libze1", "libigdgmm12", "ocl-icd-libopencl1"} == set(
        DEFAULTS["streamstack_compute_runtime_packages"])
    assert not set(DEFAULTS["streamstack_compute_runtime_packages"]) & set(DEFAULTS["streamstack_framing_packages"])
    detect = _named(FRAMING, "Detect an Intel GPU")
    assert detect["failed_when"] is False and detect["changed_when"] is False
    assert "0x8086" in detect["ansible.builtin.shell"]["cmd"]
    assert "_streamstack_framing_intel_gpu.rc == 0" in _named(FRAMING, "Let the desktop users open")["when"]


def test_a_foreign_python_skips_openvino_instead_of_failing() -> None:
    install = _named(FRAMING, "Install OpenVINO into")
    assert any("streamstack_openvino_python_tag" in str(w) for w in install["when"])
    assert "_streamstack_python_tag.stdout !=" in _named(FRAMING, "Skip OpenVINO")["when"]


def test_the_model_lands_where_streamstack_looks_for_it() -> None:
    fetch = _named(FRAMING, "Fetch the pinned YuNet")["ansible.builtin.get_url"]
    assert fetch["dest"].endswith("/.local/share/streamstack/models/{{ streamstack_framing_model_file }}")


def test_main_includes_both_groups_for_streamstack_desktops_only() -> None:
    for file, tag, flag in (
        ("streamstack-framing.yml", "streamstack-framing", "streamstack_framing_enabled"),
        ("streamstack-deepfilter.yml", "streamstack-deepfilter", "streamstack_deepfilter_enabled"),
    ):
        include = next(t for t in MAIN if isinstance(t.get("ansible.builtin.include_tasks"), dict)
                       and t["ansible.builtin.include_tasks"]["file"] == file)
        assert {"hardware", "streamstack", tag} <= set(include["tags"])
        assert {"workstation_profile == 'desktop'", "streamstack_enabled | bool", f"{flag} | bool"} <= set(include["when"])
        assert DEFAULTS[flag] is True


def test_deepfilter_pin_names_the_x86_64_release_asset() -> None:
    release = DEFAULTS["streamstack_deepfilter_release"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", release)
    assert _render(DEFAULTS["streamstack_deepfilter_url"]) == (
        f"https://github.com/Rikorose/DeepFilterNet/releases/download/v{release}/"
        f"libdeep_filter_ladspa-{release}-x86_64-unknown-linux-gnu.so")


def test_deepfilter_lands_where_the_voice_chain_searches() -> None:
    # the voice chain searches LADSPA_PATH, /usr/local/lib/ladspa,
    # /usr/lib/x86_64-linux-gnu/ladspa, /usr/lib/ladspa and /usr/lib64/ladspa
    # for exactly this file name (streamstack docs/voice-denoise.md).
    assert DEFAULTS["streamstack_deepfilter_dest"] == "/usr/local/lib/ladspa/libdeep_filter_ladspa.so"
    # The release record must not sit in a directory LADSPA hosts scan.
    assert "ladspa" not in str(Path(DEFAULTS["streamstack_deepfilter_marker"]).parent)
    copy = _named(DEEPFILTER, "Install libdeep_filter_ladspa {{")["ansible.builtin.copy"]
    assert copy["dest"] == "{{ streamstack_deepfilter_dest }}"
    assert copy["remote_src"] is True and copy["mode"] == "0644"
    assert copy["src"].endswith("{{ streamstack_deepfilter_sha256 }}.so")


def test_deepfilter_install_is_idempotent_and_never_downgrades() -> None:
    decide = _named(DEEPFILTER, "Decide whether to install")["ansible.builtin.set_fact"]
    expr = decide["_streamstack_deepfilter_install"]
    assert "not _streamstack_deepfilter_lib.stat.exists" in expr
    assert "is version(streamstack_deepfilter_release, '<')" in expr
    assert "length > 0" in expr, "an unrecorded library must be left alone"
    install = _named(DEEPFILTER, "Download and install the pinned")
    assert install["when"] == "_streamstack_deepfilter_install | bool"
    record = _named(DEEPFILTER, "Record the installed")["ansible.builtin.copy"]
    assert record["content"] == "{{ streamstack_deepfilter_release }}\n"
    assert record["dest"] == "{{ streamstack_deepfilter_marker }}"
    # Every branch of the decision, rendered with the real expression.
    env = jinja2.Environment()
    env.tests["version"] = lambda a, b, op: tuple(map(int, a.split("."))) < tuple(map(int, b.split(".")))
    template = env.from_string(expr)
    for exists, installed, want in (
        (False, "", True), (True, "", False), (True, "0.5.5", True),
        (True, DEFAULTS["streamstack_deepfilter_release"], False), (True, "9.9.9", False),
    ):
        got = template.render(
            _streamstack_deepfilter_lib={"stat": {"exists": exists}},
            _streamstack_deepfilter_installed=installed,
            streamstack_deepfilter_release=DEFAULTS["streamstack_deepfilter_release"],
        ).strip()
        assert (got == "True") is want, (exists, installed)


def test_deepfilter_never_restarts_audio_or_the_console() -> None:
    text = str(DEEPFILTER)
    for word in ("systemd", "service", "pipewire", "wireplumber", "notify"):
        assert word not in text.lower().replace("streamstack", ""), word
    assert _named(DEEPFILTER, "Install the DeepFilterNet LADSPA")["when"] == "ansible_facts['architecture'] == 'x86_64'"
