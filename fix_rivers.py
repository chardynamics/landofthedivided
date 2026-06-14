#!/usr/bin/env python3
"""Convert map/rivers.bmp into a HOI4-compatible river map."""
import argparse
import shutil
from collections import Counter, deque
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

ROOT = Path(__file__).resolve().parent
RIVERS_BMP = ROOT / "map" / "rivers.bmp"
RIVERS_BACKUP = ROOT / "map" / "rivers_backup.bmp"
PROVINCES_BMP = ROOT / "map" / "provinces.bmp"
HEIGHTMAP_BMP = ROOT / "map" / "heightmap.bmp"
PREVIEW_PNG = ROOT / "map" / "rivers_preview.png"

RIVER_INDICES = set(range(12))
BACKGROUND_INDICES = {254, 255}
TAPER_RADIUS = 10
MIN_SPUR_LENGTH = 3

NEIGHBOR_OFFSETS = [
    (-1, 0),
    (1, 0),
    (0, -1),
    (0, 1),
]


def load_indexed(path):
    image = Image.open(path)
    palette = image.getpalette()
    arr = np.asarray(image)
    return image, arr, palette


def load_heightmap(path):
    image = Image.open(path).convert("L")
    return np.asarray(image, dtype=np.uint16)


def river_mask_from_indices(arr):
    return arr <= 11


def geodesic_diameter_path(sub_mask):
    """Return a 1px centerline through a river blob via mask geodesic diameter."""
    ys, xs = np.where(sub_mask)
    if len(ys) == 0:
        return np.zeros_like(sub_mask, dtype=bool)
    if len(ys) == 1:
        path = np.zeros_like(sub_mask, dtype=bool)
        path[ys[0], xs[0]] = True
        return path

    index = {(int(y), int(x)): i for i, (y, x) in enumerate(zip(ys, xs))}
    neighbor_count = len(ys)
    adj = [[] for _ in range(neighbor_count)]
    for i, (y, x) in enumerate(zip(ys, xs)):
        for dy, dx in NEIGHBOR_OFFSETS:
            j = index.get((int(y + dy), int(x + dx)))
            if j is not None:
                adj[i].append(j)

    def bfs(start):
        dist = [-1] * neighbor_count
        parent = [-1] * neighbor_count
        queue = deque([start])
        dist[start] = 0
        while queue:
            node = queue.popleft()
            for nbr in adj[node]:
                if dist[nbr] < 0:
                    dist[nbr] = dist[node] + 1
                    parent[nbr] = node
                    queue.append(nbr)
        farthest = max(range(neighbor_count), key=lambda i: dist[i])
        return farthest, parent

    start, _ = bfs(0)
    end, parent = bfs(start)
    path = np.zeros_like(sub_mask, dtype=bool)
    node = end
    while node >= 0:
        path[ys[node], xs[node]] = True
        node = parent[node]
    return path


def skeletonize_rivers(mask):
    """Extract one 1px centerline per painted river blob."""
    labels, _count = ndimage.label(mask, structure=np.ones((3, 3), dtype=np.uint8))
    skeleton = np.zeros(mask.shape, dtype=bool)

    for label_id, slc in enumerate(ndimage.find_objects(labels), start=1):
        if slc is None:
            continue
        sub_mask = labels[slc] == label_id
        skeleton[slc] |= geodesic_diameter_path(sub_mask)
    return skeleton


def thickness_map(mask):
    dt = ndimage.distance_transform_edt(mask)
    return np.rint(2 * dt + 1).astype(np.uint8)


def neighbor_count(mask, y, x):
    height, width = mask.shape
    count = 0
    for dy, dx in NEIGHBOR_OFFSETS:
        ny, nx = y + dy, x + dx
        if 0 <= ny < height and 0 <= nx < width and mask[ny, nx]:
            count += 1
    return count


def skeleton_neighbors(mask, y, x):
    height, width = mask.shape
    coords = []
    for dy, dx in NEIGHBOR_OFFSETS:
        ny, nx = y + dy, x + dx
        if 0 <= ny < height and 0 <= nx < width and mask[ny, nx]:
            coords.append((ny, nx))
    return coords


def orthogonalize_skeleton(skeleton):
    skel = skeleton.copy()
    for _ in range(8):
        before = count_diagonal_issues(skel)
        skel = fix_diagonal_connections(skel)
        skel = remove_thick_blocks(skel)
        after = count_diagonal_issues(skel)
        if after == 0 or after == before:
            break
    return skel


def fix_diagonal_connections(skeleton):
    skel = skeleton.copy()
    a = skel[:-1, :-1]
    b = skel[:-1, 1:]
    c = skel[1:, :-1]
    d = skel[1:, 1:]
    # Diagonal top-left/bottom-right pairs need an orthogonal bridge pixel.
    skel[:-1, 1:] |= a & d & ~b & ~c
    skel[:-1, :-1] |= b & c & ~a & ~d
    return skel


