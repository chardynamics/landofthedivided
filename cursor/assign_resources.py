#!/usr/bin/env python3
"""Assign state-level resources from real North American resource geography."""
import argparse
import csv
import re
from collections import Counter, defaultdict
from pathlib import Path

from assign_infrastructure import POPULATION_CSV, ROOT, STATE_DIR, load_state_names, parse_states

OUTPUT_CSV = ROOT / "resource_assignments.csv"
RESOURCE_KEYS = ["oil", "aluminium", "steel", "tungsten", "chromium", "uranium"]

# Global generosity multiplier applied to every curated site amount so there is
# more of every resource to go around. Overridable with --scale.
RESOURCE_SCALE = 1.6

# (resource, amount, site_label, locator, abbr)
# locator matched as substring in state name; abbr limits to ", ST" suffix when set.
RESOURCE_SITES = [
    # --- California (target ~120 oil, ~12 alu, ~8 tungsten, ~3 chromium) ---
    ("oil", 55, "Kern/Bakersfield", "Bakersfield", "CA"),
    ("oil", 22, "Santa Barbara channel", "Santa Barbara", "CA"),
    ("oil", 18, "LA basin", "Los Angeles", "CA"),
    ("oil", 12, "San Joaquin north", "Fresno", "CA"),
    ("oil", 8, "Ventura/Santa Barbara fringe", "Santa Barbara", "CA"),
    ("aluminium", 4, "LA smelting", "Los Angeles", "CA"),
    ("aluminium", 3, "Bay Area", "San Francisco", "CA"),
    ("aluminium", 2, "Central Valley", "Bakersfield", "CA"),
    ("aluminium", 2, "San Diego", "San Diego", "CA"),
    ("tungsten", 5, "Inyo/Kern tungsten", "Bakersfield", "CA"),
    ("tungsten", 3, "Sierra Nevada", "Fresno", "CA"),
    ("chromium", 2, "Coastal chromite", "Santa Barbara", "CA"),
    ("chromium", 1, "Northern CA", "Redding", "CA"),
    # --- Texas (target ~220 oil, ~80 alu, ~8 uranium) ---
    ("oil", 72, "Houston/Gulf Coast", "Houston", "TX"),
    ("oil", 58, "Permian Basin", "Odessa", "TX"),
    ("oil", 28, "Corpus/Laredo shelf", "Corpus Christi", "TX"),
    ("oil", 22, "Eagle Ford", "Laredo", "TX"),
    ("oil", 18, "Panhandle", "Amarillo", "TX"),
    ("oil", 14, "West TX", "Lubbock", "TX"),
    ("oil", 12, "East TX", "Beaumont", "TX"),
    ("oil", 8, "North TX", "Wichita Falls", "TX"),
    ("aluminium", 38, "Gulf Coast refining", "Houston", "TX"),
    ("aluminium", 22, "DFW industrial", "Dallas-Fort Worth", "TX"),
    ("aluminium", 12, "East TX", "Beaumont", "TX"),
    ("aluminium", 8, "South TX", "Corpus Christi", "TX"),
    ("uranium", 5, "South Texas uranium", "Corpus Christi", "TX"),
    ("uranium", 3, "Houston belt", "Houston", "TX"),
    # --- Other US oil ---
    ("oil", 42, "Bakken", "Minot", "ND"),
    ("oil", 18, "Williston fringe", "Dickinson", "ND"),
    ("oil", 35, "Oklahoma City", "Oklahoma City", "OK"),
    ("oil", 28, "Tulsa", "Tulsa", "OK"),
    ("oil", 32, "Louisiana shelf", "Baton Rouge", "LA"),
    ("oil", 22, "North LA", "Shreveport", "LA"),
    ("oil", 26, "SE New Mexico", "Carlsbad", "NM"),
    ("oil", 18, "San Juan basin", "Farmington", "NM"),
    ("oil", 24, "Powder River", "Casper", "WY"),
    ("oil", 16, "Denver Julesburg", "Denver", "CO"),
    ("oil", 12, "Kansas", "Wichita", "KS"),
    ("oil", 14, "Utah Uinta", "Salt Lake City", "UT"),
    ("oil", 10, "Nevada fringe", "Elko", "NV"),
    # --- Canada oil ---
    ("oil", 95, "Athabasca oil sands", "Northern Alberta", ""),
    ("oil", 28, "Peace River", "Northern British Columbia", ""),
    # --- Mexico / Gulf oil ---
    ("oil", 68, "Veracruz/Tampico", "Veracruz", ""),
    ("oil", 32, "Matamoros/Reynosa", "Matamoros", "TA"),
    ("oil", 22, "Monterrey industrial belt", "Monterrey", "NL"),
    ("oil", 18, "Chihuahua", "Chihuahua", ""),
    ("oil", 14, "Sonora", "Sonora", ""),
    ("oil", 10, "Baja", "Baja California", ""),
    # --- Aluminium ---
    ("aluminium", 18, "Arkansas bauxite", "Little Rock", "AR"),
    ("aluminium", 12, "Bauxite AR", "Hot Springs", "AR"),
    ("aluminium", 22, "Columbia Basin smelting", "Columbia Basin", "WA"),
    ("aluminium", 14, "Tennessee Valley", "Knoxville", "TN"),
    ("aluminium", 10, "Kentucky", "Louisville", "KY"),
    ("aluminium", 8, "Oregon", "Medford", "OR"),
    ("aluminium", 42, "Quebec smelting", "Quebec", ""),
    ("aluminium", 18, "BC Kitimat", "British Columbia", ""),
    ("aluminium", 12, "Northern BC", "Northern British Columbia", ""),
    ("aluminium", 8, "Massena NY", "Watertown", "NY"),
    ("aluminium", 6, "Buffalo", "Buffalo", "NY"),
    # --- Steel (iron ore / steel belt) ---
    ("steel", 48, "Mesabi Range", "Duluth", "MN"),
    ("steel", 22, "Iron Range MN", "Brainerd", "MN"),
    ("steel", 28, "Upper Peninsula", "Marquette", "MI"),
    ("steel", 18, "UP copper/iron", "Houghton", "MI"),
    ("steel", 32, "Birmingham", "Birmingham", "AL"),
    ("steel", 38, "Pittsburgh", "Allegheny", "PA"),
    ("steel", 14, "Utah", "Salt Lake City", "UT"),
    ("steel", 12, "Wyoming", "Casper", "WY"),
    ("steel", 40, "Labrador Trough", "Labrador", ""),
    ("steel", 28, "Quebec iron", "Quebec", ""),
    ("steel", 32, "Ontario", "Ontario", ""),
    ("steel", 30, "Coahuila/Monclova", "Coahuila", ""),
    ("steel", 10, "Indiana", "Gary", "IN"),
    ("steel", 8, "Ohio", "Cleveland", "OH"),
    # --- Uranium ---
    ("uranium", 14, "Grants district", "Farmington", "NM"),
    ("uranium", 10, "SE NM", "Carlsbad", "NM"),
    ("uranium", 12, "Powder River", "Casper", "WY"),
    ("uranium", 8, "Colorado", "Denver", "CO"),
    ("uranium", 6, "Utah", "Salt Lake City", "UT"),
    ("uranium", 5, "Arizona", "Phoenix", "AZ"),
    ("uranium", 4, "Tucson", "Tucson", "AZ"),
    ("uranium", 32, "Athabasca basin", "Northern Saskatchwan", ""),
    ("uranium", 14, "Ontario Elliot Lake", "Ontario", ""),
    # --- Tungsten (small, western US) ---
    ("tungsten", 6, "Colorado", "Denver", "CO"),
    ("tungsten", 5, "Nevada", "Elko", "NV"),
    ("tungsten", 4, "North Carolina", "Buncombe", "NC"),
    ("tungsten", 4, "Idaho", "Boise", "ID"),
    ("tungsten", 3, "Montana", "Butte", "MT"),
    ("tungsten", 2, "Montana east", "Great Falls", "MT"),
    # --- Chromium ---
    ("chromium", 12, "Stillwater MT", "Billings", "MT"),
    ("chromium", 6, "Oregon Coast", "Eugene", "OR"),
    ("chromium", 4, "Montana", "Kalispell", "MT"),
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
            }
    return rows


