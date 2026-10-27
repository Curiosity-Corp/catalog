"""One probe of this machine, resolved to a Plan: what `doctor` and the voice chain read from a command line."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass

from streamstack import config

from .model import Snapshot
from .plan import Plan, resolve
from .probe import snapshot
from .schema import Catalogue, ProfileError, packaged

log = logging.getLogger("streamstack.hardware.found")


@dataclass(frozen=True)
class Found:
    plan: Plan
    snapshot: Snapshot
    catalogue: Catalogue


def overrides(cfg: Mapping[str, str] | None = None, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The settings the Plan reads: the settings files, beaten by the process environment (ADR 0003)."""
    from streamstack.console.hardware import overrides_from

    return overrides_from(cfg if cfg is not None else config.load_settings(), os.environ if env is None else env)


def probe_now(cfg: Mapping[str, str] | None = None, catalogue: Catalogue | None = None) -> Found | None:
    """Probe this machine and resolve it. None when the profiles or the probe fail (logged), so a caller can fall
    back to the generic behaviour instead of crashing."""
    try:
        profiles = catalogue or packaged()
        taken = snapshot()
        return Found(resolve(taken, profiles, overrides(cfg)), taken, profiles)
    except ProfileError as exc:
        log.warning("the hardware profiles are not valid: %s", exc)
    except Exception as exc:  # the probe runs tools; a failed one must not take a command line down
        log.warning("the hardware probe failed: %s: %s", type(exc).__name__, exc)
    return None
