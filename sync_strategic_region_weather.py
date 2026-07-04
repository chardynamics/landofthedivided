#!/usr/bin/env python3
"""Copy TNO strategic-region weather periods onto LOTD regions via geo calibration."""
from __future__ import annotations

import argparse
import csv
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.interpolate import RBFInterpolator

from assign_infrastructure import ROOT, parse_definition, rasterize_provinces

DEFAULT_TNO_PATH = Path.home() / (
    "Library/Application Support/Steam/steamapps/workshop/content/394360/2438003901"
)
LOTD_SR_DIR = ROOT / "map" / "strategicregions"
LOTD_DEF = ROOT / "map" / "definition.csv"
LOTD_BMP = ROOT / "map" / "provinces.bmp"
ANCHOR_CSV = ROOT / "vp_calibration_anchors.csv"
OVERRIDES_CSV = ROOT / "weather_region_overrides.csv"
AUDIT_CSV = ROOT / "weather_sync_audit.csv"
LAKE_COAST_CSV = ROOT / "lake_coast_strategic_regions.csv"
HUDSON_BAY_OVERRIDE = "108-Strategic_Region_108"
HUDSON_BAY_TNO_SOURCE = "166-Hudson Bay"
COAST_MIN_ADJACENT_PROVINCES = 3

WATER_TYPES = frozenset({"sea", "lake"})
CONFIDENCE_HIGH = 0.9
CONFIDENCE_MEDIUM = 0.6
PIXEL_LOOKUP_RADIUS = 30
RBF_SMOOTHING = 0.5

# Seasonal arctic_water per weather period (Jan→Dec). TNO Great Lakes use 0 everywhere;
# these values approximate real partial/seasonal freeze coverage by lake.
LAKE_ARCTIC_WATER: dict[str, list[float]] = {
    "30-Lake Superior": [0.850, 0.900, 0.350, 0.000, 0.000, 0.000, 0.000, 0.000, 0.000, 0.000, 0.250, 0.750],
    "31-Lake Michigan": [0.600, 0.650, 0.150, 0.000, 0.000, 0.000, 0.000, 0.000, 0.000, 0.000, 0.100, 0.500],
    "32-Lake Erie": [0.950, 1.000, 0.450, 0.000, 0.000, 0.000, 0.000, 0.000, 0.000, 0.000, 0.350, 0.850],
    "33-Lake Ontario": [0.200, 0.250, 0.050, 0.000, 0.000, 0.000, 0.000, 0.000, 0.000, 0.000, 0.050, 0.150],
    "34-Lake Huron": [0.800, 0.850, 0.300, 0.000, 0.000, 0.000, 0.000, 0.000, 0.000, 0.000, 0.200, 0.700],
}

# Seasonal shore-ice boost on land SRs adjacent to Great Lakes water (Jan→Dec).
LAKE_COAST_PATCHES: dict[str, list[float]] = {
    "snow": [0.420, 0.400, 0.180, 0.000, 0.000, 0.000, 0.000, 0.000, 0.000, 0.000, 0.120, 0.350],
    "blizzard": [0.120, 0.100, 0.040, 0.000, 0.000, 0.000, 0.000, 0.000, 0.000, 0.000, 0.030, 0.080],
    "min_snow_level": [0.180, 0.150, 0.060, 0.000, 0.000, 0.000, 0.000, 0.000, 0.000, 0.000, 0.060, 0.120],
    "arctic_water": [0.300, 0.350, 0.100, 0.000, 0.000, 0.000, 0.000, 0.000, 0.000, 0.000, 0.080, 0.220],
}


@dataclass
class StrategicRegion:
    path: Path
    stem: str
    region_id: int
    name: str
    provinces: list[int]
    weather_text: str | None


@dataclass
class MappingResult:
    lotd_stem: str
    lotd_id: int
    lotd_file: str
    tno_source: str
    vote_pct: float
    vote_count: int
    total_provinces: int
    mapped_provinces: int
    confidence: str
    override: bool
    skipped: bool


def parse_definition_with_types(path: Path) -> tuple[dict[int, int], dict[int, str]]:
    color_to_province: dict[int, int] = {}
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
                province_id = int(parts[0])
                red, green, blue = int(parts[1]), int(parts[2]), int(parts[3])
            except ValueError:
                continue
            color_to_province[(red << 16) | (green << 8) | blue] = province_id
            province_types[province_id] = parts[4]
    return color_to_province, province_types