def state_abbr(name):
    match = re.search(r",\s*([A-Z]{2})\b", name)
    return match.group(1) if match else ""


def normalized(text):
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def find_best_state(locator, abbr, states, pop_rows):
    loc_norm = normalized(locator)
    matches = []
    for state_id in states:
        name = pop_rows.get(state_id, {}).get("name", "")
        name_norm = normalized(name)
        if abbr and state_abbr(name) != abbr:
            continue
        if not abbr and loc_norm not in name_norm:
            continue
        pop = pop_rows.get(state_id, {}).get("population", states[state_id].get("manpower", 0))
        if abbr:
            if loc_norm and loc_norm in name_norm:
                score = (3, pop)
            else:
                score = (1, pop)
        else:
            if name_norm == loc_norm:
                score = (4, pop)
            elif name_norm.startswith(loc_norm) or loc_norm in name_norm:
                score = (3, pop)
            else:
                score = (2, pop)
        matches.append((score, state_id))
    if not matches:
        return None, "unmatched"
    matches.sort(reverse=True)
    top_score = matches[0][0][0]
    if top_score >= 3:
        status = "exact"
    elif abbr:
        status = "state_fallback"
    else:
        status = "name_match"
    return matches[0][1], status


def map_resource_sites(states, pop_rows, scale=RESOURCE_SCALE):
    mapped = []
    for resource, amount, label, locator, abbr in RESOURCE_SITES:
        state_id, status = find_best_state(locator, abbr, states, pop_rows)
        mapped.append(
            {
                "resource": resource,
                "amount": int(round(amount * scale)),
                "site": label,
                "locator": locator,
                "abbr": abbr,
                "state_id": state_id,
                "status": status,
            }
        )
    return mapped


