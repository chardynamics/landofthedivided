#!/usr/bin/env python3
"""Assign province-level air_base buildings from an airport dot image."""
import argparse
import csv
import re
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

from assign_infrastructure import (
    DEFINITION_CSV,
    POPULATION_CSV,
    PROVINCES_BMP,
    ROOT,
    STATE_DIR,
    build_state_raster,
    load_state_names,
    parse_definition,
    parse_states,
    rasterize_provinces,
    state_geometry,
)

OUTPUT_CSV = ROOT / "airbase_assignments.csv"
OVERLAY_PNG = ROOT / "airbase_overlay.png"
DEFAULT_AIRPORT_IMAGE = Path(
    "/Users/charlesduong/.cursor/projects/"
    "Users-charlesduong-Documents-Paradox-Interactive-Hearts-of-Iron-IV-mod-landofthedivided/"
    "assets/airports-41745c48-7244-456b-b59e-d5297704a930.png"
)

CATEGORY_TO_AIR_BASE = {
    "megalopolis": 10,
    "metropolis": 9,
    "large_city": 8,
    "city": 6,
    "large_town": 5,
    "town": 4,
    "rural": 3,
    "pastoral": 2,
    "enclave": 1,
    "small_island": 2,
    "tiny_island": 1,
    "wasteland": 1,
}

SNAP_RADIUS = 15


def load_land_provinces(path):
    land = set()
    with open(path, encoding="utf-8-sig") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(";")
            if len(parts) < 5:
                continue
            try:
                province_id = int(parts[0])
            except ValueError:
                continue
            if parts[4] == "land":
                land.add(province_id)
    return land


def detect_airport_dots(airport_path):
    image = Image.open(airport_path).convert("RGB")
    arr = np.asarray(image).astype(np.int16)
    red = arr[..., 0]
    green = arr[..., 1]
    blue = arr[..., 2]
    mask = (red >= 140) & (red - green >= 30) & (red - blue >= 30)
    labeled, count = ndimage.label(mask)
    if count == 0:
        return image.size, []

    sizes = ndimage.sum(np.ones_like(labeled), labeled, range(1, count + 1))
    centroids = ndimage.center_of_mass(mask, labeled, range(1, count + 1))
    dots = []
    for idx, ((cy, cx), size) in enumerate(zip(centroids, sizes), start=1):
        dots.append(
            {
                "id": idx,
                "image_x": float(cx),
                "image_y": float(cy),
                "size": int(size),
            }
        )
    return image.size, dots


def image_to_map_coords(image_x, image_y, image_size, map_size):
    image_width, image_height = image_size
    map_width, map_height = map_size
    map_x = image_x * map_width / image_width
    map_y = image_y * map_height / image_height
    return map_x, map_y


def snap_to_land_province(province_raster, land_provinces, map_x, map_y):
    height, width = province_raster.shape
    ix = int(round(map_x))
    iy = int(round(map_y))
    if 0 <= ix < width and 0 <= iy < height:
        province_id = int(province_raster[iy, ix])
        if province_id in land_provinces:
            return province_id, ix, iy, False

    for radius in range(1, SNAP_RADIUS + 1):
        y0 = max(0, iy - radius)
        y1 = min(height, iy + radius + 1)
        x0 = max(0, ix - radius)
        x1 = min(width, ix + radius + 1)
        window = province_raster[y0:y1, x0:x1]
        ys, xs = np.where(np.isin(window, list(land_provinces)))
        if len(xs) == 0:
            continue
        distances = (xs + x0 - ix) ** 2 + (ys + y0 - iy) ** 2
        best = int(np.argmin(distances))
        snap_x = int(xs[best] + x0)
        snap_y = int(ys[best] + y0)
        province_id = int(province_raster[snap_y, snap_x])
        return province_id, snap_x, snap_y, True
    return None, ix, iy, False


def air_base_level(category):
    return CATEGORY_TO_AIR_BASE.get(category or "", 1)


