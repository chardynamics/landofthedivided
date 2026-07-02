#!/usr/bin/env python3
"""Estimate state manpower from population grid cells + 2000 census scaling."""
from __future__ import annotations

import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.interpolate import RBFInterpolator

from assign_infrastructure import (
    DEFINITION_CSV,
    POPULATION_CSV,
    PROVINCES_BMP,
    ROOT,
    STATE_DIR,
    load_state_names,
    parse_definition,
    parse_states,
    rasterize_provinces,
    state_geometry,
)
from assign_victory_points import (
    ANCHOR_SNAPSHOT,
    VP_LOCALISATION,
    build_region_hints,
    compute_province_centroids,
    fit_forward_rbf,
    load_city_db,
    locate_city_province,
    parse_known_vp_localisations,
    project_to_pixel,
    resolve_hint_states,
)
from city_database import merged_city_entries
from import_population_grid import CELLS_CSV, GRID_DIR, METADATA_PATH, POP_NPY

LOCALISATION_FILE = ROOT / "localisation" / "english" / "state_names_l_english.yml"
OUTPUT_CSV = ROOT / "state_population_estimates.csv"
AUDIT_CSV = ROOT / "state_population_audit.csv"
CENSUS_CSV = ROOT / "data" / "census_2000_country_totals.csv"

MIN_LAND_POPULATION = 500

US_STATE_ABBRS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS",
    "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY",
    "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC", "PR", "NYC",
}
CA_ABBRS = {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}
MX_ABBRS = {
    "AG", "BC", "BS", "CM", "CS", "CH", "CO", "CL", "DF", "DG", "GT", "GR", "HG", "JA", "EM", "MI",
    "MO", "NA", "NL", "OA", "PU", "QT", "QR", "SL", "SI", "SO", "TB", "TM", "TL", "VE", "YU", "ZA",
}
CARIBBEAN_COUNTRY = {
    "BS": "BS", "JM": "JM", "HT": "HT", "DO": "DO", "CU": "CU", "TT": "TT", "BB": "BB", "KY": "KY",
    "VG": "VG", "GD": "GD", "LC": "LC", "VC": "VC", "AG": "AG", "DM": "DM", "KN": "KN", "AW": "AW",
    "CW": "CW", "SX": "SX", "GP": "GP", "MQ": "MQ", "TC": "TC", "BM": "BM",
}
CENTRAL_AMERICA = {"GT": "GT", "BZ": "BZ", "HN": "HN", "SV": "SV", "NI": "NI", "CR": "CR", "PA": "PA", "GUA": "GT"}

ISLAND_PATTERN = re.compile(r"\b(island|islands|isla|islas|ile|iles)\b", re.IGNORECASE)
NATIONAL_PARK_PATTERN = re.compile(
    r"\b(national park|yellowstone|yosemite|sequoia|capitol reef)\b",
    re.IGNORECASE,
)


def parse_localisation(path: Path) -> dict[int, str]:
    names: dict[int, str] = {}
    if not path.exists():
        return names
    pattern = re.compile(r'^\s*STATE_(\d+):[0-9]+\s*"([^"]+)"', re.MULTILINE)
    for match in pattern.finditer(path.read_text(encoding="utf-8-sig")):
        names[int(match.group(1))] = match.group(2)
    return names


def load_census_totals(path: Path) -> dict[str, int]:
    totals: dict[str, int] = {}
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            totals[row["country_code"]] = int(row["population_2000"])
    return totals


def load_population_cells() -> np.ndarray:
    if POP_NPY.exists():
        return np.load(POP_NPY)
    if not CELLS_CSV.exists():
        raise SystemExit(f"Missing population grid. Run: python3 import_population_grid.py --source <gpw.tif>")
    cells = []
    with CELLS_CSV.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            cells.append((float(row["lon"]), float(row["lat"]), float(row["population"])))
    return np.array(cells, dtype=np.float64)