def aggregate_by_state(mapped_sites, states, pop_rows, names):
    by_state = defaultdict(lambda: defaultdict(float))
    for site in mapped_sites:
        if site["state_id"] is None:
            continue
        by_state[site["state_id"]][site["resource"]] += site["amount"]
    rows = []
    for state_id in sorted(by_state):
        state = states[state_id]
        resources = dict(by_state[state_id])
        name = names.get(state_id) or pop_rows.get(state_id, {}).get("name", f"STATE_{state_id}")
        row = {
            "id": state_id,
            "file": str(state["path"].relative_to(ROOT)),
            "name": name,
            "abbr": state_abbr(name),
        }
        for key in RESOURCE_KEYS:
            row[key] = resources.get(key, 0)
        rows.append(row)
    return rows


def strip_resources_block(text):
    pattern = re.compile(r"^[ \t]*resources\s*=\s*\{.*?\n[ \t]*\}[ \t]*\n", re.MULTILINE | re.DOTALL)
    return pattern.sub("", text)


def format_resource_value(value):
    if abs(value - round(value)) < 1e-6:
        return str(int(round(value)))
    return f"{value:.3f}"


def format_resources_block(resources):
    lines = ["\tresources = {"]
    for key in RESOURCE_KEYS:
        if resources.get(key, 0):
            lines.append(f"\t\t{key} = {format_resource_value(resources[key])}")
    lines.append("\t}")
    return "\n".join(lines) + "\n"