def remove_thick_blocks(skeleton):
    skel = skeleton.copy()
    kernel = np.ones((2, 2), dtype=np.uint8)
    while True:
        block_sum = ndimage.convolve(skel.astype(np.uint8), kernel, mode="constant")
        to_remove = (block_sum == 4) & skel
        if not to_remove.any():
            break
        skel[to_remove] = False
    return skel


def prune_short_spurs(skeleton, min_length=MIN_SPUR_LENGTH):
    skel = skeleton.copy()
    structure = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=np.uint8)
    changed = True
    while changed:
        changed = False
        neighbor_sum = ndimage.convolve(skel.astype(np.uint8), structure, mode="constant")
        endpoints = skel & (neighbor_sum == 2)
        if not endpoints.any():
            break
        remove = np.zeros_like(skel)
        ys, xs = np.where(endpoints)
        for start_y, start_x in zip(ys.tolist(), xs.tolist()):
            path = [(start_y, start_x)]
            prev = None
            y, x = start_y, start_x
            while True:
                nbrs = skeleton_neighbors(skel, y, x)
                if prev is not None:
                    nbrs = [p for p in nbrs if p != prev]
                if len(nbrs) != 1:
                    break
                prev = (y, x)
                y, x = nbrs[0]
                path.append((y, x))
            if len(path) < min_length:
                for py, px in path:
                    remove[py, px] = True
                changed = True
        skel[remove] = False
    return skel


def sample_thickness_at_skeleton(skeleton, thickness):
    filtered = ndimage.maximum_filter(thickness, size=5, mode="nearest")
    return np.where(skeleton, filtered, 0).astype(np.uint8)


def thickness_to_width(thickness_value):
    if thickness_value <= 1:
        return 3
    if thickness_value <= 3:
        return 4
    if thickness_value <= 5:
        return 5
    return 6


def endpoint_distances(skeleton, max_distance=TAPER_RADIUS + 6):
    structure = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=np.uint8)
    neighbor_sum = ndimage.convolve(skeleton.astype(np.uint8), structure, mode="constant")
    endpoints = skeleton & (neighbor_sum == 2)
    dist = np.where(endpoints, 0.0, np.inf).astype(np.float32)
    dist[~skeleton] = np.inf
    for _ in range(max_distance):
        best = dist.copy()
        for dy, dx in NEIGHBOR_OFFSETS:
            shifted = np.roll(np.roll(dist, dy, axis=0), dx, axis=1)
            best = np.minimum(best, shifted + 1.0)
        dist = np.where(skeleton, best, dist)
    return dist


def apply_taper(widths, endpoint_dist):
    tapered = widths.copy()
    finite = np.isfinite(endpoint_dist)
    near_end = finite & (endpoint_dist <= TAPER_RADIUS)
    reduction = np.zeros_like(widths, dtype=np.int16)
    reduction[near_end] = ((TAPER_RADIUS - endpoint_dist[near_end]) // 3).astype(np.int16)
    tapered = np.maximum(3, tapered.astype(np.int16) - reduction).astype(np.uint8)
    tapered[~finite & (widths > 0)] = widths[~finite & (widths > 0)]
    return tapered


def assign_sources(skeleton, heightmap):
    labels, count = ndimage.label(
        skeleton, structure=np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]])
    )
    sources = np.zeros(skeleton.shape, dtype=bool)
    structure = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=np.uint8)
    neighbor_sum = ndimage.convolve(skeleton.astype(np.uint8), structure, mode="constant")
    endpoints = skeleton & (neighbor_sum == 2)

    for label_id, slc in enumerate(ndimage.find_objects(labels), start=1):
        if slc is None:
            continue
        label_slice = labels[slc] == label_id
        endpoint_slice = endpoints[slc] & label_slice
        if endpoint_slice.any():
            ys, xs = np.where(endpoint_slice)
        else:
            ys, xs = np.where(label_slice)
        local_heights = heightmap[slc][ys, xs]
        best = int(np.argmax(local_heights))
        global_y = ys[best] + slc[0].start
        global_x = xs[best] + slc[1].start
        sources[global_y, global_x] = True
    return sources


def compose_output(original, skeleton, widths, sources):
    output = np.full(original.shape, 255, dtype=np.uint8)
    output[original == 254] = 254
    output[skeleton] = widths[skeleton]
    output[sources] = 0
    return output


def count_thick_river_blocks(arr):
    river = (arr <= 11).astype(np.uint8)
    block_sum = ndimage.convolve(river, np.ones((2, 2), dtype=np.uint8), mode="constant")
    return int((block_sum == 4).sum())