def state_country_code(state_name: str) -> str:
    if not state_name:
        return "OTHER"
    key = state_name.strip().lower()
    if key == "world border":
        return "WASTELAND"
    if "puerto rico" in key:
        return "PR"
    if "greenland" in key:
        return "GL"
    if "bahamas" in key:
        return "BS"
    if "guatemala" in key and ", gt" not in key and ", gua" not in key:
        return "GT"

    parts = [part.strip() for part in state_name.split(",")]
    if len(parts) == 2:
        abbr = parts[1].upper()
        if abbr in US_STATE_ABBRS:
            return "US"
        if abbr == "PR":
            return "PR"
        if abbr in CA_ABBRS:
            return "CA"
        if abbr in MX_ABBRS:
            return "MX"
        if abbr in CARIBBEAN_COUNTRY:
            return CARIBBEAN_COUNTRY[abbr]
        if abbr in CENTRAL_AMERICA:
            return CENTRAL_AMERICA[abbr]
        if abbr == "GUA":
            return "GT"

    if any(token in key for token in ("cuba", "havana")):
        return "CU"
    if "haiti" in key or "port-au-prince" in key:
        return "HT"
    if "dominican" in key or "santo domingo" in key:
        return "DO"
    if "jamaica" in key:
        return "JM"
    if "mexico" in key or "monterrey" in key or "guadalajara" in key:
        return "MX"
    if any(token in key for token in ("ontario", "quebec", "british columbia", "alberta", "manitoba")):
        return "CA"
    return "OTHER"


def special_override_population(state_name: str) -> int | None:
    key = state_name.strip().lower()
    if key == "world border":
        return 0
    if "naval base" in key or " afb" in key or "air force base" in key:
        return 1500
    if NATIONAL_PARK_PATTERN.search(key):
        return 1000
    if ISLAND_PATTERN.search(key) and "county" not in key:
        return 10000
    return None


def assign_cells_to_states(
    cells: np.ndarray,
    rbf_x: RBFInterpolator,
    rbf_y: RBFInterpolator,
    province_raster: np.ndarray,
    province_to_state: np.ndarray,
) -> dict[int, float]:
    if cells.size == 0:
        return {}
    coords = cells[:, :2].astype(float)
    pops = cells[:, 2].astype(np.float64)
    px = np.round(rbf_x(coords)).astype(np.int64)
    py = np.round(rbf_y(coords)).astype(np.int64)

    height, width = province_raster.shape
    valid = (
        (pops > 0)
        & (px >= 0)
        & (px < width)
        & (py >= 0)
        & (py < height)
    )
    province_ids = np.zeros(len(cells), dtype=np.int64)
    province_ids[valid] = province_raster[py[valid], px[valid]]

    state_ids = np.zeros(len(cells), dtype=np.int64)
    province_ok = valid & (province_ids > 0) & (province_ids < len(province_to_state))
    state_ids[province_ok] = province_to_state[province_ids[province_ok]]

    assigned = state_ids > 0
    if not np.any(assigned):
        return {}
    max_state = int(state_ids[assigned].max())
    totals = np.bincount(state_ids[assigned], weights=pops[assigned], minlength=max_state + 1)
    return {sid: float(totals[sid]) for sid in range(1, len(totals)) if totals[sid] > 0}


def map_cities_to_states(
    cities,
    rbf_x,
    rbf_y,
    province_raster,
    province_to_state,
    centroids,
    state_provinces,
    region_hints,
    state_names,
) -> dict[int, int]:
    if not cities:
        return {}
    coords = np.array([[city.lon, city.lat] for city in cities], dtype=float)
    pops = np.array([city.population for city in cities], dtype=np.int64)
    px = np.round(rbf_x(coords)).astype(np.int64)
    py = np.round(rbf_y(coords)).astype(np.int64)

    height, width = province_raster.shape
    by_state: dict[int, int] = defaultdict(int)
    for idx, city in enumerate(cities):
        pop = int(pops[idx])
        if pop <= 0:
            continue
        hinted = resolve_hint_states(city, region_hints, state_names)
        if hinted:
            pid = locate_city_province(
                city,
                rbf_x,
                rbf_y,
                province_raster,
                province_to_state,
                centroids,
                state_provinces,
                region_hints,
                state_names,
            )
        else:
            pid = 0
            ix, iy = int(px[idx]), int(py[idx])
            if 0 <= ix < width and 0 <= iy < height:
                candidate = int(province_raster[iy, ix])
                if candidate > 0 and candidate < len(province_to_state) and province_to_state[candidate] > 0:
                    pid = candidate
        if pid <= 0:
            continue
        sid = int(province_to_state[pid])
        if sid > 0:
            by_state[sid] += pop
    return dict(by_state)


