"""Background blur: the settings contract (ranges, validators) and the health check the Console shows."""

from __future__ import annotations

import pytest

from streamstack import blur


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


def test_presets_stay_inside_the_rate_range() -> None:
    assert set(blur.RATE_PRESETS) == {"light", "balanced", "smooth"}
    assert all(blur.MIN_RATE <= rate <= blur.MAX_RATE for rate in blur.RATE_PRESETS.values())
    assert blur.MIN_STRENGTH <= blur.DEFAULT_STRENGTH <= blur.MAX_STRENGTH
    assert blur.MIN_RATE <= blur.DEFAULT_RATE <= blur.MAX_RATE


# ---- the mask backend and its health check -------------------------------------


def test_a_backend_with_models_is_usable_and_public_describes_it() -> None:
    plugin = blur.Plugin(installed=True, version="1.0", models_ok=True, devices=("openvino", "cpu"))
    assert plugin.usable and plugin.device_labels == ["OpenVINO (Arc GPU)", "CPU"]
    public = plugin.public()
    assert public["hint"] == "" and public["devices"] == ["openvino", "cpu"]


def test_a_backend_without_models_is_not_usable_and_carries_the_hint() -> None:
    plugin = blur.Plugin(installed=True, models_ok=False)
    assert not plugin.usable
    assert plugin.public()["hint"] == blur.INSTALL_HINT


def test_health_check_without_a_backend_warns_that_the_mask_is_not_in_the_engine_yet() -> None:
    check = blur.health_check()
    assert check["state"] == "warn" and check["id"] == "background-blur" and check["group"] == "video"
    assert "native video engine" in check["title"] and check["hint"] == blur.INSTALL_HINT


def test_health_check_with_a_usable_backend_names_version_and_device() -> None:
    check = blur.health_check(plugin=blur.Plugin(installed=True, version="1.4.1", models_ok=True))
    assert check["state"] == "ok"
    assert "1.4.1" in check["title"] and "CPU" in check["detail"]
