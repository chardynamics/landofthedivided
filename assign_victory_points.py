#!/usr/bin/env python3
"""Assign victory points from a curated city database using map-calibrated geography."""
from __future__ import annotations

import argparse
import csv
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.interpolate import RBFInterpolator

from assign_infrastructure import (
    POPULATION_CSV,
    ROOT,
    STATE_DIR,
    load_state_names,
    parse_definition,
    parse_states,
    rasterize_provinces,
)
from city_database import CITY_BY_LABEL, CITY_ENTRIES, city_label, resolve_city_label

DEFINITION_CSV = ROOT / "map" / "definition.csv"
PROVINCES_BMP = ROOT / "map" / "provinces.bmp"
VP_LOCALISATION = ROOT / "localisation" / "english" / "TNO_victory_points_l_english.yml"
OUTPUT_CSV = ROOT / "victory_point_assignments.csv"
# Frozen snapshot of (province_id, lon, lat) calibration anchors. The localisation
# file is the anchor source, but --apply appends new cities to it; freezing keeps
# the projection stable so re-runs stay idempotent instead of drifting.
ANCHOR_SNAPSHOT = ROOT / "vp_calibration_anchors.csv"

VALUE_TIERS = (
    (6_000_000, 80),
    (3_000_000, 55),
    (1_500_000, 50),
    (800_000, 42),
    (400_000, 33),
    (200_000, 25),
    (100_000, 16),
    (50_000, 10),
    (25_000, 6),
    (10_000, 4),
    (0, 3),
)

DENSE_POP_FLOOR = 50_000
SPARSE_POP_FLOOR = 15_000
SPARSE_ABBR_MAX_MAJOR = 4
COVERAGE_MIN_POP = 5_000
FORWARD_RBF_SMOOTHING = 0.5
PIXEL_LOOKUP_RADIUS = 20

US_STATE_ABBRS = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS",
    "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY",
    "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC", "PR",
)

REGION_NAME_PATTERNS: dict[str, tuple[str, ...]] = {
    "ON": ("Ontario",),
    "BC": ("British Columbia", "Vancouver"),
    "AB": ("Alberta", "Calgary", "Edmonton"),
    "SK": ("Saskatchewan",),
    "MB": ("Manitoba", "Winnipeg"),
    "QC": ("Montreal", "Quebec", "Laval"),
    "NB": ("Brunswick",),
    "NS": ("Nova Scotia", "Halifax"),
    "NL": ("Newfoundland",),
    "PE": ("Prince Edward",),
    "YT": ("Yukon",),
    "NT": ("Northwest",),
    "NU": ("Nunavut",),
    "CH": ("Chihuahua",),
    "JA": ("Jalisco",),
    "JM": ("Jamaica",),
    "SO": ("Sonora",),
    "YU": ("Yucatan",),
    "SL": ("San Luis Potosi",),
    "AG": ("Aguascalientes",),
    "GT": ("Guanajuato",),
    "TM": ("Tamaulipas",),
    "VE": ("Veracruz",),
    "DG": ("Durango",),
    "OA": ("Oaxaca",),
    "QT": ("Queretaro",),
    "MI": ("Michoacan",),
    "SI": ("Sinaloa",),
    "CO": ("Coahuila",),
    "BS": ("Bahamas", "Nassau"),
    "HT": ("Haiti",),
    "DO": ("Dominican", "Santo Domingo"),
    "CU": ("Cuba", "Havana"),
    "JM": ("Jamaica",),
    "TT": ("Trinidad",),
    "BB": ("Barbados",),
    "KY": ("Cayman",),
    "GC": ("Cayman",),
    "VG": ("Virgin Islands",),
    "PR": ("Puerto Rico", "San Juan", "Ponce", "Mayaguez"),
}


@dataclass
class CityRecord:
    name: str
    abbr: str
    lon: float
    lat: float
    population: int
    label: str


@dataclass
class Placement:
    city: CityRecord
    province_id: int
    state_id: int
    value: int
    included: bool
    reason: str


@dataclass
class CalibrationReport:
    pairs: int
    mean_residual_px: float
    max_residual_px: float
    leave_one_out_hits: int
    leave_one_out_total: int


