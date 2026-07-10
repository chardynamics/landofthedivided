#!/usr/bin/env python3
"""Assign infrastructure levels from a road-density image."""
import argparse
import csv
import math
import re
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent
DEFINITION_CSV = ROOT / "map" / "definition.csv"
PROVINCES_BMP = ROOT / "map" / "provinces.bmp"
STATE_DIR = ROOT / "history" / "states"
POPULATION_CSV = ROOT / "state_population_estimates.csv"
OUTPUT_CSV = ROOT / "infrastructure_ratings.csv"
OVERLAY_PNG = ROOT / "infrastructure_overlay.png"

# Simple affine calibration hook. The default assumes the road image has the
# same map extent as provinces.bmp and should be resized directly.
ROAD_SCALE_X = 1.0
ROAD_SCALE_Y = 1.0
ROAD_OFFSET_X = 0
ROAD_OFFSET_Y = 0


def encode_rgb(rgb_array):
    rgb = rgb_array.astype(np.uint32)
    return (rgb[..., 0] << 16) | (rgb[..., 1] << 8) | rgb[..., 2]


def parse_definition(path):
    color_to_province = {}
    max_province = 0
    with open(path, encoding="utf-8-sig") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(";")
            if len(parts) < 4:
                continue
            try:
                province_id = int(parts[0])
                red, green, blue = (int(parts[1]), int(parts[2]), int(parts[3]))
            except ValueError:
                continue
            color_to_province[(red << 16) | (green << 8) | blue] = province_id
            max_province = max(max_province, province_id)
    return color_to_province, max_province


def rasterize_provinces(provinces_path, color_to_province):
    image = Image.open(provinces_path).convert("RGB")
    rgb_codes = encode_rgb(np.asarray(image))
    colors = np.array(sorted(color_to_province), dtype=np.uint32)
    province_ids = np.array([color_to_province[int(color)] for color in colors], dtype=np.uint16)
    indices = np.searchsorted(colors, rgb_codes)
    valid = indices < len(colors)
    valid &= colors[np.minimum(indices, len(colors) - 1)] == rgb_codes
    province_raster = np.zeros(rgb_codes.shape, dtype=np.uint16)
    province_raster[valid] = province_ids[indices[valid]]
    return province_raster


def parse_state_file(path):
    text = path.read_text(encoding="utf-8")
    id_match = re.search(r"^\s*id\s*=\s*(\d+)\s*$", text, flags=re.MULTILINE)
    provinces_match = re.search(r"provinces\s*=\s*\{([^}]*)\}", text, flags=re.MULTILINE | re.DOTALL)
    manpower_match = re.search(r"^\s*manpower\s*=\s*(\d+)\s*$", text, flags=re.MULTILINE)
    category_match = re.search(r"^\s*state_category\s*=\s*(\w+)\s*$", text, flags=re.MULTILINE)
    if not id_match:
        raise ValueError(f"Missing id in {path}")
    if not provinces_match:
        raise ValueError(f"Missing provinces block in {path}")
    state_id = int(id_match.group(1))
    provinces = [int(value) for value in re.findall(r"\d+", provinces_match.group(1))]
    manpower = int(manpower_match.group(1)) if manpower_match else 0
    category = category_match.group(1) if category_match else ""
    return {
        "id": state_id,
        "path": path,
        "provinces": provinces,
        "manpower": manpower,
        "category": category,
    }


def parse_states(state_dir):
    states = {}
    for path in sorted(state_dir.glob("*.txt"), key=lambda p: int(p.name.split("-", 1)[0])):
        state = parse_state_file(path)
        states[state["id"]] = state
    return states


def load_state_names(path):
    names = {}
    if not path.exists():
        return names
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                names[int(row["id"])] = row.get("name", "")
            except (KeyError, ValueError):
                continue
    return names


def build_state_raster(province_raster, states, max_province):
    province_to_state = np.zeros(max_province + 1, dtype=np.uint16)
    for state in states.values():
        for province_id in state["provinces"]:
            if province_id <= max_province:
                province_to_state[province_id] = state["id"]
    return province_to_state[province_raster]


def state_geometry(state_raster):
    flat = state_raster.ravel()
    land_pixels = np.bincount(flat, minlength=int(flat.max()) + 1).astype(np.int64)
    height, width = state_raster.shape
    yy, xx = np.indices((height, width))
    sum_x = np.bincount(flat, weights=xx.ravel(), minlength=len(land_pixels))
    sum_y = np.bincount(flat, weights=yy.ravel(), minlength=len(land_pixels))
    centroids = {}
    for state_id, pixels in enumerate(land_pixels):
        if state_id == 0 or pixels == 0:
            continue
        centroids[state_id] = (float(sum_x[state_id] / pixels), float(sum_y[state_id] / pixels))
    return land_pixels, centroids


