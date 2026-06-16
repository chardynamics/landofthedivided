#!/usr/bin/env python3
"""Assign state buildings from category, population, and known real-world sites."""
import argparse
import csv
import re
from collections import Counter, defaultdict
from pathlib import Path

import math

from assign_infrastructure import POPULATION_CSV, ROOT, STATE_DIR, load_state_names, parse_states
from assign_ports import load_coastal_provinces
from generate_economy_definitions import CATEGORY_PER_CAPITA, TAG_ARCHETYPE

DEFINITION_CSV = ROOT / "map" / "definition.csv"
OUTPUT_CSV = ROOT / "building_assignments.csv"
PORT_ASSIGNMENTS_CSV = ROOT / "port_assignments.csv"

STATE_SLOTS = {
    "megalopolis": 22,
    "metropolis": 20,
    "large_city": 16,
    "city": 12,
    "large_town": 8,
    "town": 6,
    "rural": 3,
    "pastoral": 2,
    "enclave": 1,
    "small_island": 2,
    "tiny_island": 1,
    "wasteland": 0,
}

# Population thresholds per building: a state earns one level for each threshold
# it meets or exceeds. Tuned so that small/remote states stay nearly empty.
POP_THRESHOLDS = {
    "schools": [40_000, 250_000, 1_200_000],
    "hospitals": [35_000, 300_000, 1_200_000],
    "offices": [150_000, 700_000, 2_800_000],
    "prisons": [90_000, 600_000, 2_800_000],
    "barracks": [200_000, 1_000_000, 3_800_000],
    "radar_station": [450_000, 1_600_000, 4_500_000, 8_500_000, 13_000_000],
    "anti_air_building": [700_000, 2_800_000, 6_500_000],
}
# Dockyards only on real port states; level scales with population.
DOCKYARD_THRESHOLDS = [700_000, 2_800_000, 6_500_000]

# --- Power production is anchored to each country's estimated CONSUMPTION ---
#
# The economy backend (TNO_calculate_power_consumption) computes a country's
# power demand as roughly:
#
#     consumption ~= POWER_GDP_MULT * GDP(billions) + (building_power_demand / 2)
#
# Building power demand is a weighted count of the non-power buildings a state
# has (schools, offices, hospitals, ...). Because demand scales with GDP, a
# fixed city-size power template made the big economies (Canada, California)
# starve while tiny warlords overflowed. Instead, each country's total power
# output now targets `buffer * consumption`, where the buffer grows with GDP:
# real nations and the largest warlords keep a surplus, mid warlords land "just
# barely enough", and undeveloped warlords (Great Basin Republic) fall short.
POWER_GDP_MULT = 3.0  # matches KD_consumption_multiplier in the economy backend

# Weighted power demand per non-power building (matches the economy backend's
# TNO_calculate_power_consumption before the global /2).
DEMAND_WEIGHTS = {
    "schools": 1,
    "barracks": 1,
    "prisons": 1,
    "radar_station": 1,
    "supply_node": 1,
    "synthetic_refinery": 1,
    "offices": 2,
    "hospitals": 2,
    "missile_silo": 2,
    "enrichment_plant": 4,
    "dockyard": 1,
}

# Per-state power-plant economics (common/buildings/00_buildings.txt).
THERMO_POWER, THERMO_MAX = 5, 4
HYDRO_POWER, HYDRO_MAX = 10, 3
NUCLEAR_POWER = 12
STATE_ENERGY_POWER_CAP = THERMO_MAX * THERMO_POWER + HYDRO_MAX * HYDRO_POWER  # 50

# Real-world sovereign nations always keep a comfortable surplus.
LEGITIMATE_COUNTRIES = {"CAN", "MEX", "CUB", "DOM", "HAI", "BAH", "FRA", "ENG"}
LEGIT_POWER_BUFFER = 1.40       # legit nations produce ~40% over consumption

# Collapsed US successor states scale their buffer with GDP on a log curve:
# the largest (California, Emergency Federal Government) keep a real surplus,
# while the smallest/undeveloped frontier warlords run a deficit.
WARLORD_BUFFER_FLOOR = 0.60     # undeveloped warlords (Great Basin) run short
WARLORD_BUFFER_CEIL = 1.30      # largest warlords keep a surplus + buffer
WARLORD_GDP_FLOOR = 0.20        # GDP ($B) at/below which the floor buffer applies
WARLORD_GDP_CEIL = 120.0        # GDP ($B) at/above which the ceiling buffer applies

