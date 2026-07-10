"""Regional productivity multipliers for GDP estimation.

Applied per-state after pop × category, before country archetype and scale.
"""
from __future__ import annotations

# Per-state overrides (state_id -> multiplier on pop × category contribution).
STATE_GDP_MULTIPLIERS: dict[int, float] = {
    210: 0.0,  # Isle Royale — uninhabited park
}

# Per-tag multiplier applied to the full country base_gdp sum (after state factors).
# Only used for tags in POPULATION_RESYNC_TAGS (see generate_economy_definitions.py).
TAG_REGIONAL_MULT: dict[str, float] = {
    "NYC": 1.15,  # finance/media hub bump when population-resynced
}

# VP productivity: factor = 1 + min(VP_CAP, vp_sum / (population / VP_POP_DIVISOR) / VP_DENSITY_DIVISOR)
VP_CAP = 0.15
VP_POP_DIVISOR = 1000
VP_DENSITY_DIVISOR = 200


def vp_productivity_factor(vp_sum: int, population: int) -> float:
    if population <= 0 or vp_sum <= 0:
        return 1.0
    density = vp_sum / (population / VP_POP_DIVISOR)
    return 1.0 + min(VP_CAP, density / VP_DENSITY_DIVISOR)


def state_gdp_multiplier(state_id: int) -> float:
    return STATE_GDP_MULTIPLIERS.get(state_id, 1.0)


def tag_regional_mult(tag: str) -> float:
    return TAG_REGIONAL_MULT.get(tag, 1.0)
