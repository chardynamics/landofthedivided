#!/usr/bin/env python3
"""Add cities from VP-less LOTD states into city_database.py via geocoding."""
from __future__ import annotations

import argparse
import csv
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

from assign_infrastructure import POPULATION_CSV, STATE_DIR, load_state_names, parse_states
from city_database import CITY_BY_KEY

ROOT = Path(__file__).resolve().parent
OUTPUT_JSON = ROOT / "city_expansion_generated.json"
DATABASE_FILE = ROOT / "city_database.py"
MARKER = "# VP-less state coverage:"

US = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia", "PR": "Puerto Rico",
}
CA = {
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba", "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador", "NS": "Nova Scotia", "NT": "Northwest Territories",
    "NU": "Nunavut", "ON": "Ontario", "PE": "Prince Edward Island", "QC": "Quebec",
    "SK": "Saskatchewan", "YT": "Yukon",
}
MX = {
    "AG": "Aguascalientes", "BC": "Baja California", "BS": "Baja California Sur", "CM": "Campeche",
    "CS": "Chiapas", "CH": "Chihuahua", "CO": "Coahuila", "CL": "Colima", "DF": "Mexico City",
    "DG": "Durango", "GT": "Guanajuato", "GR": "Guerrero", "HG": "Hidalgo", "JA": "Jalisco",
    "EM": "Mexico", "MI": "Michoacan", "MO": "Morelos", "NA": "Nayarit", "NL": "Nuevo Leon",
    "OA": "Oaxaca", "PU": "Puebla", "QT": "Queretaro", "QR": "Quintana Roo", "SL": "San Luis Potosi",
    "SI": "Sinaloa", "SO": "Sonora", "TB": "Tabasco", "TL": "Tlaxcala", "TM": "Tamaulipas",
    "VE": "Veracruz", "YU": "Yucatan", "ZA": "Zacatecas",
}
CARIBBEAN = {"BS", "JM", "HT", "DO", "CU", "TT", "BB", "KY", "GC", "VG", "PR"}

# County / island / typo overrides for geocoder queries.
GEOCODE_ALIASES: dict[tuple[str, str], str] = {
    ("Adalair", "MO"): "Adair, Missouri, USA",
    ("Howard", "IN"): "Kokomo, Indiana, USA",
    ("Buncombe", "NC"): "Asheville, North Carolina, USA",
    ("Christian", "KY"): "Hopkinsville, Kentucky, USA",
    ("Land Between The Lakes", "TN"): "Grand Rivers, Kentucky, USA",
    ("Magladen Islands", "QC"): "Magdalen Islands, Quebec, Canada",
    ("ile Bizardl", "QC"): "Ile-Bizard, Quebec, Canada",
    ("ile Jesus", "QC"): "Laval, Quebec, Canada",
    ("Vaudreuil-Soulanges", "QC"): "Vaudreuil-Dorion, Quebec, Canada",
    ("Monteregie", "QC"): "Longueuil, Quebec, Canada",
    ("Eastern Townships", "QC"): "Sherbrooke, Quebec, Canada",
    ("Worchester", "MA"): "Worcester, Massachusetts, USA",
    ("Spotslyvania", "VA"): "Spotsylvania, Virginia, USA",
    ("Furnass", "NE"): "Beaver City, Nebraska, USA",
    ("Eleuthera", "BS"): "Eleuthera, Bahamas",
    ("Andros", "BS"): "Andros Island, Bahamas",
    ("Cat Island", "BS"): "Cat Island, Bahamas",
    ("Long Island", "BS"): "Long Island, Bahamas",
    ("Montemorelos", "NL"): "Montemorelos, Nuevo Leon, Mexico",
    ("Laredos", "TM"): "Laredo, Tamaulipas, Mexico",
}

MANUAL_ENTRIES: list[tuple[str, str, float, float, int]] = [
    ("South Bairoil", "WY", -107.5559, 41.0575, 5000),
    ("Randell", "UT", -109.2513, 40.7397, 8000),
    ("Tiptonville", "KY", -88.5717, 36.7284, 15000),
    ("Monctezuma", "SL", -103.2667, 22.6833, 15000),
    ("Great Exuma", "BS", -75.7333, 23.5167, 7000),
    ("Crooked Island", "BS", -74.8333, 22.7500, 500),
    ("Mayaguna", "BS", -73.5833, 22.3833, 300),
    ("Inagua", "BS", -73.6667, 21.0833, 1200),
]

REGION_LABELS = {
    **{abbr: name for abbr, name in US.items()},
    **{abbr: name for abbr, name in CA.items()},
    **{abbr: name for abbr, name in MX.items()},
    "BS": "Bahamas",
}


def states_without_victory_points() -> set[int]:
    states = parse_states(STATE_DIR)
    missing = set()
    for state_id, state in states.items():
        if not re.search(r"^\s*victory_points\s*=", state["path"].read_text(), re.M):
            missing.add(state_id)
    return missing