def affine_resize_road_image(road_path, target_size):
    source = Image.open(road_path).convert("RGB")
    target_width, target_height = target_size
    scaled_width = max(1, int(round(target_width * ROAD_SCALE_X)))
    scaled_height = max(1, int(round(target_height * ROAD_SCALE_Y)))
    resized = source.resize((scaled_width, scaled_height), Image.Resampling.BILINEAR)
    canvas = Image.new("RGB", (target_width, target_height), (255, 255, 255))
    canvas.paste(resized, (ROAD_OFFSET_X, ROAD_OFFSET_Y))
    return canvas


def road_masks(road_image):
    arr = np.asarray(road_image).astype(np.int16)
    red = arr[..., 0]
    green = arr[..., 1]
    blue = arr[..., 2]
    non_white = (red < 245) | (green < 245) | (blue < 245)
    green_roads = (green >= red + 2) & (green >= blue + 2) & (green < 245) & (red < 235) & (blue < 235)
    gray_roads = non_white & (np.maximum.reduce([red, green, blue]) - np.minimum.reduce([red, green, blue]) <= 14)
    road_mask = green_roads | gray_roads
    return road_mask, non_white


def content_bbox(content_mask):
    ys, xs = np.where(content_mask)
    if len(xs) == 0 or len(ys) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def in_bbox(point, bbox):
    if bbox is None:
        return False
    x, y = point
    min_x, min_y, max_x, max_y = bbox
    return min_x <= x <= max_x and min_y <= y <= max_y


def is_wasteland(state_id, state, name):
    if state["manpower"] == 0:
        return True
    if state["category"] == "wasteland":
        return True
    return (name or "").strip().lower() == "world border"


def normalize_ratings(rows):
    eligible = [row for row in rows if row["status"] == "measured" and row["density"] > 0]
    if not eligible:
        return
    logs = [math.log1p(row["density"]) for row in eligible]
    min_log = min(logs)
    max_log = max(logs)
    if math.isclose(min_log, max_log):
        for row in eligible:
            row["rating"] = 5
        return
    for row in eligible:
        value = math.log1p(row["density"])
        rating = round(10 * (value - min_log) / (max_log - min_log))
        row["rating"] = max(0, min(10, int(rating)))


def compute_ratings(states, names, state_raster, land_pixels, centroids, road_mask, road_content_bbox):
    flat_states = state_raster.ravel()
    flat_roads = road_mask.ravel().astype(np.int64)
    road_pixels = np.bincount(flat_states, weights=flat_roads, minlength=len(land_pixels))
    rows = []
    for state_id in sorted(states):
        state = states[state_id]
        name = names.get(state_id, f"STATE_{state_id}")
        pixels = int(land_pixels[state_id]) if state_id < len(land_pixels) else 0
        roads = float(road_pixels[state_id]) if state_id < len(road_pixels) else 0.0
        density = roads / pixels if pixels else 0.0
        row = {
            "id": state_id,
            "file": str(state["path"].relative_to(ROOT)),
            "name": name,
            "land_pixels": pixels,
            "road_pixels": int(round(roads)),
            "density": density,
            "rating": None,
            "status": "measured",
        }
        if is_wasteland(state_id, state, name):
            row["rating"] = 0
            row["status"] = "wasteland"
        elif state_id not in centroids or not in_bbox(centroids[state_id], road_content_bbox):
            row["rating"] = 1
            row["status"] = "off_image"
        rows.append(row)
    normalize_ratings(rows)
    for row in rows:
        if row["rating"] is None:
            row["rating"] = 0
    return rows


