#!/usr/bin/env python3
"""Generate per-country economic init blocks for ZZZ_economy_definitions.txt."""

from __future__ import annotations

import argparse
import csv
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from economy_gdp_scales import POPULATION_RESYNC_TAGS, TAG_GDP_SCALE
from economy_regional_data import (
    state_gdp_multiplier,
    tag_regional_mult,
    vp_productivity_factor,
)

ROOT = Path(__file__).resolve().parent
STATES_DIR = ROOT / "history" / "states"
POP_CSV = ROOT / "state_population_estimates.csv"
COUNTRY_TAGS = ROOT / "common" / "country_tags" / "00_countries.txt"
TARGET_FILE = ROOT / "common" / "on_actions" / "ZZZ_economy_definitions.txt"
BUILDING_ASSIGNMENTS_CSV = ROOT / "building_assignments.csv"

BEGIN_MARKER = "# === BEGIN generated economy init (generate_economy_definitions.py) ==="
END_MARKER = "# === END generated economy init ==="

SKIP_TAGS = {"USA", "ZZZ"}
POWER_GDP_MULT = 3.0

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

# Explicit tag -> archetype mapping derived from LOTD Lore.md.
TAG_ARCHETYPE: dict[str, str] = {
    "CAN": "stable_developed",
    "MEX": "stable_developing",
    "DOM": "stable_developing",
    "HAI": "poor_stable",
    "CUB": "communist",
    "BAH": "island_micro_stable",
    "PRC": "island_micro_stable",
    "FRA": "island_micro_stable",
    "ENG": "tax_haven",
    "CMM": "canadian_backed",
    "ROC": "canadian_backed",
    "SMN": "canadian_backed",
    "SIA": "canadian_backed",
    "MMS": "canadian_backed",
    "WTB": "canadian_backed",
    "SOM": "canadian_backed",
    "DET": "canadian_backed",
    "BRA": "communist",
    "EFG": "socialist_authoritarian",
    "NYC": "social_democratic",
    "RWP": "social_democratic",
    "ITH": "social_democratic",
    "CFC": "social_democratic",
    "SCA": "social_democratic",
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
    "PHI": "movement_establishment",
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
    "AMF": "foreign_occupation_failing",
    "AMO": "foreign_occupation_failing",
    "WAR": "libertarian",
    "BAR": "libertarian",
    "NGI": "decentralized_weak",
    "BAL": "populist_uprising",
    "ALF": "populist_uprising",
    "RWV": "populist_uprising",
    "SYR": "failing_uprising",
    "NAV": "tribal",
    "USM": "neutral_contested",
    "MNV": "neutral_contested",
    "MNM": "neutral_contested",
    "REO": "neutral_contested",
    "GCC": "business_booming",
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
    gdp_scale: float = 1.0
    resynced: bool = False


@dataclass
class AuditRow:
    tag: str
    name: str
    population: int
    authored_gdp_b: float
    new_gdp_b: float
    delta_pct: float
    implied_scale: float
    applied_scale: float
    resynced: bool
    raw_gdp_b: float
    est_power_demand: float
    est_power_supply: float
    power_deficit: float


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


def load_authored_gdp(path: Path | None = None) -> dict[str, float]:
    path = path or TARGET_FILE
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    gdp: dict[str, float] = {}
    for block in re.finditer(r"([A-Z]{3})\s*=\s*\{(.*?)initiate_display_vars", text, re.S):
        tag = block.group(1)
        match = re.search(r"GDP\s*=\s*([\d.]+)", block.group(2))
        if match and tag not in gdp:
            gdp[tag] = float(match.group(1))
    return gdp


def load_state_vp() -> dict[int, int]:
    vp_by_state: dict[int, int] = {}
    for path in STATES_DIR.glob("*.txt"):
        text = path.read_text(encoding="utf-8")
        sid_match = re.search(r"id\s*=\s*(\d+)", text)
        if not sid_match:
            continue
        sid = int(sid_match.group(1))
        vp_by_state[sid] = sum(
            int(value)
            for _, value in re.findall(r"victory_points\s*=\s*\{\s*(\d+)\s+(\d+)\s*\}", text)
        )
    return vp_by_state


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


def load_building_power_by_tag(owner_states: dict[str, list[int]]) -> dict[str, float]:
    """Sum thermo/hydro/nuclear power output per country from building_assignments.csv."""
    if not BUILDING_ASSIGNMENTS_CSV.exists():
        return {}
    rows = list(csv.DictReader(BUILDING_ASSIGNMENTS_CSV.open(newline="", encoding="utf-8")))
    by_id = {int(row["id"]): row for row in rows}
    power_by_tag: dict[str, float] = defaultdict(float)
    for tag, state_ids in owner_states.items():
        for sid in state_ids:
            row = by_id.get(sid)
            if not row:
                continue
            thermo = int(row.get("thermoelectric_plant") or 0) * 5
            hydro = int(row.get("hydroelectric_plant") or 0) * 10
            nuclear = int(row.get("nuclear_reactor") or 0) * 12
            power_by_tag[tag] += thermo + hydro + nuclear
    return dict(power_by_tag)


def load_building_demand_by_tag(owner_states: dict[str, list[int]]) -> dict[str, float]:
    """Weighted building power demand / 2 per country from building_assignments.csv."""
    if not BUILDING_ASSIGNMENTS_CSV.exists():
        return {}
    weights = {
        "schools": 1, "barracks": 1, "prisons": 1, "radar_station": 1,
        "supply_node": 1, "synthetic_refinery": 1, "offices": 2, "hospitals": 2,
        "missile_silo": 2, "enrichment_plant": 4, "dockyard": 1,
    }
    rows = list(csv.DictReader(BUILDING_ASSIGNMENTS_CSV.open(newline="", encoding="utf-8")))
    by_id = {int(row["id"]): row for row in rows}
    demand_by_tag: dict[str, float] = defaultdict(float)
    for tag, state_ids in owner_states.items():
        for sid in state_ids:
            row = by_id.get(sid)
            if not row:
                continue
            weighted = sum(int(row.get(k) or 0) * w for k, w in weights.items())
            demand_by_tag[tag] += weighted / 2.0
    return dict(demand_by_tag)


def compute_base_gdp(
    tag: str,
    state_ids: list[int],
    population: dict[int, int],
    categories: dict[int, str],
    vp_by_state: dict[int, int],
) -> tuple[float, int]:
    total_pop = 0
    base_gdp = 0.0
    for sid in state_ids:
        pop = population.get(sid, 0)
        category = categories.get(sid, "town")
        per_capita = CATEGORY_PER_CAPITA.get(category, CATEGORY_PER_CAPITA["town"])
        vp_factor = vp_productivity_factor(vp_by_state.get(sid, 0), pop)
        regional = state_gdp_multiplier(sid)
        total_pop += pop
        base_gdp += pop * per_capita * vp_factor * regional
    base_gdp *= tag_regional_mult(tag) if tag in POPULATION_RESYNC_TAGS else 1.0
    return base_gdp, total_pop


def gdp_scale_for_tag(tag: str) -> tuple[float, bool]:
    if tag in POPULATION_RESYNC_TAGS:
        return 1.0, True
    return TAG_GDP_SCALE.get(tag, 1.0), False


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def derive_country_economy(
    tag: str,
    name: str,
    state_ids: list[int],
    population: dict[int, int],
    categories: dict[int, str],
    vp_by_state: dict[int, int],
) -> CountryEconomy:
    archetype_key = TAG_ARCHETYPE.get(tag)
    if not archetype_key:
        raise KeyError(f"No archetype mapping for tag {tag}")
    archetype = ARCHETYPES[archetype_key]

    base_gdp, total_pop = compute_base_gdp(tag, state_ids, population, categories, vp_by_state)
    gdp_scale, resynced = gdp_scale_for_tag(tag)
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
        gdp_scale=gdp_scale,
        resynced=resynced,
    )