def scale_to_census_2000(
    raw_pop: dict[int, float],
    state_names: dict[int, str],
    census_totals: dict[str, int],
) -> dict[int, int]:
    country_raw: dict[str, float] = defaultdict(float)
    state_country: dict[int, str] = {}
    for state_id, pop in raw_pop.items():
        country = state_country_code(state_names.get(state_id, ""))
        state_country[state_id] = country
        if country != "WASTELAND":
            country_raw[country] += pop

    scaled: dict[int, int] = {}
    for state_id, pop in raw_pop.items():
        country = state_country.get(state_id, "OTHER")
        if country == "WASTELAND":
            scaled[state_id] = 0
            continue
        target = census_totals.get(country, census_totals.get("OTHER", 500000))
        denom = country_raw.get(country, 0.0)
        if denom <= 0:
            scaled[state_id] = 0
        else:
            scaled[state_id] = max(0, int(round(pop * target / denom)))
    return scaled


def apply_city_validation(
    scaled: dict[int, int],
    city_sums: dict[int, int],
) -> tuple[dict[int, int], dict[int, str]]:
    notes: dict[int, str] = {}
    updated = dict(scaled)
    for state_id, city_sum in city_sums.items():
        if city_sum <= 0:
            continue
        current = updated.get(state_id, 0)
        if current < city_sum * 0.5:
            floor = max(current, int(city_sum * 0.8))
            updated[state_id] = floor
            notes[state_id] = f"city_floor:{floor}"
    return updated, notes


def apply_special_overrides(
    scaled: dict[int, int],
    state_names: dict[int, str],
    land_pixels: dict[int, int],
) -> dict[int, int]:
    result = dict(scaled)
    for state_id, name in state_names.items():
        override = special_override_population(name)
        if override is not None:
            result[state_id] = override
            continue
        if result.get(state_id, 0) <= 0 and land_pixels.get(state_id, 0) > 0:
            result[state_id] = MIN_LAND_POPULATION
    return result


def update_manpower(path: Path, manpower: int) -> bool:
    text = path.read_text(encoding="utf-8")
    new_line = f"\tmanpower = {manpower}"
    if re.search(r"^\s*manpower\s*=", text, flags=re.MULTILINE):
        updated = re.sub(r"^\s*manpower\s*=\s*[0-9]+\s*$", new_line, text, count=1, flags=re.MULTILINE)
    else:
        updated = re.sub(r"(state\s*=\s*\{)", rf"\1\n{new_line}", text, count=1)
    if updated == text:
        return False
    path.write_text(updated, encoding="utf-8")
    return True


