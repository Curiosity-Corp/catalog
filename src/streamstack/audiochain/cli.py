"""``streamstack chain``: start, stop and inspect the voice chain from a terminal or a service unit."""

from __future__ import annotations

import os
import shutil
import sys
import time
from collections.abc import Callable

from .. import config, system
from ..hardware.found import probe_now
from ..hardware.redact import redact
from . import chain, control, defaults, graph, plugin, runtime

START_WAIT_S = 12.0


def _runtime(cfg: dict[str, str], *, autostart: bool = True, manage_default: bool = True) -> chain.ChainRuntime:
    rt = chain.ChainRuntime(control.model(), config.state_dir(), autostart=autostart, manage_default=manage_default)
    found = probe_now(cfg)
    if found is not None:
        rt.set_plan(found.plan)
    else:  # the probe cannot run: the legacy name rule, so a command line still works
        rt.set_microphone(system.legacy_microphone(cfg) or None)
    return rt


def start(cfg: dict[str, str], *, wait: float = START_WAIT_S, say: Callable[[str], None] = print) -> int:
    """Start the host (if it is not running) and wait for "StreamStack Mic" to appear. 0 when it is there."""
    rt = _runtime(cfg)
    rt.supervise()
    end = time.monotonic() + wait
    while not rt.running and rt.error and not rt.missing and not rt.declined() and time.monotonic() < end:
        time.sleep(0.4)
        rt.supervise()
    if not rt.running:
        say(f"streamstack: the voice chain is not running: {rt.error}")
        return 1
    control.apply_live(rt.model)
    rt.model.flush()
    say(f"Voice chain running: {graph.DESCRIPTION} ({graph.NODE_NAME}, {graph.RATE} Hz mono)")
    say(_default_line(rt.default_result))
    return 0


def _default_line(result: str | None) -> str:
    if result in ("claimed", "held"):
        return f'{graph.DESCRIPTION} is the default microphone: apps on "Default" get the processed voice.'
    if result == "user-changed":
        return f"The default microphone was chosen by hand, so it stays: pick {graph.DESCRIPTION} in the app."
    return f"{graph.DESCRIPTION} could not be made the default microphone ({result or 'no microphone to read'})."


def run_foreground(cfg: dict[str, str]) -> int:
    """For a service manager: write the configuration and replace this process with the host (no detaching)."""
    rt = _runtime(cfg)
    missing = graph.missing_required()
    if missing:
        print("streamstack: install " + ", ".join(missing) + " for the voice chain", file=sys.stderr)
        return 1
    if declined := rt.declined():
        print(f"streamstack: the voice chain does not start: {declined}", file=sys.stderr)
        return 1
    rt.write_conf()
    exe = shutil.which("pipewire")
    if exe is None:
        print("streamstack: pipewire is not installed", file=sys.stderr)
        return 1
    os.execv(exe, ["pipewire", "-c", str(rt.conf)])
    return 0  # pragma: no cover - execv does not return


def stop(cfg: dict[str, str]) -> int:
    conf = runtime.conf_path(config.state_dir())
    if not runtime.host_pids(conf):
        release_default(cfg)  # a claim left by a host that died is still given back
        print("The voice chain is not running.")
        return 0
    if runtime.stop_host(conf):
        release_default(cfg)
        print("Voice chain stopped; apps fall back to the raw microphone.")
        return 0
    print("streamstack: the voice chain did not stop", file=sys.stderr)
    return 1


def status(cfg: dict[str, str]) -> int:
    rt = _runtime(cfg, manage_default=False)
    rt.supervise()
    st = rt.status()
    m = rt.model
    print(f"{graph.DESCRIPTION}: {'running' if st['running'] else 'not running'}")
    if st["error"]:
        print(f"  {st['error']}")
    contract = st["contract"]
    if contract:
        print(f"  node {contract['name']}, {contract['media_class']}, {contract['rate']} Hz, {contract['channels']} ch")
    print(f"  preset: {m.preset}{' (changed)' if m.custom() else ''}; bypass: {'on' if m.bypass else 'off'}")
    print(f"  denoise: {m.active_engine()}; microphone: {redact(rt.microphone) or 'none found'}")
    print(f"  graph: {st['graph_rate']} Hz ({st['graph_rate_reason']})")
    readable, configured = defaults.read_configured()
    if not readable:
        print("  default microphone: unknown (pw-metadata cannot be read)")
    elif configured == graph.NODE_NAME:
        print(f'  default microphone: {graph.DESCRIPTION} (apps on "Default" get the processed voice)')
    else:
        print(f"  default microphone: {redact(configured) if configured else 'automatic'} (not {graph.DESCRIPTION})")
    print(f"  loudness leveller: {'on' if m.loudness_on else 'off'} (target {m.target:g} LUFS)")
    return 0 if st["running"] else 1


def print_config(cfg: dict[str, str]) -> int:
    rt = _runtime(cfg)
    m = rt.model
    print(
        graph.build_config(
            m.params,
            engine=m.active_engine(),
            microphone=rt.microphone,
            bypass=m.bypass,
            auto_db=m.auto_db,
            rate=rt.rate(),
            quantum=rt.quantum(),
        )
    )
    return 0


def claim_default(cfg: dict[str, str], *, wait: float = START_WAIT_S) -> int:
    """Make StreamStack Mic the default microphone (for a service manager: run after the host started)."""
    end = time.monotonic() + wait
    while not runtime.host_pids(runtime.conf_path(config.state_dir())) and time.monotonic() < end:
        time.sleep(0.4)
    rt = _runtime(cfg, autostart=False)  # the host is the service's own; never restart it from here
    while True:
        rt.supervise()
        if rt.running or time.monotonic() >= end:
            break
        time.sleep(0.4)
    if not rt.running:
        print(f"streamstack: not the default microphone: {rt.error}", file=sys.stderr)
        return 1
    print(_default_line(rt.default_result))
    return 0 if rt.default_result in ("claimed", "held", "user-changed") else 1


def release_default(cfg: dict[str, str]) -> int:
    """Give the default microphone back (the raw one comes back when the chain stops)."""
    result = defaults.release(config.state_dir())
    if result == "released":
        print("The default microphone is back to what it was before the voice chain.")
    elif result in ("unavailable", "failed"):
        print(f"streamstack: the default microphone was not restored ({result}); run this again", file=sys.stderr)
        return 1
    return 0


def install_plugin(cfg: dict[str, str]) -> int:
    """Fetch the DeepFilterNet LADSPA plugin that CI built and checked, verify it and put it where the chain looks."""
    try:
        path = plugin.install_deepfilternet()
    except plugin.PluginError as exc:
        print(f"streamstack: {exc}", file=sys.stderr)
        return 1
    print(f"DeepFilterNet installed: {path}")
    print("The voice chain picks it up on its next check (the host restarts once).")
    return 0
