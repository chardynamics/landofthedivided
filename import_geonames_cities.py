#!/usr/bin/env python3
"""Download GeoNames country dumps and build north_america_cities.csv for VP placement."""
from __future__ import annotations

import argparse
import csv
import io
import math
import urllib.request
import zipfile
from pathlib import Path

from assign_infrastructure import ROOT
from city_database import ANCHOR_ENTRIES, city_label

DATA_DIR = ROOT / "data"
GEONAMES_DIR = DATA_DIR / "geonames"
ADMIN_MAP_CSV = DATA_DIR / "geonames_admin_map.csv"
OUTPUT_CSV = DATA_DIR / "north_america_cities.csv"

GEONAMES_BASE = "https://download.geonames.org/export/dump"

# Countries on the LOTD North America map (full NA scope).
COUNTRY_CODES = (
    "US", "CA", "MX", "GL",
    "CU", "HT", "DO", "JM", "BS",
    "GT", "BZ", "HN", "SV", "NI", "CR", "PA",
    "TT", "BB", "KY", "VG", "GD", "LC", "VC", "AG", "DM", "KN",
    "AW", "CW", "SX", "GP", "MQ", "TC", "BM",
)

POPULATED_FEATURE_CODES = frozenset(
    {"PPL", "PPLA", "PPLA2", "PPLA3", "PPLA4", "PPLC", "PPLG", "PPLL", "PPLS"}
)

# Mod map rough bounding box (lon/lat).
MAP_BBOX = (-170.0, 7.0, -50.0, 84.0)  # min_lon, min_lat, max_lon, max_lat

MIN_POPULATION = 5000
DEDUP_KM = 0.5


def load_admin_map(path: Path) -> dict[tuple[str, str], str]:
    mapping: dict[tuple[str, str], str] = {}
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            cc = row["country_code"].strip()
            admin1 = row.get("admin1", "").strip()
            abbr = row["abbr"].strip()
            mapping[(cc, admin1)] = abbr
    return mapping


def resolve_abbr(country: str, admin1: str, admin_map: dict[tuple[str, str], str]) -> str | None:
    if (country, admin1) in admin_map:
        return admin_map[(country, admin1)]
    if (country, "") in admin_map:
        return admin_map[(country, "")]
    return None


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(min(1.0, a**0.5))


def in_bbox(lon: float, lat: float) -> bool:
    min_lon, min_lat, max_lon, max_lat = MAP_BBOX
    return min_lon <= lon <= max_lon and min_lat <= lat <= max_lat


