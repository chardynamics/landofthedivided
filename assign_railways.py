#!/usr/bin/env python3
"""Split railways at hub provinces and assign per-segment levels."""
from __future__ import annotations

import argparse
import csv
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
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
)
from assign_victory_points import (
    ANCHOR_SNAPSHOT,
    compute_province_centroids,
    parse_known_vp_localisations,
)

RAILWAYS_TXT = ROOT / "map" / "railways.txt"
VP_LOCALISATION = ROOT / "localisation" / "english" / "TNO_victory_points_l_english.yml"
VP_ASSIGNMENTS_CSV = ROOT / "victory_point_assignments.csv"
OUTPUT_CSV = ROOT / "railway_assignments.csv"

US_STATE_ABBRS = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC PR".split()
)
CANADA_TAGS = frozenset({"CAN"})
MEXICO_TAGS = frozenset({"MEX"})
CARIBBEAN_TAGS = frozenset({"CUB", "DOM", "HAI", "BAH", "JAM", "PUR", "PRI"})

# Piecewise Mississippi River (lat, lon). East = lon > river_lon(lat).
MISSISSIPPI_RIVER = (
    (29.2, -89.5),
    (32.3, -90.9),
    (35.1, -90.0),
    (38.6, -90.2),
    (42.5, -90.6),
    (44.9, -93.1),
    (47.5, -95.2),
)

RURAL_CATEGORIES = frozenset({"pastoral", "rural", "wasteland", "enclave"})
MAJOR_CATEGORIES = frozenset({"megalopolis", "metropolis"})
CANADA_NORTH_PATTERNS = (
    "Yukon", "Northwest", "Nunavut", "Northern Alberta", "Northern British",
    "North Manitoba", "Moresby Island", "Labrador",
)
MEXICO_CORRIDOR_KEYWORDS = (
    "Mexico City", "Monterrey", "Guadalajara", "Tijuana", "Ciudad Juarez",
    "Juarez", "Matamoros", "Tampico", "Puebla", "Leon", "Queretaro",
)

# Dense US metro corridors eligible for level 5.
NORTHEAST_STATE_ABBRS = frozenset({"NY", "NJ", "PA", "CT", "MA", "RI", "NH", "VT", "ME", "MD", "DE", "DC"})
DENSE_US_OWNER_TAGS = frozenset({"SCA", "STX", "NYC", "PHI", "EFG", "BRA"})
FOREIGN_LEVEL_CAP_TAGS = CANADA_TAGS | MEXICO_TAGS | CARIBBEAN_TAGS


@dataclass
class RailwayEntry:
    level: int
    provinces: list[int]


@dataclass
class SegmentRow:
    segment_id: int
    level: int
    provinces: list[int]
    regional_floor: int
    endpoint_boost: int
    boost_reason: str
    endpoint_a: int
    endpoint_b: int
    vp_a: int
    vp_b: int
    state_a: str
    state_b: str
    owner_a: str
    owner_b: str


@dataclass
class ProvinceMeta:
    lon: float = 0.0
    lat: float = 0.0
    state_id: int = 0
    state_name: str = ""
    owner: str = ""
    category: str = ""
    population: int = 0
    vp_value: int = 0


def parse_railways(path: Path) -> list[RailwayEntry]:
    entries = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        level = int(parts[0])
        count = int(parts[1])
        provinces = [int(value) for value in parts[2 : 2 + count]]
        if len(provinces) != count:
            raise ValueError(f"Province count mismatch in {path}: expected {count}, got {len(provinces)}")
        entries.append(RailwayEntry(level=level, provinces=provinces))
    return entries


def dedupe_consecutive(provinces: list[int]) -> list[int]:
    if not provinces:
        return []
    cleaned = [provinces[0]]
    for province_id in provinces[1:]:
        if province_id != cleaned[-1]:
            cleaned.append(province_id)
    return cleaned


