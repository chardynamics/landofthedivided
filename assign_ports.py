#!/usr/bin/env python3
"""Assign province-level naval_base (port) buildings from a port dot image."""
import argparse
import csv
import re
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

from assign_airbases import (
    detect_airport_dots,
    image_to_map_coords,
)
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

OUTPUT_CSV = ROOT / "port_assignments.csv"
OVERLAY_PNG = ROOT / "port_overlay.png"
DEFAULT_PORT_IMAGE = Path(
    "/Users/charlesduong/.cursor/projects/"
    "Users-charlesduong-Documents-Paradox-Interactive-Hearts-of-Iron-IV-mod-landofthedivided/"
    "assets/ports-0ed1dee8-d341-40ce-989d-aeddb41c5102.png"
)

# Naval bases are province-scoped and capped at 10 (see common/buildings/00_buildings.txt).
CATEGORY_TO_NAVAL_BASE = {
    "megalopolis": 10,
    "metropolis": 9,
    "large_city": 8,
    "city": 6,
    "large_town": 5,
    "town": 4,
    "rural": 3,
    "pastoral": 2,
    "enclave": 2,
    "small_island": 3,
    "tiny_island": 2,
    "wasteland": 1,
}

# Ports often sit just offshore in the source image, so search a little wider.
SNAP_RADIUS = 30


def load_coastal_provinces(path):
    coastal = set()
    with open(path, encoding="utf-8-sig") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(";")
            if len(parts) < 6:
                continue
            try:
                province_id = int(parts[0])
            except ValueError:
                continue
            if parts[4] == "land" and parts[5].strip().lower() == "true":
                coastal.add(province_id)
    return coastal


def snap_to_coastal_province(province_raster, coastal_provinces, map_x, map_y):
    height, width = province_raster.shape
    ix = int(round(map_x))
    iy = int(round(map_y))
    if 0 <= ix < width and 0 <= iy < height:
        province_id = int(province_raster[iy, ix])
        if province_id in coastal_provinces:
            return province_id, ix, iy, False

    coastal_list = list(coastal_provinces)
    for radius in range(1, SNAP_RADIUS + 1):
        y0 = max(0, iy - radius)
        y1 = min(height, iy + radius + 1)
        x0 = max(0, ix - radius)
        x1 = min(width, ix + radius + 1)
        window = province_raster[y0:y1, x0:x1]
        ys, xs = np.where(np.isin(window, coastal_list))
        if len(xs) == 0:
            continue
        distances = (xs + x0 - ix) ** 2 + (ys + y0 - iy) ** 2
        best = int(np.argmin(distances))
        snap_x = int(xs[best] + x0)
        snap_y = int(ys[best] + y0)
        province_id = int(province_raster[snap_y, snap_x])
        return province_id, snap_x, snap_y, True
    return None, ix, iy, False


def naval_base_level(category):
    return CATEGORY_TO_NAVAL_BASE.get(category or "", 1)


def map_dots_to_states(dots, image_size, province_raster, state_raster, coastal_provinces, states):
    map_height, map_width = province_raster.shape
    mapped = []
    for dot in dots:
        map_x, map_y = image_to_map_coords(
            dot["image_x"],
            dot["image_y"],
            image_size,
            (map_width, map_height),
        )
        province_id, snap_x, snap_y, snapped = snap_to_coastal_province(
            province_raster,
            coastal_provinces,
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
                "level": naval_base_level(state["category"]),
                "dot_size": chosen["size"],
                "status": "assigned",
            }
        )
    return assignments


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
        x = int(round(dot["snap_x"]))
        y = int(round(dot["snap_y"]))
        if not (0 <= x < overlay.shape[1] and 0 <= y < overlay.shape[0]):
            continue
        color = (25, 90, 200) if dot["status"] == "matched" else (200, 40, 40)
        y0 = max(0, y - 2)
        y1 = min(overlay.shape[0], y + 3)
        x0 = max(0, x - 2)
        x1 = min(overlay.shape[1], x + 3)
        overlay[y0:y1, x0:x1] = color
    Image.fromarray(overlay, "RGB").save(path)