def run(apply: bool, verbose: bool) -> None:
    states = parse_states(STATE_DIR)
    state_names = {**parse_localisation(LOCALISATION_FILE), **load_state_names(POPULATION_CSV)}
    census_totals = load_census_totals(CENSUS_CSV)

    color_to_province, max_province = parse_definition(DEFINITION_CSV)
    province_raster = rasterize_provinces(PROVINCES_BMP, color_to_province)
    province_to_state = np.zeros(max_province + 1, dtype=np.uint16)
    state_provinces: dict[int, list[int]] = {}
    for state in states.values():
        state_provinces[state["id"]] = state["provinces"]
        for pid in state["provinces"]:
            if pid <= max_province:
                province_to_state[pid] = state["id"]

    state_raster = province_to_state[province_raster]
    land_pixels_arr, _ = state_geometry(state_raster)
    land_pixels = {sid: int(land_pixels_arr[sid]) for sid in states if sid < len(land_pixels_arr)}
    centroids = compute_province_centroids(province_raster)

    known_vps = parse_known_vp_localisations(VP_LOCALISATION)
    calibration_pairs = []
    if ANCHOR_SNAPSHOT.exists():
        import csv as csvmod

        centroids_prov = {}
        flat = province_raster.ravel()
        height, width = province_raster.shape
        xs = np.tile(np.arange(width), height)
        ys = np.repeat(np.arange(height), width)
        counts = np.bincount(flat, minlength=int(flat.max()) + 1)
        sum_x = np.bincount(flat, weights=xs, minlength=len(counts))
        sum_y = np.bincount(flat, weights=ys, minlength=len(counts))
        for province_id in range(1, len(counts)):
            if counts[province_id] > 0:
                centroids_prov[province_id] = (sum_x[province_id] / counts[province_id], sum_y[province_id] / counts[province_id])

        with ANCHOR_SNAPSHOT.open(encoding="utf-8") as handle:
            for row in csvmod.DictReader(handle):
                province_id = int(row["province_id"])
                if province_id in centroids_prov:
                    x, y = centroids_prov[province_id]
                    calibration_pairs.append(
                        (float(row["lon"]), float(row["lat"]), x, y, province_id, row["label"])
                    )

    if len(calibration_pairs) < 6:
        raise SystemExit("Need at least 6 calibration anchors in vp_calibration_anchors.csv")

    rbf_x, rbf_y = fit_forward_rbf(calibration_pairs)
    cells = load_population_cells()
    raw_pop = assign_cells_to_states(cells, rbf_x, rbf_y, province_raster, province_to_state)

    region_hints = build_region_hints(state_names)
    cities = load_city_db()
    city_sums = map_cities_to_states(
        cities,
        rbf_x,
        rbf_y,
        province_raster,
        province_to_state,
        centroids,
        state_provinces,
        region_hints,
        state_names,
    )

    scaled = scale_to_census_2000(raw_pop, state_names, census_totals)
    scaled, validation_notes = apply_city_validation(scaled, city_sums)
    final_pop = apply_special_overrides(scaled, state_names, land_pixels)

    rows = []
    audit_rows = []
    for state_id in sorted(states):
        state = states[state_id]
        name = state_names.get(state_id, f"STATE_{state_id}")
        current = state.get("manpower", 0) or 0
        estimate = final_pop.get(state_id, 0)
        raw = int(round(raw_pop.get(state_id, 0)))
        note = validation_notes.get(state_id, "")
        source = "grid_2000_scaled"
        if special_override_population(name) is not None:
            source = "special_override"
        elif note:
            source = "grid_2000_scaled+city_validation"

        rows.append(
            {
                "id": state_id,
                "file": str(state["path"].relative_to(ROOT)),
                "name": name,
                "current_manpower": current,
                "estimated_population": estimate,
                "estimate_source": source,
                "grid_raw": raw,
                "scaled_population": estimate,
                "city_sum": city_sums.get(state_id, 0),
                "city_validation_note": note,
            }
        )
        delta_pct = ((estimate - current) / current * 100.0) if current else 0.0
        audit_rows.append(
            {
                "state_id": state_id,
                "state_name": name,
                "country": state_country_code(name),
                "land_pixels": land_pixels.get(state_id, 0),
                "grid_raw": raw,
                "city_sum": city_sums.get(state_id, 0),
                "old_manpower": current,
                "new_manpower": estimate,
                "delta_pct": f"{delta_pct:.1f}",
                "estimate_source": source,
            }
        )

    fieldnames = [
        "id", "file", "name", "current_manpower", "estimated_population", "estimate_source",
        "grid_raw", "scaled_population", "city_sum", "city_validation_note",
    ]
    with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    with AUDIT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(audit_rows[0].keys()))
        writer.writeheader()
        writer.writerows(audit_rows)

    grid_source = "unknown"
    if METADATA_PATH.exists():
        import json

        grid_source = json.loads(METADATA_PATH.read_text()).get("source", "unknown")

    print(f"Grid source: {grid_source}")
    print(f"Population cells: {len(cells)}")
    print(f"Wrote {OUTPUT_CSV}")
    print(f"Wrote {AUDIT_CSV}")

    if verbose:
        top = sorted(audit_rows, key=lambda r: abs(float(r["delta_pct"])), reverse=True)[:15]
        print("\nLargest manpower changes:")
        for row in top:
            print(
                f"  {row['state_name'][:35]:35s} {row['old_manpower']:>10} -> {row['new_manpower']:>10} "
                f"({row['delta_pct']}%)"
            )

    if not apply:
        print("\nDry run. Re-run with --apply to update state manpower files.")
        return

    changed = 0
    for row in rows:
        path = ROOT / row["file"]
        if update_manpower(path, int(row["estimated_population"])):
            changed += 1
    print(f"\nUpdated manpower in {changed} state files.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Estimate state manpower from population grid.")
    parser.add_argument("--apply", action="store_true", help="Write manpower into history/states")
    parser.add_argument("--verbose", action="store_true", help="Print summary of largest changes")
    args = parser.parse_args()
    run(apply=args.apply, verbose=args.verbose)


if __name__ == "__main__":
    main()
