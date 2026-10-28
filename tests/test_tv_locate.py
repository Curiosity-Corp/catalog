"""curiosity_tv_locate: find the Samsung TV by MAC when DHCP moves it."""

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

MODULE = Path(__file__).resolve().parents[1] / "roles/developer_workstation/files/curiosity_tv_locate.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("curiosity_tv_locate", MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


loc = _load()
NEIGH = (
    "192.168.1.250 dev wlp8s0  FAILED\n"
    "192.168.1.16 dev wlp8s0 lladdr 14:ab:c5:c0:a6:a2 REACHABLE\n"
    "192.168.1.5 dev wlp8s0 lladdr 70:09:71:93:5A:20 STALE\n"
)


def test_neighbour_lookup_is_case_insensitive_and_skips_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loc, "_ip", lambda *a: NEIGH)
    assert loc.neighbour_ip("70:09:71:93:5a:20") == "192.168.1.5"
    assert loc.neighbour_ip("70:09:71:93:5A:20") == "192.168.1.5"
    assert loc.neighbour_ip("aa:bb:cc:dd:ee:ff") is None


def test_resolve_prefers_the_mac_over_the_stale_fixed_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loc, "_ip", lambda *a: NEIGH)
    assert loc.resolve_tv_host("192.168.1.250", "70:09:71:93:5a:20") == "192.168.1.5"
    assert loc.resolve_tv_host("192.168.1.250", "") == "192.168.1.250"


def test_unknown_mac_sweeps_once_a_minute_then_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    sweeps: list[int] = []
    monkeypatch.setattr(loc, "_ip", lambda *a: "")
    monkeypatch.setattr(loc, "arp_sweep", lambda: sweeps.append(1))
    monkeypatch.setattr(loc.time, "sleep", lambda s: None)
    monkeypatch.setattr(loc, "_sweep_at", 0.0)
    assert loc.resolve_tv_host("192.168.1.5", "70:09:71:93:5a:20") == "192.168.1.5"
    assert loc.resolve_tv_host("192.168.1.5", "70:09:71:93:5a:20") == "192.168.1.5"
    assert sweeps == [1]


def test_sweep_skips_huge_subnets_and_ignores_bad_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    class Sock:
        def __enter__(self) -> "Sock":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def setblocking(self, flag: bool) -> None:
            return None

        def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
            sent.append(addr[0])

    addrs = (
        "2: wlp8s0    inet 192.168.1.17/24 brd 192.168.1.255 scope global dynamic wlp8s0\n"
        "3: big    inet 10.0.0.1/8 scope global big\n"
        "garbage line\n"
        "4: bad    inet nonsense/24 scope global bad\n"
    )
    monkeypatch.setattr(loc, "_ip", lambda *a: addrs)
    monkeypatch.setattr(loc.socket, "socket", lambda *a: Sock())
    loc.arp_sweep()
    assert len(sent) == 254 and sent[0] == "192.168.1.1" and not any(a.startswith("10.") for a in sent)


def test_a_missing_ip_command_means_no_answer_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise FileNotFoundError("ip")

    monkeypatch.setattr(loc.subprocess, "run", boom)
    assert loc.neighbour_ip("70:09:71:93:5a:20") is None
    assert isinstance(subprocess.CompletedProcess, type)