def detect_hubs(entries: list[RailwayEntry]) -> set[int]:
    entries_per_province: Counter[int] = Counter()
    degree: Counter[int] = Counter()
    for entry in entries:
        for province_id in set(entry.provinces):
            entries_per_province[province_id] += 1
        for left, right in zip(entry.provinces, entry.provinces[1:]):
            degree[left] += 1
            degree[right] += 1
    hubs = set()
    for province_id, count in entries_per_province.items():
        if count >= 2:
            hubs.add(province_id)
    for province_id, count in degree.items():
        if count >= 3:
            hubs.add(province_id)
    return hubs


def split_entry_at_hubs(provinces: list[int], hubs: set[int]) -> list[list[int]]:
    provinces = dedupe_consecutive(provinces)
    if len(provinces) < 2:
        return []
    split_indices = [0]
    for index in range(1, len(provinces) - 1):
        if provinces[index] in hubs:
            split_indices.append(index)
    split_indices.append(len(provinces) - 1)
    segments = []
    for start, end in zip(split_indices, split_indices[1:]):
        segment = provinces[start : end + 1]
        if len(segment) >= 2:
            segments.append(segment)
    return segments


def split_all_entries(entries: list[RailwayEntry], hubs: set[int]) -> list[list[int]]:
    segments: list[list[int]] = []
    for entry in entries:
        segments.extend(split_entry_at_hubs(entry.provinces, hubs))
    return segments


def fit_inverse_rbf(
    pairs: list[tuple[float, float, float, float, int, str]],
) -> tuple[RBFInterpolator, RBFInterpolator]:
    coords = np.array([[x, y] for _, _, x, y, _, _ in pairs], dtype=float)
    lons = np.array([lon for lon, _, _, _, _, _ in pairs], dtype=float)
    lats = np.array([lat for _, lat, _, _, _, _ in pairs], dtype=float)
    rbf_lon = RBFInterpolator(coords, lons, kernel="thin_plate_spline", smoothing=0.5)
    rbf_lat = RBFInterpolator(coords, lats, kernel="thin_plate_spline", smoothing=0.5)
    return rbf_lon, rbf_lat


