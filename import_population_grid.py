#!/usr/bin/env python3
"""Download or build a North America population grid for state manpower estimation."""
from __future__ import annotations

import argparse
import json
import math
import zipfile
from collections import defaultdict
from pathlib import Path
from urllib.request import urlretrieve

import numpy as np

from assign_infrastructure import (
    DEFINITION_CSV,
    PROVINCES_BMP,
    ROOT,
    STATE_DIR,
    build_state_raster,
    parse_definition,
    parse_states,
    rasterize_provinces,
    state_geometry,
)
from city_database import merged_city_entries
from import_geonames_cities import MAP_BBOX

GRID_DIR = ROOT / "data" / "population_grid"
METADATA_PATH = GRID_DIR / "metadata.json"
CELLS_CSV = GRID_DIR / "population_cells.csv"
POP_NPY = GRID_DIR / "population_lonlat.npy"

GPW_ZIP_URL = (
    "https://sedac.ciesin.columbia.edu/downloads/data/gpw-v4/"
    "gpw-v4-population-count-rev11/gpw-v4-population-count-rev11_2020_2pt5_min_tif.zip"
)

# Aggregate fine GPW cells to this step (degrees) before export. 2.5 arc-minute preserves
# GPW's standard coarse product while keeping RBF assignment tractable.
AGGREGATE_STEP_DEG = 2.5 / 60.0
KM_PER_DEG_LAT = 111.32

# Coarse synthetic grid step (degrees). ~0.05 deg ~ 5 km at mid-latitudes.
SYNTH_CELL_STEP = 0.05
SYNTH_SIGMA_DEG = 0.15
RURAL_DENSITY_PER_LAND_PIXEL = 0.35  # people per map pixel before country scaling


def load_cities() -> list[tuple[str, str, float, float, int]]:
    return merged_city_entries()


def gaussian_weight(dlon: float, dlat: float, sigma: float) -> float:
    return math.exp(-0.5 * ((dlon / sigma) ** 2 + (dlat / sigma) ** 2))


def build_synthetic_cells(
    cities: list[tuple[str, str, float, float, int]],
    province_raster: np.ndarray,
    state_raster: np.ndarray,
) -> list[tuple[float, float, float]]:
    """Return sparse (lon, lat, population) cells from city splats + rural land fill."""
    min_lon, min_lat, max_lon, max_lat = MAP_BBOX
    cells: dict[tuple[int, int], float] = defaultdict(float)

    # Urban splats from city database.
    for _name, _abbr, lon, lat, pop in cities:
        if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
            continue
        if pop <= 0:
            continue
        ilon = int(round(lon / SYNTH_CELL_STEP))
        ilat = int(round(lat / SYNTH_CELL_STEP))
        for dlon_i in range(-4, 5):
            for dlat_i in range(-4, 5):
                w = gaussian_weight(dlon_i * SYNTH_CELL_STEP, dlat_i * SYNTH_CELL_STEP, SYNTH_SIGMA_DEG)
                if w < 0.01:
                    continue
                key = (ilon + dlon_i, ilat + dlat_i)
                cells[key] += pop * w

    # Rural baseline: distribute by land pixels per state onto province centroids.
    land_pixels, centroids = state_geometry(state_raster)
    height, width = province_raster.shape
    province_land = np.bincount(province_raster.ravel().astype(np.int64))
    province_to_state = np.zeros(int(province_raster.max()) + 1, dtype=np.int32)
    states = parse_states(STATE_DIR)
    for state in states.values():
        for pid in state["provinces"]:
            if pid < len(province_to_state):
                province_to_state[pid] = state["id"]

    for province_id in range(1, len(province_land)):
        pixels = int(province_land[province_id])
        if pixels <= 0:
            continue
        state_id = int(province_to_state[province_id])
        if state_id <= 0:
            continue
        cx, cy = centroids.get(state_id, (None, None))
        if cx is None:
            ys, xs = np.nonzero(province_raster == province_id)
            if len(xs) == 0:
                continue
            cx, cy = float(xs.mean()), float(ys.mean())
        # Map pixel to approximate lon/lat using MAP_BBOX linearization (refined by RBF in estimator).
        lon = min_lon + (cx / width) * (max_lon - min_lon)
        lat = max_lat - (cy / height) * (max_lat - min_lat)
        rural_pop = pixels * RURAL_DENSITY_PER_LAND_PIXEL
        key = (int(round(lon / SYNTH_CELL_STEP)), int(round(lat / SYNTH_CELL_STEP)))
        cells[key] += rural_pop

    sparse: list[tuple[float, float, float]] = []
    for (ilon, ilat), pop in cells.items():
        if pop <= 0:
            continue
        lon = ilon * SYNTH_CELL_STEP
        lat = ilat * SYNTH_CELL_STEP
        if min_lon <= lon <= max_lon and min_lat <= lat <= max_lat:
            sparse.append((lon, lat, float(pop)))
    return sparse


