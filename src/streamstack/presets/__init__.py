"""StreamStack presets: cinematography looks, platform presets, bandwidth tiers and one-click profiles.

Data lives in ``data/presets/*.json`` (see docs/presets.md); this package validates it, resolves a platform and a tier
into a stream plan, renders the look thumbnails and generated 3D LUTs, and stores the user's own profiles.
"""

from streamstack.presets.catalog import Catalog, load
from streamstack.presets.plan import resolve
from streamstack.presets.schema import PresetError

__all__ = ["Catalog", "PresetError", "load", "resolve"]
