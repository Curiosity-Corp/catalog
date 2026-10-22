"""The streamstack command: settings, setup, previews and device parsing."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from streamstack import __version__, cli, config, micwatch, system

SRC = Path(__file__).resolve().parents[1] / "src"


def _run(*args: str, env_text: str = "", tmp: Path) -> subprocess.CompletedProcess:
    env_file = tmp / "streamstack.env"
    env_file.write_text(env_text)
    env = dict(os.environ, STREAMSTACK_ENV_FILE=str(env_file), PYTHONPATH=str(SRC))
    return subprocess.run(
        [sys.executable, "-m", "streamstack", *args], capture_output=True, text=True, env=env, check=False, timeout=60
    )


def test_version(tmp_path: Path) -> None:
    result = _run("--version", tmp=tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == f"streamstack {__version__}"


def test_call_settings_name_the_configured_devices(tmp_path: Path) -> None:
    result = _run("call-settings", env_text="STREAMSTACK_PROGRAM_LABEL='Studio Cam'\n", tmp=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "Studio Cam" in result.stdout
    assert "Easy Effects Source" in result.stdout


def test_setup_seeds_once_and_keeps_user_changes(tmp_path: Path, isolated_home: Path) -> None:
    env_text = "STREAMSTACK_WIDTH=1280\nSTREAMSTACK_HEIGHT=720\nSTREAMSTACK_CAMLINK_MODE=off\n"
    assert _run("setup", env_text=env_text, tmp=tmp_path).returncode == 0
    scenes = isolated_home / ".config/obs-studio/basic/scenes/StreamStack.json"
    profile = isolated_home / ".config/obs-studio/basic/profiles/StreamStack/basic.ini"
    voice = isolated_home / ".local/share/easyeffects/input/StreamStack Voice.json"
    doc = json.loads(scenes.read_text())
    assert "@" not in scenes.read_text()
    side = next(s for s in doc["sources"] if s["name"] == "Side by Side")
    assert side["settings"]["items"][1]["pos"] == {"x": 640, "y": 180}
    assert "OutputCX=1280" in profile.read_text()
    assert "FPSNum=30\nFPSDen=1" in profile.read_text()
    assert f"FilePath={isolated_home}/Videos/StreamStack" in profile.read_text()
    voice.write_text("{}")
    assert _run("setup", "--quiet", env_text=env_text, tmp=tmp_path).returncode == 0
    assert voice.read_text() == "{}"
    assert _run("setup", "--force", "--quiet", env_text=env_text, tmp=tmp_path).returncode == 0
    assert json.loads(voice.read_text())["input"]["plugins_order"]


def test_canon_bridge_stays_off_an_external_camera(tmp_path: Path) -> None:
    result = _run("canon-bridge", env_text="STREAMSTACK_CAMERA_EXTERNAL=true\n", tmp=tmp_path)
    assert result.returncode == 1
    assert "another tool" in result.stderr


def test_settings_layer_host_then_user_with_shell_quoting(tmp_path: Path, monkeypatch, isolated_home) -> None:
    host = tmp_path / "host.env"
    host.write_text(
        "# comment\nSTREAMSTACK_CAMERA_LABEL='Canon EOS 50D'\nSTREAMSTACK_FPS=25\n"
        "STREAMSTACK_CANON_COMMAND='canon-tool --device \"$STREAMSTACK_CAMERA_DEVICE\"'\n"
    )
    monkeypatch.setenv("STREAMSTACK_ENV_FILE", str(host))
    user = config.user_env_file()
    user.parent.mkdir(parents=True)
    user.write_text("export STREAMSTACK_FPS=60\nnot a setting\nBROKEN='unterminated\n")
    settings = config.load_settings()
    assert settings["STREAMSTACK_CAMERA_LABEL"] == "Canon EOS 50D"
    assert settings["STREAMSTACK_CANON_COMMAND"] == 'canon-tool --device "$STREAMSTACK_CAMERA_DEVICE"'
    assert settings["STREAMSTACK_FPS"] == "60"  # the user's file wins
    assert "BROKEN" not in settings
    assert settings["STREAMSTACK_WIDTH"] == "1920"  # defaults fill the rest
    assert config.load_settings(host)["STREAMSTACK_FPS"] == "25"


def test_xdg_paths_follow_the_environment(isolated_home: Path) -> None:
    assert config.state_dir() == isolated_home / ".local/state/streamstack"
    assert config.cache_dir() == isolated_home / ".cache/streamstack"
    assert config.secrets_file() == isolated_home / ".config/streamstack/auto.env"


def test_camera_modes_list_every_format_size_and_rate() -> None:
    listing = """ioctl: VIDIOC_ENUM_FMT
	Type: Video Capture

	[0]: 'YUYV' (YUYV 4:2:2)
		Size: Discrete 1920x1080
			Interval: Discrete 0.033s (29.970 fps)
		Size: Discrete 1280x720
			Interval: Discrete 0.017s (60.000 fps)
			Interval: Discrete 0.033s (30.000 fps)
	[1]: 'MJPG' (Motion-JPEG, compressed)
		Size: Discrete 1920x1080
			Interval: Discrete 0.033s (30.000 fps)