def upsert_resources(path, resources):
    if not any(resources.get(key, 0) for key in RESOURCE_KEYS):
        return
    text = strip_resources_block(path.read_text(encoding="utf-8"))
    block = format_resources_block(resources)
    match = re.search(r"^[ \t]*provinces\s*=\s*\{", text, flags=re.MULTILINE)
    if not match:
        raise ValueError(f"Missing provinces block in {path}")
    new_text = text[: match.start()] + block + text[match.start() :]
    path.write_text(new_text, encoding="utf-8")


def write_csv(rows, path):
    fieldnames = ["id", "file", "name", "abbr", *RESOURCE_KEYS]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            out = dict(row)
            for key in RESOURCE_KEYS:
                out[key] = format_resource_value(row.get(key, 0) or 0)
            writer.writerow(out)


def sum_by_abbr(rows, abbr):
    totals = defaultdict(float)
    for row in rows:
        if row["abbr"] != abbr:
            continue
        for key in RESOURCE_KEYS:
            totals[key] += float(row.get(key, 0) or 0)
    return dict(totals)


def print_summary(mapped_sites, assignment_rows):
    unmatched = [s for s in mapped_sites if s["state_id"] is None]
    continent = Counter()
    state_counts = Counter()
    for row in assignment_rows:
        for key in RESOURCE_KEYS:
            val = float(row.get(key, 0) or 0)
            if val:
                continent[key] += val
                state_counts[key] += 1
    print(f"Resource sites mapped: {len(mapped_sites) - len(unmatched)}/{len(mapped_sites)}")
    print(f"States with resources: {len(assignment_rows)}")
    print("\nContinent totals:")
    for key in RESOURCE_KEYS:
        print(f"  {key:>11}: {continent.get(key, 0):.0f} ({state_counts.get(key, 0)} states)")
    print("\nUS state totals (sanity vs CA/TX references):")
    for abbr, label in [("CA", "California"), ("TX", "Texas")]:
        totals = sum_by_abbr(assignment_rows, abbr)
        parts = ", ".join(f"{k}={totals.get(k, 0):.0f}" for k in RESOURCE_KEYS if totals.get(k, 0))
        print(f"  {label}: {parts or '(none)'}")
    print("\nTop states per resource:")
    for key in RESOURCE_KEYS:
        top = sorted(assignment_rows, key=lambda r: float(r.get(key, 0) or 0), reverse=True)[:5]
        top = [r for r in top if float(r.get(key, 0) or 0)]
        if not top:
            continue
        print(f"  {key}:")
        for row in top:
            print(f"    {row['id']:>4} {row['name']:<32} {float(row[key]):.0f}")
    if unmatched:
        print("\nUnmatched sites:")
        for site in unmatched:
            print(f"  {site['resource']:>11} {site['amount']:>4} {site['site']} ({site['locator']}, {site['abbr']})")


def parse_args():
    parser = argparse.ArgumentParser(description="Assign state resources from geographic sites.")
    parser.add_argument("--apply", action="store_true", help="Write resources blocks to state files")
    parser.add_argument("--output", default=str(OUTPUT_CSV), help="Assignments CSV output path")
    parser.add_argument(
        "--scale",
        type=float,
        default=RESOURCE_SCALE,
        help="Multiplier applied to all curated site amounts",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    states = parse_states(STATE_DIR)
    names = load_state_names(POPULATION_CSV)
    pop_rows = load_population_rows(POPULATION_CSV)
    mapped_sites = map_resource_sites(states, pop_rows, args.scale)
    assignment_rows = aggregate_by_state(mapped_sites, states, pop_rows, names)
    write_csv(assignment_rows, Path(args.output))
    print_summary(mapped_sites, assignment_rows)
    print(f"\nWrote {args.output}")
    if args.apply:
        for row in assignment_rows:
            resources = {key: row[key] for key in RESOURCE_KEYS if float(row.get(key, 0) or 0)}
            upsert_resources(ROOT / row["file"], resources)
        print(f"Applied resources to {len(assignment_rows)} state files.")
    else:
        print("Dry run only. Re-run with --apply to write state files.")


if __name__ == "__main__":
    main()