def write_ratings_csv(rows, path):
    fieldnames = [
        "id",
        "file",
        "name",
        "land_pixels",
        "road_pixels",
        "density",
        "rating",
        "status",
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            out = dict(row)
            out["density"] = f"{row['density']:.10f}"
            writer.writerow(out)


def write_overlay(state_raster, road_mask, road_content, path):
    land = state_raster > 0
    overlay = np.full((*state_raster.shape, 3), 255, dtype=np.uint8)
    overlay[land] = (235, 235, 235)
    overlay[road_content & ~road_mask] = (210, 210, 210)
    overlay[road_mask] = (25, 140, 55)
    Image.fromarray(overlay, "RGB").save(path)


def upsert_infrastructure(path, rating):
    text = path.read_text(encoding="utf-8")
    if re.search(r"^\s*infrastructure\s*=\s*\d+\s*$", text, flags=re.MULTILINE):
        new_text = re.sub(
            r"^(\s*infrastructure\s*=\s*)\d+\s*$",
            rf"\g<1>{rating}",
            text,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        match = re.search(r"(^\s*buildings\s*=\s*\{)", text, flags=re.MULTILINE)
        if match:
            indent_match = re.match(r"^(\s*)", match.group(1))
            indent = indent_match.group(1) if indent_match else ""
            building_indent = indent + "\t"
            new_text = text[: match.end()] + f"\n{building_indent}infrastructure = {rating}" + text[match.end() :]
        else:
            history_match = re.search(r"(^\s*history\s*=\s*\{)", text, flags=re.MULTILINE)
            if history_match:
                indent_match = re.match(r"^(\s*)", history_match.group(1))
                indent = indent_match.group(1) if indent_match else "\t"
                block = (
                    f"\n{indent}\tbuildings = {{"
                    f"\n{indent}\t\tinfrastructure = {rating}"
                    f"\n{indent}\t}}"
                )
                new_text = text[: history_match.end()] + block + text[history_match.end() :]
            else:
                name_match = re.search(r"(^\s*name\s*=\s*\"[^\"]+\"\s*$)", text, flags=re.MULTILINE)
                if not name_match:
                    raise ValueError(f"Missing name line for history insertion in {path}")
                block = (
                    "\n\n\thistory={"
                    "\n\t\tbuildings = {"
                    f"\n\t\t\tinfrastructure = {rating}"
                    "\n\t\t}"
                    "\n\t}"
                )
                new_text = text[: name_match.end()] + block + text[name_match.end() :]
    path.write_text(new_text, encoding="utf-8")


def print_summary(rows, road_bbox):
    counts = Counter(row["rating"] for row in rows)
    statuses = Counter(row["status"] for row in rows)
    print(f"Processed {len(rows)} states")
    print(f"Road-content bounding box: {road_bbox}")
    print("\nInfrastructure rating distribution:")
    for rating in range(11):
        print(f"  {rating:>2}: {counts.get(rating, 0)}")
    print("\nStatus counts:")
    for status, count in sorted(statuses.items()):
        print(f"  {status:>10}: {count}")
    print("\nSpot checks:")
    samples = [4, 25, 775, 26]
    seen = set()
    for state_id in samples:
        for row in rows:
            if row["id"] == state_id:
                print(
                    f"  {row['id']:>4} {row['name']:<35} "
                    f"density={row['density']:.8f} rating={row['rating']} status={row['status']}"
                )
                seen.add(state_id)
    for status in ("off_image", "wasteland"):
        for row in rows:
            if row["status"] == status and row["id"] not in seen:
                print(
                    f"  {row['id']:>4} {row['name']:<35} "
                    f"density={row['density']:.8f} rating={row['rating']} status={row['status']}"
                )
                seen.add(row["id"])
                break


def parse_args():
    parser = argparse.ArgumentParser(description="Assign infrastructure from road density.")
    parser.add_argument("--road-image", required=True, help="Road-density source image")
    parser.add_argument("--apply", action="store_true", help="Write infrastructure to state files")
    parser.add_argument("--output", default=str(OUTPUT_CSV), help="Ratings CSV output path")
    parser.add_argument("--overlay", default=str(OVERLAY_PNG), help="Overlay PNG output path")
    return parser.parse_args()


def main():
    args = parse_args()
    color_to_province, max_province = parse_definition(DEFINITION_CSV)
    province_raster = rasterize_provinces(PROVINCES_BMP, color_to_province)
    states = parse_states(STATE_DIR)
    names = load_state_names(POPULATION_CSV)
    state_raster = build_state_raster(province_raster, states, max_province)
    land_pixels, centroids = state_geometry(state_raster)

    height, width = state_raster.shape
    road_image = affine_resize_road_image(Path(args.road_image), (width, height))
    road_mask, road_content = road_masks(road_image)
    road_bbox = content_bbox(road_content)

    rows = compute_ratings(states, names, state_raster, land_pixels, centroids, road_mask, road_bbox)
    write_ratings_csv(rows, Path(args.output))
    write_overlay(state_raster, road_mask, road_content, Path(args.overlay))
    print_summary(rows, road_bbox)
    print(f"\nWrote {args.output}")
    print(f"Wrote {args.overlay}")

    if args.apply:
        for row in rows:
            upsert_infrastructure(ROOT / row["file"], row["rating"])
        print(f"Applied infrastructure values to {len(rows)} state files.")
    else:
        print("Dry run only. Re-run with --apply to write state files.")


if __name__ == "__main__":
    main()