def build_audit_rows(
    economies: list[CountryEconomy],
    authored_gdp: dict[str, float],
    owner_states: dict[str, list[int]],
    population: dict[int, int],
    categories: dict[int, str],
    vp_by_state: dict[int, int],
    power_supply: dict[str, float],
    building_demand: dict[str, float],
) -> list[AuditRow]:
    rows: list[AuditRow] = []
    for econ in economies:
        base_gdp, _ = compute_base_gdp(
            econ.tag, owner_states[econ.tag], population, categories, vp_by_state
        )
        archetype = ARCHETYPES[econ.archetype]
        raw_gdp_b = base_gdp * archetype.gdp_mult / 1e9
        old = authored_gdp.get(econ.tag, 0.0)
        implied = (old / raw_gdp_b) if raw_gdp_b > 0 else 0.0
        delta_pct = ((econ.gdp_b - old) / old * 100.0) if old else 0.0
        bld_demand = building_demand.get(econ.tag, 0.0)
        est_demand = POWER_GDP_MULT * econ.gdp_b + bld_demand
        est_supply = power_supply.get(econ.tag, 0.0)
        rows.append(
            AuditRow(
                tag=econ.tag,
                name=econ.name,
                population=econ.population,
                authored_gdp_b=old,
                new_gdp_b=econ.gdp_b,
                delta_pct=round(delta_pct, 1),
                implied_scale=round(implied, 4),
                applied_scale=round(econ.gdp_scale, 4),
                resynced=econ.resynced,
                raw_gdp_b=round(raw_gdp_b, 3),
                est_power_demand=round(est_demand, 1),
                est_power_supply=round(est_supply, 1),
                power_deficit=round(est_demand - est_supply, 1),
            )
        )
    return rows


