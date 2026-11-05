"""Chrome hardware video decode on deskmeet-b760 (catalog#144).

Chrome ran on llvmpipe there: X had no DRI3 (modesetting AccelMethod none), the
installed free iHD driver exposed no AV1 decode on the Arc A380, and Chrome was
started with no VA-API decode feature. These tests pin the declarative fix: the
profile flags, the one-VA-driver rule, the wrapper and desktop entry the role
writes, and the marker-gated removal.
"""

import base64
import re
import subprocess
from pathlib import Path

import jinja2
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/developer_workstation"
TASKS_FILE = ROLE / "tasks/browser-video-decode.yml"
DEFAULTS = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
PROFILES = DEFAULTS["hardware_profiles"]
MARKER = "Managed by Ansible (catalog#144)"


def _bool(value: object) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _env() -> jinja2.Environment:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["bool"] = _bool
    env.filters["b64decode"] = lambda value: base64.b64decode(value).decode()
    env.filters["regex_replace"] = lambda value, pattern, repl: re.sub(pattern, repl, value)
    return env


def _flatten(tasks: list[dict]) -> list[dict]:
    flat = []
    for task in tasks:
        flat.append(task)
        for key in ("block", "rescue", "always"):
            flat.extend(_flatten(task.get(key) or []))
    return flat


def _task(name_prefix: str) -> dict:
    tasks = _flatten(yaml.safe_load(TASKS_FILE.read_text()))
    return next(t for t in tasks if t["name"].startswith(name_prefix))


def _derived(name: str, profile: str) -> str:
    ctx = {"hardware_profiles": PROFILES, "hardware_profile": profile}
    ctx["developer_workstation_intel_media_non_free"] = _env().from_string(
        DEFAULTS["developer_workstation_intel_media_non_free"]
    ).render(**ctx)
    return _env().from_string(DEFAULTS[name]).render(**ctx)


def test_deskmeet_gets_dri3_and_the_non_free_driver_and_the_chrome_wrapper() -> None:
    profile = PROFILES["deskmeet-b760"]
    # glamor is what gives X its DRI3 extension; "none" left Chrome on llvmpipe.
    assert profile["xorg_accel_method"] == "glamor"
    assert profile["intel_media_non_free"] is True
    assert profile["browser_hw_video_decode"] is True
    for name in ("developer_workstation_intel_media_non_free", "developer_workstation_browser_hw_video_decode"):
        assert _derived(name, "deskmeet-b760") == "True"


def test_other_hosts_are_unchanged() -> None:
    for name, profile in PROFILES.items():
        if name == "deskmeet-b760":
            continue
        assert "intel_media_non_free" not in profile and "browser_hw_video_decode" not in profile, name
        assert profile.get("xorg_accel_method", "") in ("", "glamor"), name
        assert _derived("developer_workstation_intel_media_non_free", name) == "False"
        assert _derived("developer_workstation_browser_hw_video_decode", name) == "False"
    assert _derived("developer_workstation_browser_hw_video_decode", "no-such-profile") == "False"


def test_a_profile_has_one_intel_va_driver_flavour() -> None:
    """The free and non-free iHD packages conflict: apt would swap them every run."""
    for name, profile in PROFILES.items():
        if profile.get("intel_media_non_free"):
            assert "intel-media-va-driver" not in profile.get("packages", []), name
    assert "intel-media-va-driver" not in PROFILES["deskmeet-b760"]["packages"]
    # StreamStack's media task follows the same flag, so the two tasks agree.
    assert DEFAULTS["streamstack_intel_media_non_free"] == "{{ developer_workstation_intel_media_non_free }}"


def test_the_cpu_cap_stays_in_place() -> None:
    """The heat experiment keeps its 70% cap while the video path is fixed."""
    assert PROFILES["deskmeet-b760"]["cpu_max_perf_pct"] == 70


def test_non_free_driver_install_is_gated_on_an_intel_gpu_and_the_flag() -> None:
    detect = _task("Detect an Intel GPU")
    assert detect["changed_when"] is False and detect["failed_when"] is False
    assert "0x8086" in detect["ansible.builtin.shell"]["cmd"]
    install = _task("Install the non-free Intel VA-API driver")
    assert install["ansible.builtin.apt"] == {"name": "intel-media-va-driver-non-free", "state": "present"}
    assert install["when"] == [
        "developer_workstation_intel_media_non_free | bool",
        "_browser_video_intel_gpu.rc == 0",
    ]


def test_wrapper_and_desktop_entry_need_the_flag_chrome_and_an_intel_gpu() -> None:
    block = _task("Install the Chrome hardware video decode wrappers")
    assert block["when"] == [
        "developer_workstation_browser_hw_video_decode | bool",
        "_browser_video_chrome.stat.exists",
        "_browser_video_intel_gpu.rc == 0",
    ]
    assert _task("Check for Google Chrome")["ansible.builtin.stat"]["path"] == "/opt/google/chrome/google-chrome"
    read = _task("Read the packaged Chrome launcher entry")
    assert read["when"] == block["when"] and read["failed_when"] is False


def _rendered_wrapper() -> str:
    task = _task("Install the Chrome launcher wrappers")["ansible.builtin.copy"]
    assert task["mode"] == "0755"
    return _env().from_string(task["content"]).render(
        developer_workstation_browser_hw_decode_features=DEFAULTS["developer_workstation_browser_hw_decode_features"]
    )


