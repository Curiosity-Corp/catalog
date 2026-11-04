"""The shared light teardown (#182): every test that turns a light on turns it off again.

A Litra left on overnight on dm313red (the CEO's desk) after a test drove the camera. The suite now tracks every
light a test makes (the simulated Litra transport, and the plain fakes tests build) and `conftest.beams_off` puts each
one out when the test ends, then checks that none is still on. A new kind of fake light joins by calling `track`.
"""

from __future__ import annotations

from typing import Protocol

from streamstack import lighting


class Beam(Protocol):
    """A light a test made: it can be told to go off, and says whether it is on."""

    def power_off(self) -> None: ...

    def is_on(self) -> bool: ...


TRACKED: list[Beam] = []


def track(beam: Beam) -> None:
    TRACKED.append(beam)


class SimulatedBeam:
    """A `SimulatedTransport` as a `Beam`: switched off the way a real light is, by a power report."""

    def __init__(self, transport: lighting.SimulatedTransport) -> None:
        self.transport = transport

    def power_off(self) -> None:
        transport = self.transport
        if transport.closed:
            transport.on = False  # the controller closed it: the light itself is still ours to put out
            return
        transport.write(lighting.report_power(transport.model, False))

    def is_on(self) -> bool:
        return self.transport.on


def switch_everything_off() -> list[Beam]:
    """Put out every tracked light and forget them. Returns the ones that were on, for the caller to report."""
    lit = [beam for beam in TRACKED if beam.is_on()]
    for beam in TRACKED:
        beam.power_off()
    still = [beam for beam in TRACKED if beam.is_on()]
    TRACKED.clear()
    if still:
        raise AssertionError(f"{len(still)} light(s) are still on after the teardown switched them off")
    return lit