def write_cells_csv(path: Path, cells: list[tuple[float, float, float]]) -> None:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    if len(cells) > 500_000:
        print(f"Skipping CSV export for {len(cells)} cells (use {POP_NPY.name} instead)")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["lon", "lat", "population"])
        for lon, lat, pop in sorted(cells, key=lambda item: (-item[2], item[0], item[1])):
            writer.writerow([f"{lon:.6f}", f"{lat:.6f}", f"{pop:.6f}"])


def try_download_gpw(dest_zip: Path) -> Path | None:
    try:
        print(f"Downloading GPWv4 2.5 arc-minute grid from SEDAC...")
        dest_zip.parent.mkdir(parents=True, exist_ok=True)
        urlretrieve(GPW_ZIP_URL, dest_zip)
        return dest_zip
    except Exception as exc:  # noqa: BLE001
        print(f"GPW download failed: {exc}")
        return None


def detect_gpw_value_type(tif_path: Path) -> str:
    name = tif_path.name.lower()
    if "density" in name:
        return "density"
    if "population_count" in name or "popcount" in name or "pop_count" in name:
        return "count"
    return "count"


def detect_gpw_source_label(tif_path: Path, value_type: str) -> str:
    name = tif_path.name.lower()
    year = "2000" if "_2000" in name else "2020" if "_2020" in name else "unknown"
    if "30_sec" in name or "30sec" in name:
        res = "30sec"
    elif "2pt5_min" in name or "2.5" in name:
        res = "2pt5min"
    else:
        res = "unknown"
    return f"gpw_v4_{value_type}_{year}_{res}"


def cell_area_km2(lat_deg: np.ndarray, res_lon_deg: float, res_lat_deg: float) -> np.ndarray:
    km_lon = res_lon_deg * KM_PER_DEG_LAT * np.cos(np.radians(lat_deg))
    km_lat = res_lat_deg * KM_PER_DEG_LAT
    return km_lon * km_lat