# Grid power directly caps usable production units (PUs) ~1:1 (the economy
# backend clamps usable PUs to resource@power). On top of the multiplicative
# buffer above, every country is guaranteed at least this much power *over* its
# estimated consumption so even tiny economies can keep a few production units
# powered. It is oversized a little to survive each country's in-game resource
# extraction penalty (~0.8x). Big economies already clear this via their buffer.
PU_HEADROOM_POWER = 8


def load_state_owners(state_dir):
    owners = {}
    for path in state_dir.glob("*.txt"):
        text = path.read_text(encoding="utf-8")
        sid_match = re.search(r"id\s*=\s*(\d+)", text)
        owner_match = re.search(r"owner\s*=\s*([A-Z]{3})", text)
        if sid_match and owner_match:
            owners[int(sid_match.group(1))] = owner_match.group(1)
    return owners


def country_base_gdp_billions(state_ids, states, pop_rows):
    total = 0.0
    for sid in state_ids:
        category = states[sid]["category"]
        population = pop_rows.get(sid, {}).get("population", states[sid].get("manpower", 0))
        total += population * CATEGORY_PER_CAPITA.get(category, CATEGORY_PER_CAPITA["town"])
    return total / 1e9


def load_authored_gdp(path=None):
    """Parse the per-country GDP authored in ZZZ_economy_definitions.txt.

    This is the GDP the economy backend uses for power consumption, so anchoring
    production to it keeps supply and demand on the same scale.
    """
    path = path or (ROOT / "common" / "on_actions" / "ZZZ_economy_definitions.txt")
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    gdp = {}
    for block in re.finditer(r"([A-Z]{3})\s*=\s*\{(.*?)initiate_display_vars", text, re.S):
        tag = block.group(1)
        match = re.search(r"GDP\s*=\s*([\d.]+)", block.group(2))
        if match and tag not in gdp:
            gdp[tag] = float(match.group(1))
    return gdp


def power_buffer(tag, gdp_billions):
    """Target production-to-consumption ratio for a country.

    Legit nations keep a flat surplus; warlords scale from a deficit (tiny,
    undeveloped) up to a surplus (large) on a log-GDP curve.
    """
    if tag in LEGITIMATE_COUNTRIES:
        return LEGIT_POWER_BUFFER
    if gdp_billions <= WARLORD_GDP_FLOOR:
        return WARLORD_BUFFER_FLOOR
    if gdp_billions >= WARLORD_GDP_CEIL:
        return WARLORD_BUFFER_CEIL
    span = math.log10(WARLORD_GDP_CEIL) - math.log10(WARLORD_GDP_FLOOR)
    frac = (math.log10(gdp_billions) - math.log10(WARLORD_GDP_FLOOR)) / span
    return WARLORD_BUFFER_FLOOR + (WARLORD_BUFFER_CEIL - WARLORD_BUFFER_FLOOR) * frac


def state_power_demand(buildings):
    """Weighted power demand of a state's non-power buildings."""
    return sum(DEMAND_WEIGHTS.get(key, 0) * value for key, value in buildings.items())


def power_to_plants(power):
    """Best integer thermo/hydro mix for a per-state power budget.

    Caps at THERMO_MAX thermo (5 each) + HYDRO_MAX hydro (10 each). Ties prefer
    fewer hydro so the cheaper, on-map thermoelectric plants dominate, matching
    the original power-building style.
    """
    best = None
    for hydro in range(HYDRO_MAX + 1):
        for thermo in range(THERMO_MAX + 1):
            value = thermo * THERMO_POWER + hydro * HYDRO_POWER
            key = (abs(value - power), hydro, -value)
            if best is None or key < best[0]:
                best = (key, thermo, hydro)
    _, thermo, hydro = best
    return thermo, hydro


