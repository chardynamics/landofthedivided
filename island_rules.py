"""Shared island-name detection and metro island exemptions for population/category pipelines."""
from __future__ import annotations

import re

ISLAND_NAME_PATTERN = re.compile(
    r"\b(island|islands|isla|islas|ile|iles)\b",
    re.IGNORECASE,
)

# Populated areas whose names contain "island"/"ile" but are not small-island states.
ISLAND_POPULATION_OVERRIDE_EXEMPT = {
    "staten island, ny",
    "long island, ny",
    "montreal island, qc",
    "ile de montreal, qc",
    "ile jesus, qc",
    "ile marie, qc",
    "ile bizardl, qc",
    "ile perrot, qc",
    "ile dondaine, qc",
}

ISLAND_SMALL_ISLAND_MAX_POP = 24_999
ISLAND_GRID_TRUST_THRESHOLD = 50_000
ISLAND_POPULATION_OVERRIDE = 10_000


def normalize_state_name(name: str) -> str:
    return (name or "").strip().lower()


def is_island_name(name: str) -> bool:
    return bool(ISLAND_NAME_PATTERN.search(name or ""))


def is_island_override_exempt(name: str) -> bool:
    return normalize_state_name(name) in ISLAND_POPULATION_OVERRIDE_EXEMPT


def island_population_override(name: str) -> int | None:
    """Return the small-island population override, or None if not applicable."""
    key = normalize_state_name(name)
    if "county" in key:
        return None
    if is_island_override_exempt(name):
        return None
    if is_island_name(name):
        return ISLAND_POPULATION_OVERRIDE
    return None


def is_true_small_island(name: str, population: int) -> bool:
    """Whether a state should use tiny/small_island category from name + population."""
    if not is_island_name(name) or is_island_override_exempt(name):
        return False
    if "county" in normalize_state_name(name):
        return False
    return population <= ISLAND_SMALL_ISLAND_MAX_POP
