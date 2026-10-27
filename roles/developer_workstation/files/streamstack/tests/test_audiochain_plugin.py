"""The DeepFilterNet plugin CI builds: fetch, verify, install, find, and what the doctor says when it is absent."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from streamstack import cli
from streamstack.audiochain import graph, plugin

GOOD = b"\x7fELF" + b"\0" * 64 + b"ladspa_descriptor\0deep_filter_mono\0"
SUMS = f"{hashlib.sha256(GOOD).hexdigest()}  libdeep_filter_ladspa.so\n"


def serve(files: dict[str, bytes]) -> plugin.Fetcher:
    def fetch(url: str, token: tuple[str, str] | None) -> bytes:
        assert token == ("PRIVATE-TOKEN", "tok")
        name = url.rsplit("/", 1)[1]
        if name not in files:
            raise plugin.PluginError(f"{url}: HTTP 404")
        return files[name]

    return fetch


@pytest.fixture(autouse=True)
def token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITLAB_TOKEN", "tok")
    monkeypatch.delenv("LADSPA_PATH", raising=False)
    monkeypatch.setattr(graph, "LADSPA_DIRS", ())  # the machine running the tests may have the plugin installed


def test_the_package_url_names_the_pinned_version() -> None:
    url = plugin.package_url("git.example", "7", "SHA256SUMS")
    assert url == "https://git.example/api/v4/projects/7/packages/generic/streamstack-ladspa/0.5.6-1/SHA256SUMS"


def test_install_verifies_then_writes_where_the_chain_looks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert graph.find_plugin("deepfilternet") is None
    path = plugin.install_deepfilternet(serve({"SHA256SUMS": SUMS.encode(), plugin.PLUGIN_FILE: GOOD}))
    assert path == tmp_path / "data" / "streamstack" / "ladspa" / "libdeep_filter_ladspa.so"
    assert path.read_bytes() == GOOD and path.stat().st_mode & 0o111
    assert graph.find_plugin("deepfilternet") == path
    assert "deepfilternet" in graph.available_engines()


def test_a_download_that_does_not_match_its_checksum_is_refused(tmp_path: Path) -> None:
    files = {"SHA256SUMS": SUMS.encode(), plugin.PLUGIN_FILE: GOOD + b"tampered"}
    with pytest.raises(plugin.PluginError, match="does not match"):
        plugin.install_deepfilternet(serve(files), dest=tmp_path / "ladspa")
    assert not (tmp_path / "ladspa").exists()


def test_a_checksum_list_without_the_plugin_is_refused(tmp_path: Path) -> None:
    other = f"{'0' * 64}  something-else.so\n"
    with pytest.raises(plugin.PluginError, match="does not list"):
        plugin.install_deepfilternet(serve({"SHA256SUMS": other.encode(), plugin.PLUGIN_FILE: GOOD}), dest=tmp_path)


@pytest.mark.parametrize("data", [b"not a library" * 10, b"\x7fELF" + b"\0" * 64])
def test_something_that_is_not_the_plugin_is_refused_even_with_a_matching_checksum(data: bytes) -> None:
    sums = f"{hashlib.sha256(data).hexdigest()}  libdeep_filter_ladspa.so\n"
    with pytest.raises(plugin.PluginError):
        plugin.verify_plugin(data, sums)


def test_a_pinned_digest_beats_the_published_list() -> None:
    assert plugin.verify_plugin(GOOD, SUMS, pinned=hashlib.sha256(GOOD).hexdigest())
    with pytest.raises(plugin.PluginError, match="pins"):
        plugin.verify_plugin(GOOD, SUMS, pinned="1" * 64)


def test_a_missing_package_says_where(tmp_path: Path) -> None:
    with pytest.raises(plugin.PluginError, match="404"):
        plugin.install_deepfilternet(serve({}), dest=tmp_path)


def test_the_ci_job_token_is_used_when_there_is_no_personal_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITLAB_TOKEN")
    monkeypatch.setenv("CI_JOB_TOKEN", "job")
    monkeypatch.setattr(plugin.system, "run", lambda argv, timeout=10: None)
    seen: list[tuple[str, str] | None] = []

    def fetch(url: str, token: tuple[str, str] | None) -> bytes:
        seen.append(token)
        return SUMS.encode() if url.endswith("SHA256SUMS") else GOOD

    plugin.install_deepfilternet(fetch, dest=tmp_path / "ladspa")
    assert seen == [("JOB-TOKEN", "job")] * 2


def test_no_token_says_how_to_get_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITLAB_TOKEN")
    monkeypatch.delenv("CI_JOB_TOKEN", raising=False)
    monkeypatch.setattr(plugin.system, "run", lambda argv, timeout=10: None)
    with pytest.raises(plugin.PluginError, match="GITLAB_TOKEN"):
        plugin.install_deepfilternet()


def test_the_plugin_is_searched_in_the_user_directory_before_the_system_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    dirs = graph.ladspa_dirs()
    assert dirs == [str(graph.user_ladspa_dir())]
    monkeypatch.setattr(graph, "LADSPA_DIRS", ("/usr/local/lib/ladspa",))
    assert graph.ladspa_dirs() == [str(graph.user_ladspa_dir()), "/usr/local/lib/ladspa"]
    monkeypatch.setenv("LADSPA_PATH", "/opt/x")
    assert graph.ladspa_dirs()[:2] == ["/opt/x", str(graph.user_ladspa_dir())]


def test_doctor_reports_the_missing_plugin_with_the_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    hint = plugin.missing_hint()
    assert "streamstack chain install-plugin" in hint and str(graph.user_ladspa_dir()) in hint
    assert "0.5.6-1" in hint


def test_the_command_line_reports_a_failed_install(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from streamstack.audiochain import cli as chain_cli

    def refuse() -> Path:
        raise plugin.PluginError("no GitLab token")

    monkeypatch.setattr(plugin, "install_deepfilternet", refuse)
    assert chain_cli.install_plugin({}) == 1
    assert "no GitLab token" in capsys.readouterr().err
    assert cli.voice_plugin is plugin


@pytest.fixture(autouse=True)
def unpinned(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fixtures are not the real plugin; the pin has its own tests below."""
    monkeypatch.setattr(plugin, "PINNED_SHA256", None)


def test_the_module_pin_refuses_a_different_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugin, "PINNED_SHA256", "2" * 64)
    with pytest.raises(plugin.PluginError, match="pins"):
        plugin.verify_plugin(GOOD, SUMS)


def test_the_shipped_pin_is_a_sha256() -> None:
    import importlib

    shipped = importlib.reload(plugin).PINNED_SHA256
    assert shipped is not None and len(shipped) == 64 and int(shipped, 16) >= 0