def test_wrapper_is_installed_ahead_of_the_package_launchers() -> None:
    task = _task("Install the Chrome launcher wrappers")
    assert task["loop"] == ["google-chrome", "google-chrome-stable"]
    assert task["ansible.builtin.copy"]["dest"] == "/usr/local/bin/{{ item }}"
    assert DEFAULTS["developer_workstation_browser_hw_decode_features"] == ["AcceleratedVideoDecodeLinuxGL"]


def test_wrapper_starts_chrome_with_vaapi_decode_and_keeps_user_arguments(tmp_path: Path) -> None:
    text = _rendered_wrapper()
    assert text.startswith("#!/bin/sh\n") and MARKER in text
    assert "exec /opt/google/chrome/google-chrome --enable-features=AcceleratedVideoDecodeLinuxGL " in text
    stub = tmp_path / "google-chrome"
    stub.write_text('#!/bin/sh\nfor a in "$@"; do echo "arg:$a"; done\n')
    stub.chmod(0o755)
    wrapper = tmp_path / "wrapper"
    wrapper.write_text(text.replace("/opt/google/chrome/google-chrome", str(stub)))
    wrapper.chmod(0o755)
    out = subprocess.run([str(wrapper), "--incognito", "https://youtu.be/x y"], check=True, capture_output=True, text=True)
    assert out.stdout.splitlines() == [
        "arg:--enable-features=AcceleratedVideoDecodeLinuxGL",
        "arg:--incognito",
        "arg:https://youtu.be/x y",
    ]


def test_desktop_entry_override_points_every_exec_at_the_wrapper() -> None:
    packaged = (
        "[Desktop Entry]\nName=Google Chrome\nExec=/usr/bin/google-chrome-stable %U\n"
        "[Desktop Action new-window]\nExec=/usr/bin/google-chrome-stable\n"
        "[Desktop Action new-private-window]\nExec=/usr/bin/google-chrome-stable --incognito\n"
    )
    task = _task("Override the Chrome application entry")
    assert task["ansible.builtin.copy"]["dest"] == "/usr/local/share/applications/google-chrome.desktop"
    assert task["when"] == "_browser_video_desktop.content is defined"
    rendered = _env().from_string(task["ansible.builtin.copy"]["content"]).render(
        _browser_video_desktop={"content": base64.b64encode(packaged.encode()).decode()}
    )
    lines = rendered.splitlines()
    assert lines[0].startswith("#") and MARKER in lines[0] and lines[1] == "[Desktop Entry]"
    execs = [line for line in lines if line.startswith("Exec=")]
    assert execs == [
        "Exec=/usr/local/bin/google-chrome-stable %U",
        "Exec=/usr/local/bin/google-chrome-stable",
        "Exec=/usr/local/bin/google-chrome-stable --incognito",
    ]


def test_removal_only_touches_files_this_role_wrote(tmp_path: Path) -> None:
    task = _task("Remove the Chrome hardware video decode wrappers when")
    assert task["when"] == "not (developer_workstation_browser_hw_video_decode | bool)"
    bin_dir, apps = tmp_path / "bin", tmp_path / "apps"
    bin_dir.mkdir()
    apps.mkdir()
    managed = bin_dir / "google-chrome"
    managed.write_text(f"#!/bin/sh\n# {MARKER}.\nexec chrome\n")
    by_hand = bin_dir / "google-chrome-stable"
    by_hand.write_text("#!/bin/sh\nexec my-own-chrome\n")
    entry = apps / "google-chrome.desktop"
    entry.write_text(f"# {MARKER}.\n[Desktop Entry]\n")
    cmd = task["ansible.builtin.shell"]["cmd"].replace("/usr/local/bin", str(bin_dir)).replace("/usr/local/share/applications", str(apps))

    def run() -> str:
        return subprocess.run(["bash", "-c", cmd], check=True, capture_output=True, text=True).stdout

    first = run()
    assert sorted(first.splitlines()) == sorted([f"removed {managed}", f"removed {entry}"])
    assert not managed.exists() and not entry.exists()
    assert by_hand.read_text() == "#!/bin/sh\nexec my-own-chrome\n"
    assert run() == "", "second pass must be idempotent"
    assert "removed " in task["changed_when"]


def test_task_file_is_included_after_the_hardware_drivers() -> None:
    main = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
    names = [t["name"] for t in main]
    drivers = names.index("Include hardware-specific drivers")
    video = next(i for i, n in enumerate(names) if n.startswith("Include browser hardware video decode"))
    assert video > drivers
    include = main[video]
    assert include["ansible.builtin.include_tasks"]["file"] == "browser-video-decode.yml"
    assert include["tags"] == ["hardware", "browser-video-decode"]
    assert include["ansible.builtin.include_tasks"]["apply"]["tags"] == include["tags"]
    # Not run inside containers, where /sys/class/drm is the host's.
    assert any("ansible_virtualization_type" in str(w) for w in include["when"])


@pytest.mark.parametrize("variable", ["developer_workstation_browser_hw_video_decode", "developer_workstation_intel_media_non_free"])
def test_flags_are_documented(variable: str) -> None:
    assert variable in (ROOT / "docs/configuration.md").read_text()
