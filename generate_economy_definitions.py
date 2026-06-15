#!/usr/bin/env python3
"""Generate per-country economic init blocks for ZZZ_economy_definitions.txt."""

from __future__ import annotations

import csv
import glob
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATES_DIR = ROOT / "history" / "states"
POP_CSV = ROOT / "state_population_estimates.csv"
COUNTRY_TAGS = ROOT / "common" / "country_tags" / "00_countries.txt"
TARGET_FILE = ROOT / "common" / "on_actions" / "ZZZ_economy_definitions.txt"

BEGIN_MARKER = "# === BEGIN generated economy init (generate_economy_definitions.py) ==="
END_MARKER = "# === END generated economy init ==="

SKIP_TAGS = {"USA", "ZZZ"}

# Compressed-scale GDP per capita by state category (USD).
CATEGORY_PER_CAPITA: dict[str, float] = {
    "megalopolis": 9000,
    "metropolis": 7500,
    "large_city": 6500,
    "city": 5500,
    "large_town": 4500,
    "town": 3500,
    "rural": 2500,
    "pastoral": 1800,
    "enclave": 4000,
    "small_island": 3000,
    "tiny_island": 2500,
    "wasteland": 500,
}


@dataclass(frozen=True)
class Archetype:
    gdp_mult: float
    gdp_growth: float
    income_tax: float
    business_tax: float
    poverty_bump: float
    poverty_monthly_change: float
    debt_ratio: float
    centralization: int
    inflation: float


ARCHETYPES: dict[str, Archetype] = {
    "stable_developed": Archetype(
        1.10, 3.5, 0.26, 0.22, -8, 0.05, 0.55, 50, 3.0
    ),
    "stable_developing": Archetype(
        1.00, 3.0, 0.18, 0.20, 4, 0.08, 0.45, 45, 5.0
    ),
    "poor_stable": Archetype(
        0.70, 1.5, 0.15, 0.18, 18, 0.12, 0.35, 40, 9.0
    ),
    "business_booming": Archetype(
        1.20, 5.5, 0.12, 0.14, -10, -0.05, 0.25, 35, 3.0
    ),
    "communist": Archetype(
        0.80, 1.0, 0.38, 0.40, 6, 0.05, 0.70, 80, 10.0
    ),
    "socialist_authoritarian": Archetype(
        0.80, 0.5, 0.34, 0.32, 8, 0.10, 0.65, 75, 12.0
    ),
    "social_democratic": Archetype(
        0.90, 2.0, 0.28, 0.24, 0, 0.08, 0.50, 55, 6.0
    ),
    "military_authority": Archetype(
        0.85, 1.5, 0.20, 0.18, 4, 0.10, 0.30, 65, 8.0
    ),
    "movement_rump": Archetype(
        0.65, -1.0, 0.16, 0.15, 12, 0.20, 0.20, 55, 14.0
    ),
    "movement_establishment": Archetype(
        0.70, -2.0, 0.18, 0.16, 10, 0.25, 0.35, 60, 16.0
    ),
    "state_continuation": Archetype(
        0.85, 1.5, 0.22, 0.20, 2, 0.10, 0.60, 50, 7.0
    ),
    "canadian_backed": Archetype(
        0.95, 2.0, 0.24, 0.21, -2, 0.05, 0.40, 48, 4.0
    ),
    "foreign_occupation_failing": Archetype(
        0.55, -3.0, 0.14, 0.16, 15, 0.30, 0.80, 70, 20.0
    ),
    "libertarian": Archetype(
        0.85, 2.5, 0.10, 0.12, 6, 0.05, 0.15, 25, 5.0
    ),
    "decentralized_weak": Archetype(
        0.60, 0.0, 0.12, 0.14, 10, 0.15, 0.10, 25, 11.0
    ),
    "populist_uprising": Archetype(
        0.65, 0.5, 0.14, 0.13, 8, 0.12, 0.10, 45, 10.0
    ),
    "failing_uprising": Archetype(
        0.50, -4.0, 0.12, 0.12, 16, 0.35, 0.05, 40, 18.0
    ),
    "island_micro_stable": Archetype(
        0.95, 2.5, 0.20, 0.18, -4, 0.05, 0.35, 42, 4.5
    ),
    "tax_haven": Archetype(
        1.05, 3.0, 0.08, 0.10, -6, 0.02, 0.20, 30, 2.5
    ),
    "tribal": Archetype(
        0.70, 1.0, 0.10, 0.12, 14, 0.10, 0.15, 35, 7.0
    ),
    "neutral_contested": Archetype(
        0.80, 0.0, 0.20, 0.19, 6, 0.08, 0.35, 45, 8.5
    ),
}