def allocate_country_energy(target_power, state_weights):
    """Distribute target_power (already net of fixed nuclear/base output) across
    a country's states as thermo/hydro plant levels.

    Power is handed out in 5-unit quanta (one thermoelectric level) using the
    D'Hondt highest-averages method, so the allocation stays proportional to each
    state's size weight, conserves the total exactly (no per-state rounding
    drift), and respects the per-state power cap. Each state's quanta are then
    snapped to the best thermo/hydro mix.
    """
    energy = {sid: {} for sid in state_weights}
    if target_power <= 0 or not state_weights:
        return energy

    cap_q = STATE_ENERGY_POWER_CAP // THERMO_POWER  # 10 quanta per state
    target_q = int(round(target_power / THERMO_POWER))
    if target_q <= 0:
        return energy

    quanta = {sid: 0 for sid in state_weights}
    placed = 0
    while placed < target_q:
        best_sid = None
        best_key = None
        for sid, weight in state_weights.items():
            if quanta[sid] >= cap_q:
                continue
            # D'Hondt: the state with the smallest (assigned+1)/weight is next.
            key = ((quanta[sid] + 1) / weight, -weight)
            if best_key is None or key < best_key:
                best_key = key
                best_sid = sid
        if best_sid is None:
            break  # every state is capped
        quanta[best_sid] += 1
        placed += 1

    for sid, q in quanta.items():
        if q <= 0:
            continue
        thermo, hydro = power_to_plants(q * THERMO_POWER)
        plant = {}
        if thermo:
            plant["thermoelectric_plant"] = thermo
        if hydro:
            plant["hydroelectric_plant"] = hydro
        energy[sid] = plant
    return energy

SHARED_SLOT_KEYS = ["schools", "offices", "hospitals", "prisons", "barracks", "dockyard", "missile_silo"]
TRIM_ORDER = ["dockyard", "prisons", "offices", "schools", "hospitals", "barracks", "missile_silo"]
STATE_LEVEL_KEYS = [
    "thermoelectric_plant",
    "hydroelectric_plant",
    "nuclear_reactor",
    "schools",
    "offices",
    "hospitals",
    "prisons",
    "barracks",
    "dockyard",
    "radar_station",
    "anti_air_building",
    "missile_silo",
]

DAM_SITES = [
    ("Hoover Dam", "Las Vegas", "NV"),
    ("Grand Coulee Dam", "Columbia Basin", "WA"),
    ("Glen Canyon Dam", "Flagstaff", "AZ"),
    ("Shasta Dam", "Redding", "CA"),
    ("Oroville Dam", "Sacremento", "CA"),
    ("Fort Peck Dam", "Miles City", "MT"),
    ("Hungry Horse Dam", "Kalispell", "MT"),
    ("Garrison Dam", "Bismarck", "ND"),
    ("Oahe Dam", "Pierre", "SD"),
    ("Bonneville Dam", "Vancouver", "WA"),
    ("The Dalles Dam", "Pendleton", "OR"),
    ("John Day Dam", "Pendleton", "OR"),
    ("Chief Joseph Dam", "Columbia Basin", "WA"),
    ("Norris Dam", "Knoxville", "TN"),
    ("Kentucky Dam", "McCracken", "KY"),
    ("Wilson Dam", "Huntsville", "AL"),
    ("Hartwell Dam", "Greenville", "SC"),
    ("Conowingo Dam", "Harford", "MD"),
    ("Robert Moses Niagara Power Plant", "Buffalo", "NY"),
    ("Table Rock Dam", "Joplin", "MO"),
    ("Davis Dam", "Las Vegas", "NV"),
    ("Parker Dam", "Prescott", "AZ"),
]