def load_city_db() -> list[CityRecord]:
    cities = []
    seen = set()
    for name, abbr, lon, lat, pop in CITY_ENTRIES:
        key = (name.lower(), abbr)
        if key in seen:
            continue
        seen.add(key)
        cities.append(
            CityRecord(
                name=name,
                abbr=abbr,
                lon=lon,
                lat=lat,
                population=pop,
                label=city_label(name, abbr),
            )
        )
    return cities


def parse_known_vp_localisations(path: Path) -> dict[int, str]:
    known = {}
    if not path.exists():
        return known
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r'\s*VICTORY_POINTS_(\d+):0\s*"([^"]+)"', raw_line)
        if match:
            known[int(match.group(1))] = match.group(2)
    return known


def compute_province_centroids(province_raster: np.ndarray) -> dict[int, tuple[float, float]]:
    flat = province_raster.ravel()
    max_id = int(flat.max())
    height, width = province_raster.shape
    xs = np.tile(np.arange(width), height)
    ys = np.repeat(np.arange(height), width)
    counts = np.bincount(flat, minlength=max_id + 1)
    sum_x = np.bincount(flat, weights=xs, minlength=max_id + 1)
    sum_y = np.bincount(flat, weights=ys, minlength=max_id + 1)
    centroids = {}
    for province_id in range(1, max_id + 1):
        if counts[province_id] > 0:
            centroids[province_id] = (sum_x[province_id] / counts[province_id], sum_y[province_id] / counts[province_id])
    return centroids


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius_km = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius_km * math.asin(min(1.0, a**0.5))


def build_region_hints(state_names: dict[int, str]) -> dict[str, list[int]]:
    hints: dict[str, set[int]] = defaultdict(set)
    for state_id, name in state_names.items():
        for abbr, patterns in REGION_NAME_PATTERNS.items():
            if any(pattern in name for pattern in patterns):
                hints[abbr].add(state_id)
        for abbr in US_STATE_ABBRS:
            if f", {abbr}" in name or name.endswith(f" {abbr}"):
                hints[abbr].add(state_id)
    return {abbr: sorted(state_ids) for abbr, state_ids in hints.items()}


def _state_ids_for_names(
    state_names: dict[int, str],
    patterns: tuple[str, ...],
    exclude: tuple[str, ...] = (),
) -> list[int]:
    matches = []
    for state_id, name in state_names.items():
        if any(excluded in name for excluded in exclude):
            continue
        if any(pattern in name for pattern in patterns):
            matches.append(state_id)
    return matches


def resolve_hint_states(
    city: CityRecord,
    region_hints: dict[str, list[int]],
    state_names: dict[int, str],
) -> list[int]:
    abbr = city.abbr
    if abbr == "BC":
        if city.lon < -114 and city.lat < 42:
            return _state_ids_for_names(state_names, ("Baja California",))
        return region_hints.get("BC", [])
    if abbr == "NL":
        if city.lat < 35:
            return _state_ids_for_names(state_names, ("Monterrey", "Coahuila", "Nuevo"))
        return region_hints.get("NL", [])
    if abbr == "MI":
        if city.lat < 40 and city.lon < -90:
            return _state_ids_for_names(state_names, ("Michoacan", "Morelia"))
        return region_hints.get("MI", [])
    if abbr == "CO":
        if city.lat < 37:
            return _state_ids_for_names(state_names, ("Coahuila",))
        return region_hints.get("CO", [])
    if abbr == "DG":
        if city.lat < 37:
            return _state_ids_for_names(state_names, ("Durango",), exclude=("Colorado",))
        return region_hints.get("DG", [])
    if abbr == "MO":
        # Morelos (deep Mexico) collides with Missouri; route southern cities away.
        if city.lat < 24:
            return _state_ids_for_names(state_names, ("Morelos",))
        return region_hints.get("MO", [])
    # Safety net: a US-state abbreviation below the continental US never belongs to
    # that US state (it is an ambiguous Mexican/Caribbean abbreviation).
    if abbr in US_STATE_ABBRS and city.lat < 24.0:
        return []
    return region_hints.get(abbr, [])