# Per-tag GDP scale overrides applied on top of the archetype multiplier.
# Used to nudge individual countries without disturbing others that share an archetype.
TAG_GDP_SCALE: dict[str, float] = {
    "CAN": 1.35,  # scale up toward ~500B (keeps it distinct from MEX)
    "MEX": 1.35,  # scale up toward ~500B (keeps it distinct from CAN)
}

# Explicit tag -> archetype mapping derived from LOTD Lore.md.
TAG_ARCHETYPE: dict[str, str] = {
    # Stable North American powers
    "CAN": "stable_developed",
    "MEX": "stable_developing",
    "DOM": "stable_developing",
    "HAI": "poor_stable",
    "CUB": "communist",
    "BAH": "island_micro_stable",
    "PRC": "island_micro_stable",
    "FRA": "island_micro_stable",
    "ENG": "tax_haven",
    # Canadian-backed stability pockets
    "CMM": "canadian_backed",
    "ROC": "canadian_backed",
    "SMN": "canadian_backed",
    "SIA": "canadian_backed",
    "MMS": "canadian_backed",
    "WTB": "canadian_backed",
    "SOM": "canadian_backed",
    "DET": "canadian_backed",
    # Left / socialist factions
    "BRA": "communist",
    "EFG": "socialist_authoritarian",
    "NYC": "social_democratic",
    "RWP": "social_democratic",
    "ITH": "social_democratic",
    "CFC": "social_democratic",
    "SCA": "social_democratic",
    # US Military Authority charter states
    "TMA": "military_authority",
    "SPM": "military_authority",
    "ERE": "military_authority",
    "LIS": "military_authority",
    "NJM": "military_authority",
    "IMA": "military_authority",
    "JMA": "military_authority",
    "SFM": "military_authority",
    "AMA": "military_authority",
    "DMA": "military_authority",
    "LMA": "military_authority",
    "HMA": "military_authority",
    "NOM": "military_authority",
    "NOR": "military_authority",
    "NGM": "military_authority",
    # Movement rump / declining branches
    "CHA": "movement_rump",
    "BIR": "movement_rump",
    "TAL": "movement_rump",
    "MAR": "movement_rump",
    "MCA": "movement_rump",
    "GBR": "movement_rump",
    "OKC": "movement_rump",
    "CKC": "movement_rump",
    "WOL": "movement_rump",
    "CPN": "movement_rump",
    # Movement establishment remnant
    "PHI": "movement_establishment",
    # State government continuations
    "SVR": "state_continuation",
    "SOH": "state_continuation",
    "STX": "state_continuation",
    "SWV": "state_continuation",
    "ALB": "state_continuation",
    "SAZ": "state_continuation",
    "SDA": "state_continuation",
    "SSD": "state_continuation",
    "SWA": "state_continuation",
    "SOR": "state_continuation",
    "SUT": "state_continuation",
    "SNH": "state_continuation",
    "SSC": "state_continuation",
    "USG": "state_continuation",
    "SWI": "state_continuation",
    "SRI": "state_continuation",
    "DCO": "state_continuation",
    "OMA": "state_continuation",
    "HUD": "state_continuation",
    # Mexican border occupation (failing)
    "AMF": "foreign_occupation_failing",
    "AMO": "foreign_occupation_failing",
    # Libertarian / autonomous regions
    "WAR": "libertarian",
    "BAR": "libertarian",
    # Decentralized / weak governance
    "NGI": "decentralized_weak",
    # Populist uprisings
    "BAL": "populist_uprising",
    "ALF": "populist_uprising",
    "RWV": "populist_uprising",
    # Failing revolt
    "SYR": "failing_uprising",
    # Tribal / indigenous
    "NAV": "tribal",
    # Neutral contested zones
    "USM": "neutral_contested",
    "MNV": "neutral_contested",
    "MNM": "neutral_contested",
    "REO": "neutral_contested",
    # Business-driven prosperity
    "GCC": "business_booming",
    # Independent stable micro-state
    "VER": "island_micro_stable",
}