NUCLEAR_SITES = [
    ("Palo Verde Nuclear Generating Station", "Phoenix", "AZ", 2),
    ("Diablo Canyon Power Plant", "Santa Barbara", "CA", 1),
    ("Vogtle Electric Generating Plant", "Columbia", "GA", 2),
    ("Browns Ferry Nuclear Plant", "Huntsville", "AL", 2),
    ("South Texas Project", "Houston", "TX", 1),
    ("Comanche Peak Nuclear Power Plant", "Dallas-Fort Worth", "TX", 1),
    ("Limerick Generating Station", "Philadelphia", "PA", 1),
    ("Peach Bottom Atomic Power Station", "York", "PA", 1),
    ("Byron Nuclear Generating Station", "Rockford", "IL", 1),
    ("Braidwood Nuclear Generating Station", "Chicago", "IL", 1),
    ("Indian Point Energy Center", "Orange", "NY", 1),
    ("Oconee Nuclear Station", "Greenville", "SC", 1),
    ("Sequoyah Nuclear Plant", "Chattanooga", "TN", 1),
    ("Watts Bar Nuclear Plant", "Knoxville", "TN", 1),
    ("Catawba Nuclear Station", "Mecklenberg", "NC", 1),
    ("McGuire Nuclear Station", "Mecklenberg", "NC", 1),
    ("Calvert Cliffs Nuclear Power Plant", "Baltimore", "MD", 1),
    ("Millstone Nuclear Power Plant", "New London", "CT", 1),
    ("Seabrook Station", "Merrimack", "NH", 1),
    ("Turkey Point Nuclear Generating Station", "Miami", "FL", 1),
]


def load_population_rows(path):
    rows = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                state_id = int(row["id"])
                pop = int(row.get("estimated_population") or row.get("current_manpower") or 0)
            except (KeyError, ValueError):
                continue
            rows[state_id] = {
                "name": row.get("name", ""),
                "population": pop,
                "source": row.get("estimate_source", ""),
            }
    return rows


def state_abbr(name):
    match = re.search(r",\s*([A-Z]{2})\b", name)
    return match.group(1) if match else ""


def normalized(text):
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def state_has_coast(state, coastal_provinces):
    return any(province in coastal_provinces for province in state["provinces"])


def first_coastal_province(state, coastal_provinces):
    for province in state["provinces"]:
        if province in coastal_provinces:
            return province
    return state["provinces"][0] if state["provinces"] else None


def is_military_base(name):
    lowered = name.lower()
    tokens = ("afb", "air force", "naval base", "naval submarine base")
    return any(token in lowered for token in tokens) or normalized(name).startswith("pantex plant")


def base_type(name):
    lowered = name.lower()
    if "naval" in lowered:
        return "naval"
    if "afb" in lowered or "air force" in lowered:
        return "air"
    if "pantex" in lowered:
        return "plant"
    return "military"


def find_best_state(site_city, site_abbr, states, pop_rows):
    city_norm = normalized(site_city)
    matches = []
    for state_id, state in states.items():
        name = pop_rows.get(state_id, {}).get("name", "")
        if state_abbr(name) != site_abbr:
            continue
        name_norm = normalized(name)
        pop = pop_rows.get(state_id, {}).get("population", state.get("manpower", 0))
        if city_norm and city_norm in name_norm:
            score = (3, pop)
        elif site_abbr:
            score = (1, pop)
        else:
            continue
        matches.append((score, state_id))
    if not matches:
        return None, "unmatched"
    matches.sort(reverse=True)
    exact = "exact" if matches[0][0][0] == 3 else "state_fallback"
    return matches[0][1], exact


def map_sites(sites, states, pop_rows):
    mapped = []
    for site in sites:
        name, city, abbr, *rest = site
        state_id, status = find_best_state(city, abbr, states, pop_rows)
        row = {"site": name, "city": city, "abbr": abbr, "state_id": state_id, "status": status}
        if rest:
            row["level"] = rest[0]
        mapped.append(row)
    return mapped


def merge_site_levels(mapped_sites):
    by_state = defaultdict(list)
    for site in mapped_sites:
        if site["state_id"] is not None:
            by_state[site["state_id"]].append(site)
    return by_state


def base_buildings(name, state, coastal_provinces):
    buildings = {
        "thermoelectric_plant": 1,
        "prisons": 1,
        "barracks": 3,
        "radar_station": 6,
        "anti_air_building": 5,
        "missile_silo": 1,
    }
    kind = base_type(name)
    if kind == "naval":
        buildings["dockyard"] = 2
    return buildings


def load_port_states(path):
    if not path.exists():
        return set()
    states = set()
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                states.add(int(row["id"]))
            except (KeyError, ValueError):
                continue
    return states


def level_from_thresholds(population, thresholds):
    return sum(1 for threshold in thresholds if population >= threshold)