def extract_brace_block(text: str, start: int) -> tuple[str, int]:
    """Return substring from opening brace at `start` through its matching close."""
    if start >= len(text) or text[start] != "{":
        raise ValueError("extract_brace_block must start at '{'")
    depth = 0
    for idx in range(start, len(text)):
        char = text[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1], idx + 1
    raise ValueError("Unbalanced braces while parsing strategic region file")


def extract_weather_section(text: str) -> str | None:
    match = re.search(r"weather\s*=", text)
    if not match:
        return None
    brace_start = text.find("{", match.end())
    if brace_start < 0:
        return None
    block, _ = extract_brace_block(text, brace_start)
    return "weather = " + block


def compute_province_centroids(province_raster: np.ndarray) -> dict[int, tuple[float, float]]:
    flat = province_raster.ravel()
    max_id = int(flat.max())
    height, width = province_raster.shape
    xs = np.tile(np.arange(width), height)
    ys = np.repeat(np.arange(height), width)
    counts = np.bincount(flat, minlength=max_id + 1)
    sum_x = np.bincount(flat, weights=xs, minlength=max_id + 1)
    sum_y = np.bincount(flat, weights=ys, minlength=max_id + 1)
    centroids: dict[int, tuple[float, float]] = {}
    for province_id in range(1, max_id + 1):
        if counts[province_id] > 0:
            centroids[province_id] = (
                sum_x[province_id] / counts[province_id],
                sum_y[province_id] / counts[province_id],
            )
    return centroids