@dataclass
class CountryEconomy:
    tag: str
    name: str
    population: int
    base_gdp: float
    gdp_b: float
    gdp_growth: float
    income_tax: float
    business_tax: float
    poverty_rate: float
    poverty_monthly_change: float
    national_debt_b: float
    centralization: int
    inflation: float
    archetype: str
    state_count: int


def load_population() -> dict[int, int]:
    pop: dict[int, int] = {}
    with POP_CSV.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            pop[int(row["id"])] = int(float(row["estimated_population"]))
    return pop


def load_country_names() -> dict[str, str]:
    names: dict[str, str] = {}
    for line in COUNTRY_TAGS.read_text(encoding="utf-8").splitlines():
        match = re.match(r'\s*([A-Z]{3})\s*=\s*"countries/(.+)\.txt"', line)
        if match:
            names[match.group(1)] = match.group(2)
    return names


def load_state_data(population: dict[int, int]) -> tuple[dict[str, list[int]], dict[int, str]]:
    owner_states: dict[str, list[int]] = defaultdict(list)
    categories: dict[int, str] = {}
    for path in sorted(STATES_DIR.glob("*.txt")):
        text = path.read_text(encoding="utf-8")
        sid_match = re.search(r"id\s*=\s*(\d+)", text)
        owner_match = re.search(r"owner\s*=\s*([A-Z]{3})", text)
        cat_match = re.search(r"state_category\s*=\s*(\w+)", text)
        if not sid_match:
            continue
        sid = int(sid_match.group(1))
        if cat_match:
            categories[sid] = cat_match.group(1)
        if owner_match:
            owner_states[owner_match.group(1)].append(sid)
    return owner_states, categories


def compute_base_gdp(
    state_ids: list[int],
    population: dict[int, int],
    categories: dict[int, str],
) -> tuple[float, int]:
    total_pop = 0
    base_gdp = 0.0
    for sid in state_ids:
        pop = population.get(sid, 0)
        category = categories.get(sid, "town")
        per_capita = CATEGORY_PER_CAPITA.get(category, CATEGORY_PER_CAPITA["town"])
        total_pop += pop
        base_gdp += pop * per_capita
    return base_gdp, total_pop


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def derive_country_economy(
    tag: str,
    name: str,
    state_ids: list[int],
    population: dict[int, int],
    categories: dict[int, str],
) -> CountryEconomy:
    archetype_key = TAG_ARCHETYPE.get(tag)
    if not archetype_key:
        raise KeyError(f"No archetype mapping for tag {tag}")
    archetype = ARCHETYPES[archetype_key]

    base_gdp, total_pop = compute_base_gdp(state_ids, population, categories)
    gdp_scale = TAG_GDP_SCALE.get(tag, 1.0)
    gdp_b = round(base_gdp * archetype.gdp_mult * gdp_scale / 1e9, 3)
    gdp_per_capita = (gdp_b * 1e9 / total_pop) if total_pop else 0.0
    poverty_rate = round(
        clamp(55 - 4 * (gdp_per_capita / 1000) + archetype.poverty_bump, 5, 70),
        1,
    )
    national_debt_b = round(gdp_b * archetype.debt_ratio, 3)

    return CountryEconomy(
        tag=tag,
        name=name,
        population=total_pop,
        base_gdp=base_gdp,
        gdp_b=gdp_b,
        gdp_growth=archetype.gdp_growth,
        income_tax=archetype.income_tax,
        business_tax=archetype.business_tax,
        poverty_rate=poverty_rate,
        poverty_monthly_change=archetype.poverty_monthly_change,
        national_debt_b=national_debt_b,
        centralization=archetype.centralization,
        inflation=archetype.inflation,
        archetype=archetype_key,
        state_count=len(state_ids),
    )