def download_country(country: str, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / f"{country}.zip"
    txt_path = dest_dir / f"{country}.txt"
    if txt_path.exists():
        return txt_path
    url = f"{GEONAMES_BASE}/{country}.zip"
    print(f"Downloading {url} ...")
    with urllib.request.urlopen(url, timeout=120) as response:
        zip_path.write_bytes(response.read())
    with zipfile.ZipFile(zip_path) as archive:
        member = f"{country}.txt"
        archive.extract(member, dest_dir)
    return txt_path


def parse_geonames_row(line: str) -> dict[str, str] | None:
    parts = line.rstrip("\n").split("\t")
    if len(parts) < 15:
        return None
    return {
        "geoname_id": parts[0],
        "name": parts[1],
        "asciiname": parts[2],
        "lat": parts[4],
        "lon": parts[5],
        "feature_class": parts[6],
        "feature_code": parts[7],
        "country": parts[8],
        "admin1": parts[10],
        "population": parts[14],
    }


def import_country(
    country: str,
    admin_map: dict[tuple[str, str], str],
    geonames_dir: Path,
    min_pop: int,
) -> list[dict[str, object]]:
    txt_path = download_country(country, geonames_dir)
    rows: list[dict[str, object]] = []
    with txt_path.open(encoding="utf-8") as handle:
        for line in handle:
            parsed = parse_geonames_row(line)
            if not parsed:
                continue
            if parsed["feature_class"] != "P":
                continue
            if parsed["feature_code"] not in POPULATED_FEATURE_CODES:
                continue
            try:
                population = int(parsed["population"] or 0)
                lon = float(parsed["lon"])
                lat = float(parsed["lat"])
            except ValueError:
                continue
            if population < min_pop:
                continue
            if not in_bbox(lon, lat):
                continue
            abbr = resolve_abbr(parsed["country"], parsed["admin1"], admin_map)
            if not abbr:
                continue
            name = parsed["asciiname"] or parsed["name"]
            if not name:
                continue
            rows.append(
                {
                    "name": name,
                    "abbr": abbr,
                    "lon": lon,
                    "lat": lat,
                    "population": population,
                    "label": city_label(name, abbr),
                    "source": "geonames",
                    "geoname_id": parsed["geoname_id"],
                }
            )
    print(f"  {country}: {len(rows)} places (pop >= {min_pop})")
    return rows


def dedupe_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Keep highest-population entry per near-duplicate coordinate cluster."""
    sorted_rows = sorted(rows, key=lambda r: (-int(r["population"]), str(r["name"])))
    kept: list[dict[str, object]] = []
    for row in sorted_rows:
        lon, lat = float(row["lon"]), float(row["lat"])
        if any(
            haversine_km(lon, lat, float(other["lon"]), float(other["lat"])) < DEDUP_KM
            and str(other["name"]).lower() == str(row["name"]).lower()
            for other in kept
        ):
            continue
        kept.append(row)
    return kept


def load_curated_overrides() -> list[dict[str, object]]:
    rows = []
    for name, abbr, lon, lat, pop in ANCHOR_ENTRIES:
        rows.append(
            {
                "name": name,
                "abbr": abbr,
                "lon": lon,
                "lat": lat,
                "population": pop,
                "label": city_label(name, abbr),
                "source": "curated",
                "geoname_id": "",
            }
        )
    return rows


def merge_with_overrides(
    imported: list[dict[str, object]],
    overrides: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Curated entries win on (name.lower, abbr) conflicts."""
    by_key: dict[tuple[str, str], dict[str, object]] = {}
    for row in imported:
        key = (str(row["name"]).lower(), str(row["abbr"]))
        if key not in by_key or int(row["population"]) > int(by_key[key]["population"]):
            by_key[key] = row
    for row in overrides:
        key = (str(row["name"]).lower(), str(row["abbr"]))
        by_key[key] = row
    return sorted(by_key.values(), key=lambda r: (-int(r["population"]), str(r["name"])))


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["name", "abbr", "lon", "lat", "population", "label", "source", "geoname_id"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row[field] for field in fieldnames})


def run(download: bool, min_pop: int) -> None:
    admin_map = load_admin_map(ADMIN_MAP_CSV)
    geonames_dir = GEONAMES_DIR
    if not download and not any((geonames_dir / f"{cc}.txt").exists() for cc in COUNTRY_CODES):
        print("No local GeoNames files found; pass --download to fetch them.")
        return

    imported: list[dict[str, object]] = []
    for country in COUNTRY_CODES:
        if download or (geonames_dir / f"{country}.txt").exists():
            imported.extend(import_country(country, admin_map, geonames_dir, min_pop))
        else:
            print(f"  {country}: skipped (no local file, use --download)")

    print(f"Imported raw rows: {len(imported)}")
    imported = dedupe_rows(imported)
    print(f"After dedupe: {len(imported)}")

    merged = merge_with_overrides(imported, load_curated_overrides())
    write_csv(OUTPUT_CSV, merged)
    print(f"Wrote {len(merged)} cities to {OUTPUT_CSV}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Import GeoNames cities for North America VP placement.")
    parser.add_argument("--download", action="store_true", help="Download country dumps from geonames.org")
    parser.add_argument("--min-pop", type=int, default=MIN_POPULATION, help="Minimum population filter")
    args = parser.parse_args()
    run(download=args.download, min_pop=args.min_pop)


if __name__ == "__main__":
    main()