def population_buildings(population, category, has_port, energy=None):
    buildings = {}
    for key, thresholds in POP_THRESHOLDS.items():
        level = level_from_thresholds(population, thresholds)
        if level:
            buildings[key] = level
    if has_port:
        buildings["dockyard"] = 1 + level_from_thresholds(population, DOCKYARD_THRESHOLDS)
    for key, value in (energy or {}).items():
        if value:
            buildings[key] = value
    return {key: value for key, value in buildings.items() if value}


def trim_to_budget(buildings, category, nuclear_level=0):
    budget = STATE_SLOTS.get(category, 0) + int(nuclear_level)
    trimmed = dict(buildings)
    while sum(trimmed.get(key, 0) for key in SHARED_SLOT_KEYS) > budget:
        changed = False
        for key in TRIM_ORDER:
            if trimmed.get(key, 0) > 0:
                trimmed[key] -= 1
                if trimmed[key] == 0:
                    trimmed.pop(key)
                changed = True
                break
        if not changed:
            break
    return trimmed


def compute_assignments(states, names, pop_rows, coastal_provinces, port_states):
    dam_sites = map_sites(DAM_SITES, states, pop_rows)
    reactor_sites = map_sites(NUCLEAR_SITES, states, pop_rows)
    dams_by_state = merge_site_levels(dam_sites)
    reactors_by_state = merge_site_levels(reactor_sites)
    owners = load_state_owners(STATE_DIR)
    authored_gdp = load_authored_gdp()

    owner_states_map = defaultdict(list)
    for sid, tag in owners.items():
        owner_states_map[tag].append(sid)

    # --- Pass 1: resolve every state's non-power buildings & fixed power -------
    # We need the final non-power buildings (after slot trimming) before we can
    # estimate each country's power demand and how much fixed power it already
    # has from nuclear reactors and military-base plants.
    state_meta = {}
    for state_id in sorted(states):
        state = states[state_id]
        name = names.get(state_id) or pop_rows.get(state_id, {}).get("name", f"STATE_{state_id}")
        is_base = is_military_base(name)
        population = pop_rows.get(state_id, {}).get("population", state.get("manpower", 0))
        has_port = state_id in port_states
        nuclear_level = min(2, sum(site.get("level", 1) for site in reactors_by_state.get(state_id, [])))
        has_dam = bool(dams_by_state.get(state_id))
        dam_province = (state["provinces"][0] if state["provinces"] else None) if has_dam else None

        if is_base:
            buildings = base_buildings(name, state, coastal_provinces)
        else:
            buildings = population_buildings(population, state["category"], has_port)
            buildings = trim_to_budget(buildings, state["category"], nuclear_level)

        # Power the state already has that is *not* allocated by us below.
        fixed_power = nuclear_level * NUCLEAR_POWER
        if is_base:
            fixed_power += buildings.get("thermoelectric_plant", 0) * THERMO_POWER

        # States eligible to receive scaled thermo/hydro: inhabited, non-base.
        eligible = (not is_base) and state["category"] not in ("wasteland",)
        state_meta[state_id] = {
            "state": state,
            "name": name,
            "is_base": is_base,
            "population": population,
            "has_port": has_port,
            "nuclear_level": nuclear_level,
            "dam_province": dam_province,
            "buildings": buildings,
            "demand": state_power_demand(buildings),
            "fixed_power": fixed_power,
            "eligible": eligible,
            "coastal": state_has_coast(state, coastal_provinces),
        }

    # --- Pass 2: per-country power target anchored to estimated consumption ----
    owner_gdp = {}
    owner_buffer = {}
    owner_energy = {}
    for tag, sids in owner_states_map.items():
        gdp = authored_gdp.get(tag)
        if gdp is None:
            gdp = country_base_gdp_billions(sids, states, pop_rows)
        owner_gdp[tag] = gdp
        demand_total = sum(state_meta[sid]["demand"] for sid in sids)
        consumption = POWER_GDP_MULT * gdp + demand_total / 2.0
        buffer = power_buffer(tag, gdp)
        owner_buffer[tag] = buffer
        # Guarantee a flat minimum surplus over consumption (for a few PUs) even
        # where the multiplicative buffer alone would only break even.
        target = max(buffer * consumption, consumption + PU_HEADROOM_POWER)
        fixed_total = sum(state_meta[sid]["fixed_power"] for sid in sids)
        remaining = max(0.0, target - fixed_total)
        weights = {
            sid: max(1, STATE_SLOTS.get(state_meta[sid]["state"]["category"], 1))
            for sid in sids
            if state_meta[sid]["eligible"]
        }
        owner_energy.update(allocate_country_energy(remaining, weights))

    # --- Pass 3: assemble rows ------------------------------------------------
    rows = []
    for state_id in sorted(states):
        meta = state_meta[state_id]
        state = meta["state"]
        owner = owners.get(state_id, "")
        nuclear_level = meta["nuclear_level"]
        buildings = dict(meta["buildings"])

        if not meta["is_base"]:
            for key, value in owner_energy.get(state_id, {}).items():
                if value:
                    buildings[key] = value
        if nuclear_level:
            buildings["nuclear_reactor"] = nuclear_level
        if state["category"] == "wasteland" and not meta["is_base"] and not meta["dam_province"] and not nuclear_level:
            buildings = {}

        row = {
            "id": state_id,
            "file": str(state["path"].relative_to(ROOT)),
            "name": meta["name"],
            "owner": owner,
            "energy_factor": round(owner_buffer.get(owner, WARLORD_BUFFER_FLOOR), 2),
            "category": state["category"],
            "population": meta["population"],
            "coastal": meta["coastal"],
            "has_port": meta["has_port"],
            "is_base": meta["is_base"],
            "base_type": base_type(meta["name"]) if meta["is_base"] else "",
            "dam_sites": "|".join(site["site"] for site in dams_by_state.get(state_id, [])),
            "dam_province": meta["dam_province"] or "",
            "reactor_sites": "|".join(site["site"] for site in reactors_by_state.get(state_id, [])),
            "shared_slots": sum(buildings.get(key, 0) for key in SHARED_SLOT_KEYS),
            "slot_budget": STATE_SLOTS.get(state["category"], 0) + nuclear_level,
        }
        for key in STATE_LEVEL_KEYS:
            row[key] = buildings.get(key, 0)
        rows.append(row)

    return rows, dam_sites, reactor_sites


