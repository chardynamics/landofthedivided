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

# Power generation by category (original high-energy levels). Every inhabited
# category gets at least one thermoelectric plant, with larger categories
# stacking thermo + hydro for a strong energy surplus.
ENERGY_TEMPLATE = {
    "megalopolis": {"thermoelectric_plant": 4, "hydroelectric_plant": 3},
    "metropolis": {"thermoelectric_plant": 4, "hydroelectric_plant": 2},
    "large_city": {"thermoelectric_plant": 3, "hydroelectric_plant": 2},
    "city": {"thermoelectric_plant": 3, "hydroelectric_plant": 1},
    "large_town": {"thermoelectric_plant": 2, "hydroelectric_plant": 1},
    "town": {"thermoelectric_plant": 2, "hydroelectric_plant": 1},
    "rural": {"thermoelectric_plant": 1},
    "pastoral": {"thermoelectric_plant": 1},
    "enclave": {"thermoelectric_plant": 1},
    "small_island": {"thermoelectric_plant": 1},
    "tiny_island": {"thermoelectric_plant": 1},
    "wasteland": {},
}

# Energy is scaled per owner: better-off / developed countries keep 75% of the
# original power buildings, while the least developed warlord states drop to 50%.
# The development tier is keyed off the lore-based economy archetypes
# (see generate_economy_definitions.py) so it stays consistent with the economy.
DEV_FACTOR_BY_ARCHETYPE = {
    "stable_developed": 0.75,        # Canada
    "social_democratic": 0.75,       # California (CAS), NYC, etc.
    "business_booming": 0.75,        # Gulf Coast
    "tax_haven": 0.75,               # BVI
    "island_micro_stable": 0.70,     # Bahamas, PR, Vermont
    "canadian_backed": 0.70,         # Minnesota, Detroit, Maine mission
    "stable_developing": 0.65,       # Mexico, Dominican Republic
    "state_continuation": 0.60,      # surviving state governments
    "libertarian": 0.60,             # Wyoming, Boise
    "military_authority": 0.60,      # USMA charter states
    "poor_stable": 0.55,             # Haiti
    "neutral_contested": 0.55,       # Missouri, Nevada, etc.
    "populist_uprising": 0.55,       # Baltimore, ALF, West Virginia
    "tribal": 0.55,                  # Navajo
    "communist": 0.55,               # Boston Red Army, Cuba
    "socialist_authoritarian": 0.55, # Emergency Federal Government
    "decentralized_weak": 0.50,      # National Guard Illinois
    "movement_rump": 0.50,           # declining Movement branches
    "movement_establishment": 0.50,  # Philadelphia
    "foreign_occupation_failing": 0.50,  # Mexican border administrations
    "failing_uprising": 0.50,        # Syracuse
}
# States with no owner / no archetype get a middle-of-the-road factor.
DEFAULT_DEV_FACTOR = 0.60

# Real-world sovereign nations (the "Vanilla" tags) keep the gentle 50-75%
# development scaling from the archetype table above. Every other tag is a
# collapsed US successor state whose power is instead scaled by its economic size
# (GDP): the large industrial powers (California, the Emergency Federal
# Government) keep a lot of power, while the small frontier warlords (Wyoming,
# the Great Basin, etc.) bottom out at a 30% floor. Scaling is on a log curve
# because warlord GDP spans ~$0.1B to ~$260B.
LEGITIMATE_COUNTRIES = {"CAN", "MEX", "CUB", "DOM", "HAI", "BAH", "FRA", "ENG"}
WARLORD_FLOOR = 0.30      # smallest warlords keep 30% of original power
WARLORD_CEILING = 0.72    # largest warlord (California) keeps 72%
WARLORD_GDP_LO = 2.0      # GDP ($B) at/below which a warlord hits the floor


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


def energy_factor_for_owner(tag, gdp_billions=0.0, warlord_gdp_hi=1.0):
    if tag in LEGITIMATE_COUNTRIES:
        archetype = TAG_ARCHETYPE.get(tag)
        return DEV_FACTOR_BY_ARCHETYPE.get(archetype, DEFAULT_DEV_FACTOR)
    # Warlord: scale by economic size on a log curve, floored at WARLORD_FLOOR.
    if gdp_billions <= WARLORD_GDP_LO or warlord_gdp_hi <= WARLORD_GDP_LO:
        return WARLORD_FLOOR
    span = math.log10(warlord_gdp_hi) - math.log10(WARLORD_GDP_LO)
    frac = (math.log10(gdp_billions) - math.log10(WARLORD_GDP_LO)) / span
    frac = max(0.0, min(1.0, frac))
    return round(WARLORD_FLOOR + (WARLORD_CEILING - WARLORD_FLOOR) * frac, 2)