def write_audit_csv(rows: list[AuditRow], path: Path) -> None:
    fieldnames = [
        "tag", "name", "population", "authored_gdp_b", "new_gdp_b", "delta_pct",
        "implied_scale", "applied_scale", "resynced", "raw_gdp_b",
        "est_power_demand", "est_power_supply", "power_deficit",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in sorted(rows, key=lambda r: abs(r.delta_pct), reverse=True):
            writer.writerow(row.__dict__)


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


def strip_duplicate_economy_blocks(content: str) -> str:
    """Remove hand-authored country GDP blocks that sit outside the generated markers."""
    BEGIN = BEGIN_MARKER
    if BEGIN not in content:
        return content
    pattern = re.compile(
        r"(every_country = \{\s*\n\s*set_variable = \{ other_taxes = 0 \}.*?^\s*\})\s*\n"
        r"(?:.*?\n)*?"
        rf"(?={re.escape(BEGIN)})",
        re.MULTILINE | re.DOTALL,
    )
    content = pattern.sub(r"\1\n\n", content, count=1)
    orphan = f"\n{END_MARKER}\n\n\n{BEGIN_MARKER}"
    if orphan in content:
        content = content.replace(orphan, f"\n\n{BEGIN_MARKER}", 1)
    return content


def insert_blocks(content: str, generated: str) -> str:
    content = strip_duplicate_economy_blocks(content)
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
    print(f"{'TAG':4} {'GDP(B)':>8} {'Gr%':>6} {'Pov%':>6} {'Debt':>8} {'Scale':>6} {'Archetype':<24} name")
    for econ in sorted(economies, key=lambda e: e.gdp_b, reverse=True):
        flag = "R" if econ.resynced else ""
        print(
            f"{econ.tag:4} {econ.gdp_b:8.3f} {econ.gdp_growth:6.1f} "
            f"{econ.poverty_rate:6.1f} {econ.national_debt_b:8.3f} "
            f"{econ.gdp_scale:6.3f}{flag:<1} {econ.archetype:<24} {econ.name}"
        )
    print(f"\nTotal countries: {len(economies)}")


def print_audit_highlights(rows: list[AuditRow]) -> None:
    changed = [r for r in rows if abs(r.delta_pct) > 0.1]
    print(f"\nGDP changes: {len(changed)} tags")
    for row in sorted(changed, key=lambda r: abs(r.delta_pct), reverse=True)[:15]:
        print(
            f"  {row.tag:4} {row.authored_gdp_b:8.3f} -> {row.new_gdp_b:8.3f} "
            f"({row.delta_pct:+.1f}%) resync={row.resynced} power {row.est_power_demand:.0f}/{row.est_power_supply:.0f}"
        )
    deficits = [r for r in rows if r.power_deficit > 5]
    if deficits:
        print(f"\nPower deficits (demand > supply + 5): {len(deficits)}")
        for row in sorted(deficits, key=lambda r: r.power_deficit, reverse=True)[:10]:
            print(
                f"  {row.tag:4} demand={row.est_power_demand:.0f} supply={row.est_power_supply:.0f} "
                f"deficit={row.power_deficit:.0f} GDP={row.new_gdp_b:.1f}B"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate economy init blocks for ZZZ_economy_definitions.txt")
    parser.add_argument("--dry-run", action="store_true", help="Do not write ZZZ file")
    parser.add_argument("--audit-csv", type=Path, default=None, help="Write audit CSV to this path")
    args = parser.parse_args()

    population = load_population()
    names = load_country_names()
    owner_states, categories = load_state_data(population)
    vp_by_state = load_state_vp()
    authored_gdp = load_authored_gdp()
    power_supply = load_building_power_by_tag(owner_states)
    building_demand = load_building_demand_by_tag(owner_states)

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
                vp_by_state,
            )
        )

    validate_economies(economies, set(owner_states))
    audit_rows = build_audit_rows(
        economies, authored_gdp, owner_states, population, categories,
        vp_by_state, power_supply, building_demand,
    )

    audit_path = args.audit_csv or (ROOT / "economy_gdp_audit.csv")
    write_audit_csv(audit_rows, audit_path)

    if not args.dry_run:
        generated = generate_blocks(economies)
        content = TARGET_FILE.read_text(encoding="utf-8")
        updated = insert_blocks(content, generated)
        TARGET_FILE.write_text(updated, encoding="utf-8")
        print(f"Wrote economy init blocks to {TARGET_FILE.relative_to(ROOT)}")

    print_summary(economies)
    print_audit_highlights(audit_rows)
    print(f"\nWrote audit to {audit_path if audit_path.is_absolute() else audit_path.resolve().relative_to(ROOT)}")


if __name__ == "__main__":
    main()