def format_country_block(econ: CountryEconomy) -> str:
    lines = [
        f"\t\t\t{econ.tag} = {{",
        "\t\t\t\t##Variable Initiation",
        f"\t\t\t\tset_variable = {{ GDP = {econ.gdp_b} }} #in billions",
        f"\t\t\t\tset_variable = {{ GDP_growth = {econ.gdp_growth} }}",
        f"\t\t\t\tset_variable = {{ income_tax_rate = {econ.income_tax} }}",
        f"\t\t\t\tset_variable = {{ business_tax_rate = {econ.business_tax} }}",
        f"\t\t\t\tset_variable = {{ poverty_rate = {econ.poverty_rate} }}",
        f"\t\t\t\tset_variable = {{ poverty_monthly_change = {econ.poverty_monthly_change} }}",
        "\t\t\t\tset_variable = { money_reserves = 0.0 } #in billions",
        f"\t\t\t\tset_variable = {{ national_debt = {econ.national_debt_b} }} #in billions",
        "\t\t\t\tset_variable = { misc_income = 0.0 }",
        "\t\t\t\tset_variable = { misc_costs = 0.0 }",
        "",
        f"\t\t\t\tset_variable = {{ economic_centralization = {econ.centralization} }}",
        f"\t\t\t\tset_variable = {{ base_inflation_rate = {econ.inflation} }}",
        "",
        "\t\t\t\tinitiate_display_vars = yes",
        "\t\t\t}",
    ]
    return "\n".join(lines)


def generate_blocks(economies: list[CountryEconomy]) -> str:
    parts = [BEGIN_MARKER, ""]
    for econ in economies:
        parts.append(format_country_block(econ))
        parts.append("")
    parts.append(END_MARKER)
    return "\n".join(parts)


def insert_blocks(content: str, generated: str) -> str:
    if BEGIN_MARKER in content and END_MARKER in content:
        pattern = re.compile(
            re.escape(BEGIN_MARKER) + r".*?" + re.escape(END_MARKER),
            re.DOTALL,
        )
        return pattern.sub(generated, content, count=1)

    anchor_pattern = re.compile(
        r"\n\t\t\tevery_country = \{\n\t\t\t\tecon_reload_poverty_effects = yes",
    )
    match = anchor_pattern.search(content)
    if not match:
        raise RuntimeError("Could not find insertion anchor in ZZZ_economy_definitions.txt")
    return content[: match.start()] + "\n\n" + generated + content[match.start() :]


def validate_economies(economies: list[CountryEconomy], owner_tags: set[str]) -> None:
    emitted = {e.tag for e in economies}
    expected = owner_tags - SKIP_TAGS
    missing = sorted(expected - emitted)
    extra = sorted(emitted - expected)
    if missing:
        raise RuntimeError(f"Missing economy blocks for tags: {missing}")
    if extra:
        raise RuntimeError(f"Unexpected economy blocks for tags: {extra}")

    for econ in economies:
        if econ.gdp_b <= 0:
            raise RuntimeError(f"{econ.tag}: non-positive GDP {econ.gdp_b}")
        if not (0.08 <= econ.income_tax <= 0.40):
            raise RuntimeError(f"{econ.tag}: income tax out of range {econ.income_tax}")
        if not (5 <= econ.poverty_rate <= 70):
            raise RuntimeError(f"{econ.tag}: poverty out of range {econ.poverty_rate}")


def print_summary(economies: list[CountryEconomy]) -> None:
    print(f"{'TAG':4} {'GDP(B)':>8} {'Gr%':>6} {'Pov%':>6} {'Debt':>8} {'Archetype':<24} name")
    for econ in sorted(economies, key=lambda e: e.gdp_b, reverse=True):
        print(
            f"{econ.tag:4} {econ.gdp_b:8.3f} {econ.gdp_growth:6.1f} "
            f"{econ.poverty_rate:6.1f} {econ.national_debt_b:8.3f} "
            f"{econ.archetype:<24} {econ.name}"
        )
    print(f"\nTotal countries: {len(economies)}")


def main() -> None:
    population = load_population()
    names = load_country_names()
    owner_states, categories = load_state_data(population)

    economies: list[CountryEconomy] = []
    for tag in sorted(owner_states):
        if tag in SKIP_TAGS:
            continue
        economies.append(
            derive_country_economy(
                tag,
                names.get(tag, tag),
                owner_states[tag],
                population,
                categories,
            )
        )

    validate_economies(economies, set(owner_states))
    generated = generate_blocks(economies)

    content = TARGET_FILE.read_text(encoding="utf-8")
    updated = insert_blocks(content, generated)
    TARGET_FILE.write_text(updated, encoding="utf-8")

    print_summary(economies)
    print(f"\nWrote economy init blocks to {TARGET_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