def scale_country_energy(states_energy, factor):
    """Reduce a country's power buildings to ~factor of its original total.

    states_energy maps state_id -> {"thermoelectric_plant": x, "hydroelectric_plant": y}.
    Scaling is done at the country level (not per state) so the requested
    percentage is honored despite tiny per-state integer counts. Reductions hit
    the most-powered states first (hydro before thermo), keeping the spread of
    plants roughly proportional to each state's original size.
    """
    scaled = {sid: dict(energy) for sid, energy in states_energy.items()}
    original_total = sum(sum(energy.values()) for energy in states_energy.values())
    if original_total == 0:
        return scaled
    target = int(math.floor(original_total * factor + 0.5))
    current = original_total
    while current > target:
        best_sid = None
        best_rank = None
        for sid, energy in scaled.items():
            power = energy.get("thermoelectric_plant", 0) + energy.get("hydroelectric_plant", 0)
            if power <= 0:
                continue
            rank = (power, sum(states_energy[sid].values()), sid)
            if best_rank is None or rank > best_rank:
                best_sid = sid
                best_rank = rank
        if best_sid is None:
            break
        energy = scaled[best_sid]
        original = states_energy[best_sid]
        # Remove the plant type that is currently "over-represented" relative to
        # its original share, so each state keeps its thermo:hydro mix.
        thermo, hydro = energy.get("thermoelectric_plant", 0), energy.get("hydroelectric_plant", 0)
        orig_thermo = original.get("thermoelectric_plant", 0)
        orig_hydro = original.get("hydroelectric_plant", 0)
        thermo_share = thermo / orig_thermo if orig_thermo else -1.0
        hydro_share = hydro / orig_hydro if orig_hydro else -1.0
        if hydro > 0 and (hydro_share > thermo_share or thermo == 0):
            remove_key = "hydroelectric_plant"
        else:
            remove_key = "thermoelectric_plant"
        energy[remove_key] -= 1
        if energy[remove_key] == 0:
            energy.pop(remove_key)
        current -= 1
    return scaled

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

    # Per-owner economic size (GDP) drives warlord power scaling.
    owner_states_map = defaultdict(list)
    for sid, tag in owners.items():
        owner_states_map[tag].append(sid)
    owner_gdp = {
        tag: country_base_gdp_billions(sids, states, pop_rows)
        for tag, sids in owner_states_map.items()
    }
    warlord_gdp_hi = max(
        (gdp for tag, gdp in owner_gdp.items() if tag not in LEGITIMATE_COUNTRIES),
        default=1.0,
    )
    owner_factor = {
        tag: energy_factor_for_owner(tag, owner_gdp.get(tag, 0.0), warlord_gdp_hi)
        for tag in owner_states_map
    }

    # Pre-compute power buildings per state, scaled at the country (owner) level
    # so each country's power lands at its development/GDP-based factor.
    energy_by_owner = defaultdict(dict)
    for state_id in sorted(states):
        state = states[state_id]
        name = names.get(state_id) or pop_rows.get(state_id, {}).get("name", f"STATE_{state_id}")
        if is_military_base(name):
            continue
        original_energy = {key: value for key, value in ENERGY_TEMPLATE.get(state["category"], {}).items() if value}
        if original_energy:
            energy_by_owner[owners.get(state_id, "")][state_id] = original_energy
    scaled_energy = {}
    for owner, states_energy in energy_by_owner.items():
        factor = owner_factor.get(owner, WARLORD_FLOOR)
        scaled_energy.update(scale_country_energy(states_energy, factor))

    rows = []

    for state_id in sorted(states):
        state = states[state_id]
        name = names.get(state_id) or pop_rows.get(state_id, {}).get("name", f"STATE_{state_id}")
        coastal = state_has_coast(state, coastal_provinces)
        has_port = state_id in port_states
        is_base = is_military_base(name)
        population = pop_rows.get(state_id, {}).get("population", state.get("manpower", 0))
        has_dam = bool(dams_by_state.get(state_id))
        nuclear_level = min(2, sum(site.get("level", 1) for site in reactors_by_state.get(state_id, [])))
        owner = owners.get(state_id, "")
        energy_factor = owner_factor.get(owner, WARLORD_FLOOR)

        dam_province = None
        if has_dam:
            dam_province = state["provinces"][0] if state["provinces"] else None

        if is_base:
            buildings = base_buildings(name, state, coastal_provinces)
            if nuclear_level:
                buildings["nuclear_reactor"] = nuclear_level
        else:
            buildings = population_buildings(population, state["category"], has_port, scaled_energy.get(state_id))
            if nuclear_level:
                buildings["nuclear_reactor"] = nuclear_level
            buildings = trim_to_budget(buildings, state["category"], nuclear_level)

        if state["category"] == "wasteland" and not is_base and not dam_province and not nuclear_level:
            buildings = {}

        row = {
            "id": state_id,
            "file": str(state["path"].relative_to(ROOT)),
            "name": name,
            "owner": owner,
            "energy_factor": energy_factor,
            "category": state["category"],
            "population": pop_rows.get(state_id, {}).get("population", state.get("manpower", 0)),
            "coastal": coastal,
            "has_port": has_port,
            "is_base": is_base,
            "base_type": base_type(name) if is_base else "",
            "dam_sites": "|".join(site["site"] for site in dams_by_state.get(state_id, [])),
            "dam_province": dam_province or "",
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