def count_diagonal_issues(skeleton):
    core = skeleton[1:-1, 1:-1]
    dr = (
        skeleton[2:, 2:]
        & ~skeleton[1:-1, 2:]
        & ~skeleton[2:, 1:-1]
        & core
    )
    dl = (
        skeleton[2:, :-2]
        & ~skeleton[1:-1, :-2]
        & ~skeleton[2:, 1:-1]
        & core
    )
    return int(dr.sum() + dl.sum())


def validate_output(output, provinces_size):
    assert output.shape[::-1] == provinces_size, "Dimension mismatch with provinces.bmp"

    thick = count_thick_river_blocks(output)
    diagonal = count_diagonal_issues(output <= 11)
    labels, count = ndimage.label(output <= 11, structure=np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]]))
    source_labels = labels[output == 0]
    source_label_counts = np.bincount(source_labels[source_labels > 0], minlength=count + 1)
    wrong_sources = int(np.sum(source_label_counts[1:] != 1)) if count else 0

    width_values = output[(output >= 3) & (output <= 6)]
    stats = {
        "river_pixels": int((output <= 11).sum()),
        "components": count,
        "sources_total": int((output == 0).sum()),
        "components_with_wrong_sources": wrong_sources,
        "thick_2x2_blocks": thick,
        "diagonal_issues": diagonal,
        "width_histogram": dict(sorted(Counter(width_values.tolist()).items())),
    }
    return stats


def save_preview(output, palette, path):
    rgb = np.zeros((*output.shape, 3), dtype=np.uint8)
    for idx in np.unique(output):
        idx = int(idx)
        rgb[output == idx] = palette[idx * 3 : idx * 3 + 3]
    Image.fromarray(rgb, mode="RGB").save(path)


def save_indexed(output, palette, path):
    image = Image.fromarray(output, mode="P")
    image.putpalette(palette)
    image.save(path)


def print_stats(title, stats):
    print(title)
    for key, value in stats.items():
        print(f"  {key}: {value}")


def analyze_array(arr, title):
    river = arr <= 11
    labels, count = ndimage.label(river, structure=np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]]))
    used = np.unique(arr)
    stats = {
        "river_pixels": int(river.sum()),
        "components": count,
        "sources_total": int((arr == 0).sum()),
        "thick_2x2_blocks": count_thick_river_blocks(arr),
        "diagonal_issues": count_diagonal_issues(river),
        "indices_used": used.tolist(),
    }
    print_stats(title, stats)
    return stats


def process_rivers(original, heightmap):
    mask = river_mask_from_indices(original)
    thickness = thickness_map(mask)

    skeleton = skeletonize_rivers(mask)
    skeleton = orthogonalize_skeleton(skeleton)
    skeleton = prune_short_spurs(skeleton)
    skeleton = orthogonalize_skeleton(skeleton)

    sampled_thickness = sample_thickness_at_skeleton(skeleton, thickness)
    widths = np.zeros(original.shape, dtype=np.uint8)
    width_values = np.vectorize(thickness_to_width)(sampled_thickness[skeleton])
    widths[skeleton] = width_values.astype(np.uint8)

    endpoint_dist = endpoint_distances(skeleton)
    widths = apply_taper(widths, endpoint_dist)

    sources = assign_sources(skeleton, heightmap)
    output = compose_output(original, skeleton, widths, sources)
    return output, skeleton


def parse_args():
    parser = argparse.ArgumentParser(description="Fix HOI4 rivers.bmp compatibility.")
    parser.add_argument("--apply", action="store_true", help="Backup and overwrite map/rivers.bmp")
    parser.add_argument("--preview", default=str(PREVIEW_PNG), help="Preview PNG output path")
    return parser.parse_args()


def main():
    args = parse_args()
    _, original, palette = load_indexed(RIVERS_BMP)
    heightmap = load_heightmap(HEIGHTMAP_BMP)
    provinces = Image.open(PROVINCES_BMP)

    if original.shape != heightmap.shape:
        raise ValueError(
            f"Heightmap shape {heightmap.shape} does not match rivers shape {original.shape}"
        )

    analyze_array(original, "Before:")
    output, _skeleton = process_rivers(original, heightmap)
    after = validate_output(output, provinces.size)
    print_stats("After:", after)

    save_preview(output, palette, Path(args.preview))
    print(f"\nWrote preview to {args.preview}")

    if args.apply:
        if not RIVERS_BACKUP.exists():
            shutil.copy2(RIVERS_BMP, RIVERS_BACKUP)
            print(f"Backed up original to {RIVERS_BACKUP}")
        save_indexed(output, palette, RIVERS_BMP)
        print(f"Wrote fixed rivers to {RIVERS_BMP}")
    else:
        print("Dry run only. Re-run with --apply to overwrite map/rivers.bmp.")


if __name__ == "__main__":
    main()