def load_anchors(path: Path) -> list[tuple[float, float, int]]:
    anchors: list[tuple[float, float, int]] = []
    with open(path, encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            anchors.append((float(row["lon"]), float(row["lat"]), int(row["province_id"])))
    return anchors


def equirectangular_pixel(lon: float, lat: float, width: int, height: int) -> tuple[float, float]:
    x = (lon + 180.0) / 360.0 * width
    y = (90.0 - lat) / 180.0 * height
    return x, y


def locate_province_any_type(
    px: float,
    py: float,
    province_raster: np.ndarray,
    *,
    radius: int = PIXEL_LOOKUP_RADIUS,
) -> int:
    height, width = province_raster.shape
    ix = int(round(px))
    iy = int(round(py))
    if 0 <= ix < width and 0 <= iy < height:
        province_id = int(province_raster[iy, ix])
        if province_id > 0:
            return province_id

    best_pid = 0
    best_dist = float("inf")
    for search_radius in range(1, radius + 1):
        y0 = max(iy - search_radius, 0)
        y1 = min(iy + search_radius + 1, height)
        x0 = max(ix - search_radius, 0)
        x1 = min(ix + search_radius + 1, width)
        window = province_raster[y0:y1, x0:x1]
        if not np.any(window > 0):
            continue
        ys, xs = np.nonzero(window > 0)
        for y_idx, x_idx in zip(ys, xs):
            province_id = int(window[y_idx, x_idx])
            dist = (x0 + x_idx - px) ** 2 + (y0 + y_idx - py) ** 2
            if dist < best_dist:
                best_dist = dist
                best_pid = province_id
        if best_pid:
            return best_pid
    return 0


def locate_land_province(
    px: float,
    py: float,
    province_raster: np.ndarray,
    province_types: dict[int, str],
    *,
    radius: int = PIXEL_LOOKUP_RADIUS,
) -> int:
    height, width = province_raster.shape
    ix = int(round(px))
    iy = int(round(py))
    if 0 <= ix < width and 0 <= iy < height:
        province_id = int(province_raster[iy, ix])
        if province_id > 0 and province_types.get(province_id) not in WATER_TYPES:
            return province_id

    best_pid = 0
    best_dist = float("inf")
    for search_radius in range(1, radius + 1):
        y0 = max(iy - search_radius, 0)
        y1 = min(iy + search_radius + 1, height)
        x0 = max(ix - search_radius, 0)
        x1 = min(ix + search_radius + 1, width)
        window = province_raster[y0:y1, x0:x1]
        if not np.any(window > 0):
            continue
        ys, xs = np.nonzero(window > 0)
        for y_idx, x_idx in zip(ys, xs):
            province_id = int(window[y_idx, x_idx])
            if province_types.get(province_id) in WATER_TYPES:
                continue
            dist = (x0 + x_idx - px) ** 2 + (y0 + y_idx - py) ** 2
            if dist < best_dist:
                best_dist = dist
                best_pid = province_id
        if best_pid:
            return best_pid
    return 0


def build_lotd_geo_lookup(
    anchors: list[tuple[float, float, int]],
    centroids: dict[int, tuple[float, float]],
) -> dict[int, tuple[float, float]]:
    pairs = [
        (lon, lat, centroids[province_id][0], centroids[province_id][1])
        for lon, lat, province_id in anchors
        if province_id in centroids
    ]
    if len(pairs) < 6:
        raise ValueError(f"Need at least 6 LOTD calibration anchors, got {len(pairs)}")

    lons = np.array([item[0] for item in pairs])
    lats = np.array([item[1] for item in pairs])
    xs = np.array([item[2] for item in pairs])
    ys = np.array([item[3] for item in pairs])
    inv_lon = RBFInterpolator(
        np.column_stack([xs, ys]), lons, kernel="thin_plate_spline", smoothing=RBF_SMOOTHING
    )
    inv_lat = RBFInterpolator(
        np.column_stack([xs, ys]), lats, kernel="thin_plate_spline", smoothing=RBF_SMOOTHING
    )

    geo: dict[int, tuple[float, float]] = {}
    for province_id, (x, y) in centroids.items():
        geo[province_id] = (float(inv_lon([[x, y]])[0]), float(inv_lat([[x, y]])[0]))
    return geo


def build_tno_forward_rbf(
    anchors: list[tuple[float, float, int]],
    province_raster: np.ndarray,
    province_types: dict[int, str],
    centroids: dict[int, tuple[float, float]],
) -> tuple[RBFInterpolator, RBFInterpolator]:
    height, width = province_raster.shape
    pairs: list[tuple[float, float, float, float]] = []
    for lon, lat, _province_id in anchors:
        seed_x, seed_y = equirectangular_pixel(lon, lat, width, height)
        tno_pid = locate_land_province(seed_x, seed_y, province_raster, province_types)
        if not tno_pid or tno_pid not in centroids:
            continue
        cx, cy = centroids[tno_pid]
        pairs.append((lon, lat, cx, cy))

    if len(pairs) < 6:
        raise ValueError(f"Need at least 6 TNO calibration anchors, got {len(pairs)}")

    lons = np.array([item[0] for item in pairs])
    lats = np.array([item[1] for item in pairs])
    xs = np.array([item[2] for item in pairs])
    ys = np.array([item[3] for item in pairs])
    fwd_x = RBFInterpolator(
        np.column_stack([lons, lats]), xs, kernel="thin_plate_spline", smoothing=RBF_SMOOTHING
    )
    fwd_y = RBFInterpolator(
        np.column_stack([lons, lats]), ys, kernel="thin_plate_spline", smoothing=RBF_SMOOTHING
    )
    return fwd_x, fwd_y


def parse_strategic_regions(folder: Path) -> dict[str, StrategicRegion]:
    regions: dict[str, StrategicRegion] = {}
    for path in sorted(folder.glob("*.txt")):
        if path.name.startswith("."):
            continue
        text = path.read_text(encoding="utf-8")
        id_match = re.search(r"^\s*id\s*=\s*(\d+)\s*$", text, flags=re.MULTILINE)
        name_match = re.search(r'^\s*name\s*=\s*"([^"]+)"\s*$', text, flags=re.MULTILINE)
        provinces_match = re.search(r"provinces\s*=\s*\{([^}]*)\}", text, flags=re.DOTALL)
        if not id_match or not provinces_match:
            continue
        regions[path.stem] = StrategicRegion(
            path=path,
            stem=path.stem,
            region_id=int(id_match.group(1)),
            name=name_match.group(1) if name_match else "",
            provinces=[int(value) for value in re.findall(r"\d+", provinces_match.group(1))],
            weather_text=extract_weather_section(text),
        )
    return regions


def extract_weather_blocks(folder: Path) -> dict[str, str]:
    blocks: dict[str, str] = {}
    for path in folder.glob("*.txt"):
        text = path.read_text(encoding="utf-8")
        weather = extract_weather_section(text)
        if weather:
            blocks[path.stem] = weather
    return blocks


def load_overrides(path: Path) -> dict[str, str | None]:
    if not path.exists():
        return {}
    overrides: dict[str, str | None] = {}
    with open(path, encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            lotd_key = row["lotd_stem"].strip()
            tno_source = row.get("tno_source", "").strip()
            if row.get("skip", "").strip().lower() in {"1", "true", "yes"}:
                overrides[lotd_key] = None
            elif tno_source:
                overrides[lotd_key] = tno_source
    return overrides


def confidence_label(vote_pct: float) -> str:
    if vote_pct >= CONFIDENCE_HIGH:
        return "high"
    if vote_pct >= CONFIDENCE_MEDIUM:
        return "medium"
    return "low"


def vote_tno_source(
    provinces: list[int],
    lotd_geo: dict[int, tuple[float, float]],
    tno_fwd_x: RBFInterpolator,
    tno_fwd_y: RBFInterpolator,
    tno_raster: np.ndarray,
    tno_types: dict[int, str],
    tno_province_to_sr: dict[int, str],
) -> tuple[str | None, Counter[str], int]:
    def collect_votes(land_only: bool) -> Counter[str]:
        votes: Counter[str] = Counter()
        for province_id in provinces:
            geo = lotd_geo.get(province_id)
            if not geo:
                continue
            lon, lat = geo
            px = float(tno_fwd_x([[lon, lat]])[0])
            py = float(tno_fwd_y([[lon, lat]])[0])
            if land_only:
                tno_pid = locate_land_province(px, py, tno_raster, tno_types)
            else:
                tno_pid = locate_province_any_type(px, py, tno_raster)
            if not tno_pid:
                continue
            tno_source = tno_province_to_sr.get(tno_pid)
            if tno_source:
                votes[tno_source] += 1
        return votes

    votes = collect_votes(land_only=True)
    if not votes:
        votes = collect_votes(land_only=False)
    mapped = sum(votes.values())
    if not votes:
        return None, votes, mapped
    return votes.most_common(1)[0][0], votes, mapped


def resolve_tno_stem(requested: str, available: dict[str, str]) -> str | None:
    if requested in available:
        return requested
    for stem in available:
        if stem.endswith(requested) or stem == requested:
            return stem
        if requested in stem:
            return stem
    return None


def format_weather_block(raw_weather: str) -> str:
    """Normalize TNO weather syntax to LOTD file style."""
    text = raw_weather.strip()
    if text.startswith("weather"):
        eq_idx = text.find("=")
        brace_idx = text.find("{", eq_idx)
        inner = text[brace_idx + 1 : -1].strip()
    else:
        inner = text.strip("{}").strip()

    period_chunks = re.split(r"(?<=\})\s*(?=period\s*=)", inner)
    lines = ["\tweather = {"]
    for chunk in period_chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        chunk = chunk.removeprefix("period").strip().lstrip("=").strip()
        if chunk.startswith("{"):
            chunk = chunk[1:]
        if chunk.endswith("}"):
            chunk = chunk[:-1]
        lines.append("\t\tperiod = {")
        for part in chunk.split("\n"):
            part = part.strip()
            if not part:
                continue
            lines.append("\t\t\t" + re.sub(r"(\w+)\s*=", r"\1 = ", part))
        lines.append("\t\t}")
    lines.append("\t}")
    return "\n".join(lines)


def apply_period_field_patches(path: Path, field_values: dict[str, list[float]]) -> None:
    """Set seasonal weather fields on each period in a strategic region file."""
    period_count = len(next(iter(field_values.values())))
    for field, values in field_values.items():
        if len(values) != period_count:
            raise ValueError(f"{path.name}: {field} has {len(values)} periods, expected {period_count}")

    text = path.read_text(encoding="utf-8")
    period_re = re.compile(r"(\t\tperiod\s*=\s*\{)(.*?)(\t\t\})", re.DOTALL)
    periods = list(period_re.finditer(text))
    if len(periods) != period_count:
        raise ValueError(f"{path.name}: expected {period_count} periods, found {len(periods)}")

    parts: list[str] = []
    last_end = 0
    for index, match in enumerate(periods):
        parts.append(text[last_end : match.start()])
        body = match.group(2)
        for field, values in field_values.items():
            replacement = f"{field} = {values[index]:.3f}"
            body, count = re.subn(rf"{field}\s*=\s*[\d.]+", replacement, body, count=1)
            if count != 1:
                raise ValueError(f"{path.name}: period {index} missing {field}")
        parts.append(match.group(1) + body + match.group(3))
        last_end = match.end()
    parts.append(text[last_end:])
    path.write_text("".join(parts), encoding="utf-8")


def apply_lake_arctic_water(path: Path, values: list[float]) -> None:
    apply_period_field_patches(path, {"arctic_water": values})


def discover_lake_coast_stems(
    lotd_regions: dict[str, StrategicRegion],
    province_types: dict[int, str],
    province_raster: np.ndarray,
) -> list[str]:
    """Return SR stems with land provinces adjacent to Great Lakes water tiles."""
    from scipy.ndimage import binary_dilation

    lake_pids: set[int] = set()
    for stem in LAKE_ARCTIC_WATER:
        if stem in lotd_regions:
            lake_pids.update(lotd_regions[stem].provinces)
    lake_water = {pid for pid in lake_pids if province_types.get(pid) == "sea"}
    if not lake_water:
        return []

    water_mask = np.isin(province_raster, list(lake_water))
    border = binary_dilation(water_mask, iterations=2) & ~water_mask

    pid_to_sr = {pid: stem for stem, region in lotd_regions.items() for pid in region.provinces}
    coast_by_sr: Counter[str] = Counter()
    for pid in np.unique(province_raster[border]):
        pid = int(pid)
        if pid <= 0 or province_types.get(pid) != "land":
            continue
        stem = pid_to_sr.get(pid)
        if stem and stem not in LAKE_ARCTIC_WATER:
            coast_by_sr[stem] += 1

    return sorted(
        stem for stem, count in coast_by_sr.items() if count >= COAST_MIN_ADJACENT_PROVINCES
    )


def write_lake_coast_csv(stems: list[str], coast_counts: dict[str, int] | None = None) -> None:
    with open(LAKE_COAST_CSV, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["lotd_stem", "adjacent_coast_provinces"])
        for stem in stems:
            writer.writerow([stem, coast_counts.get(stem, "") if coast_counts else ""])


def apply_hudson_bay_weather(
    lotd_regions: dict[str, StrategicRegion],
    tno_weather: dict[str, str],
) -> bool:
    if HUDSON_BAY_OVERRIDE not in lotd_regions:
        return False
    tno_stem = resolve_tno_stem(HUDSON_BAY_TNO_SOURCE, tno_weather)
    if not tno_stem:
        raise ValueError(f"TNO weather not found for {HUDSON_BAY_TNO_SOURCE}")
    replace_weather_in_file(lotd_regions[HUDSON_BAY_OVERRIDE].path, tno_weather[tno_stem])
    return True


def apply_winter_ice_patches(
    lotd_regions: dict[str, StrategicRegion],
    province_types: dict[int, str] | None = None,
    province_raster: np.ndarray | None = None,
    tno_weather: dict[str, str] | None = None,
) -> dict[str, int]:
    """Patch lake water, lake coast land, and Hudson Bay weather for seasonal ice."""
    counts = {"lake_water": 0, "lake_coast": 0, "hudson_bay": 0}

    for stem, values in LAKE_ARCTIC_WATER.items():
        if stem not in lotd_regions:
            continue
        apply_lake_arctic_water(lotd_regions[stem].path, values)
        counts["lake_water"] += 1

    if province_types is not None and province_raster is not None:
        coast_stems = discover_lake_coast_stems(lotd_regions, province_types, province_raster)
        write_lake_coast_csv(coast_stems)
        for stem in coast_stems:
            if stem not in lotd_regions:
                continue
            apply_period_field_patches(lotd_regions[stem].path, LAKE_COAST_PATCHES)
            counts["lake_coast"] += 1

    if tno_weather and apply_hudson_bay_weather(lotd_regions, tno_weather):
        counts["hudson_bay"] = 1

    return counts


def replace_weather_in_file(path: Path, weather_block: str) -> None:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"\n\tweather\s*=", text)
    if not match:
        raise ValueError(f"No weather block found in {path}")
    weather_start = match.start() + 1
    brace_start = text.find("{", match.end())
    if brace_start < 0:
        raise ValueError(f"Malformed weather block in {path}")
    _, weather_end = extract_brace_block(text, brace_start)
    # Include the `weather =` prefix through the closing brace.
    prefix = text[:weather_start]
    suffix = text[weather_end:]
    if not suffix.startswith("\n"):
        suffix = "\n" + suffix.lstrip()
    path.write_text(prefix + format_weather_block(weather_block) + suffix, encoding="utf-8")


def build_mappings(
    lotd_regions: dict[str, StrategicRegion],
    lotd_geo: dict[int, tuple[float, float]],
    tno_fwd_x: RBFInterpolator,
    tno_fwd_y: RBFInterpolator,
    tno_raster: np.ndarray,
    tno_types: dict[int, str],
    tno_province_to_sr: dict[int, str],
    overrides: dict[str, str | None],
) -> list[MappingResult]:
    results: list[MappingResult] = []
    for stem, region in sorted(lotd_regions.items()):
        if stem in overrides:
            override_value = overrides[stem]
            if override_value is None:
                results.append(
                    MappingResult(
                        lotd_stem=stem,
                        lotd_id=region.region_id,
                        lotd_file=region.path.name,
                        tno_source="",
                        vote_pct=0.0,
                        vote_count=0,
                        total_provinces=len(region.provinces),
                        mapped_provinces=0,
                        confidence="skipped",
                        override=True,
                        skipped=True,
                    )
                )
                continue
            results.append(
                MappingResult(
                    lotd_stem=stem,
                    lotd_id=region.region_id,
                    lotd_file=region.path.name,
                    tno_source=override_value,
                    vote_pct=1.0,
                    vote_count=len(region.provinces),
                    total_provinces=len(region.provinces),
                    mapped_provinces=len(region.provinces),
                    confidence="override",
                    override=True,
                    skipped=False,
                )
            )
            continue

        tno_source, votes, mapped = vote_tno_source(
            region.provinces,
            lotd_geo,
            tno_fwd_x,
            tno_fwd_y,
            tno_raster,
            tno_types,
            tno_province_to_sr,
        )
        vote_count = votes[tno_source] if tno_source else 0
        vote_pct = vote_count / mapped if mapped else 0.0
        results.append(
            MappingResult(
                lotd_stem=stem,
                lotd_id=region.region_id,
                lotd_file=region.path.name,
                tno_source=tno_source or "",
                vote_pct=vote_pct,
                vote_count=vote_count,
                total_provinces=len(region.provinces),
                mapped_provinces=mapped,
                confidence=confidence_label(vote_pct) if tno_source else "none",
                override=False,
                skipped=False,
            )
        )
    return results


def write_audit(path: Path, results: list[MappingResult], applied: bool) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "lotd_file",
                "lotd_stem",
                "lotd_id",
                "tno_source",
                "vote_pct",
                "vote_count",
                "mapped_provinces",
                "total_provinces",
                "confidence",
                "override",
                "skipped",
                "applied",
            ]
        )
        for result in results:
            writer.writerow(
                [
                    result.lotd_file,
                    result.lotd_stem,
                    result.lotd_id,
                    result.tno_source,
                    f"{result.vote_pct:.3f}",
                    result.vote_count,
                    result.mapped_provinces,
                    result.total_provinces,
                    result.confidence,
                    int(result.override),
                    int(result.skipped),
                    int(applied and not result.skipped and bool(result.tno_source)),
                ]
            )