def strip_managed_buildings(text, strip_air=False, strip_naval=False):
    for key in STATE_LEVEL_KEYS + ["land_facility"]:
        text = re.sub(
            rf"^[ \t]*{re.escape(key)}\s*=\s*\d+[ \t]*\n",
            "",
            text,
            flags=re.MULTILINE,
        )
    text = re.sub(
        r"^[ \t]*\d+\s*=\s*\{\s*\n[ \t]*dam\s*=\s*\d+\s*\n[ \t]*\}[ \t]*\n",
        "",
        text,
        flags=re.MULTILINE,
    )
    text = re.sub(
        r"^[ \t]*\d+\s*=\s*\{\s*dam\s*=\s*\d+\s*\}[ \t]*\n",
        "",
        text,
        flags=re.MULTILINE,
    )
    if strip_air:
        text = re.sub(r"^[ \t]*air_base\s*=\s*\d+[ \t]*\n", "", text, flags=re.MULTILINE)
    if strip_naval:
        text = re.sub(
            r"^[ \t]*\d+\s*=\s*\{\s*\n[ \t]*naval_base\s*=\s*\d+\s*\n[ \t]*\}[ \t]*\n",
            "",
            text,
            flags=re.MULTILINE,
        )
        text = re.sub(
            r"^[ \t]*\d+\s*=\s*\{\s*naval_base\s*=\s*\d+\s*\}[ \t]*\n",
            "",
            text,
            flags=re.MULTILINE,
        )
    return text


