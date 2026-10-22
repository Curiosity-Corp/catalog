"""Background blur: plugin detection, filter settings, the health check and the per-frame cost guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from streamstack import blur


def make_plugin(tmp_path: Path, *, models: bool = True, payload: bytes = b"ELF") -> tuple[list[Path], list[Path]]:
    lib = tmp_path / "lib"
    share = tmp_path / "share"
    lib.mkdir()
    (lib / blur.LIBRARY).write_bytes(payload)
    if models:
        model = share / blur.MODEL
        model.parent.mkdir(parents=True)
        model.write_bytes(b"model")
    else:
        share.mkdir()
    return [lib], [share]


def write_dpkg(path: Path, version: str, status: str = "install ok installed") -> Path:
    path.write_text(
        "Package: bash\nStatus: install ok installed\nVersion: 5.3\n\n"
        f"Package: {blur.PLUGIN_NAME}\nStatus: {status}\nVersion: {version}\nDescription: blur\n x\n"
    )
    return path


# ---- detection ---------------------------------------------------------------


def test_detect_reports_missing_when_no_library_is_found(tmp_path: Path) -> None:
    found = blur.detect(lib_dirs=[tmp_path], share_dirs=[tmp_path], dpkg_status=tmp_path / "none", log_dir=tmp_path)
    assert found.installed is False
    assert found.public()["hint"] == blur.INSTALL_HINT
    assert found.usable is False


def test_detect_reads_the_version_from_dpkg_without_the_build_suffix(tmp_path: Path) -> None:
    libs, shares = make_plugin(tmp_path)
    status = write_dpkg(tmp_path / "status", "1.4.1~forky")
    found = blur.detect(lib_dirs=libs, share_dirs=shares, dpkg_status=status, log_dir=tmp_path / "nologs")
    assert found.installed and found.models_ok and found.usable
    assert found.version == "1.4.1"
    assert found.devices == ("cpu",)
    assert found.public()["hint"] == ""
    assert found.public()["device"] == "cpu"


def test_detect_falls_back_to_the_version_in_the_obs_log(tmp_path: Path) -> None:
    libs, shares = make_plugin(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "2026-10-22 09-00-00.txt").write_text(
        "12:00:00.000: [obs-backgroundremoval] Plugin loaded successfully (version 1.4.1)\n"
    )
    found = blur.detect(lib_dirs=libs, share_dirs=shares, dpkg_status=tmp_path / "none", log_dir=logs)
    assert found.version == "1.4.1"
    assert found.loaded is True


def test_detect_ignores_a_package_that_was_removed(tmp_path: Path) -> None:
    status = write_dpkg(tmp_path / "status", "1.4.1~forky", status="deinstall ok config-files")
    assert blur.dpkg_version(status) is None


def test_detect_flags_missing_models(tmp_path: Path) -> None:
    libs, shares = make_plugin(tmp_path, models=False)
    found = blur.detect(lib_dirs=libs, share_dirs=shares, dpkg_status=tmp_path / "none", log_dir=tmp_path)
    assert found.installed and not found.models_ok and not found.usable
    assert found.public()["hint"] == blur.INSTALL_HINT


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1.4.1~forky", "1.4.1"), ("1:1.4.1-2", "1.4.1"), ("1.5", "1.5"), ("garbage", None), ("", None)],
)
def test_clean_version(text: str, expected: str | None) -> None:
    assert blur.clean_version(text) == expected


def test_scan_devices_sees_the_providers_a_build_imports(tmp_path: Path) -> None:
    cpu_only = tmp_path / "a.so"
    cpu_only.write_bytes(b"\x7fELF OrtGetApiBase")
    assert blur.scan_devices(cpu_only) == ("cpu",)
    arc = tmp_path / "b.so"
    arc.write_bytes(b"\x7fELF OrtSessionOptionsAppendExecutionProvider_OpenVINO")
    assert blur.scan_devices(arc) == ("cpu", "openvino")
    assert blur.scan_devices(tmp_path / "missing.so") == ("cpu",)
    both = tmp_path / "c.so"
    both.write_bytes(b"OrtSessionOptionsAppendExecutionProvider_CUDA OpenVINOExecutionProvider")
    assert blur.scan_devices(both) == ("cpu", "cuda", "openvino")


def test_newest_log_only_counts(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "2026-10-01 09-00-00.txt").write_text(
        "[obs-backgroundremoval] Plugin loaded successfully (version 1.0.0)\n"
    )
    (logs / "2026-10-22 09-00-00.txt").write_text("nothing about the plugin\n")
    assert blur.log_evidence(logs) == (None, None)


# ---- settings ----------------------------------------------------------------


def test_low_latency_settings_use_real_plugin_keys_and_stay_on_cpu() -> None:
    settings = blur.low_latency_settings()
    assert settings["useGPU"] == "cpu"
    assert settings["numThreads"] == 2
    assert settings["mask_every_x_frames"] >= 2  # not every frame: the cheap default
    assert settings["blur_background"] >= 1  # 0 would remove the background instead of blurring it
    assert settings["enable_focal_blur"] is False
    assert settings["model_select"] == blur.MODEL
    assert settings is not blur.LOW_LATENCY  # callers may edit their copy


def test_settings_patch_maps_strength_and_rate_to_plugin_keys() -> None:
    assert blur.settings_patch(strength=12) == {"blur_background": 12}
    assert blur.settings_patch(rate=1) == {"mask_every_x_frames": 1}
    assert blur.settings_patch(strength=3, rate=3) == {"blur_background": 3, "mask_every_x_frames": 3}
    assert blur.settings_patch() == {}


@pytest.mark.parametrize("bad", [0, 21, -1, 1.5, float("nan"), "8", None, True])
def test_strength_is_a_whole_number_inside_the_plugin_range(bad: object) -> None:
    with pytest.raises(blur.BlurError):
        blur.validate_strength(bad)


@pytest.mark.parametrize("bad", [0, 7, 2.5, "2", True])
def test_rate_is_a_whole_number_inside_its_range(bad: object) -> None:
    with pytest.raises(blur.BlurError):
        blur.validate_rate(bad)


def test_valid_numbers_pass_through_as_ints() -> None:
    assert blur.validate_strength(20) == 20
    assert blur.validate_strength(8.0) == 8
    assert blur.validate_rate(1) == 1


def test_read_settings_clamps_and_defaults() -> None:
    assert blur.read_settings({"blur_background": 12, "mask_every_x_frames": 3}) == (12, 3)
    assert blur.read_settings({"blur_background": 0, "mask_every_x_frames": 300}) == (1, blur.MAX_RATE)
    assert blur.read_settings({}) == (blur.DEFAULT_STRENGTH, blur.DEFAULT_RATE)
    assert blur.read_settings({"blur_background": "x", "mask_every_x_frames": None}) == (
        blur.DEFAULT_STRENGTH,
        blur.DEFAULT_RATE,
    )


# ---- health check ------------------------------------------------------------


def test_health_check_missing_plugin_says_what_to_do() -> None:
    check = blur.health_check(plugin=blur.Plugin(installed=False))
    assert check["state"] == "warn"
    assert check["id"] == "background-blur"
    assert blur.PLUGIN_NAME in check["title"]
    assert "catalog" in check["hint"] and "restart OBS" in check["hint"]


def test_health_check_missing_models() -> None:
    check = blur.health_check(plugin=blur.Plugin(installed=True, version="1.4.1", models_ok=False))
    assert check["state"] == "warn"
    assert "models" in check["title"]


def test_health_check_ok_names_version_and_device() -> None:
    check = blur.health_check(plugin=blur.Plugin(installed=True, version="1.4.1", models_ok=True, library="/x.so"))
    assert check["state"] == "ok"
    assert "1.4.1" in check["title"] and "CPU" in check["detail"]


def test_health_check_notes_when_obs_did_not_load_it() -> None:
    check = blur.health_check(plugin=blur.Plugin(installed=True, models_ok=True, loaded=False, library="/x.so"))
    assert "did not load" in check["detail"]


# ---- cost guard --------------------------------------------------------------


def stats(render_ms: float, cpu: float = 10.0, skipped: int = 0) -> dict[str, float]:
    return {
        "averageFrameRenderTime": render_ms,
        "cpuUsage": cpu,
        "renderSkippedFrames": skipped,
        "outputSkippedFrames": 0,
    }


def feed(guard: blur.CostGuard, count: int, render_ms: float, cpu: float, on: bool, skipped: int = 0) -> None:
    for _ in range(count):
        sample = blur.parse_stats(stats(render_ms, cpu, skipped))
        assert sample is not None
        guard.observe(sample, on)


def test_parse_stats_rejects_missing_and_non_numeric_fields() -> None:
    assert blur.parse_stats({}) is None
    assert blur.parse_stats({**stats(1.0), "cpuUsage": "high"}) is None
    assert blur.parse_stats({**stats(1.0), "cpuUsage": float("nan")}) is None
    sample = blur.parse_stats({**stats(2.5, 7.0, 3), "outputSkippedFrames": 2})
    assert sample == blur.Sample(2.5, 7.0, 5)


def test_percentile_and_median() -> None:
    assert blur.percentile([], 0.95) == 0.0
    assert blur.percentile([1, 2, 3, 4, 100], 0.95) == 100
    assert blur.percentile([1, 2, 3, 4, 100], 0.5) == 3
    assert blur.median([5.0]) == 5.0


def test_guard_is_off_without_blur_then_measures_then_judges() -> None:
    guard = blur.CostGuard()
    assert guard.verdict()["state"] == "off"
    feed(guard, 10, 2.0, 10.0, on=False)
    feed(guard, 3, 3.0, 10.2, on=True)
    assert guard.verdict()["state"] == "measuring"
    feed(guard, 10, 3.0, 10.2, on=True)
    verdict = guard.verdict()
    assert verdict["state"] == "ok"
    assert verdict["added_ms"] == pytest.approx(1.0)
    assert verdict["dropped"] == 0


def test_guard_goes_over_on_frame_time() -> None:
    guard = blur.CostGuard()
    feed(guard, 10, 2.0, 10.0, on=False)
    feed(guard, 12, 11.0, 10.2, on=True)
    verdict = guard.verdict()
    assert verdict["state"] == "over"
    assert any("frame time" in r for r in verdict["reasons"])


def test_guard_goes_over_on_cpu() -> None:
    guard = blur.CostGuard()
    feed(guard, 10, 2.0, 10.0, on=False)
    feed(guard, 12, 2.5, 14.0, on=True)
    verdict = guard.verdict()
    assert verdict["state"] == "over"
    assert any("CPU" in r for r in verdict["reasons"])


def test_guard_goes_over_on_dropped_frames_since_the_blur_came_on() -> None:
    guard = blur.CostGuard()
    feed(guard, 10, 2.0, 10.0, on=False, skipped=40)  # frames dropped before the blur do not count against it
    feed(guard, 6, 2.5, 10.1, on=True, skipped=40)
    feed(guard, 6, 2.5, 10.1, on=True, skipped=43)
    verdict = guard.verdict()
    assert verdict["state"] == "over"
    assert verdict["dropped"] == 3


def test_guard_without_a_baseline_judges_frame_time_only_and_says_so() -> None:
    guard = blur.CostGuard()
    feed(guard, 12, 3.0, 50.0, on=True)
    verdict = guard.verdict()
    assert verdict["state"] == "ok"
    assert verdict["added_cpu"] is None
    assert "nothing to compare" in str(verdict["detail"])


def test_guard_forgets_everything_on_reset_and_survives_an_obs_restart() -> None:
    guard = blur.CostGuard()
    feed(guard, 10, 2.0, 10.0, on=False, skipped=100)
    feed(guard, 12, 2.5, 10.1, on=True, skipped=100)
    feed(guard, 1, 2.5, 10.1, on=True, skipped=0)  # counters restarted: measure again, no negative drop count
    assert guard.verdict()["state"] == "measuring"
    feed(guard, 11, 2.5, 10.1, on=True, skipped=0)
    assert guard.verdict()["dropped"] == 0
    guard.reset()
    assert guard.verdict()["state"] == "off"