def aggregate_population_bins(
    lons: np.ndarray,
    lats: np.ndarray,
    pops: np.ndarray,
    bbox: tuple[float, float, float, float],
    step_deg: float,
) -> list[tuple[float, float, float]]:
    min_lon, min_lat, _max_lon, _max_lat = bbox
    ilons = np.floor((lons - min_lon) / step_deg).astype(np.int64)
    ilats = np.floor((lats - min_lat) / step_deg).astype(np.int64)
    keys = ilons * 1_000_000 + ilats
    order = np.argsort(keys)
    keys = keys[order]
    pops = pops[order]

    unique_keys, start_idx = np.unique(keys, return_index=True)
    pop_sums = np.add.reduceat(pops, start_idx)
    cells: list[tuple[float, float, float]] = []
    for key, pop_sum in zip(unique_keys, pop_sums):
        if pop_sum <= 0:
            continue
        ilon = int(key // 1_000_000)
        ilat = int(key % 1_000_000)
        lon = min_lon + (ilon + 0.5) * step_deg
        lat = min_lat + (ilat + 0.5) * step_deg
        cells.append((lon, lat, float(pop_sum)))
    return cells


def extract_gpw_to_cells(
    tif_path: Path,
    bbox: tuple[float, float, float, float],
    aggregate_step_deg: float = AGGREGATE_STEP_DEG,
) -> tuple[list[tuple[float, float, float]], dict]:
    import rasterio
    from rasterio.windows import from_bounds

    min_lon, min_lat, max_lon, max_lat = bbox
    value_type = detect_gpw_value_type(tif_path)
    with rasterio.open(tif_path) as dataset:
        window = from_bounds(min_lon, min_lat, max_lon, max_lat, dataset.transform)
        data = dataset.read(1, window=window, masked=True)
        transform = dataset.window_transform(window)
        res_lon_deg, res_lat_deg = abs(dataset.res[0]), abs(dataset.res[1])
        rows, cols = np.nonzero((data > 0) & np.isfinite(data))
        if len(rows) == 0:
            return [], {"value_type": value_type, "aggregate_step_deg": aggregate_step_deg}
        values = data[rows, cols].astype(np.float64)
        lons, lats = rasterio.transform.xy(transform, rows + 0.5, cols + 0.5, offset="center")
        lons_arr = np.asarray(lons, dtype=np.float64)
        lats_arr = np.asarray(lats, dtype=np.float64)
        if value_type == "density":
            areas = cell_area_km2(lats_arr, res_lon_deg, res_lat_deg)
            pops = values * areas
        else:
            pops = values

    info = {
        "value_type": value_type,
        "aggregate_step_deg": aggregate_step_deg,
        "raw_cells": int(len(pops)),
    }
    if aggregate_step_deg > 0 and (res_lon_deg < aggregate_step_deg * 0.9 or len(pops) > 500_000):
        cells = aggregate_population_bins(lons_arr, lats_arr, pops, bbox, aggregate_step_deg)
        info["aggregated_cells"] = len(cells)
        return cells, info

    cells = [
        (float(lon), float(lat), float(pop))
        for lon, lat, pop in zip(lons_arr, lats_arr, pops)
        if pop > 0
    ]
    return cells, info


def import_from_gpw(download: bool, source_tif: Path | None) -> tuple[list[tuple[float, float, float]], str, dict]:
    tif_path = source_tif
    extra: dict = {}
    if tif_path is None and download:
        zip_path = GRID_DIR / "gpw_2020_2pt5min.zip"
        if not zip_path.exists():
            try_download_gpw(zip_path)
        if zip_path.exists():
            with zipfile.ZipFile(zip_path) as archive:
                members = [m for m in archive.namelist() if m.lower().endswith(".tif")]
                if not members:
                    raise ValueError("No .tif in GPW zip")
                extracted = GRID_DIR / Path(members[0]).name
                if not extracted.exists():
                    archive.extract(members[0], GRID_DIR)
                tif_path = extracted

    if tif_path and tif_path.exists():
        value_type = detect_gpw_value_type(tif_path)
        print(f"Extracting population cells from {tif_path} ({value_type}) ...")
        cells, extra = extract_gpw_to_cells(tif_path, MAP_BBOX)
        source = detect_gpw_source_label(tif_path, value_type)
        extra["raster_path"] = str(tif_path)
        return cells, source, extra
    return [], "unknown", extra


def write_metadata(source: str, cell_count: int, extra: dict | None = None) -> None:
    metadata = {
        "source": source,
        "bbox": MAP_BBOX,
        "cell_count": cell_count,
        "gpw_url": GPW_ZIP_URL,
        "notes": "Sparse lon/lat/population cells for estimate_state_populations_grid.py",
    }
    if extra:
        metadata.update(extra)
    METADATA_PATH.write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def run(download: bool, source_tif: Path | None, synthetic: bool) -> None:
    cells: list[tuple[float, float, float]] = []
    source = "unknown"
    extra: dict = {}

    if source_tif and not source_tif.is_absolute():
        source_tif = ROOT / source_tif

    if source_tif or download:
        cells, source, extra = import_from_gpw(download=download, source_tif=source_tif)
        if cells:
            if extra.get("value_type") == "density":
                print(
                    f"Converted GPW density (persons/km²) to population count; "
                    f"aggregated {extra.get('raw_cells', '?')} -> {len(cells)} cells"
                )
        elif download:
            raise SystemExit(
                "GPW download/extract failed. SEDAC may be unreachable from this network.\n"
                "Manual fix:\n"
                f"  1. Download: {GPW_ZIP_URL}\n"
                f"  2. Save/extract the .tif under {GRID_DIR}/\n"
                f"  3. Run: python3 import_population_grid.py --source {GRID_DIR}/<file>.tif\n"
                "Or use --synthetic for a dev-only fallback (cities + land-pixel rural baseline)."
            )

    if not cells and synthetic:
        print("Building synthetic population cells from cities + land-pixel rural baseline...")
        color_to_province, max_province = parse_definition(DEFINITION_CSV)
        province_raster = rasterize_provinces(PROVINCES_BMP, color_to_province)
        states = parse_states(STATE_DIR)
        province_to_state = np.zeros(max_province + 1, dtype=np.uint16)
        for state in states.values():
            for pid in state["provinces"]:
                if pid <= max_province:
                    province_to_state[pid] = state["id"]
        state_raster = build_state_raster(province_raster, states, max_province)
        cells = build_synthetic_cells(load_cities(), province_raster, state_raster)
        source = "synthetic_cities_land"

    if not cells:
        raise SystemExit(
            "No population cells produced. Use one of:\n"
            "  python3 import_population_grid.py --download\n"
            "  python3 import_population_grid.py --source path/to/gpw.tif\n"
            "  python3 import_population_grid.py --synthetic   # dev fallback only"
        )

    write_cells_csv(CELLS_CSV, cells)
    arr = np.array(cells, dtype=np.float64)
    np.save(POP_NPY, arr)
    write_metadata(source, len(cells), extra)
    print(f"Wrote {len(cells)} cells to {POP_NPY} (source={source})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Import/build NA population grid cells.")
    parser.add_argument("--download", action="store_true", help="Try downloading GPWv4 from SEDAC")
    parser.add_argument("--source", type=Path, help="Local GPW GeoTIFF path")
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="Dev fallback: build grid from cities + land pixels (NOT real GPW census data)",
    )
    args = parser.parse_args()
    if not args.download and not args.source and not args.synthetic:
        parser.error("Specify --download, --source, or --synthetic")
    run(download=args.download, source_tif=args.source, synthetic=args.synthetic)


if __name__ == "__main__":
    main()
