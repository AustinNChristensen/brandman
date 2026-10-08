"""Seed packs: the brands, guidelines and missions a fresh database starts with.

Brand-specific content lives in data, not code. A pack is a JSON document::

    {"name": "demo", "claim_units": ["credits"], "brands": [{
        "slug": "demo-brand", "name": "...", "mission": "...", "voice": "...",
        "compliance_rules": "...", "personas": [...], "guidelines": [...],
        "growth_mission": {"name": "...", "goals": {"x_followers": [0, 100]}}
    }]}

``BRANDMAN_SEED_PACK`` selects the pack: a built-in name (``demo``, ``none``),
a name registered by an installed package under the ``brandman.seed_packs``
entry-point group, or a path to a JSON file. The default is ``demo``.
"""
from __future__ import annotations

from functools import lru_cache
from importlib.metadata import entry_points
import json
import os
from pathlib import Path
from typing import Any

BUILTIN_DIR = Path(__file__).parent / "seeds"
DEFAULT_PACK = "demo"
DEFAULT_CLAIM_UNITS = ("credits", "seats", "users")
DEFAULT_PERSONA = {
    "name": "Core audience",
    "audience": "People seeking practical, high-confidence guidance.",
    "angles": ["save time", "avoid costly mistakes", "make a confident next move"],
}


class SeedPackError(ValueError):
    pass


def selected_pack_name() -> str:
    return os.environ.get("BRANDMAN_SEED_PACK", "").strip() or DEFAULT_PACK


def _registered_pack(name: str) -> dict[str, Any] | None:
    for entry in entry_points(group="brandman.seed_packs"):
        if entry.name != name:
            continue
        loaded = entry.load()
        value = loaded() if callable(loaded) else loaded
        if isinstance(value, (str, Path)):
            return json.loads(Path(value).read_text())
        if isinstance(value, dict):
            return value
        raise SeedPackError(f"seed pack entry point {name!r} returned {type(value).__name__}")
    return None


@lru_cache(maxsize=16)
def _load(name: str) -> dict[str, Any]:
    builtin = BUILTIN_DIR / f"{name}.json"
    if builtin.is_file():
        pack = json.loads(builtin.read_text())
    elif (registered := _registered_pack(name)) is not None:
        pack = registered
    elif Path(name).expanduser().is_file():
        pack = json.loads(Path(name).expanduser().read_text())
    else:
        raise SeedPackError(
            f"unknown seed pack {name!r}: not built in, not registered, and not a file"
        )
    return validate(pack)


def validate(pack: Any) -> dict[str, Any]:
    if not isinstance(pack, dict) or not isinstance(pack.get("brands", []), list):
        raise SeedPackError("a seed pack must be an object with a 'brands' list")
    slugs = set()
    for brand in pack.get("brands", []):
        missing = [key for key in ("slug", "name", "mission", "voice", "compliance_rules")
                   if not isinstance(brand.get(key), str) or not brand[key].strip()]
        if missing:
            raise SeedPackError(f"seed brand is missing {', '.join(missing)}")
        if brand["slug"] in slugs:
            raise SeedPackError(f"duplicate seed brand {brand['slug']!r}")
        slugs.add(brand["slug"])
    return pack


def load(name: str | None = None) -> dict[str, Any]:
    """Return the selected (or named) pack. Results are cached per name."""
    return _load(name or selected_pack_name())


def brands(name: str | None = None) -> list[dict[str, Any]]:
    return list(load(name).get("brands", []))


def brand(slug: str, name: str | None = None) -> dict[str, Any] | None:
    return next((item for item in brands(name) if item["slug"] == slug), None)


def growth_mission(slug: str, name: str | None = None) -> dict[str, Any] | None:
    found = brand(slug, name)
    return (found or {}).get("growth_mission")


def claim_units(name: str | None = None) -> tuple[str, ...]:
    """Units that mark a volatile numeric claim, e.g. '500 credits'.

    ``BRANDMAN_CLAIM_UNITS`` (comma separated) overrides the pack.
    """
    configured = os.environ.get("BRANDMAN_CLAIM_UNITS", "")
    if configured.strip():
        return tuple(unit.strip() for unit in configured.split(",") if unit.strip())
    return tuple(load(name).get("claim_units") or DEFAULT_CLAIM_UNITS)


__all__ = [
    "DEFAULT_PACK", "DEFAULT_PERSONA", "SeedPackError", "brand", "brands",
    "claim_units", "growth_mission", "load", "selected_pack_name", "validate",
]