"""
    modes = system.parse_modes(listing)
    assert [system.mode_label(m) for m in modes] == [
        "1920x1080@29.97 YUYV",
        "1280x720@60 YUYV",
        "1280x720@30 YUYV",
        "1920x1080@30 MJPG",
    ]


def test_preview_argv_maps_a_mode_to_ffplay_input_options() -> None:
    argv = cli.preview_argv("canon", "/dev/video42", "1280x720@60 MJPG", grid=True)
    joined = " ".join(argv)
    assert "-input_format mjpeg -video_size 1280x720 -framerate 60 -i /dev/video42" in joined
    assert argv[-2:] == ["-vf", "drawgrid=w=iw/3:h=ih/3:t=1:c=white@0.5"]
    with pytest.raises(ValueError):
        cli.preview_argv("canon", "/dev/video42", "huge")


def test_bridge_fills_the_frame_without_letterboxing() -> None:
    vf = cli.bridge_filter("1920", "1080", "30")
    assert "force_original_aspect_ratio=increase" in vf and "crop=1920:1080" in vf and "fps=30" in vf


SOURCES = {
    "alsa_input.usb-Elgato_Systems_Elgato_Wave_XLR-00.mono-fallback": "Wave XLR Mono",
    "alsa_input.usb-Elgato_Cam_Link_4K-03.analog-stereo": "Cam Link 4K Analog Stereo",
    "alsa_input.pci-0000_00_1f.3.analog-stereo": "Built-in Audio Analog Stereo",
    "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor": "Monitor of Built-in Audio",
    "easyeffects_source": "Easy Effects Source",
}


def test_microphone_prefers_the_configured_one_then_a_wave_then_the_default() -> None:
    builtin = "alsa_input.pci-0000_00_1f.3.analog-stereo"
    wave = "alsa_input.usb-Elgato_Systems_Elgato_Wave_XLR-00.mono-fallback"
    assert system.pick_microphone(SOURCES, "Built-in Audio") == builtin
    assert system.pick_microphone(SOURCES, "", builtin) == wave
    no_wave = {k: v for k, v in SOURCES.items() if k != wave}
    assert system.pick_microphone(no_wave, "", builtin) == builtin


def test_microphone_never_guesses_or_picks_a_capture_card_or_monitor() -> None:
    wave = "alsa_input.usb-Elgato_Systems_Elgato_Wave_XLR-00.mono-fallback"
    # A configured microphone that is unplugged is not silently replaced.
    assert system.pick_microphone(SOURCES, "Yeti") == ""
    unusable = {k: v for k, v in SOURCES.items() if k != wave and "pci" not in k}
    assert system.pick_microphone(unusable, "", "easyeffects_source") == ""
    assert system.pick_microphone(unusable, "", "alsa_input.usb-Elgato_Cam_Link_4K-03.analog-stereo") == ""


def test_status_icon_prints_an_icon_path_and_a_tooltip(monkeypatch, capsys) -> None:
    monkeypatch.setattr(system, "running", lambda name: False)
    monkeypatch.setattr(cli, "loopback_has_producer", lambda device: False)
    monkeypatch.setattr(cli, "audio_sources", lambda: {"easyeffects_source": "Easy Effects Source"})
    monkeypatch.setattr(cli, "auto_running", lambda: True)
    assert cli.main(["status", "--icon"]) == 0
    out, err = capsys.readouterr()
    icon = Path(out.splitlines()[0])
    assert icon.name == "streamstack-off.png" and icon.is_file()
    assert "auto-cinematographer on" in err


def test_auto_status_reads_the_journal(monkeypatch, capsys) -> None:
    from streamstack import auto

    monkeypatch.setattr(cli, "auto_running", lambda: False)
    auto.log("mic-gain", source="mic", before_db=0.0, after_db=1.0)
    auto.log("ai", confidence=0.8, proposal={"notes": "Warmer skin."})
    assert cli.main(["auto", "status"]) == 0
    out = capsys.readouterr().out
    assert "Auto mode: off" in out
    assert "+0.0 dB -> +1.0 dB" in out and "(0.8) Warmer skin." in out


def test_tray_and_launchers_compile() -> None:
    root = Path(__file__).resolve().parents[1]
    compile((SRC / "streamstack/tray.py").read_text(), "tray.py", "exec")
    for name in ("streamstack", "streamstack-auto", "streamstack-tray"):
        launcher = root / "packaging/bin" / name
        assert launcher.read_text().startswith("#!/usr/bin/python3 -I")
        assert os.access(launcher, os.X_OK)
        compile(launcher.read_text(), name, "exec")


def test_launcher_runs_from_an_install_tree(tmp_path: Path) -> None:
    import shutil

    root = Path(__file__).resolve().parents[1]
    (tmp_path / "bin").mkdir()
    shutil.copytree(SRC / "streamstack", tmp_path / "lib/streamstack/streamstack")
    shutil.copy2(root / "packaging/bin/streamstack", tmp_path / "bin/streamstack")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, "-I", str(tmp_path / "bin/streamstack"), "--version"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"streamstack {__version__}"


def test_user_units_need_no_install_prefix() -> None:
    units = Path(__file__).resolve().parents[1] / "packaging/systemd/user"
    auto_unit = (units / "streamstack-auto.service").read_text()
    assert "ExecStart=streamstack-auto run" in auto_unit
    assert "StartLimitBurst" in auto_unit.split("[Service]")[0]
    assert "NoNewPrivileges=true" in auto_unit
    assert "ExecStart=streamstack canon-bridge" in (units / "streamstack-canon.service").read_text()


# -- mic-revive -------------------------------------------------------------------


class ReviveRig:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, probe: micwatch.StallProbe) -> None:
        self.revived: list[str] = []
        self.result = micwatch.ReviveResult(True, "Microphone is back (suspend and resume).", ["suspend/resume"])
        monkeypatch.setattr(cli, "have", lambda tool: True)
        monkeypatch.setattr(cli, "audio_sources", lambda: {"alsa_input.wave": "Wave:3"})
        monkeypatch.setattr(system, "default_device", lambda kind: "")
        monkeypatch.setattr(cli, "_watch_microphone", lambda mic: probe)
        monkeypatch.setattr(micwatch, "revive_mic", self.revive)

    def revive(self, mic: str, **kwargs: object) -> micwatch.ReviveResult:
        self.revived.append(mic)
        return self.result


def revive_args(force: bool = False) -> object:
    return cli.build_parser().parse_args(["mic-revive", *(["--force"] if force else [])])


def test_mic_revive_leaves_a_healthy_microphone_alone(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    rig = ReviveRig(monkeypatch, micwatch.StallProbe(False, "healthy", "scheduled (quantum 1024, rate 48000)"))
    assert cli.main(["mic-revive"]) == 0
    assert rig.revived == []
    assert "nothing to revive" in capsys.readouterr().out


def test_mic_revive_refuses_when_it_cannot_tell(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    rig = ReviveRig(monkeypatch, micwatch.StallProbe(None, "unsupported", "not an ALSA device"))
    assert cli.main(["mic-revive"]) == 0
    assert rig.revived == [] and "left alone" in capsys.readouterr().out


def test_mic_revive_force_cycles_a_healthy_microphone(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = ReviveRig(monkeypatch, micwatch.StallProbe(False, "healthy", "scheduled"))
    assert cli.main(["mic-revive", "--force"]) == 0
    assert rig.revived == ["alsa_input.wave"]


def test_mic_revive_revives_a_stalled_microphone_and_says_what_it_did(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    rig = ReviveRig(monkeypatch, micwatch.StallProbe(True, "stalled", "running but not scheduled"))
    assert cli.main(["mic-revive"]) == 0
    out = capsys.readouterr().out
    assert rig.revived == ["alsa_input.wave"]
    assert "Microphone: Wave:3" in out and "not scheduled" in out
    assert "Did: suspend/resume" in out and "Microphone is back" in out


def test_mic_revive_exit_code_is_one_when_it_did_not_help(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    rig = ReviveRig(monkeypatch, micwatch.StallProbe(True, "stalled", "stalled"))
    rig.result = micwatch.ReviveResult(False, micwatch.REPLUG_HINT, ["suspend/resume", "manual help needed"])
    assert cli.main(["mic-revive"]) == 1
    captured = capsys.readouterr()
    assert "manual help needed" in captured.out and "wireplumber" in captured.err


def test_mic_revive_without_a_microphone_or_pactl(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    ReviveRig(monkeypatch, micwatch.StallProbe(None))
    monkeypatch.setattr(cli, "audio_sources", lambda: {})
    assert cli.main(["mic-revive"]) == 1
    assert "no microphone" in capsys.readouterr().err
    monkeypatch.setattr(cli, "have", lambda tool: False)
    assert cli.main(["mic-revive"]) == 1
    assert "pactl is missing" in capsys.readouterr().err


def test_mic_check_playback_has_a_deadline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    played: list[tuple[list[str], float]] = []
    monkeypatch.setattr(cli, "have", lambda tool: True)
    monkeypatch.setattr(cli, "audio_sources", lambda: {system.EE_SOURCE: "EE"})
    monkeypatch.setattr(system, "record", lambda source, seconds, path: True)
    monkeypatch.setattr(system, "run", lambda argv, timeout=10: played.append((argv, timeout)) or None)
    assert cli.main(["mic-check", "3"]) == 0
    (paplay,) = [call for call in played if call[0][0] == "paplay"]
    assert paplay[1] == 3 + 20  # the recording length plus a fixed allowance
