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
from city_database import CITY_BY_LABEL, city_label, merged_city_entries, resolve_city_label

DEFINITION_CSV = ROOT / "map" / "definition.csv"
PROVINCES_BMP = ROOT / "map" / "provinces.bmp"
VP_LOCALISATION = ROOT / "localisation" / "english" / "TNO_victory_points_l_english.yml"
OUTPUT_CSV = ROOT / "victory_point_assignments.csv"
AUDIT_CSV = ROOT / "vp_coverage_audit.csv"
AUDIT_MAP_PNG = ROOT / "vp_coverage_map.png"
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
    (5_000, 3),
    (2_500, 2),
    (1_000, 2),
    (500, 1),
    (0, 1),
)

DENSE_POP_FLOOR = 50_000
SPARSE_POP_FLOOR = 15_000
SPARSE_ABBR_MAX_MAJOR = 4
COVERAGE_MIN_POP = 5_000
SPARSE_US_COVERAGE_MIN_POP = 1_000
MERGE_DENSE_POP_FLOOR = 10_000
MERGE_SPARSE_POP_FLOOR = 5_000
MERGE_SPARSE_US_POP_FLOOR = 1_000
MIN_VP_PIXEL_DISTANCE = 55
SPARSE_US_MIN_VP_PIXEL_DISTANCE = 38
SPARSE_US_STATE_ABBRS = frozenset({"WY", "MT", "ND", "SD", "AK"})
CALIBRATION_MIN_HIT_RATE = 0.85
FORWARD_RBF_SMOOTHING = 0.5
PIXEL_LOOKUP_RADIUS = 20
WATER_PROVINCE_TYPES = frozenset({"lake", "sea"})

DENSE_STATE_CATEGORIES = frozenset({"metropolis", "large_city", "city"})
MEDIUM_STATE_CATEGORIES = frozenset({"town", "rural", "large_town", "small_town", "developed_rural_town"})
SPARSE_STATE_CATEGORIES = frozenset({"wasteland", "enclave", "tiny_island", "pastoral"})

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
    "GUA": ("Guatemala",),
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
    in_sample_hits: int
    in_sample_total: int


def load_city_db() -> list[CityRecord]:
    cities = []
    seen = set()
    for name, abbr, lon, lat, pop in merged_city_entries():
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


def load_province_types(path: Path) -> dict[int, str]:
    province_types: dict[int, str] = {}
    with open(path, encoding="utf-8-sig") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(";")
            if len(parts) < 5:
                continue
            try:
                province_types[int(parts[0])] = parts[4]
            except ValueError:
                continue
    return province_types


