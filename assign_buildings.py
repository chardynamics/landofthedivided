#!/usr/bin/env python3
"""Assign state buildings from category, population, and known real-world sites."""
import argparse
import csv
import re
from collections import Counter, defaultdict
from pathlib import Path

from assign_infrastructure import POPULATION_CSV, ROOT, STATE_DIR, load_state_names, parse_states
from assign_ports import load_coastal_provinces

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

# Power generation by category, tuned to ~50% of the original energy total so
# states keep an energy surplus without everyone being self-sufficient. The
# smallest categories (rural and below) get no power plants.
ENERGY_TEMPLATE = {
    "megalopolis": {"thermoelectric_plant": 3, "hydroelectric_plant": 2},
    "metropolis": {"thermoelectric_plant": 2, "hydroelectric_plant": 2},
    "large_city": {"thermoelectric_plant": 2, "hydroelectric_plant": 1},
    "city": {"thermoelectric_plant": 2, "hydroelectric_plant": 1},
    "large_town": {"thermoelectric_plant": 1},
    "town": {"thermoelectric_plant": 1},
    "rural": {},
    "pastoral": {},
    "enclave": {},
    "small_island": {},
    "tiny_island": {},
    "wasteland": {},
}

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


def population_buildings(population, category, has_port):
    buildings = {}
    for key, thresholds in POP_THRESHOLDS.items():
        level = level_from_thresholds(population, thresholds)
        if level:
            buildings[key] = level
    if has_port:
        buildings["dockyard"] = 1 + level_from_thresholds(population, DOCKYARD_THRESHOLDS)
    for key, value in ENERGY_TEMPLATE.get(category, {}).items():
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

        dam_province = None
        if has_dam:
            dam_province = state["provinces"][0] if state["provinces"] else None

        if is_base:
            buildings = base_buildings(name, state, coastal_provinces)
            if nuclear_level:
                buildings["nuclear_reactor"] = nuclear_level
        else:
            buildings = population_buildings(population, state["category"], has_port)
            if nuclear_level:
                buildings["nuclear_reactor"] = nuclear_level
            buildings = trim_to_budget(buildings, state["category"], nuclear_level)

        if state["category"] == "wasteland" and not is_base and not dam_province and not nuclear_level:
            buildings = {}

        row = {
            "id": state_id,
            "file": str(state["path"].relative_to(ROOT)),
            "name": name,
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