def fit_forward_rbf(
    pairs: list[tuple[float, float, float, float, int, str]],
) -> tuple[RBFInterpolator, RBFInterpolator]:
    if len(pairs) < 6:
        raise ValueError(f"Need at least 6 calibration pairs, got {len(pairs)}")
    coords = np.array([[lon, lat] for lon, lat, _, _, _, _ in pairs], dtype=float)
    xs = np.array([x for _, _, x, _, _, _ in pairs], dtype=float)
    ys = np.array([y for _, _, _, y, _, _ in pairs], dtype=float)
    rbf_x = RBFInterpolator(coords, xs, kernel="thin_plate_spline", smoothing=FORWARD_RBF_SMOOTHING)
    rbf_y = RBFInterpolator(coords, ys, kernel="thin_plate_spline", smoothing=FORWARD_RBF_SMOOTHING)
    return rbf_x, rbf_y


def project_to_pixel(
    lon: float,
    lat: float,
    rbf_x: RBFInterpolator,
    rbf_y: RBFInterpolator,
) -> tuple[float, float]:
    px = float(rbf_x([[lon, lat]])[0])
    py = float(rbf_y([[lon, lat]])[0])
    return px, py


def locate_province_pixel(
    px: float,
    py: float,
    province_raster: np.ndarray,
    province_to_state: np.ndarray,
) -> int:
    height, width = province_raster.shape
    ix = int(round(px))
    iy = int(round(py))
    if 0 <= ix < width and 0 <= iy < height:
        province_id = int(province_raster[iy, ix])
        if province_id > 0 and province_id < len(province_to_state) and province_to_state[province_id] > 0:
            return province_id

    best_pid = 0
    best_dist = float("inf")
    for radius in range(1, PIXEL_LOOKUP_RADIUS + 1):
        y0 = max(iy - radius, 0)
        y1 = min(iy + radius + 1, height)
        x0 = max(ix - radius, 0)
        x1 = min(ix + radius + 1, width)
        window = province_raster[y0:y1, x0:x1]
        if not np.any(window > 0):
            continue
        ys, xs = np.nonzero(window > 0)
        for y_idx, x_idx in zip(ys, xs):
            province_id = int(window[y_idx, x_idx])
            if province_id >= len(province_to_state) or province_to_state[province_id] <= 0:
                continue
            dist = (x0 + x_idx - px) ** 2 + (y0 + y_idx - py) ** 2
            if dist < best_dist:
                best_dist = dist
                best_pid = province_id
        if best_pid:
            return best_pid
    return 0


def nearest_province_in_states(
    px: float,
    py: float,
    state_ids: list[int],
    state_provinces: dict[int, list[int]],
    centroids: dict[int, tuple[float, float]],
) -> int:
    best_pid = 0
    best_dist = float("inf")
    for state_id in state_ids:
        for province_id in state_provinces.get(state_id, []):
            centroid = centroids.get(province_id)
            if not centroid:
                continue
            dist = (centroid[0] - px) ** 2 + (centroid[1] - py) ** 2
            if dist < best_dist:
                best_dist = dist
                best_pid = province_id
    return best_pid


def locate_city_province(
    city: CityRecord,
    rbf_x: RBFInterpolator,
    rbf_y: RBFInterpolator,
    province_raster: np.ndarray,
    province_to_state: np.ndarray,
    centroids: dict[int, tuple[float, float]],
    state_provinces: dict[int, list[int]],
    region_hints: dict[str, list[int]],
    state_names: dict[int, str],
) -> int:
    px, py = project_to_pixel(city.lon, city.lat, rbf_x, rbf_y)
    hinted_states = resolve_hint_states(city, region_hints, state_names)
    if hinted_states:
        return nearest_province_in_states(px, py, hinted_states, state_provinces, centroids)
    return locate_province_pixel(px, py, province_raster, province_to_state)


def build_calibration_pairs(
    known_vps: dict[int, str],
    centroids: dict[int, tuple[float, float]],
) -> list[tuple[float, float, float, float, int, str]]:
    pairs = []
    for province_id, label in known_vps.items():
        city = resolve_city_label(label)
        if not city or province_id not in centroids:
            continue
        x, y = centroids[province_id]
        pairs.append((city["lon"], city["lat"], x, y, province_id, label))
    return pairs