def load_calibration_pairs(centroids: dict[int, tuple[float, float]]) -> list[tuple[float, float, float, float, int, str]]:
    pairs = []
    with open(ANCHOR_SNAPSHOT, encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            province_id = int(row["province_id"])
            if province_id not in centroids:
                continue
            x, y = centroids[province_id]
            pairs.append((float(row["lon"]), float(row["lat"]), x, y, province_id, row["label"]))
    if len(pairs) < 6:
        known_vps = parse_known_vp_localisations(VP_LOCALISATION)
        for province_id, label in known_vps.items():
            if province_id not in centroids:
                continue
            from assign_victory_points import resolve_city_label

            city = resolve_city_label(label)
            if not city:
                continue
            x, y = centroids[province_id]
            pairs.append((city["lon"], city["lat"], x, y, province_id, label))
    if len(pairs) < 6:
        raise ValueError("Need at least 6 calibration pairs for inverse RBF")
    return pairs


def mississippi_lon_at(lat: float) -> float:
    if lat <= MISSISSIPPI_RIVER[0][0]:
        return MISSISSIPPI_RIVER[0][1]
    if lat >= MISSISSIPPI_RIVER[-1][0]:
        return MISSISSIPPI_RIVER[-1][1]
    for (lat_a, lon_a), (lat_b, lon_b) in zip(MISSISSIPPI_RIVER, MISSISSIPPI_RIVER[1:]):
        if lat_a <= lat <= lat_b:
            frac = (lat - lat_a) / (lat_b - lat_a) if lat_b != lat_a else 0.0
            return lon_a + frac * (lon_b - lon_a)
    return MISSISSIPPI_RIVER[-1][1]


def state_abbr_from_name(name: str) -> str:
    match = re.search(r",\s*([A-Z]{2})\b", name)
    return match.group(1) if match else ""


def is_us_state(meta: ProvinceMeta) -> bool:
    abbr = state_abbr_from_name(meta.state_name)
    return abbr in US_STATE_ABBRS


def is_canada_north(meta: ProvinceMeta) -> bool:
    if any(pattern in meta.state_name for pattern in CANADA_NORTH_PATTERNS):
        return True
    return meta.lat >= 53.0


def is_mexico_corridor_state(name: str) -> bool:
    return any(keyword in name for keyword in MEXICO_CORRIDOR_KEYWORDS)


def is_northeast_us(meta: ProvinceMeta) -> bool:
    abbr = state_abbr_from_name(meta.state_name)
    if abbr in NORTHEAST_STATE_ABBRS:
        return True
    return meta.lon > -80.0 and 36.0 <= meta.lat <= 47.5 and meta.lon > mississippi_lon_at(meta.lat)


def is_california_corridor(meta: ProvinceMeta) -> bool:
    return meta.owner == "SCA" or state_abbr_from_name(meta.state_name) == "CA"


def is_texas_corridor(meta: ProvinceMeta) -> bool:
    return meta.owner == "STX" or state_abbr_from_name(meta.state_name) == "TX"


def is_dense_us_metro(meta: ProvinceMeta) -> bool:
    if meta.owner in DENSE_US_OWNER_TAGS:
        return True
    if is_northeast_us(meta) or is_california_corridor(meta) or is_texas_corridor(meta):
        return True
    if meta.category in MAJOR_CATEGORIES and is_us_state(meta):
        return meta.lon > mississippi_lon_at(meta.lat) or is_california_corridor(meta) or is_texas_corridor(meta)
    return False


def is_foreign_domestic_segment(meta_a: ProvinceMeta, meta_b: ProvinceMeta) -> bool:
    """Both endpoints belong to Canada, Mexico, or Caribbean tags."""
    foreign_a = meta_a.owner in FOREIGN_LEVEL_CAP_TAGS
    foreign_b = meta_b.owner in FOREIGN_LEVEL_CAP_TAGS
    if not foreign_a or not foreign_b:
        return False
    if meta_a.owner in CANADA_TAGS and meta_b.owner in CANADA_TAGS:
        return True
    if meta_a.owner in MEXICO_TAGS and meta_b.owner in MEXICO_TAGS:
        return True
    if meta_a.owner in CARIBBEAN_TAGS and meta_b.owner in CARIBBEAN_TAGS:
        return True
    return False


def foreign_level_cap(meta_a: ProvinceMeta, meta_b: ProvinceMeta) -> int | None:
    """Maximum level for segments touching Canada, Mexico, or the Caribbean."""
    if is_foreign_domestic_segment(meta_a, meta_b):
        return 3
    owners = {meta_a.owner, meta_b.owner}
    if owners & MEXICO_TAGS or owners & CARIBBEAN_TAGS:
        return 3
    if owners & CANADA_TAGS:
        return 4
    return None


def province_regional_floor(meta: ProvinceMeta) -> int:
    if meta.owner in CANADA_TAGS:
        return 2 if is_canada_north(meta) else 3
    if meta.owner in MEXICO_TAGS:
        if meta.vp_value >= 25 or is_mexico_corridor_state(meta.state_name):
            return 3
        if meta.vp_value >= 16 or meta.population >= 500_000:
            return 3
        return 2
    if meta.owner in CARIBBEAN_TAGS:
        return 3 if meta.vp_value >= 16 else 2
    if is_us_state(meta) or (meta.owner and meta.owner not in CANADA_TAGS | MEXICO_TAGS | CARIBBEAN_TAGS):
        return 3 if meta.lon > mississippi_lon_at(meta.lat) else 2
    # Fallback for odd tags: use Mississippi geography when inside continental US lat/lon box.
    if 24.0 <= meta.lat <= 50.0 and -125.0 <= meta.lon <= -66.0:
        return 3 if meta.lon > mississippi_lon_at(meta.lat) else 2
    return 2


def endpoint_boost_level(meta_a: ProvinceMeta, meta_b: ProvinceMeta) -> tuple[int, str]:
    vp_a, vp_b = meta_a.vp_value, meta_b.vp_value
    dense_a = is_dense_us_metro(meta_a)
    dense_b = is_dense_us_metro(meta_b)

    if vp_a >= 50 or vp_b >= 50 or (vp_a >= 40 and vp_b >= 40):
        return 5, "major_corridor"
    if (dense_a or dense_b) and (
        vp_a >= 25
        or vp_b >= 25
        or meta_a.category in MAJOR_CATEGORIES
        or meta_b.category in MAJOR_CATEGORIES
    ):
        return 5, "dense_metro"
    if dense_a and dense_b and (vp_a >= 16 or vp_b >= 16):
        return 5, "dense_urban_link"
    if is_northeast_us(meta_a) and is_northeast_us(meta_b) and (vp_a >= 16 or vp_b >= 16):
        return 5, "northeast_corridor"
    if (
        vp_a >= 33
        or vp_b >= 33
        or (vp_a >= 25 and vp_b >= 25)
    ):
        return 4, "city_link"
    if vp_a >= 16 or vp_b >= 16:
        return 3, "regional_city"
    return 2, "none"


def segment_regional_floor(meta_a: ProvinceMeta, meta_b: ProvinceMeta) -> int:
    return max(province_regional_floor(meta_a), province_regional_floor(meta_b))


def is_remote_low_development(meta: ProvinceMeta) -> bool:
    if meta.owner in CANADA_TAGS and is_canada_north(meta):
        return True
    if meta.owner in MEXICO_TAGS and province_regional_floor(meta) <= 2 and meta.vp_value < 16:
        return True
    return False


def assign_segment_level(meta_a: ProvinceMeta, meta_b: ProvinceMeta) -> tuple[int, int, int, str]:
    regional = segment_regional_floor(meta_a, meta_b)
    boost, reason = endpoint_boost_level(meta_a, meta_b)

    rural_a = meta_a.category in RURAL_CATEGORIES and meta_a.vp_value < 16
    rural_b = meta_b.category in RURAL_CATEGORIES and meta_b.vp_value < 16
    if rural_a and rural_b:
        boost = min(boost, regional)

    level = max(regional, boost)
    cap = foreign_level_cap(meta_a, meta_b)
    if cap is not None:
        level = min(level, cap)
    if is_remote_low_development(meta_a) and is_remote_low_development(meta_b):
        level = min(level, 3)
    level = max(2, min(5, level))
    return level, regional, boost, reason


def load_state_owners() -> dict[int, str]:
    owners: dict[int, str] = {}
    for path in STATE_DIR.glob("*.txt"):
        text = path.read_text(encoding="utf-8")
        state_match = re.search(r"id\s*=\s*(\d+)", text)
        owner_match = re.search(r"owner\s*=\s*([A-Z]{3})", text)
        if state_match and owner_match:
            owners[int(state_match.group(1))] = owner_match.group(1)
    return owners


def load_victory_points() -> dict[int, int]:
    values: dict[int, int] = {}
    for path in STATE_DIR.glob("*.txt"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"victory_points\s*=\s*\{\s*(\d+)\s+(\d+)\s*\}", text):
            province_id = int(match.group(1))
            value = int(match.group(2))
            values[province_id] = max(values.get(province_id, 0), value)
    if VP_ASSIGNMENTS_CSV.exists():
        with open(VP_ASSIGNMENTS_CSV, encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row.get("included") != "1" or not row.get("province_id"):
                    continue
                province_id = int(row["province_id"])
                value = int(row["value"])
                values[province_id] = max(values.get(province_id, 0), value)
    return values


def load_state_population() -> dict[int, int]:
    population: dict[int, int] = {}
    if not POPULATION_CSV.exists():
        return population
    with open(POPULATION_CSV, encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                population[int(row["id"])] = int(row.get("estimated_population") or row.get("current_manpower") or 0)
            except (KeyError, ValueError):
                continue
    return population


def build_province_metadata(
    centroids: dict[int, tuple[float, float]],
    rbf_lon: RBFInterpolator,
    rbf_lat: RBFInterpolator,
    states: dict,
    state_names: dict[int, str],
    state_owners: dict[int, str],
    state_population: dict[int, int],
    vp_values: dict[int, int],
) -> dict[int, ProvinceMeta]:
    province_to_state: dict[int, int] = {}
    province_to_category: dict[int, str] = {}
    for state in states.values():
        for province_id in state["provinces"]:
            province_to_state[province_id] = state["id"]
            province_to_category[province_id] = state.get("category", "")

    metadata: dict[int, ProvinceMeta] = {}
    for province_id, (x, y) in centroids.items():
        lon = float(rbf_lon([[x, y]])[0])
        lat = float(rbf_lat([[x, y]])[0])
        state_id = province_to_state.get(province_id, 0)
        metadata[province_id] = ProvinceMeta(
            lon=lon,
            lat=lat,
            state_id=state_id,
            state_name=state_names.get(state_id, f"STATE_{state_id}"),
            owner=state_owners.get(state_id, ""),
            category=province_to_category.get(province_id, ""),
            population=state_population.get(state_id, 0),
            vp_value=vp_values.get(province_id, 0),
        )
    return metadata


def meta_for_province(province_id: int, metadata: dict[int, ProvinceMeta]) -> ProvinceMeta:
    return metadata.get(province_id, ProvinceMeta())


def build_segment_rows(segments: list[list[int]], metadata: dict[int, ProvinceMeta]) -> list[SegmentRow]:
    rows: list[SegmentRow] = []
    for segment_id, provinces in enumerate(segments, start=1):
        meta_a = meta_for_province(provinces[0], metadata)
        meta_b = meta_for_province(provinces[-1], metadata)
        level, regional, boost, reason = assign_segment_level(meta_a, meta_b)
        rows.append(
            SegmentRow(
                segment_id=segment_id,
                level=level,
                provinces=provinces,
                regional_floor=regional,
                endpoint_boost=boost,
                boost_reason=reason,
                endpoint_a=provinces[0],
                endpoint_b=provinces[-1],
                vp_a=meta_a.vp_value,
                vp_b=meta_b.vp_value,
                state_a=meta_a.state_name,
                state_b=meta_b.state_name,
                owner_a=meta_a.owner,
                owner_b=meta_b.owner,
            )
        )
    return rows


def validate_segments(
    rows: list[SegmentRow],
    valid_provinces: set[int],
    owned_provinces: set[int],
) -> list[str]:
    errors: list[str] = []
    warnings: list[str] = []
    for row in rows:
        if row.level < 2 or row.level > 5:
            errors.append(f"segment {row.segment_id}: invalid level {row.level}")
        if len(row.provinces) < 2:
            errors.append(f"segment {row.segment_id}: fewer than 2 provinces")
        if len(row.provinces) > 25:
            warnings.append(
                f"segment {row.segment_id}: long segment ({len(row.provinces)} provinces) "
                f"{row.endpoint_a}->{row.endpoint_b}"
            )
        for left, right in zip(row.provinces, row.provinces[1:]):
            if left == right:
                errors.append(f"segment {row.segment_id}: duplicate consecutive provinces {left}")
        for province_id in row.provinces:
            if province_id not in valid_provinces:
                errors.append(f"segment {row.segment_id}: unknown province {province_id}")
            elif province_id not in owned_provinces:
                errors.append(f"segment {row.segment_id}: stateless province {province_id}")
    return errors + warnings


def format_railway_line(row: SegmentRow) -> str:
    count = len(row.provinces)
    return " ".join([str(row.level), str(count), *map(str, row.provinces)])


def write_railways(path: Path, rows: list[SegmentRow]) -> None:
    lines = [format_railway_line(row) + "\n" for row in rows]
    path.write_text("".join(lines), encoding="utf-8")


def write_csv(path: Path, rows: list[SegmentRow]) -> None:
    fieldnames = [
        "segment_id",
        "level",
        "province_count",
        "endpoint_a",
        "endpoint_b",
        "vp_a",
        "vp_b",
        "regional_floor",
        "endpoint_boost",
        "boost_reason",
        "state_a",
        "state_b",
        "owner_a",
        "owner_b",
        "province_list",
    ]
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "segment_id": row.segment_id,
                    "level": row.level,
                    "province_count": len(row.provinces),
                    "endpoint_a": row.endpoint_a,
                    "endpoint_b": row.endpoint_b,
                    "vp_a": row.vp_a,
                    "vp_b": row.vp_b,
                    "regional_floor": row.regional_floor,
                    "endpoint_boost": row.endpoint_boost,
                    "boost_reason": row.boost_reason,
                    "state_a": row.state_a,
                    "state_b": row.state_b,
                    "owner_a": row.owner_a,
                    "owner_b": row.owner_b,
                    "province_list": " ".join(map(str, row.provinces)),
                }
            )


def print_summary(
    original_count: int,
    hub_count: int,
    rows: list[SegmentRow],
    issues: list[str],
) -> None:
    levels = Counter(row.level for row in rows)
    lengths = [len(row.provinces) for row in rows]
    print(f"Original railway entries: {original_count}")
    print(f"Hub provinces: {hub_count}")
    print(f"Segments after split: {len(rows)}")
    print(f"Avg segment length: {sum(lengths) / len(lengths):.1f} provinces")
    print(f"Max segment length: {max(lengths)} provinces")
    print("Level histogram:")
    for level in sorted(levels):
        print(f"  level {level}: {levels[level]}")
    if issues:
        print("\nValidation notes:")
        for issue in issues[:20]:
            print(f"  {issue}")
        if len(issues) > 20:
            print(f"  ... and {len(issues) - 20} more")

    keywords = ("Los Angeles", "Chicago", "Des Moines", "Toronto", "Montreal", "Mexico", "Wyoming", "Iowa", "Philadelphia", "New York")
    print("\nSpot checks:")
    for row in rows:
        if any(keyword in row.state_a or keyword in row.state_b for keyword in keywords):
            print(
                f"  seg {row.segment_id:4} lvl={row.level} len={len(row.provinces):2} "
                f"{row.state_a[:24]} ({row.vp_a}) -> {row.state_b[:24]} ({row.vp_b}) "
                f"[floor={row.regional_floor}, boost={row.endpoint_boost}/{row.boost_reason}]"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Split railways at hubs and assign levels.")
    parser.add_argument("--apply", action="store_true", help="Rewrite map/railways.txt")
    parser.add_argument("--input", default=str(RAILWAYS_TXT), help="Input railways file")
    parser.add_argument("--output", default=str(OUTPUT_CSV), help="Assignments CSV output path")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    entries = parse_railways(input_path)
    hubs = detect_hubs(entries)
    segments = split_all_entries(entries, hubs)

    color_to_province, max_province = parse_definition(DEFINITION_CSV)
    valid_provinces = set(range(1, max_province + 1))
    province_raster = rasterize_provinces(PROVINCES_BMP, color_to_province)
    centroids = compute_province_centroids(province_raster)
    calibration_pairs = load_calibration_pairs(centroids)
    rbf_lon, rbf_lat = fit_inverse_rbf(calibration_pairs)

    states = parse_states(STATE_DIR)
    state_names = load_state_names(POPULATION_CSV)
    state_owners = load_state_owners()
    state_population = load_state_population()
    vp_values = load_victory_points()
    owned_provinces = {province_id for state in states.values() for province_id in state["provinces"]}

    metadata = build_province_metadata(
        centroids,
        rbf_lon,
        rbf_lat,
        states,
        state_names,
        state_owners,
        state_population,
        vp_values,
    )
    rows = build_segment_rows(segments, metadata)
    issues = validate_segments(rows, valid_provinces, owned_provinces)
    errors = [issue for issue in issues if "unknown province" in issue or "stateless province" in issue or "invalid level" in issue or "duplicate consecutive" in issue or "fewer than 2" in issue]
    if errors:
        raise RuntimeError("Railway validation failed:\n" + "\n".join(errors))

    write_csv(Path(args.output), rows)
    print_summary(len(entries), len(hubs), rows, issues)
    print(f"\nWrote {args.output}")
    if args.apply:
        write_railways(RAILWAYS_TXT, rows)
        print(f"Applied {len(rows)} railway segments to {RAILWAYS_TXT}")
    else:
        print("Dry run only. Re-run with --apply to rewrite map/railways.txt.")


if __name__ == "__main__":
    main()