def pick_state_assignments(dots, states, centroids):
    by_state = defaultdict(list)
    for dot in dots:
        if dot["status"] != "matched":
            continue
        by_state[dot["state_id"]].append(dot)

    assignments = []
    for state_id, state_dots in by_state.items():
        state = states[state_id]
        centroid = centroids.get(state_id)
        if centroid is None:
            continue

        def sort_key(dot):
            cx, cy = dot["map_x"], dot["map_y"]
            dist = (cx - centroid[0]) ** 2 + (cy - centroid[1]) ** 2
            return (-dot["size"], dist)

        chosen = sorted(state_dots, key=sort_key)[0]
        province_id = chosen["province_id"]
        if province_id not in state["provinces"]:
            continue
        assignments.append(
            {
                "id": state_id,
                "file": str(state["path"].relative_to(ROOT)),
                "province": province_id,
                "category": state["category"],
                "level": air_base_level(state["category"]),
                "dot_size": chosen["size"],
                "status": "assigned",
            }
        )
    return assignments


def map_dots_to_states(dots, image_size, province_raster, state_raster, land_provinces, states):
    map_height, map_width = province_raster.shape
    mapped = []
    for dot in dots:
        map_x, map_y = image_to_map_coords(
            dot["image_x"],
            dot["image_y"],
            image_size,
            (map_width, map_height),
        )
        province_id, snap_x, snap_y, snapped = snap_to_land_province(
            province_raster,
            land_provinces,
            map_x,
            map_y,
        )
        row = {
            **dot,
            "map_x": map_x,
            "map_y": map_y,
            "snap_x": snap_x,
            "snap_y": snap_y,
            "snapped": snapped,
            "province_id": province_id,
            "state_id": None,
            "status": "unmatched",
        }
        if province_id is not None:
            state_id = int(state_raster[snap_y, snap_x])
            if state_id in states:
                row["state_id"] = state_id
                row["status"] = "matched"
        mapped.append(row)
    return mapped


