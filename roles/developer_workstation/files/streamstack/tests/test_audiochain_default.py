"""StreamStack Mic as the default source: claim when the chain runs, give the raw microphone back when it stops."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from test_audiochain_runtime import DUMP, done, model

from streamstack.audiochain import chain, cli, defaults, graph, stages

WAVE = "alsa_input.usb-Elgato_Systems_Elgato_Wave_XLR_DS23M2A00049-00.mono-fallback"


@pytest.fixture
def plugins_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(graph, "missing_required", lambda: [])


class Metadata:
    """A `pw-metadata` and `pw-dump` that keep the default metadata in memory and record every call."""

    def __init__(self, configured: str | None = WAVE, readable: bool = True, set_ok: bool = True) -> None:
        self.configured = configured
        self.readable = readable
        self.set_ok = set_ok
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], timeout: float = 10) -> subprocess.CompletedProcess[str] | None:
        self.calls.append(argv)
        if argv[0] == "pw-dump":
            return done(json.dumps(DUMP))
        if argv[0] == "pw-cli":
            return done()
        if argv[0] != "pw-metadata":
            return None
        if not self.readable:
            return done("", 1)
        if argv[:3] == ["pw-metadata", "-n", "default"] and len(argv) == 3:
            lines = [
                'Found "default" metadata 97',
                "update: id:0 key:'default.configured.audio.sink' "
                "value:'{\"name\":\"bluez_output.x\"}' type:'Spa:String:JSON'",
                "update: id:77 key:'target.object' value:'20697' type:'Spa:Id'",
            ]
            if self.configured is not None:
                value = '{ "name": "' + self.configured + '" }'
                lines.append(f"update: id:0 key:'{defaults.KEY}' value:'{value}' type:'Spa:String:JSON'")
            return done("\n".join(lines) + "\n")
        if not self.set_ok:
            return done("", 1)
        if "-d" in argv:
            self.configured = None
            return done()
        self.configured = json.loads(argv[5])["name"]
        return done()

    def writes(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "pw-metadata" and len(c) > 3]


def test_reading_the_configured_default_ignores_other_keys_and_objects() -> None:
    assert defaults.read_configured(Metadata(WAVE)) == (True, WAVE)
    assert defaults.read_configured(Metadata(None)) == (True, None)
    assert defaults.read_configured(Metadata(readable=False)) == (False, None)
    removed = "remove: id:0 key:'default.configured.audio.source'"
    runner = lambda argv, timeout=10: done(  # noqa: E731
        f"update: id:0 key:'{defaults.KEY}' value:'{{\"name\":\"a\"}}' type:'Spa:String:JSON'\n{removed}\n"
    )
    assert defaults.read_configured(runner) == (True, None)


def test_claim_then_release_gives_the_previous_microphone_back(tmp_path: Path) -> None:
    md = Metadata(WAVE)
    assert defaults.claim(tmp_path, md) == "claimed"
    assert md.configured == graph.NODE_NAME and defaults.claimed(tmp_path)
    assert defaults.release(tmp_path, md) == "released"
    assert md.configured == WAVE and not defaults.claimed(tmp_path)
    assert defaults.release(tmp_path, md) == "none"  # nothing left to give back, nothing written
    assert len(md.writes()) == 2


def test_with_no_configured_default_the_key_is_deleted_on_release(tmp_path: Path) -> None:
    md = Metadata(None)
    assert defaults.claim(tmp_path, md) == "claimed"
    assert defaults.release(tmp_path, md) == "released"
    assert md.configured is None and any("-d" in c for c in md.writes())


def test_a_second_claim_changes_nothing(tmp_path: Path) -> None:
    md = Metadata(WAVE)
    defaults.claim(tmp_path, md)
    writes = len(md.writes())
    assert defaults.claim(tmp_path, md) == "held"
    assert len(md.writes()) == writes
    defaults.release(tmp_path, md)
    assert md.configured == WAVE  # the first previous value survived the second claim


def test_a_default_chosen_by_hand_is_left_alone(tmp_path: Path) -> None:
    md = Metadata(WAVE)
    defaults.claim(tmp_path, md)
    md.configured = "alsa_input.other"  # the person picked another microphone while the chain ran
    assert defaults.claim(tmp_path, md) == "user-changed"
    assert md.configured == "alsa_input.other" and not defaults.claimed(tmp_path)
    md.configured = graph.NODE_NAME
    defaults.claim(tmp_path, md)
    md.configured = "alsa_input.third"
    assert defaults.release(tmp_path, md) == "user-changed"
    assert md.configured == "alsa_input.third"


def test_a_stale_claim_of_ours_is_never_restored_as_the_raw_microphone(tmp_path: Path) -> None:
    md = Metadata(graph.NODE_NAME)  # a crash lost our record, the key still names the chain
    assert defaults.claim(tmp_path, md) == "claimed"
    defaults.release(tmp_path, md)
    assert md.configured is None


def test_unreadable_metadata_keeps_the_claim_for_the_next_try(tmp_path: Path) -> None:
    md = Metadata(WAVE)
    defaults.claim(tmp_path, md)
    md.readable = False
    assert defaults.release(tmp_path, md) == "unavailable" and defaults.claimed(tmp_path)
    md.readable = True
    assert defaults.release(tmp_path, md) == "released" and md.configured == WAVE
    assert defaults.claim(tmp_path, Metadata(readable=False)) == "unavailable"


def test_a_refused_write_leaves_no_claim_behind(tmp_path: Path) -> None:
    md = Metadata(WAVE, set_ok=False)
    assert defaults.claim(tmp_path, md) == "failed" and not defaults.claimed(tmp_path)
    ok = Metadata(WAVE)
    defaults.claim(tmp_path, ok)
    ok.set_ok = False
    assert defaults.release(tmp_path, ok) == "failed" and defaults.claimed(tmp_path)


def runtime(tmp_path: Path, md: Metadata, now: list[float], **kw: object) -> chain.ChainRuntime:
    rt = chain.ChainRuntime(
        model(tmp_path, ["deepfilternet"]), tmp_path, runner=md, autostart=False, clock=lambda: now[0], **kw
    )  # type: ignore[arg-type]
    rt.set_microphone(WAVE)
    return rt


def test_the_running_chain_takes_the_default_and_stopping_it_gives_it_back(
    tmp_path: Path, plugins_present: None
) -> None:
    md, now = Metadata(WAVE), [0.0]
    rt = runtime(tmp_path, md, now)
    rt.supervise()
    assert rt.running and md.configured == graph.NODE_NAME
    writes = len(md.writes())
    rt.supervise()
    assert len(md.writes()) == writes  # once per run, not once per second
    rt.stop(keep_host=False)
    assert md.configured == WAVE


def test_a_chain_that_cannot_start_does_not_keep_the_default(
    tmp_path: Path, plugins_present: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    md, now = Metadata(WAVE), [0.0]
    rt = runtime(tmp_path, md, now)
    rt.supervise()
    monkeypatch.setattr(graph, "missing_required", lambda: ["zam-plugins"])
    rt.supervise()
    assert md.configured == WAVE and not rt.running


def test_without_an_explicit_microphone_the_chain_never_claims_the_default(
    tmp_path: Path, plugins_present: None
) -> None:
    md, now = Metadata(WAVE), [0.0]
    rt = runtime(tmp_path, md, now)
    rt.set_microphone(None)  # it would read the default source, which would be itself
    rt.supervise()
    assert rt.running and md.configured == WAVE and not md.writes()


def test_a_failed_claim_is_retried_later_not_every_second(tmp_path: Path, plugins_present: None) -> None:
    md, now = Metadata(WAVE, readable=False), [0.0]
    rt = runtime(tmp_path, md, now)
    rt.supervise()
    tries = len([c for c in md.calls if c[0] == "pw-metadata"])
    now[0] += 1.0
    rt.supervise()
    assert len([c for c in md.calls if c[0] == "pw-metadata"]) == tries
    md.readable = True
    now[0] += chain.DEFAULT_RETRY_S
    rt.supervise()
    assert md.configured == graph.NODE_NAME


def test_the_default_can_be_left_alone_entirely(tmp_path: Path, plugins_present: None) -> None:
    md, now = Metadata(WAVE), [0.0]
    rt = runtime(tmp_path, md, now, manage_default=False)
    rt.supervise()
    rt.stop(keep_host=False)
    assert rt.running is True and not [c for c in md.calls if c[0] == "pw-metadata"]


def test_cli_status_says_which_microphone_apps_get(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(defaults, "read_configured", lambda run=None: (True, graph.NODE_NAME))
    cli.status({})
    assert "default microphone: StreamStack Mic" in capsys.readouterr().out
    monkeypatch.setattr(defaults, "read_configured", lambda run=None: (True, WAVE))
    cli.status({})
    assert "(not StreamStack Mic)" in capsys.readouterr().out


def test_cli_release_default_with_no_claim_is_quiet(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.release_default({}) == 0
    assert capsys.readouterr().out == ""


def test_the_chain_node_keeps_the_name_apps_show() -> None:
    text = graph.build_config(stages.defaults(), engine="off", microphone=WAVE)
    assert 'node.description = "StreamStack Mic"' in text and "media.class = Audio/Source" in text