def strip_naval_base_entries(text):
    """Remove existing naval_base province blocks (single-line or multi-line)."""
    # Multi-line province block: <id> = {\n naval_base = N\n }
    text = re.sub(
        r"^[ \t]*\d+\s*=\s*\{\s*\n[ \t]*naval_base\s*=\s*\d+\s*\n[ \t]*\}[ \t]*\n",
        "",
        text,
        flags=re.MULTILINE,
    )
    # Single-line province block: <id> = { naval_base = N }
    text = re.sub(
        r"^[ \t]*\d+\s*=\s*\{\s*naval_base\s*=\s*\d+\s*\}[ \t]*\n",
        "",
        text,
        flags=re.MULTILINE,
    )
    return text


def upsert_naval_base(path, province_id, level):
    text = strip_naval_base_entries(path.read_text(encoding="utf-8"))
    block = f"\n\t\t\t{province_id} = {{\n\t\t\t\tnaval_base = {level}\n\t\t\t}}"

    air_match = re.search(r"(^\s*air_base\s*=\s*\d+\s*$)", text, flags=re.MULTILINE)
    infra_match = re.search(r"(^\s*infrastructure\s*=\s*\d+\s*$)", text, flags=re.MULTILINE)
    anchor = air_match or infra_match
    if anchor:
        insert_at = anchor.end()
        new_text = text[:insert_at] + block + text[insert_at:]
    else:
        buildings_match = re.search(r"(^\s*buildings\s*=\s*\{)", text, flags=re.MULTILINE)
        if not buildings_match:
            raise ValueError(f"Missing buildings block in {path}")
        insert_at = buildings_match.end()
        new_text = text[:insert_at] + block + text[insert_at:]
    path.write_text(new_text, encoding="utf-8")


def print_summary(dots, assignments, names):
    dot_status = Counter(dot["status"] for dot in dots)
    level_counts = Counter(row["level"] for row in assignments)
    print(f"Detected port dots: {len(dots)}")
    print("Dot status counts:")
    for status, count in sorted(dot_status.items()):
        print(f"  {status:>10}: {count}")
    print(f"\nAssigned naval bases: {len(assignments)}")
    print("Level distribution:")
    for level in range(1, 11):
        print(f"  {level:>2}: {level_counts.get(level, 0)}")
    print("\nSample assignments:")
    for row in sorted(assignments, key=lambda r: -r["level"])[:8]:
        print(
            f"  {row['id']:>4} {names.get(row['id'], ''):<30} province={row['province']} "
            f"level={row['level']} category={row['category']}"
        )


def parse_args():
    parser = argparse.ArgumentParser(description="Assign naval bases from port dot image.")
    parser.add_argument(
        "--port-image",
        default=str(DEFAULT_PORT_IMAGE),
        help="Port dot source image",
    )
    parser.add_argument("--apply", action="store_true", help="Write naval_base blocks to state files")
    parser.add_argument("--output", default=str(OUTPUT_CSV), help="Assignments CSV output path")
    parser.add_argument("--overlay", default=str(OVERLAY_PNG), help="Overlay PNG output path")
    return parser.parse_args()


def main():
    args = parse_args()
    port_path = Path(args.port_image)
    if not port_path.exists():
        raise FileNotFoundError(f"Port image not found: {port_path}")

    color_to_province, max_province = parse_definition(DEFINITION_CSV)
    coastal_provinces = load_coastal_provinces(DEFINITION_CSV)
    province_raster = rasterize_provinces(PROVINCES_BMP, color_to_province)
    states = parse_states(STATE_DIR)
    names = load_state_names(POPULATION_CSV)
    state_raster = build_state_raster(province_raster, states, max_province)
    _, centroids = state_geometry(state_raster)

    image_size, dots = detect_airport_dots(port_path)
    mapped_dots = map_dots_to_states(
        dots,
        image_size,
        province_raster,
        state_raster,
        coastal_provinces,
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
            upsert_naval_base(ROOT / row["file"], row["province"], row["level"])
        print(f"Applied naval_base assignments to {len(assignments)} state files.")
    else:
        print("Dry run only. Re-run with --apply to write state files.")


if __name__ == "__main__":
    main()