def update_buildings_file(row, states, coastal_provinces):
    path = ROOT / row["file"]
    base_kind = row["base_type"]
    text = strip_managed_buildings(
        path.read_text(encoding="utf-8"),
        strip_air=base_kind == "air",
        strip_naval=base_kind == "naval",
    )
    lines = []
    if base_kind == "air":
        lines.append("\t\t\tair_base = 10")
    for key in STATE_LEVEL_KEYS:
        value = int(row.get(key, 0) or 0)
        if value:
            lines.append(f"\t\t\t{key} = {value}")
    if row["dam_province"]:
        lines.append(f"\t\t\t{row['dam_province']} = {{")
        lines.append("\t\t\t\tdam = 1")
        lines.append("\t\t\t}")
    if base_kind == "naval":
        state = states[row["id"]]
        province = first_coastal_province(state, coastal_provinces)
        if province:
            lines.append(f"\t\t\t{province} = {{")
            lines.append("\t\t\t\tnaval_base = 10")
            lines.append("\t\t\t}")
    block = "\n" + "\n".join(lines) if lines else ""

    air_match = re.search(r"(^\s*air_base\s*=\s*\d+\s*$)", text, flags=re.MULTILINE)
    infra_match = re.search(r"(^\s*infrastructure\s*=\s*\d+\s*$)", text, flags=re.MULTILINE)
    buildings_match = re.search(r"(^\s*buildings\s*=\s*\{)", text, flags=re.MULTILINE)
    anchor = air_match or infra_match or buildings_match
    if not anchor:
        raise ValueError(f"Missing buildings block in {path}")
    new_text = text[: anchor.end()] + block + text[anchor.end() :]
    path.write_text(new_text, encoding="utf-8")


def write_csv(rows, path):
    fieldnames = [
        "id",
        "file",
        "name",
        "owner",
        "energy_factor",
        "category",
        "population",
        "coastal",
        "has_port",
        "is_base",
        "base_type",
        "dam_sites",
        "dam_province",
        "reactor_sites",
        "shared_slots",
        "slot_budget",
        *STATE_LEVEL_KEYS,
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def print_summary(rows, dam_sites, reactor_sites):
    categories = Counter(row["category"] for row in rows)
    bases = [row for row in rows if row["is_base"]]
    dam_matched = sum(1 for site in dam_sites if site["state_id"] is not None)
    reactor_matched = sum(1 for site in reactor_sites if site["state_id"] is not None)
    print(f"States processed: {len(rows)}")
    print("Category counts:")
    for category, count in sorted(categories.items()):
        print(f"  {category:>13}: {count}")
    print(f"\nMilitary-base states: {len(bases)}")
    for row in bases:
        print(f"  {row['id']:>4} {row['name']:<35} type={row['base_type']} air={row['base_type'] == 'air'} naval={row['base_type'] == 'naval'}")
    print(f"\nDams matched: {dam_matched}/{len(dam_sites)}")
    for site in dam_sites:
        if site["state_id"] is None:
            print(f"  unmatched dam: {site['site']} ({site['city']}, {site['abbr']})")
    print(f"Nuclear sites matched: {reactor_matched}/{len(reactor_sites)}")
    for site in reactor_sites:
        if site["state_id"] is None:
            print(f"  unmatched reactor: {site['site']} ({site['city']}, {site['abbr']})")
    print("\nSpot checks:")
    for row in rows:
        if row["id"] in {1, 97, 173, 187, 650, 659, 720, 764} or row["dam_sites"] or row["reactor_sites"]:
            print(
                f"  {row['id']:>4} {row['name']:<32} cat={row['category']:<11} "
                f"shared={row['shared_slots']}/{row['slot_budget']} dam={bool(row['dam_sites'])} "
                f"reactor={row['nuclear_reactor']} base={row['base_type']}"
            )


def parse_args():
    parser = argparse.ArgumentParser(description="Assign economic buildings to state history files.")
    parser.add_argument("--apply", action="store_true", help="Write buildings to state files")
    parser.add_argument("--output", default=str(OUTPUT_CSV), help="Assignments CSV output path")
    return parser.parse_args()


def main():
    args = parse_args()
    states = parse_states(STATE_DIR)
    names = load_state_names(POPULATION_CSV)
    pop_rows = load_population_rows(POPULATION_CSV)
    coastal_provinces = load_coastal_provinces(DEFINITION_CSV)
    port_states = load_port_states(PORT_ASSIGNMENTS_CSV)
    rows, dam_sites, reactor_sites = compute_assignments(
        states,
        names,
        pop_rows,
        coastal_provinces,
        port_states,
    )
    write_csv(rows, Path(args.output))
    print_summary(rows, dam_sites, reactor_sites)
    print(f"\nWrote {args.output}")
    if args.apply:
        for row in rows:
            update_buildings_file(row, states, coastal_provinces)
        print(f"Applied managed buildings to {len(rows)} state files.")
    else:
        print("Dry run only. Re-run with --apply to write state files.")


if __name__ == "__main__":
    main()