def missing_city_targets() -> list[tuple[str, str, str]]:
    names = load_state_names(POPULATION_CSV)
    valid_abbrs = set(US) | set(CA) | set(MX) | CARIBBEAN
    targets = []
    for state_id in sorted(states_without_victory_points()):
        name = names.get(state_id, "")
        parts = [part.strip() for part in name.split(",")]
        if len(parts) != 2:
            continue
        city, abbr = parts
        if abbr not in valid_abbrs:
            continue
        if (city.lower(), abbr) in CITY_BY_KEY:
            continue
        targets.append((city, abbr, name))
    return targets


def load_state_populations() -> dict[tuple[str, str], int]:
    pops: dict[tuple[str, str], int] = {}
    with POPULATION_CSV.open() as handle:
        for row in csv.DictReader(handle):
            parts = [part.strip() for part in row["name"].split(",")]
            if len(parts) == 2:
                pops[(parts[0].lower(), parts[1])] = int(row["estimated_population"] or 0)
    return pops


def geocode_query(city: str, abbr: str) -> str:
    alias = GEOCODE_ALIASES.get((city, abbr))
    if alias:
        return alias
    if abbr in CARIBBEAN and abbr != "PR":
        return f"{city}, Bahamas" if abbr == "BS" else f"{city}, {abbr}"
    if abbr in US:
        return f"{city}, {US[abbr]}, USA"
    if abbr in CA:
        return f"{city}, {CA[abbr]}, Canada"
    if abbr in MX:
        return f"{city}, {MX[abbr]}, Mexico"
    return f"{city}, {abbr}"


def geocode(query: str) -> tuple[float, float] | None:
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "limit": 1}
    )
    request = urllib.request.Request(url, headers={"User-Agent": "LOTD-mod-city-expander/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode())
    if not payload:
        return None
    return float(payload[0]["lon"]), float(payload[0]["lat"])


def collect_entries(delay_s: float = 1.1) -> tuple[list[tuple[str, str, float, float, int]], list[tuple[str, str, str]]]:
    pops = load_state_populations()
    results: list[tuple[str, str, float, float, int]] = []
    failed: list[tuple[str, str, str]] = []
    for city, abbr, _ in missing_city_targets():
        query = geocode_query(city, abbr)
        try:
            coords = geocode(query)
            if not coords:
                failed.append((city, abbr, query))
                continue
            lon, lat = coords
            pop = pops.get((city.lower(), abbr), 25000)
            if pop < 500:
                pop = 5000
            results.append((city, abbr, lon, lat, pop))
            print(f"OK {city}, {abbr}")
        except Exception as exc:  # noqa: BLE001 - report all geocoder failures
            failed.append((city, abbr, str(exc)))
        time.sleep(delay_s)

    seen = {(name.lower(), abbr) for name, abbr, *_ in results}
    for entry in MANUAL_ENTRIES:
        key = (entry[0].lower(), entry[1])
        if key not in seen:
            results.append(entry)
            seen.add(key)
    return results, failed


def format_block(entries: list[tuple[str, str, float, float, int]]) -> str:
    unique: list[tuple[str, str, float, float, int]] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        key = (entry[0].lower(), entry[1])
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)

    by_abbr: dict[str, list[tuple[str, str, float, float, int]]] = {}
    for entry in sorted(unique, key=lambda item: (item[1], item[0])):
        by_abbr.setdefault(entry[1], []).append(entry)

    lines = [
        "",
        MARKER + " cities named in LOTD state files but missing from the DB above.",
        "CITY_ENTRIES += [",
    ]
    for abbr in sorted(by_abbr):
        lines.append(f"    # --- {REGION_LABELS.get(abbr, abbr)} ---")
        for name, region, lon, lat, pop in by_abbr[abbr]:
            escaped = name.replace('"', '\\"')
            lines.append(f'    ("{escaped}", "{region}", {lon:.4f}, {lat:.4f}, {pop}),')
    lines.append("]")
    return "\n".join(lines)


def apply_to_database(block: str) -> None:
    text = DATABASE_FILE.read_text()
    pattern = re.compile(r"\n" + re.escape(MARKER) + r".*?\nCITY_ENTRIES \+= \[.*?\]\n", re.S)
    if pattern.search(text):
        text = pattern.sub(block + "\n", text, count=1)
    else:
        text = text.replace("# fmt: on\n", block + "\n# fmt: on\n", 1)
    DATABASE_FILE.write_text(text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Patch city_database.py with geocoded entries")
    parser.add_argument("--delay", type=float, default=1.1, help="Seconds between Nominatim requests")
    args = parser.parse_args()

    targets = missing_city_targets()
    print(f"Missing city targets for VP-less states: {len(targets)}")
    if not targets:
        return

    results, failed = collect_entries(delay_s=args.delay)
    OUTPUT_JSON.write_text(json.dumps({"results": results, "failed": failed}, indent=2))
    print(f"Geocoded {len(results)} cities; {len(failed)} failed -> {OUTPUT_JSON.name}")

    if args.apply:
        apply_to_database(format_block(results))
        print(f"Updated {DATABASE_FILE.name}")


if __name__ == "__main__":
    main()