def is_land_province(province_id: int, province_types: dict[int, str] | None) -> bool:
    if not province_types:
        return True
    return province_types.get(province_id, "land") not in WATER_PROVINCE_TYPES


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
    if abbr == "GUA":
        return region_hints.get("GUA", []) or _state_ids_for_names(state_names, ("Guatemala",))
    if abbr == "GT" and city.lat < 18:
        return _state_ids_for_names(state_names, ("Guatemala",))
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
    province_types: dict[int, str] | None = None,
) -> int:
    height, width = province_raster.shape
    ix = int(round(px))
    iy = int(round(py))
    if 0 <= ix < width and 0 <= iy < height:
        province_id = int(province_raster[iy, ix])
        if (
            province_id > 0
            and province_id < len(province_to_state)
            and province_to_state[province_id] > 0
            and is_land_province(province_id, province_types)
        ):
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
            if not is_land_province(province_id, province_types):
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
    province_types: dict[int, str] | None = None,
) -> int:
    best_pid = 0
    best_dist = float("inf")
    for state_id in state_ids:
        for province_id in state_provinces.get(state_id, []):
            if not is_land_province(province_id, province_types):
                continue
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
    province_types: dict[int, str] | None = None,
) -> int:
    px, py = project_to_pixel(city.lon, city.lat, rbf_x, rbf_y)
    hinted_states = resolve_hint_states(city, region_hints, state_names)
    if hinted_states:
        return nearest_province_in_states(
            px, py, hinted_states, state_provinces, centroids, province_types
        )
    return locate_province_pixel(px, py, province_raster, province_to_state, province_types)


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
    province_types: dict[int, str] | None = None,
) -> CalibrationReport:
    rbf_x, rbf_y = fit_forward_rbf(pairs)
    residuals_px = []
    in_sample_hits = 0
    for lon, lat, x, y, expected_pid, _ in pairs:
        px, py = project_to_pixel(lon, lat, rbf_x, rbf_y)
        residuals_px.append(((px - x) ** 2 + (py - y) ** 2) ** 0.5)
        assigned_pid = locate_province_pixel(px, py, province_raster, province_to_state, province_types)
        if assigned_pid == expected_pid:
            in_sample_hits += 1

    hits = 0
    for idx, (lon, lat, x, y, expected_pid, label) in enumerate(pairs):
        subset = [pairs[j] for j in range(len(pairs)) if j != idx]
        if len(subset) < 6:
            continue
        rbf_x_loo, rbf_y_loo = fit_forward_rbf(subset)
        px, py = project_to_pixel(lon, lat, rbf_x_loo, rbf_y_loo)
        residual = ((px - x) ** 2 + (py - y) ** 2) ** 0.5
        assigned_pid = locate_province_pixel(px, py, province_raster, province_to_state, province_types)
        if assigned_pid == expected_pid or residual <= PIXEL_LOOKUP_RADIUS:
            hits += 1

    total = max(len(pairs) - (1 if len(pairs) >= 6 else 0), 0)
    return CalibrationReport(
        pairs=len(pairs),
        mean_residual_px=float(np.mean(residuals_px)) if residuals_px else 0.0,
        max_residual_px=float(np.max(residuals_px)) if residuals_px else 0.0,
        leave_one_out_hits=hits,
        leave_one_out_total=total,
        in_sample_hits=in_sample_hits,
        in_sample_total=len(pairs),
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


def state_in_sparse_us_region(state_name: str) -> bool:
    parts = [part.strip() for part in state_name.split(",")]
    return len(parts) == 2 and parts[1] in SPARSE_US_STATE_ABBRS


def state_density_tier(category: str, province_count: int) -> str:
    if category in DENSE_STATE_CATEGORIES or province_count >= 12:
        return "dense"
    if category in SPARSE_STATE_CATEGORIES or province_count <= 3:
        return "sparse"
    return "medium"


def state_vp_quota(tier: str, province_count: int, state_name: str = "") -> int:
    if tier == "dense":
        quota = max(3, min(8, 2 + province_count // 3))
    elif tier == "medium":
        quota = max(2, min(4, 1 + province_count // 4))
    else:
        quota = max(1, min(2, 1 + province_count // 6))
    if state_in_sparse_us_region(state_name) and tier in {"sparse", "medium"} and province_count >= 4:
        quota += 1
    return quota


def state_pop_floor(tier: str, state_name: str = "") -> int:
    if tier == "dense":
        return MERGE_DENSE_POP_FLOOR
    if state_in_sparse_us_region(state_name):
        return MERGE_SPARSE_US_POP_FLOOR
    return MERGE_SPARSE_POP_FLOOR


def coverage_min_pop(state_name: str) -> int:
    if state_in_sparse_us_region(state_name):
        return SPARSE_US_COVERAGE_MIN_POP
    return COVERAGE_MIN_POP


def min_vp_pixel_distance(tier: str, state_name: str) -> float:
    if state_in_sparse_us_region(state_name) and tier in {"sparse", "medium"}:
        return SPARSE_US_MIN_VP_PIXEL_DISTANCE
    return MIN_VP_PIXEL_DISTANCE


def pixel_distance(px1: float, py1: float, px2: float, py2: float) -> float:
    return ((px1 - px2) ** 2 + (py1 - py2) ** 2) ** 0.5


def choose_inclusions_with_dispersion(
    mapped: list[tuple[CityRecord, int, int]],
    states: dict,
    state_names: dict[int, str],
    centroids: dict[int, tuple[float, float]],
    existing_vp_provinces: set[int],
    rbf_x: RBFInterpolator,
    rbf_y: RBFInterpolator,
    reserved_labels: set[str] | None = None,
) -> dict[tuple[int, int], Placement]:
    by_province: dict[int, list[tuple[CityRecord, int]]] = defaultdict(list)
    for city, province_id, state_id in mapped:
        by_province[province_id].append((city, state_id))

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

    by_state: dict[int, list[Placement]] = defaultdict(list)
    for placement in chosen.values():
        by_state[placement.state_id].append(placement)

    for state_id, placements in by_state.items():
        state = states[state_id]
        state_name = state_names.get(state_id, "")
        tier = state_density_tier(state.get("category", ""), len(state["provinces"]))
        quota = state_vp_quota(tier, len(state["provinces"]), state_name)
        pop_floor = state_pop_floor(tier, state_name)
        min_distance = min_vp_pixel_distance(tier, state_name)

        existing_in_state = sum(
            1 for pid in state["provinces"] if pid in existing_vp_provinces
        )
        slots = max(0, quota - existing_in_state)
        if slots <= 0:
            continue

        eligible = [p for p in placements if p.city.population >= pop_floor]
        eligible.sort(key=lambda p: (-p.city.population, p.province_id))

        included_pixels: list[tuple[float, float]] = []
        for pid in state["provinces"]:
            if pid in existing_vp_provinces and pid in centroids:
                included_pixels.append(centroids[pid])

        picked = 0
        for placement in eligible:
            if picked >= slots:
                break
            if placement.province_id in existing_vp_provinces:
                continue
            px, py = project_to_pixel(placement.city.lon, placement.city.lat, rbf_x, rbf_y)
            if any(pixel_distance(px, py, ex, ey) < min_distance for ex, ey in included_pixels):
                continue
            placement.included = True
            placement.reason = f"quota_{tier}"
            included_pixels.append((px, py))
            picked += 1

        if existing_in_state == 0 and picked == 0:
            min_cov = coverage_min_pop(state_name)
            fallback = [p for p in placements if p.city.population >= min_cov]
            if fallback:
                best = max(fallback, key=lambda p: p.city.population)
                if best.province_id not in existing_vp_provinces:
                    best.included = True
                    best.reason = "state_coverage_guarantee"

    enforce_unique_labels(chosen, candidates, reserved_labels or set())
    return chosen


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
    occupied_provinces: set[int],
    only_empty_states: bool,
    states_with_vp: set[int],
    province_types: dict[int, str] | None = None,
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
            province_types,
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
        if province_id in occupied_provinces:
            skipped.append((city, "province_has_vp"))
            continue
        if only_empty_states and state_id in states_with_vp:
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


def parse_existing_vp_assignments(state_dir: Path) -> dict[int, dict[int, int]]:
    """Return {state_id: {province_id: value}} for all existing victory points."""
    assignments: dict[int, dict[int, int]] = defaultdict(dict)
    pattern = re.compile(r"^\s*victory_points\s*=\s*\{([^}]*)\}", flags=re.MULTILINE)
    id_pattern = re.compile(r"^\s*id\s*=\s*(\d+)\s*$", flags=re.MULTILINE)
    for path in state_dir.glob("*.txt"):
        text = path.read_text(encoding="utf-8")
        id_match = id_pattern.search(text)
        if not id_match:
            continue
        state_id = int(id_match.group(1))
        for match in pattern.finditer(text):
            nums = [int(value) for value in re.findall(r"\d+", match.group(1))]
            for idx in range(0, len(nums) - 1, 2):
                assignments[state_id][nums[idx]] = nums[idx + 1]
    return assignments


def merge_victory_points(path: Path, assignments: list[tuple[int, int]]) -> int:
    """Append new victory points; returns count of provinces added."""
    text = path.read_text(encoding="utf-8")
    existing_pids = set()
    pattern = re.compile(r"^\s*victory_points\s*=\s*\{([^}]*)\}", flags=re.MULTILINE)
    for match in pattern.finditer(text):
        nums = [int(value) for value in re.findall(r"\d+", match.group(1))]
        for idx in range(0, len(nums) - 1, 2):
            existing_pids.add(nums[idx])

    new_assignments = [(pid, value) for pid, value in assignments if pid not in existing_pids]
    if not new_assignments:
        return 0

    lines = []
    for province_id, value in sorted(new_assignments, key=lambda item: (-item[1], item[0])):
        lines.append(f"\t\tvictory_points = {{ {province_id} {value} }}")

    if pattern.search(text):
        last_end = 0
        for match in pattern.finditer(text):
            last_end = match.end()
        block = "\n" + "\n".join(lines) + "\n"
        updated = text[:last_end] + block + text[last_end:]
    else:
        insert_at = find_history_insert_index(text)
        if insert_at is None:
            raise ValueError(f"Could not locate history block in {path}")
        block = "\n" + "\n".join(lines) + "\n"
        updated = text[:insert_at] + block + text[insert_at:]

    path.write_text(updated, encoding="utf-8")
    return len(new_assignments)


def build_province_population_map(
    known_vps: dict[int, str],
    mapped: list[tuple[CityRecord, int, int]],
    existing_vp_provinces: set[int],
) -> dict[int, int]:
    """Map existing VP provinces to best-known population for value rescaling."""
    province_pop: dict[int, int] = {}
    for province_id, label in known_vps.items():
        if province_id not in existing_vp_provinces:
            continue
        city = resolve_city_label(label)
        if city:
            province_pop[province_id] = int(city["population"])

    by_province: dict[int, list[CityRecord]] = defaultdict(list)
    for city, province_id, _state_id in mapped:
        by_province[province_id].append(city)

    for province_id in existing_vp_provinces:
        candidates = by_province.get(province_id, [])
        if not candidates:
            continue
        best_pop = max(city.population for city in candidates)
        if province_id not in province_pop or best_pop > province_pop[province_id]:
            province_pop[province_id] = best_pop
    return province_pop


def rescale_victory_points_in_file(path: Path, province_population: dict[int, int]) -> int:
    """Rewrite VP values in one state file from population tiers. Returns count changed."""
    text = path.read_text(encoding="utf-8")
    block_pattern = re.compile(r"^(\s*victory_points\s*=\s*\{\s*)(\d+)(\s+)(\d+)(\s*\})", flags=re.MULTILINE)
    changed = 0

    def replace_block(match: re.Match[str]) -> str:
        nonlocal changed
        province_id = int(match.group(2))
        old_value = int(match.group(4))
        population = province_population.get(province_id)
        if population is None:
            return match.group(0)
        new_value = value_for_population(population)
        if new_value == old_value:
            return match.group(0)
        changed += 1
        return f"{match.group(1)}{province_id}{match.group(3)}{new_value}{match.group(5)}"

    updated = block_pattern.sub(replace_block, text)
    if changed:
        path.write_text(updated, encoding="utf-8")
    return changed


def rescale_all_victory_point_values(
    states: dict,
    province_population: dict[int, int],
) -> int:
    total = 0
    for state in states.values():
        total += rescale_victory_points_in_file(state["path"], province_population)
    return total


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


@dataclass
class LakeRelocation:
    state_id: int
    old_pid: int
    new_pid: int
    value: int
    label: str
    state_path: Path


def find_nearest_land_vp_target(
    lake_pid: int,
    state_provinces: list[int],
    existing_vps: dict[int, int],
    centroids: dict[int, tuple[float, float]],
    province_types: dict[int, str],
) -> int:
    lake_centroid = centroids.get(lake_pid)
    if not lake_centroid:
        return 0
    cx, cy = lake_centroid
    best_pid = 0
    best_dist = float("inf")
    for candidate in state_provinces:
        if candidate == lake_pid:
            continue
        if not is_land_province(candidate, province_types):
            continue
        if candidate in existing_vps:
            continue
        centroid = centroids.get(candidate)
        if not centroid:
            continue
        dist = (centroid[0] - cx) ** 2 + (centroid[1] - cy) ** 2
        if dist < best_dist:
            best_dist = dist
            best_pid = candidate
    return best_pid


def plan_lake_vp_relocations(
    states: dict,
    province_types: dict[int, str],
    centroids: dict[int, tuple[float, float]],
    known_vps: dict[int, str],
) -> tuple[list[LakeRelocation], list[tuple[int, int, int, str]]]:
    assignments = parse_existing_vp_assignments(STATE_DIR)
    relocations: list[LakeRelocation] = []
    failures: list[tuple[int, int, int, str]] = []
    for state_id, vps in sorted(assignments.items()):
        state = states.get(state_id)
        if not state:
            continue
        for old_pid, value in sorted(vps.items()):
            if province_types.get(old_pid) != "lake":
                continue
            new_pid = find_nearest_land_vp_target(
                old_pid,
                state["provinces"],
                vps,
                centroids,
                province_types,
            )
            label = known_vps.get(old_pid, f"province_{old_pid}")
            if not new_pid:
                failures.append((state_id, old_pid, value, label))
                continue
            relocations.append(
                LakeRelocation(
                    state_id=state_id,
                    old_pid=old_pid,
                    new_pid=new_pid,
                    value=value,
                    label=label,
                    state_path=state["path"],
                )
            )
    return relocations, failures


def relocate_vp_in_state_file(path: Path, old_pid: int, new_pid: int) -> bool:
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"^(\s*victory_points\s*=\s*\{{\s*){old_pid}(\s+)(\d+)(\s*\}})",
        flags=re.MULTILINE,
    )
    if not pattern.search(text):
        return False

    def replace_block(match: re.Match[str]) -> str:
        return f"{match.group(1)}{new_pid}{match.group(2)}{match.group(3)}{match.group(4)}"

    updated = pattern.sub(replace_block, text, count=1)
    path.write_text(updated, encoding="utf-8")
    return True


def relocate_vp_localisation(path: Path, old_pid: int, new_pid: int) -> bool:
    text = path.read_text(encoding="utf-8-sig")
    old_pattern = re.compile(
        rf'^(\s*)VICTORY_POINTS_{old_pid}:0\s*"([^"]+)"\s*$',
        flags=re.MULTILINE,
    )
    match = old_pattern.search(text)
    if not match:
        return False
    indent, label = match.group(1), match.group(2)
    if re.search(rf"VICTORY_POINTS_{new_pid}:0", text):
        text = old_pattern.sub("", text)
    else:
        text = old_pattern.sub(rf'{indent}VICTORY_POINTS_{new_pid}:0 "{label}"', text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    path.write_text(text, encoding="utf-8")
    return True


def update_anchor_snapshot_for_relocation(old_pid: int, new_pid: int) -> bool:
    if not ANCHOR_SNAPSHOT.exists():
        return False
    rows = list(csv.reader(ANCHOR_SNAPSHOT.open(encoding="utf-8")))
    if not rows:
        return False
    header, *data = rows
    changed = False
    updated_rows = [header]
    for row in data:
        if len(row) >= 4 and int(row[0]) == old_pid:
            row[0] = str(new_pid)
            changed = True
        updated_rows.append(row)
    if changed:
        with ANCHOR_SNAPSHOT.open("w", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerows(updated_rows)
    return changed


def fix_lake_victory_points(apply: bool) -> None:
    states = parse_states(STATE_DIR)
    province_types = load_province_types(DEFINITION_CSV)
    color_to_province, _max_province = parse_definition(DEFINITION_CSV)
    province_raster = rasterize_provinces(PROVINCES_BMP, color_to_province)
    centroids = compute_province_centroids(province_raster)
    known_vps = parse_known_vp_localisations(VP_LOCALISATION)
    relocations, failures = plan_lake_vp_relocations(states, province_types, centroids, known_vps)

    print(f"\n=== Lake province VP relocation ===")
    print(f"Found {len(relocations)} victory points on lake provinces.")
    for move in relocations:
        print(
            f"  state {move.state_id:3d}: {move.old_pid} -> {move.new_pid} "
            f"(value {move.value}) {move.label}"
        )
    if failures:
        print(f"\nCould not relocate {len(failures)} lake VPs:")
        for state_id, old_pid, value, label in failures:
            print(f"  state {state_id}: {old_pid} (value {value}) {label}")

    if not apply:
        print("\nDry run only. Re-run with --fix-lakes --apply to edit state files and localisation.")
        return

    moved_states = 0
    moved_loc = 0
    moved_anchors = 0
    for move in relocations:
        if relocate_vp_in_state_file(move.state_path, move.old_pid, move.new_pid):
            moved_states += 1
        if relocate_vp_localisation(VP_LOCALISATION, move.old_pid, move.new_pid):
            moved_loc += 1
        if update_anchor_snapshot_for_relocation(move.old_pid, move.new_pid):
            moved_anchors += 1

    remaining = sum(
        1
        for _state_id, vps in parse_existing_vp_assignments(STATE_DIR).items()
        for pid in vps
        if province_types.get(pid) == "lake"
    )
    print(
        f"\nRelocated {len(relocations)} lake VPs "
        f"({moved_states} state edits, {moved_loc} localisation keys, {moved_anchors} anchor rows)."
    )
    print(f"Remaining lake VPs: {remaining}")


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
    if calibration.in_sample_total:
        rate = 100.0 * calibration.in_sample_hits / calibration.in_sample_total
        print(
            f"In-sample province hit-rate: {calibration.in_sample_hits}/"
            f"{calibration.in_sample_total} ({rate:.1f}%)"
        )

    print("\n=== Placement ===")
    print(f"Cities in database: {len(load_city_db())}")
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
    province_types: dict[int, str] | None = None,
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
            province_types,
        )
        status = "OK" if pid == expected_pid else "MISS"
        print(f"  {label:24s} expected {expected_pid:5d} got {pid:5d} [{status}]")


def write_audit_csv(
    path: Path,
    states: dict,
    state_names: dict[int, str],
    placements: list[Placement],
    existing_vp_provinces: set[int],
    centroids: dict[int, tuple[float, float]],
    rbf_x: RBFInterpolator,
    rbf_y: RBFInterpolator,
) -> None:
    included_by_state: dict[int, list[Placement]] = defaultdict(list)
    for placement in placements:
        if placement.included:
            included_by_state[placement.state_id].append(placement)

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "state_id",
                "state_name",
                "category",
                "province_count",
                "existing_vps",
                "proposed_new_vps",
                "tier",
                "quota",
                "max_gap_px",
            ]
        )
        for state_id in sorted(states):
            state = states[state_id]
            tier = state_density_tier(state.get("category", ""), len(state["provinces"]))
            quota = state_vp_quota(tier, len(state["provinces"]), state_names.get(state_id, ""))
            existing = sum(1 for pid in state["provinces"] if pid in existing_vp_provinces)
            proposed = len(included_by_state.get(state_id, []))

            vp_pixels: list[tuple[float, float]] = []
            for pid in state["provinces"]:
                if pid in existing_vp_provinces and pid in centroids:
                    vp_pixels.append(centroids[pid])
            for placement in included_by_state.get(state_id, []):
                vp_pixels.append(project_to_pixel(placement.city.lon, placement.city.lat, rbf_x, rbf_y))

            max_gap = 0.0
            for pid in state["provinces"]:
                if pid not in centroids:
                    continue
                cx, cy = centroids[pid]
                if not vp_pixels:
                    max_gap = max(max_gap, 9999.0)
                    continue
                nearest = min(pixel_distance(cx, cy, px, py) for px, py in vp_pixels)
                max_gap = max(max_gap, nearest)

            writer.writerow(
                [
                    state_id,
                    state_names.get(state_id, ""),
                    state.get("category", ""),
                    len(state["provinces"]),
                    existing,
                    proposed,
                    tier,
                    quota,
                    f"{max_gap:.1f}",
                ]
            )


def write_audit_map(
    path: Path,
    province_raster: np.ndarray,
    placements: list[Placement],
    existing_vp_provinces: set[int],
    centroids: dict[int, tuple[float, float]],
    rbf_x: RBFInterpolator,
    rbf_y: RBFInterpolator,
) -> None:
    from PIL import Image, ImageDraw

    scale = 4
    height, width = province_raster.shape
    out_h, out_w = height // scale, width // scale
    base = Image.new("RGB", (out_w, out_h), (24, 24, 24))
    pixels = base.load()
    for y in range(out_h):
        for x in range(out_w):
            pid = int(province_raster[y * scale, x * scale])
            if pid > 0:
                pixels[x, y] = (48, 52, 58)

    draw = ImageDraw.Draw(base)
    for pid in existing_vp_provinces:
        centroid = centroids.get(pid)
        if not centroid:
            continue
        draw.ellipse(
            (centroid[0] / scale - 2, centroid[1] / scale - 2, centroid[0] / scale + 2, centroid[1] / scale + 2),
            fill=(220, 180, 60),
        )
    for placement in placements:
        if not placement.included:
            continue
        px, py = project_to_pixel(placement.city.lon, placement.city.lat, rbf_x, rbf_y)
        draw.ellipse((px / scale - 3, py / scale - 3, px / scale + 3, py / scale + 3), fill=(80, 200, 120))

    base.save(path)


def run(apply: bool, merge: bool, audit: bool, only_empty_states: bool) -> None:
    states = parse_states(STATE_DIR)
    state_names = load_state_names(POPULATION_CSV)
    province_types = load_province_types(DEFINITION_CSV)
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
        province_types,
    )

    states_with_vp = {state_id for state_id, state in states.items() if state_has_victory_points(state["path"])}
    existing_vp_provinces = parse_existing_vp_provinces(STATE_DIR)
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
        existing_vp_provinces,
        only_empty_states,
        states_with_vp,
        province_types,
    )
    reserved_labels = set(known_vps.values())
    if merge and not only_empty_states:
        chosen = choose_inclusions_with_dispersion(
            mapped,
            states,
            state_names,
            centroids,
            existing_vp_provinces,
            rbf_x,
            rbf_y,
            reserved_labels,
        )
    else:
        chosen = choose_inclusions(mapped, sparse_abbrs, reserved_labels)
    placements = list(chosen.values())

    if calibration.in_sample_total:
        hit_rate = calibration.in_sample_hits / calibration.in_sample_total
        if hit_rate < CALIBRATION_MIN_HIT_RATE:
            print(
                f"\nERROR: In-sample calibration hit-rate {hit_rate:.1%} is below "
                f"{CALIBRATION_MIN_HIT_RATE:.0%} gate. Aborting before apply."
            )
            apply = False

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
        province_types,
    )

    write_csv(OUTPUT_CSV, placements, skipped, state_names)
    print(f"\nWrote {OUTPUT_CSV}")

    if audit:
        write_audit_csv(
            AUDIT_CSV,
            states,
            state_names,
            placements,
            existing_vp_provinces,
            centroids,
            rbf_x,
            rbf_y,
        )
        write_audit_map(
            AUDIT_MAP_PNG,
            province_raster,
            placements,
            existing_vp_provinces,
            centroids,
            rbf_x,
            rbf_y,
        )
        print(f"Wrote {AUDIT_CSV}")
        print(f"Wrote {AUDIT_MAP_PNG}")

    if not apply:
        print("\nDry run only. Re-run with --apply to edit state files and localisation.")
        return

    province_population = build_province_population_map(
        known_vps,
        mapped,
        existing_vp_provinces,
    )
    for placement in placements:
        if placement.included:
            province_population[placement.province_id] = placement.city.population

    rescaled = rescale_all_victory_point_values(states, province_population)
    print(f"Rescaled VP values for {rescaled} existing province entries using population tiers (min value 1).")

    by_state: dict[int, list[tuple[int, int]]] = defaultdict(list)
    loc_entries: list[tuple[int, str]] = []
    for placement in placements:
        if not placement.included:
            continue
        by_state[placement.state_id].append((placement.province_id, placement.value))
        loc_entries.append((placement.province_id, placement.city.label))

    edited_states = 0
    added_vp_count = 0
    for state_id, assignments in sorted(by_state.items()):
        if merge and not only_empty_states:
            added = merge_victory_points(states[state_id]["path"], assignments)
            if added:
                edited_states += 1
                added_vp_count += added
        elif insert_victory_points(states[state_id]["path"], assignments):
            edited_states += 1
            added_vp_count += len(assignments)

    added_loc = append_localisations(VP_LOCALISATION, loc_entries)
    print(f"\nApplied victory points to {edited_states} states ({added_vp_count} new province VPs).")
    print(f"Appended {added_loc} localisation entries to {VP_LOCALISATION}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Assign victory points from GeoNames city database.")
    parser.add_argument("--apply", action="store_true", help="Write state files and localisation.")
    parser.add_argument(
        "--fix-lakes",
        action="store_true",
        help="Move victory points off lake provinces onto nearby land provinces in the same state.",
    )
    parser.add_argument(
        "--only-empty-states",
        action="store_true",
        help="Legacy mode: only fill states that have zero victory points.",
    )
    parser.add_argument("--audit", action="store_true", help="Write vp_coverage_audit.csv and vp_coverage_map.png.")
    args = parser.parse_args()
    if args.fix_lakes:
        fix_lake_victory_points(apply=args.apply)
        return
    merge = not args.only_empty_states
    run(apply=args.apply, merge=merge, audit=args.audit, only_empty_states=args.only_empty_states)


if __name__ == "__main__":
    main()