def load_or_create_anchor_snapshot(
    known_vps: dict[int, str],
    centroids: dict[int, tuple[float, float]],
) -> list[tuple[float, float, float, float, int, str]]:
    """Return calibration pairs from a frozen snapshot, creating it on first use.

    Freezing the (province, lon, lat) anchors means appending script-generated
    localisations later cannot perturb the projection on subsequent runs.
    """
    if ANCHOR_SNAPSHOT.exists():
        pairs = []
        with open(ANCHOR_SNAPSHOT, encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                province_id = int(row["province_id"])
                if province_id not in centroids:
                    continue
                x, y = centroids[province_id]
                pairs.append((float(row["lon"]), float(row["lat"]), x, y, province_id, row["label"]))
        if len(pairs) >= 6:
            return pairs

    pairs = build_calibration_pairs(known_vps, centroids)
    with open(ANCHOR_SNAPSHOT, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["province_id", "lon", "lat", "label"])
        for lon, lat, _, _, province_id, label in sorted(pairs, key=lambda item: item[4]):
            writer.writerow([province_id, lon, lat, label])
    return pairs


def evaluate_calibration(
    pairs: list[tuple[float, float, float, float, int, str]],
    centroids: dict[int, tuple[float, float]],
    province_raster: np.ndarray,
    province_to_state: np.ndarray,
    state_provinces: dict[int, list[int]],
    region_hints: dict[str, list[int]],
    state_names: dict[int, str],
) -> CalibrationReport:
    rbf_x, rbf_y = fit_forward_rbf(pairs)
    residuals_px = []
    for lon, lat, x, y, _, _ in pairs:
        px, py = project_to_pixel(lon, lat, rbf_x, rbf_y)
        residuals_px.append(((px - x) ** 2 + (py - y) ** 2) ** 0.5)

    hits = 0
    for idx, (lon, lat, _, _, expected_pid, label) in enumerate(pairs):
        subset = [pairs[j] for j in range(len(pairs)) if j != idx]
        if len(subset) < 6:
            continue
        city = resolve_city_label(label)
        if not city:
            continue
        rbf_x_loo, rbf_y_loo = fit_forward_rbf(subset)
        assigned_pid = locate_city_province(
            CityRecord(city["name"], city["abbr"], lon, lat, city["population"], city["label"]),
            rbf_x_loo,
            rbf_y_loo,
            province_raster,
            province_to_state,
            centroids,
            state_provinces,
            region_hints,
            state_names,
        )
        if assigned_pid == expected_pid:
            hits += 1

    total = max(len(pairs) - (1 if len(pairs) >= 6 else 0), 0)
    return CalibrationReport(
        pairs=len(pairs),
        mean_residual_px=float(np.mean(residuals_px)) if residuals_px else 0.0,
        max_residual_px=float(np.max(residuals_px)) if residuals_px else 0.0,
        leave_one_out_hits=hits,
        leave_one_out_total=total,
    )


def value_for_population(population: int) -> int:
    for threshold, value in VALUE_TIERS:
        if population >= threshold:
            return value
    return 0


def state_has_victory_points(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    return bool(re.search(r"^\s*victory_points\s*=", text, flags=re.MULTILINE))


def parse_existing_vp_provinces(state_dir: Path) -> set[int]:
    provinces = set()
    pattern = re.compile(r"^\s*victory_points\s*=\s*\{([^}]*)\}", flags=re.MULTILINE)
    for path in state_dir.glob("*.txt"):
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            for pid in re.findall(r"\d+", match.group(1)):
                provinces.add(int(pid))
    return provinces


def sparse_abbreviations(cities: list[CityRecord]) -> set[str]:
    counts = Counter(city.abbr for city in cities if city.population >= DENSE_POP_FLOOR)
    return {abbr for abbr, count in counts.items() if count < SPARSE_ABBR_MAX_MAJOR}


def choose_inclusions(
    mapped: list[tuple[CityRecord, int, int]],
    sparse_abbrs: set[str],
    reserved_labels: set[str] | None = None,
) -> dict[tuple[int, int], Placement]:
    by_province: dict[int, list[tuple[CityRecord, int]]] = defaultdict(list)
    for city, province_id, state_id in mapped:
        by_province[province_id].append((city, state_id))

    # One candidate city per province (largest population). Keep the rest as
    # ordered fallbacks so colliding names can be resolved with an alternate city.
    candidates: dict[int, list[tuple[CityRecord, int]]] = {}
    for province_id, entries in by_province.items():
        candidates[province_id] = sorted(entries, key=lambda item: -item[0].population)

    chosen: dict[tuple[int, int], Placement] = {}
    for province_id, entries in candidates.items():
        city, state_id = entries[0]
        chosen[(province_id, state_id)] = Placement(
            city=city,
            province_id=province_id,
            state_id=state_id,
            value=value_for_population(city.population),
            included=False,
            reason="pending",
        )

    for placement in chosen.values():
        pop = placement.city.population
        if pop >= DENSE_POP_FLOOR:
            placement.included = True
            placement.reason = "dense_floor"
        elif pop >= SPARSE_POP_FLOOR and placement.city.abbr in sparse_abbrs:
            placement.included = True
            placement.reason = "sparse_floor"
        else:
            placement.included = False
            placement.reason = "below_threshold"

    by_state: dict[int, list[Placement]] = defaultdict(list)
    for placement in chosen.values():
        by_state[placement.state_id].append(placement)

    for state_id, placements in by_state.items():
        if any(p.included for p in placements):
            continue
        eligible = [p for p in placements if p.city.population >= COVERAGE_MIN_POP]
        if not eligible:
            continue
        best = max(eligible, key=lambda p: p.city.population)
        best.included = True
        best.reason = "state_coverage_guarantee"

    enforce_unique_labels(chosen, candidates, reserved_labels or set())
    return chosen


def enforce_unique_labels(
    chosen: dict[tuple[int, int], Placement],
    candidates: dict[int, list[tuple[CityRecord, int]]],
    reserved_labels: set[str],
) -> None:
    """Guarantee that every included victory point carries a distinct city label.

    If two provinces resolve to the same city name, the lower-population one is
    swapped for the next distinct city that also lands in its province; if no
    alternate exists, the name is disambiguated so no two VPs read identically.
    """
    used_labels: set[str] = set(reserved_labels)
    included = [p for p in chosen.values() if p.included]
    for placement in sorted(included, key=lambda p: (-p.city.population, p.province_id)):
        if placement.city.label not in used_labels:
            used_labels.add(placement.city.label)
            continue
        replaced = False
        for alt_city, _ in candidates.get(placement.province_id, []):
            if alt_city.label not in used_labels:
                placement.city = alt_city
                placement.value = value_for_population(alt_city.population)
                placement.reason = f"{placement.reason}+relabel"
                used_labels.add(alt_city.label)
                replaced = True
                break
        if replaced:
            continue
        base = placement.city.label
        suffix = 2
        new_label = f"{base} ({suffix})"
        while new_label in used_labels:
            suffix += 1
            new_label = f"{base} ({suffix})"
        disambiguated = CityRecord(
            name=placement.city.name,
            abbr=placement.city.abbr,
            lon=placement.city.lon,
            lat=placement.city.lat,
            population=placement.city.population,
            label=new_label,
        )
        placement.city = disambiguated
        placement.reason = f"{placement.reason}+suffix"
        used_labels.add(new_label)


def map_cities(
    cities: list[CityRecord],
    rbf_x: RBFInterpolator,
    rbf_y: RBFInterpolator,
    province_raster: np.ndarray,
    province_to_state: np.ndarray,
    centroids: dict[int, tuple[float, float]],
    state_provinces: dict[int, list[int]],
    region_hints: dict[str, list[int]],
    state_names: dict[int, str],
    states_with_vp: set[int],
) -> tuple[list[tuple[CityRecord, int, int]], list[tuple[CityRecord, str]]]:
    mapped = []
    skipped = []
    for city in cities:
        province_id = locate_city_province(
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
        if province_id <= 0:
            skipped.append((city, "ocean_or_offmap"))
            continue
        if province_id >= len(province_to_state):
            skipped.append((city, "offmap"))
            continue
        state_id = int(province_to_state[province_id])
        if state_id <= 0:
            skipped.append((city, "no_state"))
            continue
        if state_id in states_with_vp:
            skipped.append((city, "state_has_existing_vp"))
            continue
        mapped.append((city, province_id, state_id))
    return mapped, skipped


def find_history_insert_index(text: str) -> int | None:
    history_match = re.search(r"\bhistory\s*=\s*\{", text)
    if not history_match:
        return None
    start = history_match.end() - 1
    depth = 0
    for idx in range(start, len(text)):
        char = text[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return idx
    return None


def insert_victory_points(path: Path, assignments: list[tuple[int, int]]) -> bool:
    text = path.read_text(encoding="utf-8")
    if re.search(r"^\s*victory_points\s*=", text, flags=re.MULTILINE):
        return False
    insert_at = find_history_insert_index(text)
    if insert_at is None:
        raise ValueError(f"Could not locate history block in {path}")
    lines = []
    for province_id, value in sorted(assignments, key=lambda item: (-item[1], item[0])):
        lines.append(f"\t\tvictory_points = {{ {province_id} {value} }}")
    block = "\n" + "\n".join(lines) + "\n"
    updated = text[:insert_at] + block + text[insert_at:]
    path.write_text(updated, encoding="utf-8")
    return True


def append_localisations(path: Path, entries: list[tuple[int, str]]) -> int:
    if not entries:
        return 0
    existing = parse_known_vp_localisations(path)
    text = path.read_text(encoding="utf-8-sig")
    if not text.endswith("\n"):
        text += "\n"
    added = 0
    for province_id, label in sorted(entries, key=lambda item: item[0]):
        if province_id in existing:
            continue
        text += f' VICTORY_POINTS_{province_id}:0 "{label}"\n'
        existing[province_id] = label
        added += 1
    path.write_text(text, encoding="utf-8")
    return added


def write_csv(path: Path, placements: list[Placement], skipped: list[tuple[CityRecord, str]], state_names: dict[int, str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["city", "abbr", "label", "population", "province_id", "state_id", "state_name", "value", "included", "reason"]
        )
        for placement in sorted(placements, key=lambda p: (-p.value, p.state_id, p.province_id)):
            writer.writerow(
                [
                    placement.city.name,
                    placement.city.abbr,
                    placement.city.label,
                    placement.city.population,
                    placement.province_id,
                    placement.state_id,
                    state_names.get(placement.state_id, ""),
                    placement.value,
                    int(placement.included),
                    placement.reason,
                ]
            )
        for city, reason in sorted(skipped, key=lambda item: (item[1], item[0].label)):
            writer.writerow([city.name, city.abbr, city.label, city.population, "", "", "", "", 0, reason])


def print_summary(
    calibration: CalibrationReport,
    placements: list[Placement],
    skipped: list[tuple[CityRecord, str]],
    state_names: dict[int, str],
) -> None:
    included = [p for p in placements if p.included]
    print("=== Calibration ===")
    print(f"Pairs: {calibration.pairs}")
    print(f"Mean pixel residual: {calibration.mean_residual_px:.2f}")
    print(f"Max pixel residual: {calibration.max_residual_px:.2f}")
    if calibration.leave_one_out_total:
        rate = 100.0 * calibration.leave_one_out_hits / calibration.leave_one_out_total
        print(
            f"Leave-one-out province hit-rate: {calibration.leave_one_out_hits}/"
            f"{calibration.leave_one_out_total} ({rate:.1f}%)"
        )

    print("\n=== Placement ===")
    print(f"Cities in database: {len(CITY_ENTRIES)}")
    print(f"Mapped placements (one per province): {len(placements)}")
    print(f"Included VPs: {len(included)}")
    print(f"States affected: {len({p.state_id for p in included})}")
    print(f"Skipped city projections: {len(skipped)}")

    hist = Counter(p.value for p in included)
    print("\nValue histogram:")
    for value in sorted(hist, reverse=True):
        print(f"  {value}: {hist[value]}")

    print("\nTop included cities:")
    for placement in sorted(included, key=lambda p: (-p.value, -p.city.population))[:20]:
        state_name = state_names.get(placement.state_id, str(placement.state_id))
        print(
            f"  {placement.city.label:28s} -> prov {placement.province_id:5d} "
            f"state {placement.state_id:3d} ({state_name}) value {placement.value}"
        )

    skip_counts = Counter(reason for _, reason in skipped)
    if skip_counts:
        print("\nSkipped reasons:")
        for reason, count in skip_counts.most_common():
            print(f"  {reason}: {count}")

    unmatched = [
        label
        for label in parse_known_vp_localisations(VP_LOCALISATION).values()
        if resolve_city_label(label) is None
    ]
    if unmatched:
        print(f"\nKnown VP labels missing from CITY_DB ({len(unmatched)}):")
        for label in sorted(unmatched)[:15]:
            print(f"  {label}")
        if len(unmatched) > 15:
            print(f"  ... and {len(unmatched) - 15} more")


def spot_check(
    rbf_x: RBFInterpolator,
    rbf_y: RBFInterpolator,
    province_raster: np.ndarray,
    province_to_state: np.ndarray,
    centroids: dict[int, tuple[float, float]],
    state_provinces: dict[int, list[int]],
    region_hints: dict[str, list[int]],
    state_names: dict[int, str],
    checks: list[tuple[str, int]],
) -> None:
    print("\n=== Spot checks ===")
    for label, expected_pid in checks:
        city_entry = CITY_BY_LABEL.get(label)
        if not city_entry:
            print(f"  {label}: missing from CITY_DB")
            continue
        city = CityRecord(
            city_entry["name"],
            city_entry["abbr"],
            city_entry["lon"],
            city_entry["lat"],
            city_entry["population"],
            city_entry["label"],
        )
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
        status = "OK" if pid == expected_pid else "MISS"
        print(f"  {label:24s} expected {expected_pid:5d} got {pid:5d} [{status}]")


def run(apply: bool) -> None:
    states = parse_states(STATE_DIR)
    state_names = load_state_names(POPULATION_CSV)
    color_to_province, max_province = parse_definition(DEFINITION_CSV)
    province_raster = rasterize_provinces(PROVINCES_BMP, color_to_province)
    centroids = compute_province_centroids(province_raster)

    province_to_state = np.zeros(max_province + 1, dtype=np.uint16)
    state_provinces: dict[int, list[int]] = {}
    for state in states.values():
        state_provinces[state["id"]] = state["provinces"]
        for province_id in state["provinces"]:
            if province_id <= max_province:
                province_to_state[province_id] = state["id"]

    region_hints = build_region_hints(state_names)
    known_vps = parse_known_vp_localisations(VP_LOCALISATION)
    calibration_pairs = load_or_create_anchor_snapshot(known_vps, centroids)
    rbf_x, rbf_y = fit_forward_rbf(calibration_pairs)
    calibration = evaluate_calibration(
        calibration_pairs,
        centroids,
        province_raster,
        province_to_state,
        state_provinces,
        region_hints,
        state_names,
    )

    states_with_vp = {state_id for state_id, state in states.items() if state_has_victory_points(state["path"])}
    cities = load_city_db()
    sparse_abbrs = sparse_abbreviations(cities)
    mapped, skipped = map_cities(
        cities,
        rbf_x,
        rbf_y,
        province_raster,
        province_to_state,
        centroids,
        state_provinces,
        region_hints,
        state_names,
        states_with_vp,
    )
    reserved_labels = set(known_vps.values())
    chosen = choose_inclusions(mapped, sparse_abbrs, reserved_labels)
    placements = list(chosen.values())

    print_summary(calibration, placements, skipped, state_names)
    spot_check(
        rbf_x,
        rbf_y,
        province_raster,
        province_to_state,
        centroids,
        state_provinces,
        region_hints,
        state_names,
        [
            ("Chicago, IL", 4073),
            ("Houston, TX", 4438),
            ("Seattle, WA", 4005),
        ],
    )

    write_csv(OUTPUT_CSV, placements, skipped, state_names)
    print(f"\nWrote {OUTPUT_CSV}")

    if not apply:
        print("\nDry run only. Re-run with --apply to edit state files and localisation.")
        return

    by_state: dict[int, list[tuple[int, int]]] = defaultdict(list)
    loc_entries: list[tuple[int, str]] = []
    for placement in placements:
        if not placement.included:
            continue
        by_state[placement.state_id].append((placement.province_id, placement.value))
        loc_entries.append((placement.province_id, placement.city.label))

    edited_states = 0
    for state_id, assignments in sorted(by_state.items()):
        if insert_victory_points(states[state_id]["path"], assignments):
            edited_states += 1

    added_loc = append_localisations(VP_LOCALISATION, loc_entries)
    print(f"\nApplied victory points to {edited_states} states.")
    print(f"Appended {added_loc} localisation entries to {VP_LOCALISATION}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Assign victory points from curated city database.")
    parser.add_argument("--apply", action="store_true", help="Write state files and localisation.")
    args = parser.parse_args()
    run(apply=args.apply)


if __name__ == "__main__":
    main()