def write_assignments_csv(assignments, names, path):
    fieldnames = ["id", "file", "name", "province", "category", "level", "dot_size", "status"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in assignments:
            out = dict(row)
            out["name"] = names.get(row["id"], f"STATE_{row['id']}")
            writer.writerow(out)


def write_overlay(province_raster, dots, path):
    land = province_raster > 0
    overlay = np.full((*province_raster.shape, 3), 255, dtype=np.uint8)
    overlay[land] = (235, 235, 235)
    for dot in dots:
        x = int(round(dot["map_x"]))
        y = int(round(dot["map_y"]))
        if not (0 <= x < overlay.shape[1] and 0 <= y < overlay.shape[0]):
            continue
        color = (25, 140, 55) if dot["status"] == "matched" else (200, 40, 40)
        y0 = max(0, y - 2)
        y1 = min(overlay.shape[0], y + 3)
        x0 = max(0, x - 2)
        x1 = min(overlay.shape[1], x + 3)
        overlay[y0:y1, x0:x1] = color
    Image.fromarray(overlay, "RGB").save(path)


def strip_air_base_entries(text):
    """Remove any existing air_base entries (state-level or province-scoped)."""
    # Province-scoped block possibly appended to the infrastructure line.
    text = re.sub(
        r"(^\s*infrastructure\s*=\s*\d+)[ \t]+\d+\s*=\s*\{\s*air_base\s*=\s*\d+\s*\}[ \t]*$",
        r"\1",
        text,
        flags=re.MULTILINE,
    )
    # Province-scoped block on its own line.
    text = re.sub(
        r"^[ \t]*\d+\s*=\s*\{\s*air_base\s*=\s*\d+\s*\}[ \t]*\n",
        "",
        text,
        flags=re.MULTILINE,
    )
    # Existing state-level air_base line.
    text = re.sub(
        r"^[ \t]*air_base\s*=\s*\d+[ \t]*\n",
        "",
        text,
        flags=re.MULTILINE,
    )
    return text


def upsert_air_base(path, level):
    text = strip_air_base_entries(path.read_text(encoding="utf-8"))
    air_base_line = f"\n\t\t\tair_base = {level}"

    infra_match = re.search(r"(^\s*infrastructure\s*=\s*\d+\s*$)", text, flags=re.MULTILINE)
    if infra_match:
        insert_at = infra_match.end()
        new_text = text[:insert_at] + air_base_line + text[insert_at:]
    else:
        buildings_match = re.search(r"(^\s*buildings\s*=\s*\{)", text, flags=re.MULTILINE)
        if not buildings_match:
            raise ValueError(f"Missing buildings block in {path}")
        insert_at = buildings_match.end()
        new_text = text[:insert_at] + air_base_line + text[insert_at:]
    path.write_text(new_text, encoding="utf-8")


def print_summary(dots, assignments, names):
    dot_status = Counter(dot["status"] for dot in dots)
    level_counts = Counter(row["level"] for row in assignments)
    print(f"Detected airport dots: {len(dots)}")
    print("Dot status counts:")
    for status, count in sorted(dot_status.items()):
        print(f"  {status:>10}: {count}")
    print(f"\nAssigned air bases: {len(assignments)}")
    print("Level distribution:")
    for level in range(1, 11):
        print(f"  {level:>2}: {level_counts.get(level, 0)}")
    print("\nSpot checks:")
    targets = {
        "Los Angeles, CA": 10,
        "Los Angeles Metro, CA": 10,
    }
    for row in assignments:
        name = names.get(row["id"], "")
        if name in targets:
            print(
                f"  {row['id']:>4} {name:<30} province={row['province']} "
                f"level={row['level']} category={row['category']}"
            )
    sparse = [row for row in assignments if row["level"] <= 3][:3]
    for row in sparse:
        print(
            f"  {row['id']:>4} {names.get(row['id'], ''):<30} province={row['province']} "
            f"level={row['level']} category={row['category']}"
        )


def parse_args():
    parser = argparse.ArgumentParser(description="Assign air bases from airport dot image.")
    parser.add_argument(
        "--airport-image",
        default=str(DEFAULT_AIRPORT_IMAGE),
        help="Airport dot source image",
    )
    parser.add_argument("--apply", action="store_true", help="Write air_base blocks to state files")
    parser.add_argument("--output", default=str(OUTPUT_CSV), help="Assignments CSV output path")
    parser.add_argument("--overlay", default=str(OVERLAY_PNG), help="Overlay PNG output path")
    return parser.parse_args()


def main():
    args = parse_args()
    airport_path = Path(args.airport_image)
    if not airport_path.exists():
        raise FileNotFoundError(f"Airport image not found: {airport_path}")

    color_to_province, max_province = parse_definition(DEFINITION_CSV)
    land_provinces = load_land_provinces(DEFINITION_CSV)
    province_raster = rasterize_provinces(PROVINCES_BMP, color_to_province)
    states = parse_states(STATE_DIR)
    names = load_state_names(POPULATION_CSV)
    state_raster = build_state_raster(province_raster, states, max_province)
    _, centroids = state_geometry(state_raster)

    image_size, dots = detect_airport_dots(airport_path)
    mapped_dots = map_dots_to_states(
        dots,
        image_size,
        province_raster,
        state_raster,
        land_provinces,
        states,
    )
    assignments = pick_state_assignments(mapped_dots, states, centroids)

    write_assignments_csv(assignments, names, Path(args.output))
    write_overlay(province_raster, mapped_dots, Path(args.overlay))
    print_summary(mapped_dots, assignments, names)
    print(f"\nWrote {args.output}")
    print(f"Wrote {args.overlay}")

    if args.apply:
        for row in assignments:
            upsert_air_base(ROOT / row["file"], row["level"])
        print(f"Applied air_base assignments to {len(assignments)} state files.")
    else:
        print("Dry run only. Re-run with --apply to write state files.")


if __name__ == "__main__":
    main()