def run(tno_path: Path, overrides_path: Path, apply: bool, winter_ice_only: bool = False) -> None:
    if winter_ice_only:
        lotd_regions = parse_strategic_regions(LOTD_SR_DIR)
        lotd_color_map, _ = parse_definition(LOTD_DEF)
        lotd_raster = rasterize_provinces(LOTD_BMP, lotd_color_map)
        _, province_types = parse_definition_with_types(LOTD_DEF)

        tno_weather: dict[str, str] | None = None
        if tno_path.exists():
            tno_weather = extract_weather_blocks(tno_path / "map" / "strategicregions")
        else:
            print(f"Warning: TNO path not found ({tno_path}); skipping Hudson Bay weather patch.")

        counts = apply_winter_ice_patches(
            lotd_regions,
            province_types=province_types,
            province_raster=lotd_raster,
            tno_weather=tno_weather,
        )
        print(
            f"Patched winter ice: {counts['lake_water']} lake SRs, "
            f"{counts['lake_coast']} coast SRs, Hudson Bay={bool(counts['hudson_bay'])}."
        )
        if counts["lake_coast"]:
            print(f"Coast SR list: {LAKE_COAST_CSV}")
        return

    if not tno_path.exists():
        raise SystemExit(f"TNO mod path not found: {tno_path}")

    anchors = load_anchors(ANCHOR_CSV)
    overrides = load_overrides(overrides_path)

    lotd_color_map, _ = parse_definition(LOTD_DEF)
    lotd_raster = rasterize_provinces(LOTD_BMP, lotd_color_map)
    lotd_centroids = compute_province_centroids(lotd_raster)
    lotd_geo = build_lotd_geo_lookup(anchors, lotd_centroids)

    tno_color_map, tno_types = parse_definition_with_types(tno_path / "map" / "definition.csv")
    tno_raster = rasterize_provinces(tno_path / "map" / "provinces.bmp", tno_color_map)
    tno_centroids = compute_province_centroids(tno_raster)
    tno_fwd_x, tno_fwd_y = build_tno_forward_rbf(anchors, tno_raster, tno_types, tno_centroids)

    lotd_regions = parse_strategic_regions(LOTD_SR_DIR)
    tno_regions = parse_strategic_regions(tno_path / "map" / "strategicregions")
    tno_weather = extract_weather_blocks(tno_path / "map" / "strategicregions")
    tno_province_to_sr = {
        province_id: stem for stem, region in tno_regions.items() for province_id in region.provinces
    }

    results = build_mappings(
        lotd_regions,
        lotd_geo,
        tno_fwd_x,
        tno_fwd_y,
        tno_raster,
        tno_types,
        tno_province_to_sr,
        overrides,
    )

    applied_count = 0
    missing_weather: list[str] = []
    if apply:
        for result in results:
            if result.skipped or not result.tno_source:
                continue
            lotd_region = lotd_regions[result.lotd_stem]
            tno_stem = resolve_tno_stem(result.tno_source, tno_weather)
            if not tno_stem:
                missing_weather.append(result.lotd_stem)
                continue
            replace_weather_in_file(lotd_region.path, tno_weather[tno_stem])
            applied_count += 1

        _, lotd_types = parse_definition_with_types(LOTD_DEF)
        ice_counts = apply_winter_ice_patches(
            lotd_regions,
            province_types=lotd_types,
            province_raster=lotd_raster,
            tno_weather=tno_weather,
        )
        print(
            f"Winter ice patches: {ice_counts['lake_water']} lakes, "
            f"{ice_counts['lake_coast']} coasts, Hudson Bay={bool(ice_counts['hudson_bay'])}."
        )

    write_audit(AUDIT_CSV, results, applied=apply)

    high = sum(1 for item in results if item.confidence == "high")
    medium = sum(1 for item in results if item.confidence == "medium")
    low = sum(1 for item in results if item.confidence == "low")
    override = sum(1 for item in results if item.confidence == "override")
    skipped = sum(1 for item in results if item.skipped)

    print(f"LOTD strategic regions: {len(lotd_regions)}")
    print(f"TNO weather sources available: {len(tno_weather)}")
    print(f"Mapping confidence: high={high}, medium={medium}, low={low}, override={override}, skipped={skipped}")
    print(f"Wrote {AUDIT_CSV}")
    if apply:
        print(f"Applied weather to {applied_count} strategic region files.")
        if missing_weather:
            print(f"Missing TNO weather for {len(missing_weather)} regions: {', '.join(missing_weather[:10])}")
    else:
        print("\nDry run only. Re-run with --apply to patch strategic region files.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync TNO strategic-region weather onto LOTD.")
    parser.add_argument(
        "--tno-path",
        type=Path,
        default=DEFAULT_TNO_PATH,
        help="Path to The New Order mod directory (default: Steam workshop 2438003901).",
    )
    parser.add_argument(
        "--overrides",
        type=Path,
        default=OVERRIDES_CSV,
        help="CSV file with lotd_stem,tno_source overrides.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Replace weather blocks in LOTD strategic region files.",
    )
    parser.add_argument(
        "--winter-ice-only",
        action="store_true",
        help="Patch lake arctic_water, Great Lakes coast snow/ice, and Hudson Bay weather.",
    )
    parser.add_argument(
        "--lake-ice-only",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    winter_ice_only = args.winter_ice_only or args.lake_ice_only
    run(args.tno_path, args.overrides, apply=args.apply, winter_ice_only=winter_ice_only)


if __name__ == "__main__":
    main()
